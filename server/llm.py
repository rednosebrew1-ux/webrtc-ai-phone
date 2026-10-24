"""Streaming client for any OpenAI-compatible chat completions endpoint."""

import json
import re

import aiohttp

# Split after sentence-ending punctuation followed by whitespace.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


async def stream_chat(cfg, messages, user=None):
    """Yield text deltas from the model."""
    body = {"model": cfg.llm_model, "messages": messages, "stream": True}
    if user:
        body["user"] = user
    headers = {"Authorization": f"Bearer {cfg.llm_api_key}"} if cfg.llm_api_key else {}
    timeout = aiohttp.ClientTimeout(total=120, sock_read=60)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post(cfg.llm_url, json=body, headers=headers) as r:
            if r.status >= 400:
                raise RuntimeError(f"LLM returned {r.status}: {(await r.text())[:300]}")
            if "text/event-stream" not in r.headers.get("Content-Type", ""):
                # Server ignored stream=true and sent one JSON response.
                data = await r.json()
                yield data["choices"][0]["message"]["content"] or ""
                return
            async for raw in r.content:
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    return
                try:
                    delta = json.loads(payload)["choices"][0].get("delta", {}).get("content")
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
                if delta:
                    yield delta


async def stream_sentences(deltas):
    """Group a stream of text deltas into speakable sentences."""
    buf = ""
    async for delta in deltas:
        buf += delta
        parts = _SENTENCE_END.split(buf)
        for sentence in parts[:-1]:
            if sentence.strip():
                yield sentence.strip()
        buf = parts[-1]
    if buf.strip():
        yield buf.strip()
