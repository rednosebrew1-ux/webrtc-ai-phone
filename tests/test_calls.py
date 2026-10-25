"""End-to-end call tests: a Python WebRTC client talks to the real server, with fake STT/TTS/LLM.

Run: python -m pytest tests
"""

import asyncio
import fractions
import json
import os
import time
import wave

import aiohttp
import av
import numpy as np
from aiohttp import web
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription

from server.app import PhoneApp
from server.audio import resample_pcm
from server.config import Settings
from server.llm import speakable, stream_sentences
from tests.fakes import FakeSTT, FakeTTS, fake_llm_app

TOKEN = "test-token"
PASSWORD = "pw"


SPEECH_WAV = os.path.join(os.path.dirname(__file__), "fixtures", "speech.wav")


def load_speech():
    """A real spoken sentence (espeak-ng), as 48 kHz 16-bit mono samples."""
    with wave.open(SPEECH_WAV) as w:
        pcm = resample_pcm(w.readframes(w.getnframes()), w.getframerate(), 48000)
    return np.frombuffer(pcm, dtype=np.int16)


class VoiceTrack(MediaStreamTrack):
    """Microphone stand-in: a loud tone during `talk_windows`, plus audio `clips` [(start_s, samples)]."""

    kind = "audio"

    def __init__(self, talk_windows, clips=()):
        super().__init__()
        self.windows = talk_windows
        self.clips = clips
        self.start = None
        self.pts = 0

    async def recv(self):
        if self.start is None:
            self.start = time.time()
        else:
            self.pts += 960
            wait = self.start + self.pts / 48000 - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
        now = self.pts / 48000
        talking = any(a <= now < b for a, b in self.windows)
        t = (self.pts + np.arange(960)) / 48000
        samples = (np.sin(2 * np.pi * 300 * t) * (9000 if talking else 0)).astype(np.int16)
        for start, clip in self.clips:
            i = self.pts - int(start * 48000)
            if 0 <= i < len(clip):
                part = clip[i:i + 960]
                samples[:len(part)] += part
        frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = 48000
        frame.pts = self.pts
        frame.time_base = fractions.Fraction(1, 48000)
        return frame


async def start_servers(llm_behavior=None, **overrides):
    llm_requests = []
    llm_runner = web.AppRunner(fake_llm_app(llm_requests, llm_behavior))
    await llm_runner.setup()
    llm_site = web.TCPSite(llm_runner, "127.0.0.1", 0)
    await llm_site.start()
    llm_port = llm_site._server.sockets[0].getsockname()[1]

    cfg = Settings()
    cfg.app_password, cfg.api_token = PASSWORD, TOKEN
    cfg.llm_url = f"http://127.0.0.1:{llm_port}/v1/chat/completions"
    cfg.ice_servers = ""
    cfg.ai_answer_delay = 0.5
    cfg.vad_backend = "energy"  # the fake voice is a tone, which the speech model rightly ignores
    cfg.vad_min_rms = 300
    cfg.ring_timeout = 5
    for key, value in overrides.items():
        setattr(cfg, key, value)
    stt = FakeSTT()
    phone = PhoneApp(cfg, stt, FakeTTS(max_seconds=overrides.get("tts_max_seconds", 1.5)))
    runner = web.AppRunner(phone.build())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return f"http://127.0.0.1:{port}", phone, stt, llm_requests, [runner, llm_runner]


async def dial(http, base, talk_windows, ring_id=None, clips=()):
    """Place a call from a fake browser. Returns (pc, events list, received-audio counter)."""
    pc = RTCPeerConnection()
    events = []
    channel = pc.createDataChannel("events")
    channel.on("message", lambda m: events.append(json.loads(m)))
    pc.addTrack(VoiceTrack(talk_windows, clips))
    heard = {"loud_frames": 0}

    @pc.on("track")
    def on_track(track):
        async def drain():
            try:
                while True:
                    f = await track.recv()
                    if np.abs(f.to_ndarray()).max() > 1000:
                        heard["loud_frames"] += 1
            except Exception:
                pass
        asyncio.ensure_future(drain())

    await pc.setLocalDescription(await pc.createOffer())
    async with http.post(f"{base}/api/offer", json={"sdp": pc.localDescription.sdp,
                                                    "type": pc.localDescription.type,
                                                    "ringId": ring_id}) as r:
        assert r.status == 200, await r.text()
        answer = await r.json()
    await pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))
    return pc, channel, events, heard


async def wait_for(predicate, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


def types(events):
    return [e["type"] for e in events]


async def _user_calls_ai():
    base, phone, stt, llm_requests, runners = await start_servers()
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as http:
            # Login is required.
            async with http.post(f"{base}/api/offer", json={}) as r:
                assert r.status == 401
            async with http.post(f"{base}/api/login", json={"password": PASSWORD}) as r:
                assert r.status == 200

            # Speak from 3.0 s to 4.0 s, after the AI has answered and greeted.
            pc, channel, events, heard = await dial(http, base, [(3.0, 4.0)])
            assert await wait_for(lambda: "answered" in types(events)), events
            assert types(events).index("ringing") < types(events).index("answered")
            assert await wait_for(lambda: "transcript" in types(events)), events
            greeting = [e for e in events if e["type"] == "transcript"][0]
            assert greeting["role"] == "assistant" and greeting["text"] == phone.cfg.greeting

            assert await wait_for(lambda: any(e.get("role") == "user" for e in events)), events
            assert await wait_for(lambda: any(e.get("text") == "Anything else?" for e in events)), events
            assert await wait_for(lambda: heard["loud_frames"] > 20), heard  # AI audio reached us
            assert llm_requests[-1]["messages"][-1] == {"role": "user", "content": "hello there"}

            channel.send(json.dumps({"type": "hangup"}))
            assert await wait_for(lambda: not phone.calls)
            await pc.close()
    finally:
        for r in runners:
            await r.cleanup()


async def _ai_calls_user():
    base, phone, stt, llm_requests, runners = await start_servers()
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as http:
            async with http.post(f"{base}/api/login", json={"password": PASSWORD}) as r:
                assert r.status == 200

            # A browser tab listening for rings.
            sse = await http.get(f"{base}/api/events")
            auth = {"Authorization": f"Bearer {TOKEN}"}
            async with aiohttp.ClientSession() as api:  # separate client: token only, no cookie
                async with api.post(f"{base}/api/ring", json={"reason": "The build finished."}) as r:
                    assert r.status == 401
                async with api.post(f"{base}/api/ring", json={"reason": "The build finished."}, headers=auth) as r:
                    ring = await r.json()
                assert ring["status"] == "ringing" and ring["listeners"] == 1

                line = b""
                while not line.startswith(b"data:"):
                    line = await asyncio.wait_for(sse.content.readline(), 5)
                event = json.loads(line[5:])
                assert event == {"type": "ring", "id": ring["id"], "reason": "The build finished."}

                # Accept: the AI should speak first, then hang up on its own.
                waiter = asyncio.ensure_future(api.get(f"{base}/api/ring/{ring['id']}?wait=1&timeout=20", headers=auth))
                pc, channel, events, heard = await dial(http, base, [], ring_id=ring["id"])
                assert await wait_for(lambda: "hangup" in types(events), 15), events
                assert types(events)[0] == "answered"
                hangup = [e for e in events if e["type"] == "hangup"][0]
                assert hangup["by"] == "ai"
                said = " ".join(e["text"] for e in events if e["type"] == "transcript")
                assert "build finished" in said and "<hangup/>" not in said
                assert "The build finished." in llm_requests[0]["messages"][0]["content"]

                result = await (await waiter).json()
                assert result["status"] == "completed"
                assert result["transcript"][0]["role"] == "assistant"
                await pc.close()

                # A second ring nobody answers is reported as missed.
                async with api.post(f"{base}/api/ring", json={"reason": "x"}, headers=auth) as r:
                    ring2 = await r.json()
                async with api.get(f"{base}/api/ring/{ring2['id']}?wait=1&timeout=10", headers=auth) as r:
                    assert (await r.json())["status"] == "missed"
            sse.close()
    finally:
        for r in runners:
            await r.cleanup()


async def _barge_in_with_real_speech():
    """Silero VAD: talking over the AI stops it; loud noise over the AI does not."""
    base, phone, stt, llm_requests, runners = await start_servers(
        vad_backend="silero", tts_max_seconds=8, greeting="x" * 200)
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as http:
            async with http.post(f"{base}/api/login", json={"password": PASSWORD}) as r:
                assert r.status == 200
            rng = np.random.default_rng(0)
            noise = rng.normal(0, 4000, 48000 * 2).astype(np.int16)   # 2 s of loud static
            speech = load_speech()
            # Greeting plays from ~0.5 s to ~8.5 s. Noise at 1.5-3.5 s, then speech at 4.5 s.
            pc, channel, events, heard = await dial(http, base, [], clips=[(1.5, noise), (4.5, speech)])
            assert await wait_for(lambda: "answered" in types(events)), events

            await asyncio.sleep(4.2)  # through the noise, before the speech
            assert "interrupted" not in types(events), "noise interrupted the AI"
            assert heard["loud_frames"] > 50, "AI should still be talking"

            assert await wait_for(lambda: "interrupted" in types(events), 4), events
            interrupted = [e for e in events if e["type"] == "interrupted"][0]
            assert interrupted["spoken"] is True
            assert await wait_for(lambda: any(e.get("role") == "user" for e in events), 8), events
            assert stt.calls == 1, "noise should never have reached speech-to-text"
            await pc.close()
    finally:
        for r in runners:
            await r.cleanup()


async def _one_turn(llm_behavior=None, **overrides):
    """Call the AI, say one thing, and return the call's events after the reply."""
    base, phone, stt, llm_requests, runners = await start_servers(llm_behavior, **overrides)
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as http:
            async with http.post(f"{base}/api/login", json={"password": PASSWORD}) as r:
                assert r.status == 200
            pc, channel, events, heard = await dial(http, base, [(2.0, 2.6)])
            replied = lambda: sum(e.get("role") == "assistant" for e in events) >= 2
            assert await wait_for(replied, 15), events
            await pc.close()
            return events
    finally:
        for r in runners:
            await r.cleanup()


def ai_lines(events):
    return [e["text"] for e in events if e.get("role") == "assistant"]


async def _sentences():
    async def deltas():
        for d in ["Hel", "lo there. How", " are you? I'm", " fine"]:
            yield d
    return [s async for s in stream_sentences(deltas())]


def test_user_calls_ai():
    asyncio.run(_user_calls_ai())


def test_ai_calls_user():
    asyncio.run(_ai_calls_user())


def test_barge_in_with_real_speech():
    asyncio.run(_barge_in_with_real_speech())


def test_slow_reply_says_filler_first():
    events = asyncio.run(_one_turn({"delay": 1.5}, filler_delay=0.5, fillers="One sec."))
    assert types(events).index("filler") < max(i for i, e in enumerate(events) if e.get("role") == "assistant")
    assert "Got it." in ai_lines(events)
    # After the filler is said, the screen goes back to "Thinking…" until the reply starts.
    after_filler = [e.get("state") for e in events[types(events).index("filler"):] if e["type"] == "state"]
    assert after_filler[:2] == ["speaking", "thinking"], after_filler


def test_fast_reply_has_no_filler():
    events = asyncio.run(_one_turn({}, filler_delay=1.0))
    assert "filler" not in types(events)


def test_rate_limited():
    events = asyncio.run(_one_turn({"status": 429}))
    assert "rate limited" in ai_lines(events)[-1]


def test_error_inside_stream():
    # What OpenClaw sends when its upstream model provider rate-limits it.
    events = asyncio.run(_one_turn({"stream_error": "custom-openrouter-ai is asking us to slow down."}))
    assert "rate limited" in ai_lines(events)[-1]


def test_bad_api_key():
    events = asyncio.run(_one_turn({"status": 401}))
    assert "API key" in ai_lines(events)[-1]


def test_llm_hangs():
    events = asyncio.run(_one_turn({"delay": 30}, filler_delay=0, llm_timeout=1.5))
    assert "taking too long" in ai_lines(events)[-1]


def test_llm_not_running():
    events = asyncio.run(_one_turn({}, llm_url="http://127.0.0.1:9/v1/chat/completions"))
    assert "can't reach" in ai_lines(events)[-1]


async def _speech_while_ringing_is_ignored():
    base, phone, stt, llm_requests, runners = await start_servers(ai_answer_delay=2.5)
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as http:
            async with http.post(f"{base}/api/login", json={"password": PASSWORD}) as r:
                assert r.status == 200
            # Talk at 0.5-1.2 s, while it's still ringing.
            pc, channel, events, heard = await dial(http, base, [(0.5, 1.2)])
            assert await wait_for(lambda: "answered" in types(events)), events
            await asyncio.sleep(3)
            assert stt.calls == 0 and not llm_requests, (stt.calls, llm_requests)
            await pc.close()
    finally:
        for r in runners:
            await r.cleanup()


def test_speech_while_ringing_is_ignored():
    asyncio.run(_speech_while_ringing_is_ignored())


async def _talking_while_ai_thinks():
    """Speaking again while the AI is still thinking replaces the request; quiet speech counts too."""
    behavior = {"delay": 6}
    base, phone, stt, llm_requests, runners = await start_servers(
        behavior, vad_backend="silero", filler_delay=1.0, llm_timeout=20)
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as http:
            async with http.post(f"{base}/api/login", json={"password": PASSWORD}) as r:
                assert r.status == 200
            speech = load_speech()[:48000]                 # ~1 s
            quiet = (speech * 0.2).astype(np.int16)        # well below VAD_MIN_RMS
            pc, channel, events, heard = await dial(http, base, [], clips=[(2.0, speech), (5.5, quiet)])
            assert await wait_for(lambda: sum(e.get("role") == "user" for e in events) == 2, 12), events
            interrupted = [e for e in events if e["type"] == "interrupted"]
            assert interrupted and interrupted[0]["spoken"] is False, events
            assert await wait_for(lambda: len(llm_requests) == 2 and behavior["inflight"] == 0, 15)
            assert behavior.get("abandoned") == 1, "the first request's connection wasn't closed"
            assert behavior["max_inflight"] == 1, "two requests overlapped at the LLM"
            await pc.close()
    finally:
        for r in runners:
            await r.cleanup()


def test_talking_while_ai_thinks():
    asyncio.run(_talking_while_ai_thinks())


async def _speakable(chunks):
    async def deltas():
        for c in chunks:
            yield c
    return "".join([s async for s in speakable(deltas())])


def test_speakable_drops_code_json_and_thinking():
    chunks = ["<thi", "nk>Let me reason about this.</th", "ink>Sure! Here is a call: {\"name\": ",
              "\"memory_get\", \"args\": {\"path\": \"/x\"}} Done. ```py\nprint(1)\n``` **Bye** #now"]
    assert " ".join(asyncio.run(_speakable(chunks)).split()) == "Sure! Here is a call: Done. Bye now"


def test_speakable_keeps_normal_text():
    assert asyncio.run(_speakable(["I'm your ", "assistant. 5 < 6, ok?"])) == "I'm your assistant. 5 < 6, ok?"


def test_reply_that_is_only_a_tool_call():
    events = asyncio.run(_one_turn({"reply": '{"name": "dispatch_action", "parameters": {"id": "memory_get"}}'}))
    assert "say that again" in ai_lines(events)[-1]
    assert not any("dispatch_action" in line for line in ai_lines(events))


def test_sentence_splitting():
    assert asyncio.run(_sentences()) == ["Hello there.", "How are you?", "I'm fine"]
