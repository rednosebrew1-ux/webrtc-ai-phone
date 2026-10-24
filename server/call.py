"""One live call: listens, detects turns, transcribes, asks the LLM, speaks back."""

import asyncio
import json
import logging
import math

import numpy as np

from .audio import InputResampler, SpeakerTrack
from .llm import stream_chat, stream_sentences

log = logging.getLogger(__name__)

FRAME_MS = 20
START_MS = 60          # sustained speech needed to count as talking
PREROLL_MS = 300       # audio kept from just before speech starts
MAX_UTTERANCE_S = 30
HANGUP_TAG = "<hangup/>"


def rms(pcm):
    if not pcm:
        return 0.0
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    return float(math.sqrt(np.mean(a * a)))


class CallSession:
    def __init__(self, cfg, stt, tts, pc, call_id, reason=None, on_end=None):
        self.cfg, self.stt, self.tts, self.pc = cfg, stt, tts, pc
        self.call_id = call_id
        self.reason = reason            # set when the AI placed the call
        self.on_end = on_end
        self.speaker = SpeakerTrack()
        self.channel = None
        self.history = []
        self.transcript = []            # [{"role": "user"|"assistant", "text": ...}]
        self.ended = False
        self._task = None               # current response task (thinking/speaking)
        self._listen_task = None
        self._ready = asyncio.Event()

    # ---- signaling helpers ----

    def send(self, **event):
        if self.channel and self.channel.readyState == "open":
            self.channel.send(json.dumps(event))

    def attach_channel(self, channel):
        self.channel = channel

        @channel.on("message")
        def on_message(msg):
            try:
                data = json.loads(msg)
            except (TypeError, json.JSONDecodeError):
                return
            if data.get("type") == "hangup":
                asyncio.ensure_future(self.end("user"))

        if channel.readyState == "open":
            self._ready.set()
        else:
            channel.on("open", self._ready.set)

    def attach_track(self, track):
        self._listen_task = asyncio.ensure_future(self._listen(track))

    # ---- call flow ----

    async def run(self):
        """Ring/answer, then greet. Listening runs in parallel once the track arrives."""
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=15)
        except asyncio.TimeoutError:
            log.warning("[%s] data channel never opened", self.call_id)
            await self.end("error")
            return
        if self.reason:
            # The AI rang the user and they accepted: pick up right away and say why.
            self.send(type="answered")
            self._task = asyncio.ensure_future(self._opener())
        else:
            # The user rang the AI: let it ring a bit, like a real phone.
            self.send(type="ringing")
            await asyncio.sleep(self.cfg.ai_answer_delay)
            if self.ended:
                return
            self.send(type="answered")
            self._task = asyncio.ensure_future(self._say_fixed(self.cfg.greeting))

    async def end(self, by):
        if self.ended:
            return
        self.ended = True
        log.info("[%s] call ended by %s", self.call_id, by)
        self.send(type="hangup", by=by)
        for task in (self._task, self._listen_task):
            if task and not task.done() and task is not asyncio.current_task():
                task.cancel()
        await asyncio.sleep(0.2)  # let the hangup message flush
        await self.pc.close()
        if self.on_end:
            self.on_end(self)

    # ---- listening & turn-taking ----

    async def _listen(self, track):
        resample = InputResampler(16000)
        floor = self.cfg.vad_min_rms / self.cfg.vad_ratio
        preroll, buf = [], bytearray()
        in_speech, loud_ms, silence_ms = False, 0, 0
        preroll_frames = PREROLL_MS // FRAME_MS
        peak, frames = 0.0, 0
        try:
            while not self.ended:
                frame = await track.recv()
                pcm = resample(frame)
                if not pcm:
                    continue
                level = rms(pcm)
                threshold = max(self.cfg.vad_min_rms, floor * self.cfg.vad_ratio)
                # Once a second, log the loudest level heard (LOG_LEVEL=DEBUG) to help tune VAD_MIN_RMS.
                peak, frames = max(peak, level), frames + 1
                if frames == 50:
                    log.debug("[%s] mic peak %.0f, threshold %.0f", self.call_id, peak, threshold)
                    peak, frames = 0.0, 0
                loud = level > threshold
                busy = self._task is not None and not self._task.done()

                if not in_speech:
                    if not loud:
                        floor = 0.95 * floor + 0.05 * level
                    preroll.append(pcm)
                    del preroll[:-preroll_frames]
                    loud_ms = loud_ms + FRAME_MS if loud else 0
                    # While the AI talks, require longer speech before interrupting it.
                    needed = self.cfg.barge_in_ms if busy else START_MS
                    if loud_ms >= needed:
                        if busy:
                            self._interrupt()
                        in_speech, silence_ms = True, 0
                        buf = bytearray(b"".join(preroll))
                        self.send(type="state", state="hearing")
                    continue

                buf += pcm
                silence_ms = 0 if loud else silence_ms + FRAME_MS
                if silence_ms >= self.cfg.silence_ms or len(buf) >= MAX_UTTERANCE_S * 32000:
                    in_speech, loud_ms = False, 0
                    utterance, buf = bytes(buf), bytearray()
                    self._task = asyncio.ensure_future(self._respond(utterance))
        except asyncio.CancelledError:
            pass
        except Exception as e:  # MediaStreamError when the peer goes away
            log.info("[%s] audio input ended: %r", self.call_id, e)
            await self.end("network")

    def _interrupt(self):
        log.info("[%s] caller interrupted", self.call_id)
        self.speaker.clear()
        if self._task and not self._task.done():
            self._task.cancel()
        self.send(type="interrupted")

    # ---- responding ----

    def _messages(self):
        system = self.cfg.system_prompt
        if self.reason:
            system += (f"\n\nYou ({self.cfg.ai_name}) placed this call to the user. "
                       f"The reason for the call: {self.reason}")
        return [{"role": "system", "content": system}] + self.history

    async def _respond(self, utterance):
        self.send(type="state", state="thinking")
        text = await self.stt.transcribe(utterance)
        if not text:
            self.send(type="state", state="listening")
            return
        log.info("[%s] user: %s", self.call_id, text)
        self.history.append({"role": "user", "content": text})
        self.transcript.append({"role": "user", "text": text})
        self.send(type="transcript", role="user", text=text)
        await self._speak_llm()

    async def _opener(self):
        self.history.append({"role": "user", "content": "(The user just picked up the phone. "
                                                        "Greet them and say why you're calling.)"})
        await self._speak_llm()

    async def _say_fixed(self, text):
        self.history.append({"role": "assistant", "content": text})
        self.transcript.append({"role": "assistant", "text": text})
        self.send(type="transcript", role="assistant", text=text)
        await self._speak(text)
        await self._finish_turn()

    async def _speak(self, text):
        pcm, rate = await self.tts.synthesize(text)
        self.send(type="state", state="speaking")
        self.speaker.play(pcm, rate)

    async def _speak_llm(self):
        spoken, hang_up = [], False
        try:
            deltas = stream_chat(self.cfg, self._messages(), user=f"call-{self.call_id}")
            async for sentence in stream_sentences(deltas):
                if HANGUP_TAG in sentence:
                    hang_up = True
                    sentence = sentence.replace(HANGUP_TAG, "").strip()
                if not sentence:
                    continue
                spoken.append(sentence)
                self.send(type="transcript", role="assistant", text=sentence, append=len(spoken) > 1)
                await self._speak(sentence)
        except asyncio.CancelledError:
            if spoken:
                self._remember(" ".join(spoken) + " —")
            raise
        except Exception as e:
            log.error("[%s] LLM/TTS error: %r", self.call_id, e)
            fallback = "Sorry, I'm having trouble thinking right now."
            spoken.append(fallback)
            self.send(type="transcript", role="assistant", text=fallback, append=len(spoken) > 1)
            await self._speak(fallback)
        self._remember(" ".join(spoken))
        await self._finish_turn()
        if hang_up:
            await self.end("ai")

    def _remember(self, text):
        if text:
            self.history.append({"role": "assistant", "content": text})
            self.transcript.append({"role": "assistant", "text": text})

    async def _finish_turn(self):
        await self.speaker.wait_idle()
        self.send(type="state", state="listening")
