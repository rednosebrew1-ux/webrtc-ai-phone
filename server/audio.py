"""Audio plumbing between aiortc frames and 16-bit mono PCM."""

import asyncio
import fractions
import time

import av
import numpy as np
from aiortc import MediaStreamTrack

OUT_RATE = 48000
FRAME_MS = 20
OUT_SAMPLES = OUT_RATE * FRAME_MS // 1000  # 960 samples per 20 ms frame


def resample_pcm(pcm, src_rate, dst_rate):
    """Resample 16-bit mono PCM using libav (via PyAV)."""
    if src_rate == dst_rate or not pcm:
        return pcm
    samples = np.frombuffer(pcm, dtype=np.int16).reshape(1, -1)
    frame = av.AudioFrame.from_ndarray(samples, format="s16", layout="mono")
    frame.sample_rate = src_rate
    resampler = av.AudioResampler(format="s16", layout="mono", rate=dst_rate)
    out = b"".join(f.to_ndarray().tobytes() for f in resampler.resample(frame))
    out += b"".join(f.to_ndarray().tobytes() for f in resampler.resample(None))
    return out


class InputResampler:
    """Turns incoming WebRTC frames (any rate/layout) into 16 kHz mono PCM."""

    def __init__(self, rate=16000):
        self.rate = rate
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)

    def __call__(self, frame):
        return b"".join(f.to_ndarray().tobytes() for f in self._resampler.resample(frame))


class SpeakerTrack(MediaStreamTrack):
    """Outgoing audio track: plays queued PCM, silence otherwise."""

    kind = "audio"

    def __init__(self):
        super().__init__()
        self._buf = bytearray()
        self._start = None
        self._pts = 0
        self._idle = asyncio.Event()
        self._idle.set()

    def play(self, pcm, rate):
        """Queue 16-bit mono PCM at any sample rate."""
        self._buf += resample_pcm(pcm, rate, OUT_RATE)
        if self._buf:
            self._idle.clear()

    def clear(self):
        """Stop playback immediately (used for barge-in)."""
        self._buf.clear()
        self._idle.set()

    @property
    def playing(self):
        return bool(self._buf)

    async def wait_idle(self):
        await self._idle.wait()

    async def recv(self):
        # Pace frames in real time, like aiortc's AudioStreamTrack.
        if self._start is None:
            self._start = time.time()
        else:
            self._pts += OUT_SAMPLES
            wait = self._start + self._pts / OUT_RATE - time.time()
            if wait > 0:
                await asyncio.sleep(wait)

        need = OUT_SAMPLES * 2
        chunk = bytes(self._buf[:need])
        del self._buf[:need]
        if len(chunk) < need:
            chunk = chunk.ljust(need, b"\x00")
        if not self._buf:
            self._idle.set()

        frame = av.AudioFrame.from_ndarray(np.frombuffer(chunk, dtype=np.int16).reshape(1, -1),
                                           format="s16", layout="mono")
        frame.sample_rate = OUT_RATE
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, OUT_RATE)
        return frame
