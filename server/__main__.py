"""Run with: python -m server"""

import logging
import os

from aiohttp import web

from .app import PhoneApp
from .config import settings
from .speech import make_stt, make_tts


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("server")
    os.makedirs(settings.data_dir, exist_ok=True)
    if not settings.app_password:
        log.warning("APP_PASSWORD is not set: anyone who can reach this server can talk to your AI.")
    if not settings.api_token:
        log.warning("API_TOKEN is not set: the ring API only works from a logged-in browser.")

    stt, tts = make_stt(settings), make_tts(settings)
    app = PhoneApp(settings, stt, tts).build()
    log.info("Open http://%s:%s", "localhost" if settings.host == "0.0.0.0" else settings.host, settings.port)
    web.run_app(app, host=settings.host, port=settings.port, print=None)


if __name__ == "__main__":
    main()
