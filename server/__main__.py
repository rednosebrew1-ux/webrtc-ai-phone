"""Run with: python -m server"""

import logging
import os

from aiohttp import web

from .app import PhoneApp
from .config import settings
from .speech import make_stt, make_tts


def use_certifi_if_needed():
    """python.org builds of Python on macOS ship without CA certificates; fall back to certifi's bundle."""
    if os.environ.get("SSL_CERT_FILE"):
        return
    try:
        import certifi
    except ImportError:
        return
    os.environ["SSL_CERT_FILE"] = certifi.where()


def main():
    use_certifi_if_needed()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("server")
    # Connection setup chatter (ICE candidate checks) drowns out the useful lines; LOG_LEVEL=DEBUG shows it.
    if logging.getLogger().level > logging.DEBUG:
        for noisy in ("aioice", "aiortc", "aiohttp.access"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    os.makedirs(settings.data_dir, exist_ok=True)
    if not settings.app_password:
        log.warning("APP_PASSWORD is not set: anyone who can reach this server can talk to your AI.")
    if not settings.api_token:
        log.warning("API_TOKEN is not set: the ring API only works from a logged-in browser.")

    log.info("AI brain: %s (model %s, %s)", settings.llm_url, settings.llm_model,
             "with API key" if settings.llm_api_key else "no API key")
    stt, tts = make_stt(settings), make_tts(settings)
    app = PhoneApp(settings, stt, tts).build()
    log.info("Open http://%s:%s", "localhost" if settings.host == "0.0.0.0" else settings.host, settings.port)
    web.run_app(app, host=settings.host, port=settings.port, print=None)


if __name__ == "__main__":
    main()
