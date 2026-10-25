"""Stand-ins for STT, TTS and the LLM, so the call flow can be tested without models."""

import asyncio
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


def fake_llm_app(requests, behavior=None):
    """An OpenAI-compatible streaming endpoint. Appends each request body to `requests`.

    `behavior` (a dict you can change between calls) simulates trouble:
      delay: seconds to wait before answering
      status: HTTP status to fail with (e.g. 429)
      stream_error: error message sent inside the stream (as OpenClaw does for upstream failures)
    """
    behavior = behavior if behavior is not None else {}

    async def chat(request):
        body = await request.json()
        requests.append(body)
        # Track requests in flight at once (a real agent may serialize or reject overlapping ones).
        behavior["inflight"] = behavior.get("inflight", 0) + 1
        behavior["max_inflight"] = max(behavior.get("max_inflight", 0), behavior["inflight"])
        try:
            # Wait out the delay, but stop as soon as the client hangs up on us.
            end = asyncio.get_running_loop().time() + behavior.get("delay", 0)
            while asyncio.get_running_loop().time() < end:
                if request.transport is None or request.transport.is_closing():
                    behavior["abandoned"] = behavior.get("abandoned", 0) + 1
                    return web.Response(status=499)
                await asyncio.sleep(0.02)
        finally:
            behavior["inflight"] -= 1
        if behavior.get("status"):
            return web.json_response({"error": {"message": "nope"}}, status=behavior["status"])
        last = body["messages"][-1]["content"]
        if "picked up" in last:
            reply = "Hi, it's your assistant. I'm calling because the build finished. Talk soon! <hangup/>"
        else:
            reply = "Got it. Anything else?"
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        if behavior.get("stream_error"):
            await resp.write(f"data: {json.dumps({'error': {'message': behavior['stream_error']}})}\n\n".encode())
            return resp
        for word in reply.split(" "):
            chunk = {"choices": [{"delta": {"content": word + " "}}]}
            await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())
        await resp.write(b"data: [DONE]\n\n")
        return resp

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    return app
