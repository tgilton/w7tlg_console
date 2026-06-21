"""
SDRplay RSPdx-R2 Client — IQ capture + FFT pipeline

Owns the SDRplay device session and turns its IQ stream into spectrum
frames for the panadapter. See ARCHITECTURE.md for the thread-bridge
design rationale (native vendor callback thread -> bounded queue ->
dedicated consumer thread -> asyncio via run_coroutine_threadsafe).

Confirmed against the real RSPdx-R2 (Phase 0 spike, 5-minute soak):
~2.0Msps sustained with zero real overloads, and a naive per-callback
queue handoff comfortably outpaces the display's actual frame-rate need
even though it can't keep up with the full raw sample rate — so the
consumer here only computes a fresh FFT frame once per display tick,
discarding everything else, by design.
"""

import asyncio
import ctypes as C
import logging
import queue
import threading
import time
from collections.abc import Callable, Coroutine
from typing import Optional

import numpy as np

from . import sdrplay_capi as capi
from .audio_demod import AudioDemodulator

logger = logging.getLogger(__name__)

SpectrumCallback = Callable[[dict], Coroutine]


class SdrClient:
    """
    Usage:
        sdr = SdrClient()
        sdr.on_spectrum(my_handler)
        await sdr.start()
        ...
        await sdr.stop()
    """

    def __init__(
        self,
        rf_freq_hz: float = 14_074_000.0,
        sample_rate_hz: float = 2_000_000.0,
        fft_size: int = 65536,
        display_fps: float = 18.0,
        gr_db: int = 40,
        lna_state: int = 4,
        lib_path: str = capi.DEFAULT_LIB_PATH,
    ):
        self.rf_freq_hz = rf_freq_hz
        self.sample_rate_hz = sample_rate_hz
        self.fft_size = fft_size
        self.display_fps = display_fps
        self.gr_db = gr_db
        self.lna_state = lna_state
        self.lib_path = lib_path

        self.available = False
        self.status = "stopped"   # stopped | live | unavailable
        self.dropped_count = 0

        self._lib: Optional[object] = None
        self._device = capi.DeviceT()
        self._has_device = False
        # 8 slots (the original size) gave almost no headroom against
        # ordinary thread-scheduling/GIL jitter between the native callback
        # thread and the consumer — measured ~1000+/s drops continuously
        # even after fixing the consumer's own per-chunk cost separately.
        self._q: "queue.Queue" = queue.Queue(maxsize=512)
        self._stop_event = threading.Event()
        self._consumer_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._spectrum_callbacks: list[SpectrumCallback] = []
        self._window = np.hanning(fft_size).astype(np.float32)
        # 0dBFS reference: the coherent FFT magnitude a full-scale (32767)
        # input would produce through this window. Without this, magnitude
        # dB is raw-and-uncalibrated (scales with FFT size), and every
        # reading comes out as an unfamiliar large positive number instead
        # of the conventional dBFS sign (real signals negative, 0 = full scale).
        self._fullscale_ref = 32767.0 * float(np.sum(self._window))
        self._cb_stream = capi.StreamCallback_t(self._on_stream_data)
        self._cb_stream_b = capi.StreamCallback_t()
        self._cb_event = capi.EventCallback_t(self._on_event)
        # Playback now runs through an AudioWorklet ring buffer (panadapter.html),
        # which is immune to per-message scheduling jitter — so batch size is
        # purely a latency knob now, not a glitch-avoidance one. Smaller is
        # better: it also caps the relative cost of the accumulation buffer.
        self.audio = AudioDemodulator(input_rate_hz=sample_rate_hz)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_spectrum(self, cb: SpectrumCallback):
        self._spectrum_callbacks.append(cb)

    async def start(self):
        self._loop = asyncio.get_running_loop()
        try:
            await self._loop.run_in_executor(None, self._open_and_init)
        except Exception as e:
            logger.warning(f"SDR unavailable: {e}")
            self.available = False
            self.status = "unavailable"
            return

        self.available = True
        self.status = "live"
        self._stop_event.clear()
        self._consumer_thread = threading.Thread(
            target=self._consumer_loop, name="sdr-fft", daemon=True)
        self._consumer_thread.start()
        self.audio.rf_center_hz = self.rf_freq_hz
        self.audio.start(self._loop)
        logger.info("SdrClient started")

    async def stop(self):
        if not self.available:
            return
        self._stop_event.set()
        self.audio.stop()
        if self._consumer_thread:
            await self._loop.run_in_executor(None, self._consumer_thread.join, 3.0)
        await self._loop.run_in_executor(None, self._close)
        self.available = False
        self.status = "stopped"
        logger.info("SdrClient stopped")

    def set_center_freq_hz(self, freq_hz: float):
        """Retune. Safe to call from the event loop — the blocking vendor
        call is pushed onto the executor, matching how AcomSerial.send()
        offloads pyserial writes."""
        if not self.available:
            return
        self.rf_freq_hz = freq_hz
        self.audio.rf_center_hz = freq_hz
        if self._loop:
            self._loop.run_in_executor(None, self._apply_center_freq, freq_hz)

    # ------------------------------------------------------------------
    # Internal: device lifecycle (runs on executor threads, not the loop)
    # ------------------------------------------------------------------

    def _open_and_init(self):
        lib = capi.load_library(self.lib_path)
        self._lib = lib

        def check(err, label):
            if err != 0:
                raise RuntimeError(f"{label} failed: err={err} ({capi.error_string(lib, err)})")

        check(lib.sdrplay_api_Open(), "Open")
        try:
            devices = (capi.DeviceT * capi.SDRPLAY_MAX_DEVICES)()
            num_devs = C.c_uint(0)
            check(lib.sdrplay_api_GetDevices(devices, C.byref(num_devs),
                                              capi.SDRPLAY_MAX_DEVICES), "GetDevices")
            if num_devs.value == 0:
                raise RuntimeError("no SDRplay devices found")

            target = next((devices[i] for i in range(num_devs.value)
                            if devices[i].hwVer == capi.SDRPLAY_RSPdxR2_ID), devices[0])
            self._device = target

            check(lib.sdrplay_api_SelectDevice(C.byref(self._device)), "SelectDevice")
            self._has_device = True

            dp_ptr = C.POINTER(capi.DeviceParamsT)()
            check(lib.sdrplay_api_GetDeviceParams(self._device.dev, C.byref(dp_ptr)),
                  "GetDeviceParams")
            dp = dp_ptr.contents
            dp.devParams.contents.fsHz = self.sample_rate_hz
            dp.devParams.contents.rspDxParams.antennaSel = capi.RspDx_ANTENNA_A
            ch_a = dp.rxChannelA.contents
            ch_a.tunerParams.rfFreq.rfHz = self.rf_freq_hz
            ch_a.tunerParams.bwType = capi.BW_1_536
            ch_a.tunerParams.ifType = capi.IF_Zero
            ch_a.tunerParams.gain.gRdB = self.gr_db
            ch_a.tunerParams.gain.LNAstate = self.lna_state
            ch_a.ctrlParams.agc.enable = capi.AGC_DISABLE

            # Warm up numpy's FFT planning cache now, off the real-time path —
            # Phase 0 measured a one-time ~68ms first-call cost otherwise.
            dummy = np.zeros(self.fft_size, dtype=np.complex64)
            np.fft.fft(dummy * self._window)

            callbacks = capi.CallbackFnsT(StreamACbFn=self._cb_stream,
                                           StreamBCbFn=self._cb_stream_b,
                                           EventCbFn=self._cb_event)
            check(lib.sdrplay_api_Init(self._device.dev, C.byref(callbacks), None),
                  "Init (start streaming)")
        except Exception:
            self._safe_release()
            raise

    def _apply_center_freq(self, freq_hz: float):
        if not self._has_device or self._lib is None:
            return
        dp_ptr = C.POINTER(capi.DeviceParamsT)()
        if self._lib.sdrplay_api_GetDeviceParams(self._device.dev, C.byref(dp_ptr)) != 0:
            return
        dp_ptr.contents.rxChannelA.contents.tunerParams.rfFreq.rfHz = freq_hz
        self._lib.sdrplay_api_Update(self._device.dev, capi.Tuner_A,
                                      capi.Update_Tuner_Frf, capi.Update_Ext1_None)

    def _safe_release(self):
        if self._has_device and self._lib is not None:
            try:
                self._lib.sdrplay_api_ReleaseDevice(C.byref(self._device))
            except Exception:
                pass
            self._has_device = False
        if self._lib is not None:
            try:
                self._lib.sdrplay_api_Close()
            except Exception:
                pass

    def _close(self):
        if self._has_device and self._lib is not None:
            try:
                self._lib.sdrplay_api_Uninit(self._device.dev)
            except Exception:
                logger.exception("sdrplay_api_Uninit failed")
        self._safe_release()

    # ------------------------------------------------------------------
    # Internal: native callback thread (vendor-owned) — minimum work only
    # ------------------------------------------------------------------

    def _on_stream_data(self, xi, xq, params, num_samples, reset, cb_context):
        i = np.ctypeslib.as_array(xi, shape=(num_samples,)).astype(np.int16, copy=True)
        q_arr = np.ctypeslib.as_array(xq, shape=(num_samples,)).astype(np.int16, copy=True)
        try:
            self._q.put_nowait((i, q_arr))
        except queue.Full:
            try:
                self._q.get_nowait()
                self.dropped_count += 1
            except queue.Empty:
                pass
            try:
                self._q.put_nowait((i, q_arr))
            except queue.Full:
                pass
        self.audio.feed(i, q_arr)

    def _on_event(self, event_id, tuner, params, cb_context):
        if event_id == capi.Event_DeviceRemoved:
            if self.available:
                logger.warning("SDR device removed — stopping stream and releasing resources")
                self.available = False
                self.status = "unavailable"
                if self._loop is not None:
                    try:
                        asyncio.run_coroutine_threadsafe(self._handle_device_removed(), self._loop)
                    except RuntimeError:
                        pass   # loop closing/closed during shutdown
        elif event_id == capi.Event_DeviceFailure:
            logger.warning("SDR device failure event")

    async def _handle_device_removed(self):
        """Previously this just logged a warning forever while the stream
        kept trying to run against a device that was already gone — left
        the session in a half-dead state with no way to recover short of a
        full app restart. Now actually tears down like a normal stop()."""
        self._stop_event.set()
        self.audio.stop()
        if self._consumer_thread:
            await self._loop.run_in_executor(None, self._consumer_thread.join, 3.0)
        await self._loop.run_in_executor(None, self._close)
        logger.info("SdrClient cleaned up after device removal")

    # ------------------------------------------------------------------
    # Internal: dedicated consumer thread — FFT pipeline
    # ------------------------------------------------------------------

    def _consumer_loop(self):
        # Pre-allocated, double-sized buffer written via slice assignment,
        # with the trailing fft_size window compacted back to the front
        # only when about to overflow — amortized O(chunk size) per write.
        # The previous concatenate-and-slice approach recopied the entire
        # fft_size window on every single native callback regardless of
        # chunk size, which was the actual bottleneck: it made the consumer
        # fall behind badly enough to drop the vast majority of callbacks
        # (measured ~1400/s into an 8-slot queue), splicing non-contiguous
        # IQ together in every FFT frame.
        buf_cap = self.fft_size * 2
        buf_i = np.empty(buf_cap, dtype=np.int16)
        buf_q = np.empty(buf_cap, dtype=np.int16)
        write_pos = 0

        tick_interval = 1.0 / self.display_fps
        next_tick = time.monotonic()
        next_drop_log = time.monotonic() + 5.0
        last_logged_drops = 0

        while not self._stop_event.is_set():
            now_check = time.monotonic()
            if now_check >= next_drop_log:
                if self.dropped_count != last_logged_drops:
                    logger.warning(
                        f"Spectrum queue drops: {self.dropped_count} total "
                        f"(+{self.dropped_count - last_logged_drops} in last 5s) — "
                        f"native callbacks arriving faster than the FFT consumer can drain them")
                    last_logged_drops = self.dropped_count
                next_drop_log = now_check + 5.0
            try:
                i, q_arr = self._q.get(timeout=0.5)
            except queue.Empty:
                continue

            n = len(i)
            if write_pos + n > buf_cap:
                keep = min(write_pos, self.fft_size)
                buf_i[:keep] = buf_i[write_pos - keep:write_pos]
                buf_q[:keep] = buf_q[write_pos - keep:write_pos]
                write_pos = keep
            buf_i[write_pos:write_pos + n] = i
            buf_q[write_pos:write_pos + n] = q_arr
            write_pos += n

            now = time.monotonic()
            if now < next_tick or write_pos < self.fft_size:
                continue
            next_tick = now + tick_interval

            frame = self._compute_frame(buf_i[write_pos - self.fft_size:write_pos],
                                         buf_q[write_pos - self.fft_size:write_pos])
            if self._loop is not None:
                try:
                    asyncio.run_coroutine_threadsafe(self._publish(frame), self._loop)
                except RuntimeError:
                    pass  # loop is closing/closed during shutdown

    def _compute_frame(self, block_i, block_q) -> dict:
        iq = (block_i.astype(np.float32) + 1j * block_q.astype(np.float32)).astype(np.complex64)
        iq *= self._window
        spectrum = np.fft.fftshift(np.fft.fft(iq))
        mag_db = (20.0 * np.log10(np.abs(spectrum) / self._fullscale_ref + 1e-12)).astype(np.float32)
        return {
            "ts": time.time(),
            "center_freq_hz": self.rf_freq_hz,
            "span_hz": self.sample_rate_hz,
            "sample_rate_hz": self.sample_rate_hz,
            "data": mag_db,
        }

    async def _publish(self, frame: dict):
        for cb in self._spectrum_callbacks:
            await cb(frame)
