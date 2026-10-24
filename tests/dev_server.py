"""Run the app with fake STT/TTS/LLM, to try the UI without downloading any models.

    python -m tests.dev_server        # then open http://localhost:8080

The fake AI answers with beeps instead of a voice.
"""

import asyncio
import logging
import os

from aiohttp import web

from server.app import PhoneApp
from server.config import Settings
from tests.fakes import FakeSTT, FakeTTS, fake_llm_app


async def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s: %(message)s")
    llm = web.AppRunner(fake_llm_app([]))
    await llm.setup()
    await web.TCPSite(llm, "127.0.0.1", 8099).start()

    cfg = Settings()
    cfg.llm_url = "http://127.0.0.1:8099/v1/chat/completions"
    cfg.ai_answer_delay = float(os.environ.get("AI_ANSWER_DELAY", "2"))
    app = web.AppRunner(PhoneApp(cfg, FakeSTT(), FakeTTS()).build())
    await app.setup()
    await web.TCPSite(app, cfg.host, cfg.port).start()
    print(f"Dev server on http://localhost:{cfg.port} (fake AI). Ctrl+C to stop.")
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
