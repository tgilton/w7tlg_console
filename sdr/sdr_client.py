"""
SDRplay RSPduo Client — IQ capture + FFT pipeline

Owns the SDRplay device session and turns its IQ stream into spectrum
frames for the panadapter. See ARCHITECTURE.md for the thread-bridge
design rationale (native vendor callback thread -> bounded queue ->
dedicated consumer thread -> asyncio via run_coroutine_threadsafe).

Confirmed against the real RSPdx-R2, this client's original hardware
(Phase 0 spike, 5-minute soak): ~2.0Msps sustained with zero real
overloads, and a naive per-callback queue handoff comfortably outpaces
the display's actual frame-rate need even though it can't keep up with
the full raw sample rate — so the consumer here only computes a fresh
FFT frame once per display tick, discarding everything else, by design.

RSPduo replaces the RSPdx-R2 as of the dual-antenna receive project
(2026-09-10): two independently-tunable tuners sharing one ADC clock,
opened in Dual Tuner mode (see sdrplay_capi.RspDuoMode_Dual_Tuner).
Antenna routing is now fixed by physical wiring per tuner — Tuner 1 /
Ant A is TX-capable (the only antenna the amp can key), Tuner 2 / Ant B
is receive-only — instead of the RSPdx-R2's single-tuner, software-
switched 3-port model, which is why _antenna_for_freq-style band
switching is gone.

Dual-tuner rollout is staged: this pass (Phase 0) proves Dual Tuner mode
actually brings up two live, simultaneous IQ streams from real hardware
— Channel A drives the existing single-channel FFT/audio/digital-audio
pipeline unchanged, Channel B is proof-of-life only (liveness stamp +
sample count, no FFT/audio pipeline of its own yet). The second full
pipeline, independent per-channel tuning, and the dual-panadapter/dual-
audio UI are later phases once Phase 0 confirms the device layer works
on this hardware.
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
from .combiner import Combiner
from .virtual_audio_output import DigitalAudioOutput

logger = logging.getLogger(__name__)

SpectrumCallback = Callable[[dict], Coroutine]

# Manual RF gain: one 0-100% knob drives both of SDRplay's underlying front-
# end gain parameters together (same combined-knob convention SDRuno uses),
# rather than exposing gRdB/LNAstate separately — see project memory on
# preferring direct manual controls over auto-heuristics. 100% is max gain
# (gRdB=20, LNAstate=0), 0% is min gain/max attenuation (gRdB=59, LNAstate
# at the current band's max step) for headroom against strong signals.
_GR_DB_MAX_GAIN = 20
_GR_DB_MIN_GAIN = 59

# Highest valid LNAstate, BY FREQUENCY — confirmed 2026-09-12 against
# SDRplay_API_Specification_v3.15.pdf Sec. 5 (Gain Reduction Tables), not
# on hand when this was first written, hence the uniform-10 placeholder
# that used to be here. The RSPduo's normal (50 Ohm) port is NOT a
# uniform state count across the tuning range: 0-60MHz (everything this
# station operates in — every HF ham band plus WWV) has only 7 valid
# states (0-6), not 10; 60-1000MHz has 10 (0-9); 1000-2000MHz/L-band has
# 9 (0-8). Using 10 uniformly wasted the bottom of the RF Gain slider's
# range on invalid states for HF, producing large flat spots and sudden
# jumps instead of smooth response — confirmed live (Terry 2026-09-12: no
# audible/visible change until ~12% away from 80% on either channel, at
# both 5 and 10 MHz).
def _max_lna_state_for_freq(freq_hz: float) -> int:
    freq_mhz = freq_hz / 1e6
    if freq_mhz < 60:
        return 6
    elif freq_mhz < 1000:
        return 9
    else:
        return 8

# Raw ADC rate written to dp.devParams.contents.fsHz — NOT the rate IQ
# samples actually arrive at (that's SdrClient.sample_rate_hz, the
# constructor param every other consumer in this file assumes). Dual
# Tuner mode's Low IF down-converter (see ch_a/ch_b.tunerParams.ifType in
# _open_and_init) only engages for a small fixed set of (fsHz, bwType,
# ifType) triples, and once engaged, applies its OWN internal decimation
# before handing samples to us — confirmed (SDRplay's rsp-recorder
# project docs, since the API spec PDF 403'd and this station's headers
# don't have it) that 6,000,000 / BW_1_536 / IF_1_620 carries an internal
# decimation of 3, landing the delivered rate at exactly 2,000,000 — the
# same value sample_rate_hz already defaults to. This explicit split
# exists so a future change to either number can't accidentally
# reintroduce the "wrong rate assumed" bug that broke Phase 0's first
# live test (see sample_rate_hz's own comment).
_DEVICE_FS_HZ = 6_000_000.0


def _rf_gain_params(pct: float, freq_hz: float) -> tuple:
    """Map the 0-100% RF Gain knob to (gRdB, LNAstate) — see
    _max_lna_state_for_freq for why LNAstate's max varies by frequency."""
    pct = max(0.0, min(100.0, pct))
    gr_db = round(_GR_DB_MIN_GAIN - (_GR_DB_MIN_GAIN - _GR_DB_MAX_GAIN) * pct / 100.0)
    lna_state = round(_max_lna_state_for_freq(freq_hz) * (1.0 - pct / 100.0))
    return gr_db, lna_state


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
        # This is the EFFECTIVE/delivered IQ rate — what every consumer
        # downstream of the native callback (FFT span, AudioDemodulator,
        # passband_strength_db) assumes each sample represents. It is NOT
        # the same as the raw ADC rate written to the device (_DEVICE_FS_HZ,
        # see _open_and_init) once Dual Tuner mode's Low IF down-converter
        # is involved — that engages a fixed internal decimation the API
        # applies before samples ever reach us. Stays at 2MHz, this
        # client's original RSPdx-R2 value — confirmed unchanged (see
        # _DEVICE_FS_HZ) by SDRplay's rsp-recorder project docs, pulled via
        # web search 2026-09-10 since neither this station's local headers
        # nor the API spec PDF (403/blocked) had it directly. Divides
        # 16 kHz exactly (decim_factor 125).
        sample_rate_hz: float = 2_000_000.0,
        fft_size: int = 65536,
        display_fps: float = 18.0,
        spectrum_avg_frames: float = 10.0,
        rf_gain_pct: float = 80.0,
        lib_path: str = capi.DEFAULT_LIB_PATH,
    ):
        self.rf_freq_hz = rf_freq_hz
        self.sample_rate_hz = sample_rate_hz
        self.fft_size = fft_size
        self.display_fps = display_fps
        # Live-adjustable front-end gain (see _rf_gain_params above) — was
        # fixed constants (gr_db=40, lna_state=4) until the operator asked
        # for a manual RF Gain control to pull weak SSB signals out of the
        # noise. The original 50% default landed at LNAstate=14 and produced
        # a visible DC/LO-leakage spike in the wideband spectrum display —
        # confirmed live 2026-08-30 by sweeping the slider: the spike is
        # present in a middle LNAstate range and clears at both low and high
        # gain. 80% (LNAstate=5) sits back in the same clean high-gain zone
        # the old fixed LNAstate=4 was in (not an exact reproduction — the
        # two underlying parameters don't scale together linearly enough for
        # one percent to hit both old constants at once), which conveniently
        # is also more gain for weak-signal work, the whole reason this
        # control exists.
        self.rf_gain_pct = rf_gain_pct
        # Channel B (Tuner 2 / Antenna 2) starts parked on the same
        # freq/gain as Channel A — independent tuning is exactly what
        # Phase 1 adds these for; see set_center_freq_hz_b/set_rf_gain_pct_b.
        self.rf_freq_hz_b = rf_freq_hz
        self.rf_gain_pct_b = rf_gain_pct
        # RF/DAB notch — RSPduo hardware filters ahead of the mixer, per-
        # tuner, off by default (matches the API's own default). Existed
        # for the old RSPdx-R2 client but never carried over to the
        # RSPduo backend when that hardware swapped in (2026-09-10) — see
        # project memory on the migration. RF notch pulls down strong MW/
        # AM broadcast energy that could desense the front end; DAB notch
        # targets the European/UK digital-radio band, rarely relevant on
        # HF but included for parity since both fields live in the same
        # rspDuoTunerParams struct either way.
        self.rf_notch_enabled = False
        self.rf_notch_enabled_b = False
        self.dab_notch_enabled = False
        self.dab_notch_enabled_b = False
        self.lib_path = lib_path
        # Exponential moving average in linear power across consecutive FFT
        # frames, weighted to match the steady-state variance reduction of an
        # N-frame box average: (1-decay)/(1+decay) = 1/N => decay = (N-1)/(N+1).
        # A raw single-shot periodogram (what this was before) has the same
        # bin variance regardless of fft_size — only averaging independent
        # frames together actually quiets the noise floor.
        self.spectrum_avg_frames = spectrum_avg_frames
        self._avg_decay = max(0.0, (spectrum_avg_frames - 1.0) / (spectrum_avg_frames + 1.0))
        self._avg_power: Optional[np.ndarray] = None
        self._reset_avg_event = threading.Event()
        # Channel B's own averaged-spectrum state — separate from Channel
        # A's above, same reasoning throughout.
        self._avg_power_b: Optional[np.ndarray] = None
        self._reset_avg_event_b = threading.Event()
        # Diversity phase-coherence tap (2026-09-11) — the raw (unaveraged)
        # complex FFT bin nearest each channel's own tuned frequency, from
        # the same per-frame FFT _compute_frame/_compute_frame_b already
        # computes for the spectrum display. See iq_phase_diff_deg for why
        # this exists: Dual Tuner mode's two tuners share one ADC/sample
        # clock, but whether their downconversion LOs also stay phase-
        # locked to each other (not just the sample clock) was flagged as
        # unverified when this project started. None while unavailable
        # (no frame yet, or mid-TX, where the SDR Switch disconnects the
        # antenna and the bin is disconnected-input noise, not a reading).
        self._last_phasor_a: Optional[complex] = None
        self._last_phasor_b: Optional[complex] = None

        self.available = False
        self.status = "stopped"   # stopped | live | unavailable
        self.dropped_count = 0
        self.dropped_count_b = 0

        self._lib: Optional[object] = None
        self._device = capi.DeviceT()
        self._has_device = False
        # 8 slots (the original size) gave almost no headroom against
        # ordinary thread-scheduling/GIL jitter between the native callback
        # thread and the consumer — measured ~1000+/s drops continuously
        # even after fixing the consumer's own per-chunk cost separately.
        self._q: "queue.Queue" = queue.Queue(maxsize=512)
        self._q_b: "queue.Queue" = queue.Queue(maxsize=512)
        self._stop_event = threading.Event()
        self._consumer_thread: Optional[threading.Thread] = None
        self._consumer_thread_b: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # A device-removed/failure event fires on the vendor callback thread
        # and can arrive in a burst (removed + failure together). This gate
        # collapses that burst into exactly one in-flight recovery attempt,
        # set/cleared under the lock from two different threads.
        self._recovery_lock = threading.Lock()
        self._recovery_pending = False
        # Stall watchdog: the RSPdx can silently stop delivering IQ mid-session
        # WITHOUT firing a DeviceRemoved/DeviceFailure event — the vendor
        # callback thread just goes quiet and the device wedges (confirmed:
        # GetDevices still lists it, but Init fails until a USB replug). The
        # event path above never fires for this, so without a data-liveness
        # check the app sits forever holding a dead session. _last_sample_at is
        # stamped on every callback; _stall_watchdog trips if it goes stale.
        self._last_sample_at = 0.0
        self._watchdog_task: Optional[asyncio.Task] = None
        # Channel B (Tuner 2 / Antenna 2, receive-only) — Phase 0 tracked
        # only liveness; Phase 1 (this) gives it a real FFT/audio pipeline,
        # same liveness stamp/watchdog reasoning as Channel A above.
        self._last_sample_at_b = 0.0
        self._sample_count_b = 0
        self._ch_b_first_log_done = False
        # 2 MHz sustained means callbacks arrive continuously (thousands/s),
        # and they keep coming during TX too (the antenna switch mutes the
        # input, not the stream) — so any multi-second gap is a real stall,
        # not TX or normal jitter.
        self._stall_timeout_s = 3.0
        self._spectrum_callbacks: list[SpectrumCallback] = []
        # Deliberately separate from _spectrum_callbacks above, NOT a
        # shared list distinguished only by the "channel" tag — server.py
        # already subscribes to _spectrum_callbacks (via on_spectrum) for
        # today's single-panadapter display, and nothing calls
        # on_spectrum_b yet. If Channel B published onto the same shared
        # list, its frames would start interleaving into that existing
        # display the moment this client starts, before any dual-channel
        # frontend support exists to make sense of them — a real
        # regression to the just-confirmed-working live view, not a
        # theoretical one. This list stays empty (Channel B frames
        # computed but published nowhere) until a future pass wires up
        # on_spectrum_b from the dual-panadapter UI.
        self._spectrum_callbacks_b: list[SpectrumCallback] = []
        self._window = np.hanning(fft_size).astype(np.float32)
        # 0dBFS reference: the coherent FFT magnitude a full-scale (32767)
        # input would produce through this window. Without this, magnitude
        # dB is raw-and-uncalibrated (scales with FFT size), and every
        # reading comes out as an unfamiliar large positive number instead
        # of the conventional dBFS sign (real signals negative, 0 = full scale).
        self._fullscale_ref = 32767.0 * float(np.sum(self._window))
        self._cb_stream = capi.StreamCallback_t(self._on_stream_data)
        self._cb_stream_b = capi.StreamCallback_t(self._on_stream_data_b)
        self._cb_event = capi.EventCallback_t(self._on_event)
        # Playback now runs through an AudioWorklet ring buffer (panadapter.html),
        # which is immune to per-message scheduling jitter — so batch size is
        # purely a latency knob now, not a glitch-avoidance one. Smaller is
        # better: it also caps the relative cost of the accumulation buffer.
        self.audio = AudioDemodulator(input_rate_hz=sample_rate_hz)
        # Channel B's own demodulator — independent mode/frequency/gain
        # from Channel A's, per the dual-watch plan. NOT wired to
        # digital_audio below yet: which channel feeds the single BlackHole
        # cable is meant to be operator-switchable (see project memory on
        # the dual-receiver migration), and that source-select control is
        # still frontend/server.py work, not built this pass — defaults to
        # Channel A, matching today's behavior exactly until it exists.
        self.audio_b = AudioDemodulator(input_rate_hz=sample_rate_hz)
        # RX0 — manual tunable RX1/RX2 combine (diversity experiment,
        # 2026-09-12). Off until the Diversity page actually enables it
        # (Combiner.enabled), so it costs nothing when nobody's looking at
        # it. See sdr/combiner.py for the full design.
        self.combiner = Combiner(input_rate_hz=sample_rate_hz)
        # Second subscriber on the same demodulated audio — feeds digital-mode
        # software (WSJT-X etc.) via a virtual audio cable instead of needing
        # the antenna switched back to the radio's own receiver.
        self.digital_audio = DigitalAudioOutput()
        self.audio.on_audio(self.digital_audio.on_audio_frame)
        # Digital-mode fine spectrum rides the same broadcast path as the
        # wideband one (_publish already just fans out to whatever's
        # subscribed via on_spectrum) — the "kind": "fine" tag on the frame
        # is what lets the frontend and SpectrumConnectionManager tell them
        # apart downstream.
        # AudioDemodulator is channel-agnostic (same class for both
        # instances, doesn't know which tuner it's demodulating) — these
        # thin wrappers stamp which channel a fine-spectrum frame came
        # from before it reaches _publish's shared callback list, the same
        # way _compute_frame/_compute_frame_b stamp the wideband ones.
        self.audio.on_fine_spectrum(self._publish_fine_a)
        self.audio_b.on_fine_spectrum(self._publish_fine_b)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def antenna_label(self) -> str:
        """Which antenna Channel A (the pipeline this client currently
        publishes) is on. Fixed by physical wiring on the RSPduo — no
        software switching — so this is a constant until a later phase
        exposes Channel B's own status separately."""
        return "A"

    @property
    def antenna_label_b(self) -> str:
        """Channel B's antenna — see antenna_label. Fixed at "B"."""
        return "B"

    @property
    def channel_b_alive(self) -> bool:
        """Whether Channel B (Tuner 2 / Antenna 2) has delivered IQ
        recently."""
        if not self.available or self._last_sample_at_b == 0.0:
            return False
        return time.monotonic() - self._last_sample_at_b < self._stall_timeout_s

    def on_spectrum(self, cb: SpectrumCallback):
        self._spectrum_callbacks.append(cb)

    def on_spectrum_b(self, cb: SpectrumCallback):
        """Channel B's spectrum feed — separate from on_spectrum, see
        _spectrum_callbacks_b. Nothing subscribes to this yet."""
        self._spectrum_callbacks_b.append(cb)

    def gate_tx(self):
        """Gate all audio output for TX start: flush the IQ queue, the
        BlackHole/digital-audio queue, and reset AGC.  Call this instead of
        audio.gate_tx() directly so every audio output path is covered.

        Channel B's audio queue is flushed defensively here too, but its
        AudioDemodulator.tx_active flag isn't wired to PTT yet — that's
        server.py's job (currently only touches sdr.audio.tx_active), not
        done this pass since Channel B has no UI/control wiring yet. Until
        that lands, Channel B's own TX-gated average-power reset
        (_compute_frame_b's tx_active check) is a no-op in practice."""
        self.audio.gate_tx()
        self.audio_b.gate_tx()
        self.combiner.gate_tx()
        self.digital_audio.flush()

    async def start(self):
        self._loop = asyncio.get_running_loop()
        if not await self._init_with_retries():
            return
        self._start_pipeline()
        logger.info("SdrClient started")

    async def _init_with_retries(self) -> bool:
        """Open + Init the device, retrying with backoff. On success sets
        available/live and returns True; on exhaustion sets unavailable and
        returns False. Shared by first start() and post-glitch recovery.

        The RSPdx often returns sdrplay_api_Fail on Init if a previous
        session's device handle hasn't fully settled — classically a quick
        console restart, where the prior process's Uninit and this process's
        Init land within a few seconds of each other, or a USB glitch where
        the device re-enumerates a beat after it dropped. A single attempt
        then leaves the SDR dead until someone physically replugs the USB.
        Retry with backoff instead: _open_and_init() calls _safe_release() on
        every failure, so each attempt starts clean, and the growing gaps give
        the device time to recover on its own. Delays are BEFORE each retry."""
        retry_delays_s = [0.0, 3.0, 5.0, 7.0]
        last_err = None
        for i, delay in enumerate(retry_delays_s):
            if delay:
                await asyncio.sleep(delay)
            try:
                await self._loop.run_in_executor(None, self._open_and_init)
                self.available = True
                self.status = "live"
                return True
            except Exception as e:
                last_err = e
                logger.warning(
                    f"SDR init attempt {i + 1}/{len(retry_delays_s)} failed: {e}")
        logger.warning(
            f"SDR unavailable after {len(retry_delays_s)} attempts "
            f"(device may need a USB replug / daemon restart): {last_err}")
        self.available = False
        self.status = "unavailable"
        return False

    def _start_pipeline(self):
        """Spin up the consumer/audio/digital threads against a device that
        _open_and_init has just brought live. Non-blocking; safe on the loop."""
        self._stop_event.clear()
        self._consumer_thread = threading.Thread(
            target=self._consumer_loop, name="sdr-fft", daemon=True)
        self._consumer_thread.start()
        self._consumer_thread_b = threading.Thread(
            target=self._consumer_loop_b, name="sdr-fft-b", daemon=True)
        self._consumer_thread_b.start()
        self.audio.rf_center_hz = self.rf_freq_hz
        self.audio.start(self._loop)
        self.audio_b.rf_center_hz = self.rf_freq_hz_b
        self.audio_b.start(self._loop)
        self.combiner.start(self._loop)
        self.digital_audio.start()
        # Fresh baseline so the watchdog doesn't trip on the gap before the
        # first callback arrives.
        self._last_sample_at = time.monotonic()
        self._last_sample_at_b = time.monotonic()
        self._watchdog_task = self._loop.create_task(self._stall_watchdog())

    async def _teardown_pipeline(self):
        """Stop the consumer/audio/digital threads and release the device.
        Every blocking step (thread joins up to 3s each, plus the vendor
        Uninit/Close) runs on the executor as one unit so the event loop is
        never stalled — important on the device-removed path, which fires
        while the UI is live and waiting on it."""
        self._stop_event.set()
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            self._watchdog_task = None
        consumer = self._consumer_thread
        self._consumer_thread = None
        consumer_b = self._consumer_thread_b
        self._consumer_thread_b = None

        def _blocking_teardown():
            self.audio.stop()
            self.audio_b.stop()
            self.combiner.stop()
            self.digital_audio.stop()
            if consumer:
                consumer.join(3.0)
            if consumer_b:
                consumer_b.join(3.0)
            self._close()

        await self._loop.run_in_executor(None, _blocking_teardown)
        self._avg_power = None
        self._avg_power_b = None

    async def stop(self):
        if not self.available:
            return
        await self._teardown_pipeline()
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
        # A retune is a discontinuity, not noise — don't let the average blend
        # the old frequency's content into the new view's first few frames.
        self._reset_avg_event.set()
        if self._loop:
            self._loop.run_in_executor(None, self._apply_center_freq, freq_hz)

    def set_rf_gain_pct(self, pct: float):
        """Manual RF Gain, 0-100% (see _rf_gain_params). Safe to call from
        the event loop — same executor-offload pattern as set_center_freq_hz."""
        if not self.available:
            return
        self.rf_gain_pct = max(0.0, min(100.0, pct))
        if self._loop:
            self._loop.run_in_executor(None, self._apply_rf_gain)

    def set_center_freq_hz_b(self, freq_hz: float):
        """Retune Channel B — see set_center_freq_hz, same pattern,
        independent of Channel A's frequency."""
        if not self.available:
            return
        self.rf_freq_hz_b = freq_hz
        self.audio_b.rf_center_hz = freq_hz
        self._reset_avg_event_b.set()
        if self._loop:
            self._loop.run_in_executor(None, self._apply_center_freq_b, freq_hz)

    def set_rf_gain_pct_b(self, pct: float):
        """Manual RF Gain for Channel B, 0-100% — see set_rf_gain_pct."""
        if not self.available:
            return
        self.rf_gain_pct_b = max(0.0, min(100.0, pct))
        if self._loop:
            self._loop.run_in_executor(None, self._apply_rf_gain_b)

    def set_rf_notch(self, enabled: bool):
        if not self.available:
            return
        self.rf_notch_enabled = bool(enabled)
        if self._loop:
            self._loop.run_in_executor(None, self._apply_rf_notch)

    def set_rf_notch_b(self, enabled: bool):
        if not self.available:
            return
        self.rf_notch_enabled_b = bool(enabled)
        if self._loop:
            self._loop.run_in_executor(None, self._apply_rf_notch_b)

    def set_dab_notch(self, enabled: bool):
        if not self.available:
            return
        self.dab_notch_enabled = bool(enabled)
        if self._loop:
            self._loop.run_in_executor(None, self._apply_dab_notch)

    def set_dab_notch_b(self, enabled: bool):
        if not self.available:
            return
        self.dab_notch_enabled_b = bool(enabled)
        if self._loop:
            self._loop.run_in_executor(None, self._apply_dab_notch_b)

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
                            if devices[i].hwVer == capi.SDRPLAY_RSPduo_ID), devices[0])
            self._device = target
            # Request Dual Tuner mode before SelectDevice — both tuners
            # streaming simultaneously off one shared ADC clock, per
            # sdrplay_api_rspDuo.h.
            self._device.tuner = capi.Tuner_Both
            self._device.rspDuoMode = capi.RspDuoMode_Dual_Tuner

            check(lib.sdrplay_api_SelectDevice(C.byref(self._device)), "SelectDevice")
            self._has_device = True

            dp_ptr = C.POINTER(capi.DeviceParamsT)()
            check(lib.sdrplay_api_GetDeviceParams(self._device.dev, C.byref(dp_ptr)),
                  "GetDeviceParams")
            dp = dp_ptr.contents
            # _DEVICE_FS_HZ (raw ADC rate) is deliberately NOT
            # self.sample_rate_hz (the effective/delivered rate) — see
            # _DEVICE_FS_HZ's own comment.
            dp.devParams.contents.fsHz = _DEVICE_FS_HZ
            gr_db, lna_state = _rf_gain_params(self.rf_gain_pct, self.rf_freq_hz)

            # Dual Tuner mode requires Low IF, not Zero IF (see datasheet).
            # ifType isn't freely choosable, though — the SDRplay API's
            # internal low-IF-to-baseband down-converter only engages for a
            # small fixed set of (fsHz, bwType, ifType) triples (confirmed
            # against the API Specification, consistent from v3.0 through
            # our installed v3.15.1 — not in the local headers or the
            # spec PDF, which 403'd; pulled via web search 2026-09-10
            # after a first attempt at IF_2_048 produced un-shifted raw IF
            # data live: wrong spectrum, no audio). _DEVICE_FS_HZ=6,000,000
            # with bwType<=BW_1_536 only enables down-conversion paired
            # with ifType=IF_1_620 — IF_2_048 is only valid paired with
            # fsHz=8,000,000, a combination this client doesn't use.
            ch_a = dp.rxChannelA.contents
            ch_a.tunerParams.rfFreq.rfHz = self.rf_freq_hz
            ch_a.tunerParams.bwType = capi.BW_1_536
            ch_a.tunerParams.ifType = capi.IF_1_620
            ch_a.tunerParams.gain.gRdB = gr_db
            ch_a.tunerParams.gain.LNAstate = lna_state
            ch_a.ctrlParams.agc.enable = capi.AGC_DISABLE
            ch_a.rspDuoTunerParams.rfNotchEnable = int(self.rf_notch_enabled)
            ch_a.rspDuoTunerParams.rfDabNotchEnable = int(self.dab_notch_enabled)

            # Channel B (Tuner 2 / Antenna 2) — rxChannelB is only valid
            # once Dual Tuner mode is actually granted above. Phase 0:
            # same starting freq/gain as Channel A just to bring the
            # tuner up and prove it streams; independent per-channel
            # tuning is a later phase.
            ch_b = dp.rxChannelB.contents
            ch_b.tunerParams.rfFreq.rfHz = self.rf_freq_hz
            ch_b.tunerParams.bwType = capi.BW_1_536
            ch_b.tunerParams.ifType = capi.IF_1_620
            ch_b.tunerParams.gain.gRdB = gr_db
            ch_b.tunerParams.gain.LNAstate = lna_state
            ch_b.ctrlParams.agc.enable = capi.AGC_DISABLE
            ch_b.rspDuoTunerParams.rfNotchEnable = int(self.rf_notch_enabled_b)
            ch_b.rspDuoTunerParams.rfDabNotchEnable = int(self.dab_notch_enabled_b)

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
        ch_a = dp_ptr.contents.rxChannelA.contents
        ch_a.tunerParams.rfFreq.rfHz = freq_hz
        gr_db, lna_state = _rf_gain_params(self.rf_gain_pct, freq_hz)
        ch_a.tunerParams.gain.gRdB = gr_db
        ch_a.tunerParams.gain.LNAstate = lna_state
        self._lib.sdrplay_api_Update(
            self._device.dev, capi.Tuner_A,
            capi.Update_Tuner_Frf | capi.Update_Tuner_Gr, capi.Update_Ext1_None)

    def _apply_rf_gain(self):
        if not self._has_device or self._lib is None:
            return
        dp_ptr = C.POINTER(capi.DeviceParamsT)()
        if self._lib.sdrplay_api_GetDeviceParams(self._device.dev, C.byref(dp_ptr)) != 0:
            return
        gr_db, lna_state = _rf_gain_params(self.rf_gain_pct, self.rf_freq_hz)
        ch_a = dp_ptr.contents.rxChannelA.contents
        ch_a.tunerParams.gain.gRdB = gr_db
        ch_a.tunerParams.gain.LNAstate = lna_state
        err = self._lib.sdrplay_api_Update(self._device.dev, capi.Tuner_A,
                                            capi.Update_Tuner_Gr, capi.Update_Ext1_None)
        if err != 0:
            logger.warning(f"RSPdx RF gain update failed: err={err}")

    def _apply_center_freq_b(self, freq_hz: float):
        """Channel B's retune — see _apply_center_freq, targets Tuner_B/
        rxChannelB instead of Tuner_A/rxChannelA."""
        if not self._has_device or self._lib is None:
            return
        dp_ptr = C.POINTER(capi.DeviceParamsT)()
        if self._lib.sdrplay_api_GetDeviceParams(self._device.dev, C.byref(dp_ptr)) != 0:
            return
        ch_b = dp_ptr.contents.rxChannelB.contents
        ch_b.tunerParams.rfFreq.rfHz = freq_hz
        gr_db, lna_state = _rf_gain_params(self.rf_gain_pct_b, freq_hz)
        ch_b.tunerParams.gain.gRdB = gr_db
        ch_b.tunerParams.gain.LNAstate = lna_state
        self._lib.sdrplay_api_Update(
            self._device.dev, capi.Tuner_B,
            capi.Update_Tuner_Frf | capi.Update_Tuner_Gr, capi.Update_Ext1_None)

    def _apply_rf_gain_b(self):
        """Channel B's RF gain — see _apply_rf_gain, targets Tuner_B."""
        if not self._has_device or self._lib is None:
            return
        dp_ptr = C.POINTER(capi.DeviceParamsT)()
        if self._lib.sdrplay_api_GetDeviceParams(self._device.dev, C.byref(dp_ptr)) != 0:
            return
        gr_db, lna_state = _rf_gain_params(self.rf_gain_pct_b, self.rf_freq_hz_b)
        ch_b = dp_ptr.contents.rxChannelB.contents
        ch_b.tunerParams.gain.gRdB = gr_db
        ch_b.tunerParams.gain.LNAstate = lna_state
        err = self._lib.sdrplay_api_Update(self._device.dev, capi.Tuner_B,
                                            capi.Update_Tuner_Gr, capi.Update_Ext1_None)
        if err != 0:
            logger.warning(f"RSPduo Channel B RF gain update failed: err={err}")

    def _apply_rf_notch(self):
        if not self._has_device or self._lib is None:
            return
        dp_ptr = C.POINTER(capi.DeviceParamsT)()
        if self._lib.sdrplay_api_GetDeviceParams(self._device.dev, C.byref(dp_ptr)) != 0:
            return
        ch_a = dp_ptr.contents.rxChannelA.contents
        ch_a.rspDuoTunerParams.rfNotchEnable = int(self.rf_notch_enabled)
        err = self._lib.sdrplay_api_Update(self._device.dev, capi.Tuner_A,
                                            capi.Update_RspDuo_RfNotchControl, capi.Update_Ext1_None)
        if err != 0:
            logger.warning(f"RSPduo Channel A RF notch update failed: err={err}")

    def _apply_rf_notch_b(self):
        """Channel B's RF notch — see _apply_rf_notch, targets Tuner_B."""
        if not self._has_device or self._lib is None:
            return
        dp_ptr = C.POINTER(capi.DeviceParamsT)()
        if self._lib.sdrplay_api_GetDeviceParams(self._device.dev, C.byref(dp_ptr)) != 0:
            return
        ch_b = dp_ptr.contents.rxChannelB.contents
        ch_b.rspDuoTunerParams.rfNotchEnable = int(self.rf_notch_enabled_b)
        err = self._lib.sdrplay_api_Update(self._device.dev, capi.Tuner_B,
                                            capi.Update_RspDuo_RfNotchControl, capi.Update_Ext1_None)
        if err != 0:
            logger.warning(f"RSPduo Channel B RF notch update failed: err={err}")

    def _apply_dab_notch(self):
        if not self._has_device or self._lib is None:
            return
        dp_ptr = C.POINTER(capi.DeviceParamsT)()
        if self._lib.sdrplay_api_GetDeviceParams(self._device.dev, C.byref(dp_ptr)) != 0:
            return
        ch_a = dp_ptr.contents.rxChannelA.contents
        ch_a.rspDuoTunerParams.rfDabNotchEnable = int(self.dab_notch_enabled)
        err = self._lib.sdrplay_api_Update(self._device.dev, capi.Tuner_A,
                                            capi.Update_RspDuo_RfDabNotchControl, capi.Update_Ext1_None)
        if err != 0:
            logger.warning(f"RSPduo Channel A DAB notch update failed: err={err}")

    def _apply_dab_notch_b(self):
        """Channel B's DAB notch — see _apply_dab_notch, targets Tuner_B."""
        if not self._has_device or self._lib is None:
            return
        dp_ptr = C.POINTER(capi.DeviceParamsT)()
        if self._lib.sdrplay_api_GetDeviceParams(self._device.dev, C.byref(dp_ptr)) != 0:
            return
        ch_b = dp_ptr.contents.rxChannelB.contents
        ch_b.rspDuoTunerParams.rfDabNotchEnable = int(self.dab_notch_enabled_b)
        err = self._lib.sdrplay_api_Update(self._device.dev, capi.Tuner_B,
                                            capi.Update_RspDuo_RfDabNotchControl, capi.Update_Ext1_None)
        if err != 0:
            logger.warning(f"RSPduo Channel B DAB notch update failed: err={err}")

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
        # Liveness stamp for _stall_watchdog — a plain float write from the
        # vendor callback thread, GIL-atomic, no lock needed on the read side.
        self._last_sample_at = time.monotonic()
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
        if self.combiner.enabled:
            # RX0's target/filter tracks RX1's own live settings each
            # callback — cheap attribute reads/copies, no separate sync
            # path needed. rf_center_hz_a is set on retune, below.
            # try/except: this runs inside a ctypes-registered vendor
            # callback — an uncaught exception here can be silently
            # swallowed by ctypes instead of surfacing as a normal
            # traceback, so log explicitly rather than trust the default
            # thread exception hook to catch it.
            try:
                self.combiner.target_freq_hz = self.audio.target_freq_hz
                self.combiner.mode = self.audio.mode
                self.combiner.bandwidth_hz = self.audio.bandwidth_hz
                self.combiner.low_cut_hz = self.audio.low_cut_hz
                self.combiner.rf_center_hz_a = self.audio.rf_center_hz
                self.combiner.feed_a(i, q_arr)
            except Exception:
                logger.exception("Combiner feed_a error")

    def _on_stream_data_b(self, xi, xq, params, num_samples, reset, cb_context):
        """Channel B (Tuner 2 / Antenna 2) — mirrors _on_stream_data
        exactly (liveness stamp, queue handoff, feed to its own
        AudioDemodulator), plus the one-time first-samples log kept from
        Phase 0 as a quick live sanity check."""
        self._last_sample_at_b = time.monotonic()
        self._sample_count_b += num_samples
        if not self._ch_b_first_log_done:
            self._ch_b_first_log_done = True
            logger.info(
                f"RSPduo Channel B (Tuner 2 / Antenna 2) live — "
                f"first {num_samples} samples received")
        i = np.ctypeslib.as_array(xi, shape=(num_samples,)).astype(np.int16, copy=True)
        q_arr = np.ctypeslib.as_array(xq, shape=(num_samples,)).astype(np.int16, copy=True)
        try:
            self._q_b.put_nowait((i, q_arr))
        except queue.Full:
            try:
                self._q_b.get_nowait()
                self.dropped_count_b += 1
            except queue.Empty:
                pass
            try:
                self._q_b.put_nowait((i, q_arr))
            except queue.Full:
                pass
        self.audio_b.feed(i, q_arr)
        if self.combiner.enabled:
            try:
                self.combiner.rf_center_hz_b = self.audio_b.rf_center_hz
                self.combiner.feed_b(i, q_arr)
            except Exception:
                logger.exception("Combiner feed_b error")

    def _on_event(self, event_id, tuner, params, cb_context):
        # Both events mean "the stream against this device is now invalid."
        # A removed event is usually a USB glitch (drop + re-enumerate); a
        # failure is a transient fault. Either way, don't sit half-dead
        # streaming garbage from a gone device until a full app restart —
        # tear down and try to come back in place. _schedule_recovery
        # collapses a removed+failure burst into one attempt.
        if event_id == capi.Event_DeviceRemoved:
            self._schedule_recovery("device removed")
        elif event_id == capi.Event_DeviceFailure:
            self._schedule_recovery("device failure")

    def _schedule_recovery(self, reason: str):
        """Kick off recovery from the vendor callback thread. Idempotent
        across a burst of events: the first caller wins the gate and the
        rest no-op until that attempt finishes."""
        with self._recovery_lock:
            if self._recovery_pending:
                return
            self._recovery_pending = True
        self.available = False
        self.status = "unavailable"
        logger.warning(f"SDR {reason} — tearing down and attempting recovery")
        if self._loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._recover(), self._loop)
                return
            except RuntimeError:
                pass   # loop closing/closed during shutdown
        with self._recovery_lock:
            self._recovery_pending = False

    async def _recover(self):
        """Tear the pipeline down, then re-init in place with backoff. A
        transient glitch re-enumerates within a second or two and the
        _init_with_retries backoff catches it, resuming the stream with no
        app restart. A genuine unplug exhausts the retries and lands in a
        clean 'unavailable' (the frontend's staleness watchdog surfaces that
        instead of a frozen-but-'LIVE' panel)."""
        try:
            await self._teardown_pipeline()
            if await self._init_with_retries():
                self._start_pipeline()
                logger.info("SDR recovered — stream resumed")
            else:
                logger.warning(
                    "SDR recovery failed — device likely gone "
                    "(needs replug / daemon restart)")
        except Exception:
            logger.exception("SDR recovery raised — leaving device unavailable")
            self.available = False
            self.status = "unavailable"
        finally:
            with self._recovery_lock:
                self._recovery_pending = False

    async def _stall_watchdog(self):
        """Trip when the IQ callback goes silent while we believe we're live.
        The RSPdx can stop delivering samples mid-session without raising any
        DeviceRemoved/DeviceFailure event — the _on_event path never fires for
        this — so data-liveness is the only reliable signal for it. On a trip
        we route through the same _schedule_recovery path as a real event: it
        tears the dead session down (freeing the device, so a later restart
        isn't fighting a held handle) and attempts one in-place re-init. A
        transient stall resumes; a hard wedge keeps failing Init and lands in a
        clean 'unavailable', where the frontend badge tells the operator to
        replug the USB (the only thing that clears it — confirmed by probe)."""
        try:
            while True:
                await asyncio.sleep(1.0)
                if not self.available:
                    return
                gap = time.monotonic() - self._last_sample_at
                gap_b = time.monotonic() - self._last_sample_at_b
                # Both channels share one device/lib handle, so a stall on
                # either is grounds for the same whole-session recovery —
                # there's no such thing as "just restart Channel B".
                if gap > self._stall_timeout_s:
                    logger.warning(
                        f"SDR IQ stream stalled — no samples for {gap:.1f}s and "
                        f"no device event fired; tearing down (device likely "
                        f"wedged — a USB replug is what clears this)")
                    self._schedule_recovery("stream stalled")
                    return
                if gap_b > self._stall_timeout_s:
                    logger.warning(
                        f"SDR Channel B IQ stream stalled — no samples for "
                        f"{gap_b:.1f}s; tearing down (device likely wedged — "
                        f"a USB replug is what clears this)")
                    self._schedule_recovery("channel B stream stalled")
                    return
        except asyncio.CancelledError:
            pass

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
        power = (np.abs(spectrum) ** 2).astype(np.float32)

        # Diversity phase tap — raw (unaveraged) bin nearest the tuned
        # frequency, every frame. See iq_phase_diff_deg / _last_phasor_a.
        if self.audio.tx_active or self.audio.target_freq_hz is None:
            self._last_phasor_a = None
        else:
            n = len(spectrum)
            bin_hz = self.sample_rate_hz / n
            full_lo_hz = self.rf_freq_hz - self.sample_rate_hz / 2
            target_bin = int(round((self.audio.target_freq_hz - full_lo_hz) / bin_hz))
            self._last_phasor_a = complex(spectrum[max(0, min(n - 1, target_bin))])

        if self.audio.tx_active:
            # SDR Switch disconnects the antenna during TX — this magnitude
            # is disconnected-input noise, not a real reading. Same reasoning
            # as AudioDemodulator's TX handling: don't let it pollute the
            # rolling average, or RX would resume post-TX blended with a few
            # hundred ms of garbage instead of starting clean. The frontend
            # already freezes its own display during TX (rig.ptt), but the
            # server kept computing+publishing real frames underneath that
            # freeze regardless — this average is shared state across calls,
            # so it has to stay correct even though nobody's looking at it.
            self._avg_power = None
            mag_db = (10.0 * np.log10(power / (self._fullscale_ref ** 2) + 1e-12)).astype(np.float32)
        else:
            if self._reset_avg_event.is_set():
                self._avg_power = None
                self._reset_avg_event.clear()
            if self._avg_power is None:
                self._avg_power = power
            else:
                # Averaged in linear power, not dB — averaging dB values directly
                # is biased low by log compression. Converting to dB once at the
                # end, after averaging, matches how Bartlett/Welch averaging is
                # done in real spectrum analyzers.
                self._avg_power = self._avg_power * self._avg_decay + power * (1.0 - self._avg_decay)
            mag_db = (10.0 * np.log10(self._avg_power / (self._fullscale_ref ** 2) + 1e-12)).astype(np.float32)
        return {
            "ts": time.time(),
            "channel": "A",
            "center_freq_hz": self.rf_freq_hz,
            "span_hz": self.sample_rate_hz,
            "sample_rate_hz": self.sample_rate_hz,
            "data": mag_db,
        }

    def _consumer_loop_b(self):
        """Channel B's FFT consumer — mirrors _consumer_loop exactly,
        against _q_b/dropped_count_b and _compute_frame_b instead."""
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
                if self.dropped_count_b != last_logged_drops:
                    logger.warning(
                        f"Channel B spectrum queue drops: {self.dropped_count_b} total "
                        f"(+{self.dropped_count_b - last_logged_drops} in last 5s)")
                    last_logged_drops = self.dropped_count_b
                next_drop_log = now_check + 5.0
            try:
                i, q_arr = self._q_b.get(timeout=0.5)
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

            frame = self._compute_frame_b(buf_i[write_pos - self.fft_size:write_pos],
                                           buf_q[write_pos - self.fft_size:write_pos])
            if self._loop is not None:
                try:
                    asyncio.run_coroutine_threadsafe(self._publish_b(frame), self._loop)
                except RuntimeError:
                    pass  # loop is closing/closed during shutdown

    def _compute_frame_b(self, block_i, block_q) -> dict:
        """Channel B's spectrum frame — mirrors _compute_frame exactly,
        against Channel B's own avg-power/reset-event state and
        AudioDemodulator instance."""
        iq = (block_i.astype(np.float32) + 1j * block_q.astype(np.float32)).astype(np.complex64)
        iq *= self._window
        spectrum = np.fft.fftshift(np.fft.fft(iq))
        power = (np.abs(spectrum) ** 2).astype(np.float32)

        # Diversity phase tap — see _compute_frame's own comment.
        if self.audio_b.tx_active or self.audio_b.target_freq_hz is None:
            self._last_phasor_b = None
        else:
            n = len(spectrum)
            bin_hz = self.sample_rate_hz / n
            full_lo_hz = self.rf_freq_hz_b - self.sample_rate_hz / 2
            target_bin = int(round((self.audio_b.target_freq_hz - full_lo_hz) / bin_hz))
            self._last_phasor_b = complex(spectrum[max(0, min(n - 1, target_bin))])

        if self.audio_b.tx_active:
            self._avg_power_b = None
            mag_db = (10.0 * np.log10(power / (self._fullscale_ref ** 2) + 1e-12)).astype(np.float32)
        else:
            if self._reset_avg_event_b.is_set():
                self._avg_power_b = None
                self._reset_avg_event_b.clear()
            if self._avg_power_b is None:
                self._avg_power_b = power
            else:
                self._avg_power_b = self._avg_power_b * self._avg_decay + power * (1.0 - self._avg_decay)
            mag_db = (10.0 * np.log10(self._avg_power_b / (self._fullscale_ref ** 2) + 1e-12)).astype(np.float32)
        return {
            "ts": time.time(),
            "channel": "B",
            "center_freq_hz": self.rf_freq_hz_b,
            "span_hz": self.sample_rate_hz,
            "sample_rate_hz": self.sample_rate_hz,
            "data": mag_db,
        }

    def passband_strength_db(self, center_hz: float, bandwidth_hz: float) -> Optional[float]:
        """Average power (dBFS) across the bins spanning [center_hz ± bandwidth_hz/2]
        in the latest averaged frame — used as the S-meter source, since the
        radio's own receive antenna port sees nothing under this station's SDR
        Switch wiring (the RSPdx-R2 is the actual receiver). None if no frame
        computed yet, or mid-TX (averaging is reset/skipped during TX, see
        _compute_frame)."""
        if self._avg_power is None:
            return None
        n = len(self._avg_power)
        span_hz = self.sample_rate_hz
        bin_hz = span_hz / n
        full_lo_hz = self.rf_freq_hz - span_hz / 2
        start_bin = int((center_hz - bandwidth_hz / 2 - full_lo_hz) / bin_hz)
        end_bin = int(np.ceil((center_hz + bandwidth_hz / 2 - full_lo_hz) / bin_hz))
        start_bin = max(0, min(n - 1, start_bin))
        end_bin = max(start_bin + 1, min(n, end_bin))
        avg_power = float(np.mean(self._avg_power[start_bin:end_bin]))
        return 10.0 * float(np.log10(avg_power / (self._fullscale_ref ** 2) + 1e-30))

    def passband_strength_db_b(self, center_hz: float, bandwidth_hz: float) -> Optional[float]:
        """Channel B's S-meter source — see passband_strength_db."""
        if self._avg_power_b is None:
            return None
        n = len(self._avg_power_b)
        span_hz = self.sample_rate_hz
        bin_hz = span_hz / n
        full_lo_hz = self.rf_freq_hz_b - span_hz / 2
        start_bin = int((center_hz - bandwidth_hz / 2 - full_lo_hz) / bin_hz)
        end_bin = int(np.ceil((center_hz + bandwidth_hz / 2 - full_lo_hz) / bin_hz))
        start_bin = max(0, min(n - 1, start_bin))
        end_bin = max(start_bin + 1, min(n, end_bin))
        avg_power = float(np.mean(self._avg_power_b[start_bin:end_bin]))
        return 10.0 * float(np.log10(avg_power / (self._fullscale_ref ** 2) + 1e-30))

    def iq_phase_diff_deg(self) -> Optional[float]:
        """Instantaneous phase difference (-180..+180) between Channel A's
        and Channel B's tuned-frequency FFT bin, in degrees. Diversity
        diagnostic, not an S-meter-style average — see the phasor fields'
        own comment for why. This number's absolute value is meaningless
        (it includes whatever arbitrary phase offset each channel's own
        capture happened to start at) — what matters is its behavior over
        TIME: if the RSPduo's two tuners are genuinely phase-locked to each
        other (not just sample-clock-locked, which Dual Tuner mode
        guarantees regardless), this stays close to a fixed value, wobbling
        only with real propagation differences between the two antennas.
        If the tuners' own LOs are free-running relative to each other,
        this rotates continuously at their beat frequency instead. None
        if either channel has no live reading right now (no frame yet, no
        audio target tuned, or either side is mid-TX)."""
        if self._last_phasor_a is None or self._last_phasor_b is None:
            return None
        return float(np.degrees(np.angle(self._last_phasor_a * np.conj(self._last_phasor_b))))

    async def _publish(self, frame: dict):
        for cb in self._spectrum_callbacks:
            await cb(frame)

    async def _publish_b(self, frame: dict):
        for cb in self._spectrum_callbacks_b:
            await cb(frame)

    async def _publish_fine_a(self, frame: dict):
        frame["channel"] = "A"
        await self._publish(frame)

    async def _publish_fine_b(self, frame: dict):
        frame["channel"] = "B"
        await self._publish_b(frame)
