"""
Digital Audio Output — Virtual-Cable Bridge for Digital Modes

Feeds the SDR's demodulated RX audio (the same audio the panadapter browser
tab plays) to a virtual audio device (BlackHole 2ch) so digital-mode
software (WSJT-X etc.) can use it as a soundcard input — replacing the old
workflow of physically switching the antenna back to the radio's own
receiver to run digital modes. Same antenna wiring, same panadapter tuning,
for both voice and digital; this is just a second subscriber on
AudioDemodulator's existing on_audio() hook.

TX is unaffected: digital-mode software still drives the radio's own USB
Audio CODEC for transmit audio, and PTT/CAT are unaffected — only the RX
audio source changes.
"""

import logging
import queue
import threading
from typing import Optional

import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly

from sdr.audio_demod import INTERMEDIATE_RATE_HZ

logger = logging.getLogger(__name__)

OUTPUT_DEVICE_NAME = "BlackHole 2ch"
OUTPUT_SAMPLE_RATE_HZ = 48_000
RESAMPLE_RATIO = OUTPUT_SAMPLE_RATE_HZ // INTERMEDIATE_RATE_HZ   # exact: 3


class DigitalAudioOutput:
    """Mirrors AudioDemodulator's own queue+thread pattern: the async
    on_audio_frame callback (invoked from the asyncio loop via
    AudioDemodulator._publish) only does a fast, non-blocking queue put —
    the actual (blocking) device write happens on a dedicated thread so a
    slow or stalled virtual-audio device can never stall the event loop."""

    def __init__(self):
        self.available = False   # BlackHole found at the OS level
        self.active = False      # stream open and actively writing
        self._device_index: Optional[int] = None
        self._stream: Optional[sd.OutputStream] = None
        self._q: "queue.Queue" = queue.Queue(maxsize=64)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._warned_no_device = False

    def start(self):
        self._device_index = self._find_device()
        if self._device_index is None:
            if not self._warned_no_device:
                logger.warning(
                    f'"{OUTPUT_DEVICE_NAME}" not found — digital-mode audio '
                    f"output disabled (install BlackHole to enable it)")
                self._warned_no_device = True
            self.available = False
            return
        self.available = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="digital-audio-out", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3.0)
        self._thread = None
        self.active = False

    async def on_audio_frame(self, audio_bytes: bytes):
        """Registered via AudioDemodulator.on_audio() — fast, non-blocking,
        same drop-oldest-on-full policy as AudioDemodulator.feed()."""
        if not self.available:
            return
        try:
            self._q.put_nowait(audio_bytes)
        except queue.Full:
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(audio_bytes)
            except queue.Full:
                pass

    def _find_device(self) -> Optional[int]:
        try:
            devices = sd.query_devices()
        except Exception:
            logger.exception("Could not query audio devices")
            return None
        for i, d in enumerate(devices):
            if d.get("name") == OUTPUT_DEVICE_NAME and d.get("max_output_channels", 0) >= 2:
                return i
        return None

    def _run(self):
        try:
            with sd.OutputStream(
                device=self._device_index, samplerate=OUTPUT_SAMPLE_RATE_HZ,
                channels=2, dtype="float32",
            ) as stream:
                self._stream = stream
                self.active = True
                logger.info(f'Digital audio output -> "{OUTPUT_DEVICE_NAME}" active')
                while not self._stop_event.is_set():
                    try:
                        audio_bytes = self._q.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    try:
                        pcm16 = np.frombuffer(audio_bytes, dtype=np.int16)
                        audio_f32 = pcm16.astype(np.float32) / 32768.0
                        audio_48k = resample_poly(audio_f32, RESAMPLE_RATIO, 1).astype(np.float32)
                        stereo = np.repeat(audio_48k[:, None], 2, axis=1)
                        stream.write(stereo)
                    except Exception:
                        logger.exception("Digital audio output write error")
        except Exception:
            logger.exception(
                f'Failed to open "{OUTPUT_DEVICE_NAME}" — digital-mode audio output disabled')
        finally:
            self.active = False
            self._stream = None
