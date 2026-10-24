---
name: ring-me
description: Phone the user through their self-hosted AI Phone web app, have a live voice conversation, and get the transcript back. Use when the user asks to be called, or when something is urgent or easier to discuss by voice and the user has said calls are OK.
---

# Ring me

Rings the user's browser (or phone with the page open). If they pick up, a voice
assistant talks with them about the reason you give, and you get the transcript
back when the call ends.

## Setup (once)

The user must set two environment variables for the agent:

- `AI_PHONE_URL`: where the app runs, e.g. `http://localhost:8080`
- `AI_PHONE_TOKEN`: the app's `API_TOKEN`

## How to call

```bash
{baseDir}/scripts/ring.sh "Short reason for the call, written as context for the voice assistant"
```

The script rings, waits for the call to finish (up to 10 minutes), then prints JSON:

- `status`: `completed`, `declined`, or `missed`
- `transcript`: list of `{role, text}` turns (`assistant` = the voice assistant, `user` = the person)

## Rules

- Only call when the user asked to be called, or has said it's OK to call for this kind of thing.
- Put everything the voice assistant needs in the reason: what happened, what to ask, what decision is needed.
  It cannot see this chat.
- If the status is `missed` or `declined`, don't immediately call again. Send a text message instead if you can.
- After a completed call, act on what the user said in the transcript and confirm in chat what you did.
