"""Settings, read once from environment variables. See .env.example for docs."""

import os
import secrets
from dataclasses import dataclass, field


def load_dotenv(path=".env"):
    """Load KEY=VALUE lines from .env without overriding real environment variables.

    If a key appears more than once, the last line wins (like other dotenv tools).
    """
    if not os.path.exists(path):
        return
    values = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    for key, value in values.items():
        os.environ.setdefault(key, value)


def _env(name, default=""):
    return os.environ.get(name, default)


def _float(name, default):
    return float(os.environ.get(name, default))


@dataclass
class Settings:
    host: str = field(default_factory=lambda: _env("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(_env("PORT", "8080")))
    data_dir: str = field(default_factory=lambda: _env("DATA_DIR", "./data"))

    # Auth: password for the web page, token for the ring API.
    app_password: str = field(default_factory=lambda: _env("APP_PASSWORD"))
    api_token: str = field(default_factory=lambda: _env("API_TOKEN"))
    session_secret: str = field(default_factory=lambda: _env("SESSION_SECRET") or secrets.token_hex(32))

    # LLM: any OpenAI-compatible chat completions endpoint.
    llm_url: str = field(default_factory=lambda: _env("LLM_URL", "http://127.0.0.1:11434/v1/chat/completions"))
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "llama3.2"))
    system_prompt: str = field(default_factory=lambda: _env(
        "SYSTEM_PROMPT",
        "You are a friendly voice assistant talking with someone over a live call. "
        "Speak naturally in short sentences, one to three at a time. Never use lists, "
        "markdown, emojis, or URLs, since everything you say is read aloud. "
        "When the conversation is clearly over and you've said goodbye, end your "
        "reply with <hangup/>.",
    ))

    # Speech-to-text: "faster-whisper" (local) or "openai" (any compatible /v1/audio/transcriptions).
    stt_backend: str = field(default_factory=lambda: _env("STT_BACKEND", "faster-whisper"))
    whisper_model: str = field(default_factory=lambda: _env("WHISPER_MODEL", "base.en"))
    whisper_device: str = field(default_factory=lambda: _env("WHISPER_DEVICE", "cpu"))
    stt_url: str = field(default_factory=lambda: _env("STT_URL", "https://api.openai.com/v1/audio/transcriptions"))
    stt_api_key: str = field(default_factory=lambda: _env("STT_API_KEY"))
    stt_model: str = field(default_factory=lambda: _env("STT_MODEL", "whisper-1"))

    # Text-to-speech: "piper" (local) or "openai" (any compatible /v1/audio/speech).
    tts_backend: str = field(default_factory=lambda: _env("TTS_BACKEND", "piper"))
    piper_voice: str = field(default_factory=lambda: _env("PIPER_VOICE", "en_US-lessac-medium"))
    tts_url: str = field(default_factory=lambda: _env("TTS_URL", "https://api.openai.com/v1/audio/speech"))
    tts_api_key: str = field(default_factory=lambda: _env("TTS_API_KEY"))
    tts_model: str = field(default_factory=lambda: _env("TTS_MODEL", "gpt-4o-mini-tts"))
    tts_voice: str = field(default_factory=lambda: _env("TTS_VOICE", "alloy"))

    # Calls
    ai_name: str = field(default_factory=lambda: _env("AI_NAME", "Assistant"))
    greeting: str = field(default_factory=lambda: _env("GREETING", "Hey, it's your assistant. What's up?"))
    ai_answer_delay: float = field(default_factory=lambda: _float("AI_ANSWER_DELAY", "4"))
    ring_timeout: float = field(default_factory=lambda: _float("RING_TIMEOUT", "30"))

    # Voice activity detection
    vad_min_rms: float = field(default_factory=lambda: _float("VAD_MIN_RMS", "300"))
    vad_ratio: float = field(default_factory=lambda: _float("VAD_RATIO", "3"))
    silence_ms: float = field(default_factory=lambda: _float("SILENCE_MS", "700"))
    barge_in_ms: float = field(default_factory=lambda: _float("BARGE_IN_MS", "300"))

    # WebRTC: comma-separated STUN/TURN URLs, plus optional TURN credentials.
    ice_servers: str = field(default_factory=lambda: _env("ICE_SERVERS", "stun:stun.l.google.com:19302"))
    turn_username: str = field(default_factory=lambda: _env("TURN_USERNAME"))
    turn_password: str = field(default_factory=lambda: _env("TURN_PASSWORD"))

    def ice_server_list(self):
        """ICE servers as dicts in the browser's RTCIceServer shape."""
        out = []
        for url in filter(None, (u.strip() for u in self.ice_servers.split(","))):
            entry = {"urls": url}
            if url.startswith(("turn:", "turns:")) and self.turn_username:
                entry["username"] = self.turn_username
                entry["credential"] = self.turn_password
            out.append(entry)
        return out


load_dotenv()
settings = Settings()
