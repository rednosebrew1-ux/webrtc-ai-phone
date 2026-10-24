# AI Phone

**Call your AI, and let your AI call you, right in the browser.** Self-hosted voice calls over
WebRTC with a real ring, an answer, a live transcript and a hang-up, for any OpenAI-compatible model
(Ollama, OpenClaw, OpenAI, OpenRouter, …).

- 📞 **You ring the AI**: press call, hear it ring, and it picks up and talks.
- 🔔 **The AI rings you**: one API call makes your browser ring with the reason on screen. Answer and it
  explains why it called. You get the transcript back.
- 🗣️ **Natural turn-taking**: it waits for you to finish, and you can talk over it to interrupt.
- 🏠 **Runs fully local and free** with Ollama + faster-whisper + Piper, or plug in hosted APIs.
- 🔒 **No phone numbers, carriers or per-minute fees.** Audio goes straight from the browser to your server.

```
Browser (mic/speaker) ⇄ WebRTC ⇄ server: speech-to-text → LLM → text-to-speech
```

## Quick start

### Docker (Linux server or VPS)

```bash
git clone https://github.com/rednosebrew1-ux/webrtc-ai-phone.git
cd webrtc-ai-phone
cp .env.example .env        # set APP_PASSWORD, API_TOKEN and your LLM
docker compose up -d
```

Open `http://localhost:8080`. The first start downloads the speech models (~200 MB) into a Docker volume.

> WebRTC needs UDP, so the container uses host networking. On **Docker Desktop (Mac/Windows)**, enable
> host networking in Settings → Resources → Network, or use the no-Docker install below.

### Without Docker (macOS, Linux, Windows)

Requires Python 3.10+.

```bash
git clone https://github.com/rednosebrew1-ux/webrtc-ai-phone.git
cd webrtc-ai-phone
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # edit it
python -m server
```

### A free, local brain

```bash
# https://ollama.com
ollama pull llama3.2
```

The default `.env.example` already points at Ollama. Smaller models answer faster on a voice call.

### Try the UI without any models

```bash
python -m tests.dev_server   # fake AI that beeps instead of talking
```

## Use it from your phone or another computer

Browsers only allow the microphone on **HTTPS** pages (or `localhost`). Two easy options:

- **Tailscale (private, free):** install Tailscale on the server and your phone, then
  `tailscale serve --bg 8080`. Open the `https://<machine>.<tailnet>.ts.net` address on your phone.
  Use `tailscale funnel` instead to make it public.
- **Your own domain:** put a reverse proxy like [Caddy](https://caddyserver.com) in front of it.
  `caddy reverse-proxy --from phone.example.com --to localhost:8080` handles certificates for you.

**Calls connect but there's no audio?** Strict networks (some mobile carriers, corporate Wi-Fi) need a
TURN relay. Run [coturn](https://github.com/coturn/coturn) or use a hosted TURN service, then set
`ICE_SERVERS`, `TURN_USERNAME` and `TURN_PASSWORD`.

## Let the AI call you

Keep the page open in a browser tab (or on your phone) and tap the page once so it's allowed to ring.
Then:

```bash
curl -X POST http://localhost:8080/api/ring \
  -H "Authorization: Bearer $API_TOKEN" -H "Content-Type: application/json" \
  -d '{"reason": "Your deploy failed. Ask whether to roll back."}'
# → {"id": "a1b2c3", "status": "ringing", "listeners": 1, ...}

# Wait for the call to finish and get the transcript:
curl "http://localhost:8080/api/ring/a1b2c3?wait=1" -H "Authorization: Bearer $API_TOKEN"
# → {"status": "completed", "transcript": [{"role": "assistant", "text": "..."}, ...]}
```

`status` is one of `ringing`, `answered`, `completed`, `declined` or `missed` (after `RING_TIMEOUT` seconds).
Your browser rings, vibrates on phones, and shows a notification if the tab is in the background.
When you answer, the AI opens the call by explaining the `reason`.

### OpenClaw skill

[`skills/openclaw/ring-me`](skills/openclaw/ring-me) lets an OpenClaw agent phone you. Copy it into your
OpenClaw skills folder and set `AI_PHONE_URL` and `AI_PHONE_TOKEN` for the agent.

### Using OpenClaw as the brain

Enable the OpenClaw Gateway's OpenAI-compatible chat completions endpoint, then set
`LLM_URL=http://127.0.0.1:18789/v1/chat/completions`, `LLM_MODEL=openclaw` and `LLM_API_KEY=<gateway token>`.
Check the OpenClaw docs for the exact setting name. Each call is sent with a stable `user` id so the
agent can keep context within the call.

## Configuration

Everything is set with environment variables or `.env`. See [`.env.example`](.env.example) for all options.

| Setting | Default | What it does |
|---|---|---|
| `APP_PASSWORD` | *(none)* | Password for the web page. **Set this** before exposing the server. |
| `API_TOKEN` | *(none)* | Bearer token for the ring API. |
| `LLM_URL` / `LLM_MODEL` / `LLM_API_KEY` | Ollama, `llama3.2` | Any OpenAI-compatible chat completions endpoint. |
| `SYSTEM_PROMPT` | friendly assistant | Personality and rules. Keep "short spoken sentences" in it. |
| `STT_BACKEND` | `faster-whisper` | `faster-whisper` (local) or `openai` (any compatible API). |
| `WHISPER_MODEL` | `base.en` | `tiny.en` is fastest, `small.en` is more accurate. |
| `TTS_BACKEND` | `piper` | `piper` (local) or `openai` (any compatible API). |
| `PIPER_VOICE` | `en_US-lessac-medium` | Any [Piper voice](https://huggingface.co/rhasspy/piper-voices). Downloaded automatically. |
| `AI_NAME` / `GREETING` | `Assistant` | Shown on screen / said when the AI answers. |
| `AI_ANSWER_DELAY` | `4` | Seconds your call rings before the AI picks up. |
| `RING_TIMEOUT` | `30` | Seconds the AI's call rings before it's missed. |
| `VAD_MIN_RMS` | `300` | Mic level counted as speech. Raise it if background noise triggers replies. |
| `SILENCE_MS` | `700` | Pause that ends your turn. |
| `BARGE_IN_MS` | `300` | How long you must talk over the AI to interrupt it. |
| `ICE_SERVERS` | Google STUN | Comma-separated STUN/TURN URLs. |

The AI can end a call itself: when it ends a reply with `<hangup/>` (the default prompt tells it how),
the call hangs up after it finishes speaking.

## How it works

- `web/`: plain HTML/JS, no build step. Ringtones are synthesized with the Web Audio API.
  Incoming rings arrive over Server-Sent Events, and call events (answered, transcript, hang-up) over a WebRTC data channel.
- `server/app.py`: aiohttp server for login, WebRTC signaling (via [aiortc](https://github.com/aiortc/aiortc))
  and the ring API.
- `server/call.py`: per-call loop. Energy-based voice detection, then speech-to-text, then the LLM streamed
  sentence-by-sentence, then text-to-speech, so the AI starts talking before the whole reply is generated.
- `server/speech.py`: pluggable STT/TTS backends.

## Development

```bash
pip install -r requirements.txt pytest
python -m pytest tests          # end-to-end WebRTC calls with fake STT/TTS/LLM
```

## Notes

- **Privacy:** with the local backends, audio never leaves your machine. Hosted backends send audio or text
  to those providers.
- **Licenses:** this project is MIT. Piper (`piper-tts`) is GPL-3.0, so a distributed build that includes it
  (such as the Docker image) is also covered by the GPL. Use `TTS_BACKEND=openai` with a different engine
  if that matters to you.
- **AI disclosure:** if you share the page with other people, make it clear they're talking to an AI.
