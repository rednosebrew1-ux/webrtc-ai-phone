"""One live call: listens, detects turns, transcribes, asks the LLM, speaks back."""

import asyncio
import json
import logging
import math

import numpy as np

from .audio import InputResampler, SpeakerTrack
from .llm import LLMError, stream_chat, stream_sentences
from .vad import SileroStream

log = logging.getLogger(__name__)

FRAME_MS = 20
START_MS = 60          # sustained speech needed to count as talking
PREROLL_MS = 300       # audio kept from just before speech starts
MAX_UTTERANCE_S = 30
HANGUP_TAG = "<hangup/>"


def friendly_error(e):
    """What the AI says out loud when its brain fails. Details go to the server log."""
    text = str(e).lower()
    status = getattr(e, "status", None)
    if isinstance(e, asyncio.TimeoutError):
        return "Sorry, my brain is taking too long to answer. Try me again in a minute."
    if status == 429 or "rate limit" in text or "slow down" in text or "too many requests" in text:
        return "My brain is being rate limited right now. Give it a minute and try again."
    if status in (401, 403):
        return "I can't log in to my brain. Check the API key in the settings."
    if status == 404 or "not found" in text or "unknown agent" in text:
        return "My brain's address or model name looks wrong. Check the settings."
    if isinstance(e, (OSError, ConnectionError)) or "cannot connect" in text:
        return "I can't reach my brain right now. Is it running?"
    return "Sorry, I'm having trouble thinking right now."


def rms(pcm):
    if not pcm:
        return 0.0
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    return float(math.sqrt(np.mean(a * a)))


class EnergyVAD:
    """Fallback detector (VAD_BACKEND=energy): anything loud enough above the noise floor counts as speech."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.floor = cfg.vad_min_rms / cfg.vad_ratio

    def __call__(self, pcm):
        level = rms(pcm)
        loud = level > max(self.cfg.vad_min_rms, self.floor * self.cfg.vad_ratio)
        if not loud:
            self.floor = 0.95 * self.floor + 0.05 * level
        return [1.0 if loud else 0.0]


def make_vad(cfg):
    return EnergyVAD(cfg) if cfg.vad_backend == "energy" else SileroStream()


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
        self._untranscribed = b""       # caller audio sent to STT but not transcribed yet
        self._answered = False          # mic audio is ignored while the call is still ringing
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
            self._answered = True
            self.send(type="answered")
            self._start(self._opener())
        else:
            # The user rang the AI: let it ring a bit, like a real phone.
            self.send(type="ringing")
            await asyncio.sleep(self.cfg.ai_answer_delay)
            if self.ended:
                return
            self._answered = True
            self.send(type="answered")
            self._start(self._say_fixed(self.cfg.greeting))

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
        vad = make_vad(self.cfg)
        preroll, buf = [], bytearray()
        speech, in_speech, speech_ms, silence_ms = False, False, 0, 0
        preroll_frames = PREROLL_MS // FRAME_MS
        peak_level, peak_prob, frames = 0.0, 0.0, 0
        try:
            while not self.ended:
                frame = await track.recv()
                pcm = resample(frame)
                if not pcm:
                    continue
                probs = vad(pcm)
                if probs:  # Silero steps are 32 ms, frames are 20 ms: keep the latest verdict
                    speech = max(probs) >= self.cfg.vad_threshold
                level = rms(pcm)
                # Once a second, log levels (LOG_LEVEL=DEBUG) to help tune VAD settings.
                peak_level, frames = max(peak_level, level), frames + 1
                peak_prob = max([peak_prob] + probs)
                if frames == 50:
                    log.debug("[%s] mic peak level %.0f, peak speech probability %.2f",
                              self.call_id, peak_level, peak_prob)
                    peak_level, peak_prob, frames = 0.0, 0.0, 0
                if not self._answered:
                    continue  # still ringing: nobody is listening yet
                busy = self._task is not None and not self._task.done()

                if not in_speech:
                    preroll.append(pcm)
                    del preroll[:-preroll_frames]
                    # While the AI is talking, interrupting takes longer, louder speech, so leftover
                    # echo of its own voice doesn't cut it off. While it's only thinking, any speech counts.
                    counts = speech and (not self.speaker.playing or level >= self.cfg.vad_min_rms)
                    speech_ms = speech_ms + FRAME_MS if counts else 0
                    if speech_ms >= (self.cfg.barge_in_ms if busy else START_MS):
                        unheard = self._interrupt() if busy else b""
                        in_speech, silence_ms = True, 0
                        buf = bytearray(unheard + b"".join(preroll))
                        self.send(type="state", state="hearing")
                    continue

                buf += pcm
                silence_ms = 0 if speech else silence_ms + FRAME_MS
                if silence_ms >= self.cfg.silence_ms or len(buf) >= MAX_UTTERANCE_S * 32000:
                    in_speech, speech_ms = False, 0
                    utterance, buf = bytes(buf), bytearray()
                    self._start(self._respond(utterance))
        except asyncio.CancelledError:
            pass
        except Exception as e:  # MediaStreamError when the peer goes away
            log.info("[%s] audio input ended: %r", self.call_id, e)
            await self.end("network")

    def _start(self, coro):
        """Start the next reply. Only one runs at a time: an unfinished one is cancelled."""
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = asyncio.ensure_future(coro)

    def _interrupt(self):
        """Stop the current reply. Returns audio the caller said that was never transcribed."""
        spoken = self.speaker.playing
        log.info("[%s] caller interrupted%s", self.call_id, "" if spoken else " (before the reply started)")
        self.speaker.clear()
        unheard, self._untranscribed = self._untranscribed, b""
        if self._task and not self._task.done():
            self._task.cancel()
        self.send(type="interrupted", spoken=spoken)
        return unheard

    # ---- responding ----

    def _messages(self):
        system = self.cfg.system_prompt
        if self.reason:
            system += (f"\n\nYou ({self.cfg.ai_name}) placed this call to the user. "
                       f"The reason for the call: {self.reason}")
        return [{"role": "system", "content": system}] + self.history

    async def _respond(self, utterance):
        self.send(type="state", state="thinking")
        # If the caller keeps talking before this is transcribed, it gets merged into their next turn.
        self._untranscribed = utterance
        text = await self.stt.transcribe(utterance)
        self._untranscribed = b""
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
        deltas = stream_chat(self.cfg, self._messages(), user=f"call-{self.call_id}")
        sentences = stream_sentences(deltas)
        try:
            first = await self._first_sentence(sentences)
            if first is not None:
                pending = [first]
                while pending:
                    sentence = pending.pop()
                    if HANGUP_TAG in sentence:
                        hang_up = True
                        sentence = sentence.replace(HANGUP_TAG, "").strip()
                    if sentence:
                        spoken.append(sentence)
                        self.send(type="transcript", role="assistant", text=sentence, append=len(spoken) > 1)
                        await self._speak(sentence)
                    try:
                        pending.append(await sentences.__anext__())
                    except StopAsyncIteration:
                        pass
        except asyncio.CancelledError:
            if spoken:
                self._remember(" ".join(spoken) + " —")
            raise
        except (Exception, asyncio.TimeoutError) as e:
            log.error("[%s] LLM/TTS error: %r", self.call_id, e)
            message = friendly_error(e)
            spoken.append(message)
            self.send(type="transcript", role="assistant", text=message, append=len(spoken) > 1, error=True)
            await self._speak(message)
        finally:
            await sentences.aclose()
            await deltas.aclose()  # closes the HTTP connection to the LLM
        self._remember(" ".join(spoken))
        await self._finish_turn()
        if hang_up:
            await self.end("ai")

    async def _first_sentence(self, sentences):
        """Wait for the reply to start. Says a filler if it's slow; gives up after LLM_TIMEOUT."""
        first = asyncio.ensure_future(sentences.__anext__())
        try:
            use_filler = 0 < self.cfg.filler_delay < self.cfg.llm_timeout
            done = (await asyncio.wait({first}, timeout=self.cfg.filler_delay))[0] if use_filler else {first}
            if not done:
                fillers = [f.strip() for f in self.cfg.fillers.split("|") if f.strip()]
                if fillers:
                    self._filler_index = (getattr(self, "_filler_index", -1) + 1) % len(fillers)
                    filler = fillers[self._filler_index]
                    log.info("[%s] reply is slow, saying filler: %s", self.call_id, filler)
                    self.send(type="filler", text=filler)
                    await self._speak(filler)
            remaining = self.cfg.llm_timeout - (self.cfg.filler_delay if use_filler else 0)
            return await asyncio.wait_for(asyncio.shield(first), remaining)
        except StopAsyncIteration:
            return None  # empty reply
        finally:
            if not first.done():
                first.cancel()
                # Let the cancelled read finish before the stream is closed.
                await asyncio.gather(first, return_exceptions=True)

    def _remember(self, text):
        if text:
            self.history.append({"role": "assistant", "content": text})
            self.transcript.append({"role": "assistant", "text": text})

    async def _finish_turn(self):
        await self.speaker.wait_idle()
        self.send(type="state", state="listening")
