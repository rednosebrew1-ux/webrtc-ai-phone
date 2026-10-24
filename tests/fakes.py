"""Stand-ins for STT, TTS and the LLM, so the call flow can be tested without models."""

import json

import numpy as np
from aiohttp import web


class FakeSTT:
    def __init__(self, text="hello there"):
        self.text = text
        self.calls = 0

    async def transcribe(self, pcm16k):
        self.calls += 1
        return self.text if pcm16k else ""


class FakeTTS:
    """Speaks a 440 Hz tone, 60 ms per character (capped at max_seconds)."""

    def __init__(self, max_seconds=1.5):
        self.max_seconds = max_seconds

    async def synthesize(self, text):
        rate = 16000
        seconds = min(self.max_seconds, 0.06 * len(text))
        t = np.arange(int(rate * seconds)) / rate
        return (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16).tobytes(), rate


def fake_llm_app(requests):
    """An OpenAI-compatible streaming endpoint. Appends each request body to `requests`."""

    async def chat(request):
        body = await request.json()
        requests.append(body)
        last = body["messages"][-1]["content"]
        if "picked up" in last:
            reply = "Hi, it's your assistant. I'm calling because the build finished. Talk soon! <hangup/>"
        else:
            reply = "Got it. Anything else?"
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        for word in reply.split(" "):
            chunk = {"choices": [{"delta": {"content": word + " "}}]}
            await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())
        await resp.write(b"data: [DONE]\n\n")
        return resp

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    return app
