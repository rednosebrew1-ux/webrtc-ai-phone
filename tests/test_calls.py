"""End-to-end call tests: a Python WebRTC client talks to the real server, with fake STT/TTS/LLM.

Run: python -m pytest tests
"""

import asyncio
import fractions
import json
import time

import aiohttp
import av
import numpy as np
from aiohttp import web
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription

from server.app import PhoneApp
from server.config import Settings
from server.llm import stream_sentences
from tests.fakes import FakeSTT, FakeTTS, fake_llm_app

TOKEN = "test-token"
PASSWORD = "pw"


class VoiceTrack(MediaStreamTrack):
    """Microphone stand-in: talks (loud tone) during given time windows, silent otherwise."""

    kind = "audio"

    def __init__(self, talk_windows):
        super().__init__()
        self.windows = talk_windows
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
        frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = 48000
        frame.pts = self.pts
        frame.time_base = fractions.Fraction(1, 48000)
        return frame


async def start_servers():
    llm_requests = []
    llm_runner = web.AppRunner(fake_llm_app(llm_requests))
    await llm_runner.setup()
    llm_site = web.TCPSite(llm_runner, "127.0.0.1", 0)
    await llm_site.start()
    llm_port = llm_site._server.sockets[0].getsockname()[1]

    cfg = Settings()
    cfg.app_password, cfg.api_token = PASSWORD, TOKEN
    cfg.llm_url = f"http://127.0.0.1:{llm_port}/v1/chat/completions"
    cfg.ice_servers = ""
    cfg.ai_answer_delay = 0.5
    cfg.ring_timeout = 5
    stt = FakeSTT()
    phone = PhoneApp(cfg, stt, FakeTTS())
    runner = web.AppRunner(phone.build())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return f"http://127.0.0.1:{port}", phone, stt, llm_requests, [runner, llm_runner]


async def dial(http, base, talk_windows, ring_id=None):
    """Place a call from a fake browser. Returns (pc, events list, received-audio counter)."""
    pc = RTCPeerConnection()
    events = []
    channel = pc.createDataChannel("events")
    channel.on("message", lambda m: events.append(json.loads(m)))
    pc.addTrack(VoiceTrack(talk_windows))
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


async def _sentences():
    async def deltas():
        for d in ["Hel", "lo there. How", " are you? I'm", " fine"]:
            yield d
    return [s async for s in stream_sentences(deltas())]


def test_user_calls_ai():
    asyncio.run(_user_calls_ai())


def test_ai_calls_user():
    asyncio.run(_ai_calls_user())


def test_sentence_splitting():
    assert asyncio.run(_sentences()) == ["Hello there.", "How are you?", "I'm fine"]
