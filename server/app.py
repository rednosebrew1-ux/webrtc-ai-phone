"""HTTP server: web UI, login, WebRTC signaling, and the ring API."""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import uuid

from aiohttp import web
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription

from .call import CallSession
from .config import Settings

log = logging.getLogger(__name__)

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
COOKIE = "aiphone_session"


class Ring:
    """A call the AI placed to the user."""

    def __init__(self, reason):
        self.id = uuid.uuid4().hex[:12]
        self.reason = reason
        self.status = "ringing"       # ringing -> answered -> completed | declined | missed
        self.created = time.time()
        self.transcript = []
        self.done = asyncio.Event()

    def finish(self, status):
        self.status = status
        self.done.set()

    def to_json(self):
        return {"id": self.id, "reason": self.reason, "status": self.status, "transcript": self.transcript}


class PhoneApp:
    def __init__(self, cfg: Settings, stt, tts):
        self.cfg, self.stt, self.tts = cfg, stt, tts
        self.rings = {}
        self.calls = set()
        self.listeners = set()   # SSE queues, one per open browser tab
        self._cookie = hmac.new(cfg.session_secret.encode(), b"v1", hashlib.sha256).hexdigest()

    # ---- auth ----

    def _logged_in(self, request):
        if not self.cfg.app_password:
            return True
        return hmac.compare_digest(request.cookies.get(COOKIE, ""), self._cookie)

    def _api_allowed(self, request):
        """Ring API: the API token, or a browser that actually logged in with the password."""
        auth = request.headers.get("Authorization", "")
        if self.cfg.api_token and hmac.compare_digest(auth, f"Bearer {self.cfg.api_token}"):
            return True
        if self.cfg.app_password:
            return self._logged_in(request)
        # No password set: only allow the API when no token is configured either (local-only setups).
        return not self.cfg.api_token

    def _require_login(self, request):
        if not self._logged_in(request):
            raise web.HTTPUnauthorized(text="login required")

    async def login(self, request):
        data = await request.json()
        if not self.cfg.app_password or hmac.compare_digest(str(data.get("password", "")), self.cfg.app_password):
            resp = web.json_response({"ok": True})
            resp.set_cookie(COOKIE, self._cookie, httponly=True, samesite="Strict",
                            secure=request.secure, max_age=60 * 60 * 24 * 90)
            return resp
        await asyncio.sleep(1)  # slow down guessing
        raise web.HTTPUnauthorized(text="wrong password")

    async def config(self, request):
        return web.json_response({
            "loggedIn": self._logged_in(request),
            "needsPassword": bool(self.cfg.app_password),
            "aiName": self.cfg.ai_name,
            "iceServers": self.cfg.ice_server_list() if self._logged_in(request) else [],
        })

    # ---- browser event stream (for incoming rings) ----

    def broadcast(self, **event):
        for q in list(self.listeners):
            q.put_nowait(event)

    async def events(self, request):
        self._require_login(request)
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                           "X-Accel-Buffering": "no"})
        await resp.prepare(request)
        q = asyncio.Queue()
        self.listeners.add(q)
        # A tab opened mid-ring should ring too.
        for ring in self.rings.values():
            if ring.status == "ringing":
                q.put_nowait({"type": "ring", "id": ring.id, "reason": ring.reason})
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    await resp.write(b": keepalive\n\n")
                    continue
                if event is None:  # server shutting down
                    break
                await resp.write(f"data: {json.dumps(event)}\n\n".encode())
        except ConnectionResetError:
            pass
        finally:
            self.listeners.discard(q)
        return resp

    # ---- ring API (the AI calls the user) ----

    async def create_ring(self, request):
        if not self._api_allowed(request):
            raise web.HTTPUnauthorized(text="bad or missing API token")
        data = await request.json() if request.can_read_body else {}
        reason = str(data.get("reason") or "Just checking in.")[:1000]
        ring = Ring(reason)
        self.rings[ring.id] = ring
        log.info("Ringing user (ring %s): %s", ring.id, reason)
        self.broadcast(type="ring", id=ring.id, reason=reason)
        asyncio.ensure_future(self._ring_timeout(ring))
        return web.json_response({**ring.to_json(), "listeners": len(self.listeners)})

    async def _ring_timeout(self, ring):
        await asyncio.sleep(self.cfg.ring_timeout)
        if ring.status == "ringing":
            ring.finish("missed")
            self.broadcast(type="ring_end", id=ring.id, status="missed")
        await asyncio.sleep(3600)  # keep results around for an hour
        self.rings.pop(ring.id, None)

    async def get_ring(self, request):
        if not self._api_allowed(request):
            raise web.HTTPUnauthorized(text="bad or missing API token")
        ring = self.rings.get(request.match_info["id"])
        if not ring:
            raise web.HTTPNotFound(text="unknown ring id")
        if request.query.get("wait"):
            timeout = min(float(request.query.get("timeout", 600)), 3600)
            try:
                await asyncio.wait_for(ring.done.wait(), timeout)
            except asyncio.TimeoutError:
                pass
        return web.json_response(ring.to_json())

    async def decline_ring(self, request):
        self._require_login(request)
        ring = self.rings.get(request.match_info["id"])
        if ring and ring.status == "ringing":
            ring.finish("declined")
            self.broadcast(type="ring_end", id=ring.id, status="declined")
        return web.json_response({"ok": True})

    # ---- WebRTC signaling ----

    async def offer(self, request):
        self._require_login(request)
        data = await request.json()
        ring = None
        if data.get("ringId"):
            ring = self.rings.get(data["ringId"])
            if not ring or ring.status != "ringing":
                raise web.HTTPConflict(text="that call is no longer ringing")
            ring.status = "answered"
            self.broadcast(type="ring_end", id=ring.id, status="answered")

        ice = [RTCIceServer(urls=s["urls"], username=s.get("username"), credential=s.get("credential"))
               for s in self.cfg.ice_server_list()]
        pc = RTCPeerConnection(RTCConfiguration(iceServers=ice))
        call_id = uuid.uuid4().hex[:8]

        def on_end(session):
            self.calls.discard(session)
            if ring:
                ring.transcript = session.transcript
                ring.finish("completed")

        session = CallSession(self.cfg, self.stt, self.tts, pc, call_id,
                              reason=ring.reason if ring else None, on_end=on_end)
        self.calls.add(session)

        @pc.on("datachannel")
        def on_datachannel(channel):
            session.attach_channel(channel)

        @pc.on("track")
        def on_track(track):
            if track.kind == "audio":
                session.attach_track(track)

        @pc.on("connectionstatechange")
        async def on_state():
            log.info("[%s] connection %s", call_id, pc.connectionState)
            if pc.connectionState in ("failed", "closed"):
                await session.end("network")

        await pc.setRemoteDescription(RTCSessionDescription(sdp=data["sdp"], type=data["type"]))
        pc.addTrack(session.speaker)
        await pc.setLocalDescription(await pc.createAnswer())
        asyncio.ensure_future(session.run())
        log.info("[%s] call started (%s)", call_id, "AI rang user" if ring else "user rang AI")
        return web.json_response({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type,
                                  "callId": call_id})

    # ---- app ----

    async def index(self, request):
        return web.FileResponse(os.path.join(WEB_DIR, "index.html"))

    async def health(self, request):
        return web.json_response({"ok": True, "calls": len(self.calls), "listeners": len(self.listeners)})

    async def on_shutdown(self, app):
        # Close open event streams so Ctrl+C doesn't wait for browser tabs to disconnect.
        for q in list(self.listeners):
            q.put_nowait(None)
        for session in list(self.calls):
            await session.end("server")

    def build(self):
        app = web.Application()
        app.add_routes([
            web.get("/", self.index),
            web.get("/healthz", self.health),
            web.get("/api/config", self.config),
            web.post("/api/login", self.login),
            web.get("/api/events", self.events),
            web.post("/api/offer", self.offer),
            web.post("/api/ring", self.create_ring),
            web.get("/api/ring/{id}", self.get_ring),
            web.post("/api/ring/{id}/decline", self.decline_ring),
            web.static("/static", WEB_DIR),
        ])
        app.on_shutdown.append(self.on_shutdown)
        return app
