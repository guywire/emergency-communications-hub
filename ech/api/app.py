"""
ech/api/app.py
--------------
FastAPI application. Exposes:
  GET  /                        → serves the HTMX UI
  GET  /ws                      → WebSocket push channel
  GET  /api/messages            → paginated message log
  POST /api/messages            → send a message
  GET  /api/channels            → adapter health
  GET  /api/nodes/{adapter}     → mesh node list
  GET  /api/contacts            → contact list
  POST /api/contacts            → create/update contact
  DELETE /api/contacts/{id}     → delete contact
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, File, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ech.core.models import Priority
from ech import __version__ as ECH_VERSION

log = logging.getLogger(__name__)

# PSKReporter rate-limit: 1 upstream request per 5 minutes per IP.
_psk_cache: dict = {}          # key → (timestamp, payload)
_PSK_CACHE_TTL = 300           # seconds
_psk_stats: dict = {"last_success": None, "last_failure": None, "last_error": None, "total_fetches": 0}

UI_DIR = Path(__file__).parent.parent / "ui"


def create_app(router, db, anomaly_engine=None, wx_service=None, auth=None, ech_state=None, mc_bridge=None, gps_reader=None, secure_cookies: bool = False, cat_ctrl=None, ca_cert_pem: bytes | None = None, config_path: str | None = None) -> FastAPI:
    app = FastAPI(title="Emergency Communications Hub", version=ECH_VERSION)
    app.state.cat_ctrl = cat_ctrl        # CATController instance (may be None)
    app.state.ca_cert_pem = ca_cert_pem  # CA cert PEM for /ca.crt download (may be None)
    app.state.config_path = config_path  # Path to config.yaml (for live editing)

    # ── Role helpers ──────────────────────────────────────────────────────
    from fastapi import HTTPException
    from fastapi.responses import RedirectResponse as _Redir

    async def _admin_required(request: Request):
        """Raise 403 if the caller is not an admin. No-op when auth is disabled."""
        if not auth:
            return
        from ech.core.auth import SESSION_COOKIE
        token = request.cookies.get(SESSION_COOKIE, "")
        session = await auth.get_session(token)
        if not session or session.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")

    # ── Auth middleware ───────────────────────────────────────────────────
    # Public paths that don't require login
    PUBLIC_PATHS = {"/login", "/change-password", "/api/auth/login", "/api/auth/change-password",
                    "/api/health", "/ws", "/static", "/favicon.ico", "/metrics",
                    "/ca.crt", "/tls-setup"}

    # Paths/prefixes that require admin role (operators are blocked).
    # Covers the settings page, all adapter mutations, user management,
    # bridge rules, system commands, and message log wipe.
    ADMIN_PATHS = {"/settings", "/simulation"}
    ADMIN_PREFIXES = (
        "/api/adapters/",        # all adapter mutations (enable, channel, rename…)
        "/api/users",            # user CRUD
        "/api/config",           # bridge rules
        "/api/adapter-config",   # full config.yaml read/write (contains credentials)
        "/api/meshcore-bridge",  # MQTT bridge toggle
        "/api/system/",          # service restarts, reboot
        "/api/anomalies/clear",
        "/api/messages/clear",
    )

    @app.middleware("http")
    async def auth_middleware(request, call_next):
        path = request.url.path
        method = request.method
        # Allow public paths and static files
        if not auth:
            return await call_next(request)
        is_public = (
            path in PUBLIC_PATHS or
            path.startswith("/static/") or
            path.startswith("/api/auth/")
        )
        if is_public:
            return await call_next(request)
        # Check session cookie
        from ech.core.auth import SESSION_COOKIE
        from fastapi.responses import RedirectResponse, JSONResponse
        token = request.cookies.get(SESSION_COOKIE, "")
        session = await auth.get_session(token)
        if not session:
            # API requests get 401, page requests get redirect
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Not authenticated"}, status_code=401)
            return RedirectResponse(url=f"/login?next={path}", status_code=302)

        # Force password change if flagged (SEC-01)
        if session.get("must_change_pw") and path not in ("/change-password", "/api/auth/change-password"):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Password change required", "must_change_pw": True}, status_code=403)
            return RedirectResponse(url="/change-password", status_code=302)

        # Admin-only enforcement: page GETs redirect, API calls get 403
        is_admin_path = (
            path in ADMIN_PATHS or
            any(path.startswith(p) for p in ADMIN_PREFIXES)
        )
        # Read-only adapter GETs (health, node list, packet log) are allowed for operators
        is_readonly_adapter = (
            path.startswith("/api/adapters/") and method == "GET"
        )
        if is_admin_path and not is_readonly_adapter and session.get("role") != "admin":
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Admin access required"}, status_code=403)
            return RedirectResponse(url="/?access=denied", status_code=302)

        return await call_next(request)

    @app.middleware("http")
    async def security_headers_middleware(request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'self'")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        if secure_cookies:
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=63072000; includeSubDomains"
            )
        return response

    # ── Static files ──────────────────────────────────────────────────────
    static_dir = UI_DIR / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # ── WebSocket ─────────────────────────────────────────────────────────

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        # Authenticate before accepting — reject unauthenticated callers.
        # WebSocket handshake carries cookies, so the session cookie is available.
        if auth:
            from ech.core.auth import SESSION_COOKIE
            token = ws.cookies.get(SESSION_COOKIE, "")
            session = await auth.get_session(token)
            if not session:
                await ws.close(code=4401)
                return
        await ws.accept()
        router.add_ws_client(ws)
        log.info("WS client connected: %s", ws.client)
        try:
            while True:
                # Keep alive — client can send pings
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            router.remove_ws_client(ws)
            log.info("WS client disconnected: %s", ws.client)

    # ── UI ────────────────────────────────────────────────────────────────
    _NO_CACHE = {"Cache-Control": "no-store"}

    def _render_template(name: str) -> str:
        content = (UI_DIR / "templates" / name).read_text()
        if ech_state and ech_state.simulation_enabled:
            content = content.replace("</head>", "<script>window.ECH_SIM=true;</script></head>", 1)
        return content

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return HTMLResponse(content=_render_template("index.html"), headers=_NO_CACHE)

    @app.get("/hamlog", response_class=HTMLResponse)
    async def hamlog_page():
        return HTMLResponse(content=_render_template("hamlog.html"), headers=_NO_CACHE)

    # ── Version ───────────────────────────────────────────────────────────

    @app.get("/api/version")
    async def get_version():
        import socket
        return {
            "version": ECH_VERSION,
            "host": socket.gethostname(),
        }

    # ── Messages ──────────────────────────────────────────────────────────

    @app.get("/api/messages")
    async def get_messages(
        limit: int = Query(100, le=500),
        offset: int = 0,
        adapter: str | None = None,
        since: str | None = None,
        priority_min: int | None = None,
        from_id: str | None = None,
    ):
        msgs = await db.get_messages(
            limit=limit, offset=offset,
            adapter=adapter, since=since,
            priority_min=priority_min,
            from_id=from_id,
        )
        total = await db.message_count()
        return {"total": total, "messages": msgs}

    class SendRequest(BaseModel):
        body: str
        adapters: list[str] | None = None
        to_id: str | None = None
        priority: int = 0

    @app.post("/api/messages")
    async def send_message(request: Request):
        data = await request.json()
        body      = data.get("body", "")
        adapters  = data.get("adapters")
        to_id     = data.get("to_id")
        priority  = Priority(data.get("priority", 0))
        if not body or not body.strip():
            return {"error": "Message body is empty"}
        if len(body) > 900:
            return {"error": f"Message too long ({len(body)} chars, max 900)"}
        tracked = await router.send_tracked(
            body=body,
            adapter_names=adapters,
            to_id=to_id,
            priority=priority,
        )
        # Build per-adapter failure reasons from router adapter state
        failure_reasons: dict[str, str] = {}
        for name, v in tracked.items():
            if not v["ok"]:
                adapter = router._adapters.get(name)
                if adapter is None:
                    failure_reasons[name] = "not found"
                elif not adapter._connected:
                    failure_reasons[name] = "not connected"
                elif adapter._paused:
                    failure_reasons[name] = "paused"
                else:
                    failure_reasons[name] = "send failed"
        return {
            "results": {k: v["ok"] for k, v in tracked.items()},
            "msg_ids": {k: v["msg_id"] for k, v in tracked.items() if v["msg_id"]},
            "failure_reasons": failure_reasons,
        }

    # ── Messages clear ────────────────────────────────────────────────────

    @app.post("/api/messages/clear")
    async def clear_messages_endpoint():
        count = await db.clear_messages()
        # Notify all WS clients
        try:
            await router.broadcast_ws_event("messages_cleared", {"count": count})
        except Exception:
            pass
        return {"status": "ok", "cleared": count}

    # ── Adapters enable/disable ───────────────────────────────────────────

    @app.post("/api/adapters/{adapter_name}/enabled")
    async def set_adapter_enabled(adapter_name: str, request: Request):
        data = await request.json()
        enabled = bool(data.get("enabled", True))
        adapter = router._adapters.get(adapter_name)
        if not adapter:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail=f"Adapter '{adapter_name}' not found")
        if enabled:
            adapter.resume()
        else:
            adapter.pause()
        return {"status": "ok", "adapter": adapter_name, "enabled": enabled}

    # ── MeshCore channel switch ───────────────────────────────────────────

    @app.post("/api/adapters/{adapter_name}/channel")
    async def set_adapter_channel(adapter_name: str, request: Request):
        data = await request.json()
        channel = data.get("channel")  # name like "#testing", "LongFast", or index int
        adapter = router._adapters.get(adapter_name)
        if not adapter:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail=f"Adapter '{adapter_name}' not found")
        # Meshtastic TX channel change
        if hasattr(adapter, 'set_tx_channel'):
            if isinstance(channel, int):
                adapter.set_tx_channel(channel)
                return {"status": "ok", "channel_idx": channel}
            elif isinstance(channel, str):
                # Try matching by name in the device channel list
                want = channel.lower()
                for c in getattr(adapter, '_channel_list', []):
                    ch_name = (c.get("name") or "").lower()
                    if ch_name == want or (c.get("index") == 0 and want in ("longfast", "long fast", "primary", "")):
                        adapter.set_tx_channel(c["index"])
                        return {"status": "ok", "channel_idx": c["index"], "channel_name": c.get("name") or "primary"}
                # Try as integer string
                try:
                    idx = int(channel)
                    adapter.set_tx_channel(idx)
                    return {"status": "ok", "channel_idx": idx}
                except ValueError:
                    pass
                return {"status": "error", "detail": f"Channel '{channel}' not found on device"}
        # MeshCoreAdapter channel switching
        if not hasattr(adapter, '_channels'):
            return {"status": "error", "detail": "Adapter does not support channel switching"}
        if isinstance(channel, int):
            adapter._channel_idx = channel
            adapter._channel_name_resolved = True
            ch_name = adapter._channels.get(channel, "")
            return {"status": "ok", "channel_idx": channel, "channel_name": ch_name}
        elif isinstance(channel, str):
            want = channel.lstrip('#').lower()
            # Try as integer index first (user typed "0", "1", etc.)
            try:
                idx = int(want)
                if 0 <= idx <= 7:
                    adapter._channel_idx = idx
                    adapter._channel_name_resolved = True
                    ch_name = adapter._channels.get(idx, "")
                    log.info("Channel switch: %s → ch%d (%s)", adapter_name, idx, ch_name)
                    return {"status": "ok", "channel_idx": idx, "channel_name": ch_name}
            except ValueError:
                pass
            # Try by name
            for idx, name in adapter._channels.items():
                if name.lower() == want or name.lower() == channel.lower():
                    adapter._channel_idx = idx
                    adapter._channel_name_resolved = True
                    log.info("Channel switch: %s → ch%d (%s)", adapter_name, idx, name)
                    return {"status": "ok", "channel_idx": idx, "channel_name": name}
            known = list(adapter._channels.values()) or ["(none yet — init in progress)"]
            return {"status": "error", "detail": f"Channel '{channel}' not found. Known names: {known}. Or enter index 0–7."}
        return {"status": "error", "detail": "Provide channel name or index"}

    # ── Meshtastic-specific endpoints ─────────────────────────────────────

    @app.post("/api/adapters/{adapter_name}/meshtastic/channel")
    async def configure_meshtastic_channel(adapter_name: str, request: Request):
        """Configure a channel slot on the connected Meshtastic device."""
        data = await request.json()
        adapter = router._adapters.get(adapter_name)
        if not adapter:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail=f"Adapter '{adapter_name}' not found")
        if not hasattr(adapter, 'configure_channel'):
            return {"status": "error", "detail": "Not a Meshtastic adapter"}
        try:
            idx = int(data.get("idx", 0))
        except (ValueError, TypeError):
            return {"status": "error", "detail": "idx must be an integer 0–7"}
        if not 0 <= idx <= 7:
            return {"status": "error", "detail": "idx must be 0–7"}
        name       = data.get("name", "").strip()
        psk_b64    = data.get("psk_b64") or None
        preset     = data.get("modem_preset") or None
        return await adapter.configure_channel(idx, name, psk_b64, preset)

    @app.post("/api/adapters/{adapter_name}/meshtastic/lora")
    async def set_meshtastic_lora(adapter_name: str, request: Request):
        """Set LoRa region and/or modem preset on the device."""
        data = await request.json()
        adapter = router._adapters.get(adapter_name)
        if not adapter:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail=f"Adapter '{adapter_name}' not found")
        if not hasattr(adapter, 'set_lora_config'):
            return {"status": "error", "detail": "Not a Meshtastic adapter"}
        return await adapter.set_lora_config(
            region=data.get("region"),
            modem_preset=data.get("modem_preset"),
            channel_num=data.get("channel_num"),
        )

    @app.post("/api/adapters/{adapter_name}/meshtastic/gps")
    async def set_meshtastic_gps(adapter_name: str, request: Request):
        """Configure GPS mode on the Meshtastic device."""
        data = await request.json()
        adapter = router._adapters.get(adapter_name)
        if not adapter:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail=f"Adapter '{adapter_name}' not found")
        if not hasattr(adapter, 'configure_gps'):
            return {"status": "error", "detail": "Not a Meshtastic adapter"}
        return await adapter.configure_gps(
            gps_mode=data.get("gps_mode", "enabled"),
            broadcast_secs=data.get("broadcast_secs"),
        )

    @app.post("/api/adapters/{adapter_name}/meshtastic/monitored")
    async def set_meshtastic_monitored(adapter_name: str, request: Request):
        """Set which channel indices to receive from. Empty list = all."""
        data = await request.json()
        adapter = router._adapters.get(adapter_name)
        if not adapter:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail=f"Adapter '{adapter_name}' not found")
        if not hasattr(adapter, 'set_monitored_channels'):
            return {"status": "error", "detail": "Not a Meshtastic adapter"}
        indices = data.get("indices")  # list[int] or null
        adapter.set_monitored_channels(indices or None)
        return {"status": "ok", "monitored_channels": adapter._monitored_channels}

    @app.post("/api/adapters/{adapter_name}/rename")
    async def rename_adapter_device(adapter_name: str, request: Request):
        data = await request.json()
        new_name = data.get("name", "").strip()
        if not new_name:
            return {"status": "error", "detail": "name required"}
        adapter = router._adapters.get(adapter_name)
        if not adapter:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail=f"Adapter '{adapter_name}' not found")
        if not hasattr(adapter, "rename_device"):
            return {"status": "error", "detail": "Adapter does not support rename"}
        import inspect
        sig = inspect.signature(adapter.rename_device)
        if "short_name" in sig.parameters:
            ok = await adapter.rename_device(new_name, data.get("short_name") or None)
        else:
            ok = await adapter.rename_device(new_name)
        return {"status": "ok" if ok else "error", "name": new_name}

    @app.post("/api/adapters/{adapter_name}/add_channel")
    async def add_meshcore_channel(adapter_name: str, request: Request):
        data = await request.json()
        try:
            idx = int(data.get("idx", 1))
        except (ValueError, TypeError):
            return {"status": "error", "detail": "idx must be an integer (0-15)"}
        if not 0 <= idx <= 15:
            return {"status": "error", "detail": "idx must be 0–15"}
        name = data.get("name", "").strip()
        key_hex = data.get("key_hex") or None
        if not name:
            return {"status": "error", "detail": "name required"}
        if key_hex and len(key_hex.replace(" ", "")) < 32:
            return {"status": "error",
                    "detail": f"key_hex too short ({len(key_hex.replace(' ',''))} hex chars, need ≥32 = 16 bytes)"}
        adapter = router._adapters.get(adapter_name)
        if not adapter:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail=f"Adapter '{adapter_name}' not found")
        if not hasattr(adapter, "create_channel"):
            return {"status": "error", "detail": "Adapter does not support channel creation"}
        ok = await adapter.create_channel(idx, name, key_hex)
        return {"status": "ok" if ok else "error", "idx": idx, "name": name}

    @app.post("/api/adapters/{adapter_name}/scan_channels")
    async def scan_meshcore_channels(adapter_name: str, request: Request):
        """Query device for all configured channel slots using CMD_GET_CHANNEL (0x1F)."""
        data = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        try:
            max_idx = int(data.get("max_idx", 255)) if isinstance(data, dict) else 255
        except (ValueError, TypeError):
            max_idx = 255
        max_idx = max(0, min(max_idx, 255))
        adapter = router._adapters.get(adapter_name)
        if not adapter:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail=f"Adapter '{adapter_name}' not found")
        if not hasattr(adapter, "scan_channels"):
            return {"status": "error", "detail": "Adapter does not support channel scan"}
        channels = await adapter.scan_channels(max_idx=max_idx)
        channel_list = [{"idx": i, "name": n or f"ch{i}"} for i, n in sorted(channels.items())]
        return {"status": "ok", "adapter": adapter_name, "channels": channel_list, "count": len(channel_list)}

    @app.get("/api/adapters/{adapter_name}/privkey")
    async def get_meshcore_privkey(adapter_name: str):
        adapter = router._adapters.get(adapter_name)
        if not adapter:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail=f"Adapter '{adapter_name}' not found")
        if not hasattr(adapter, "get_privkey_hex"):
            return {"status": "error", "detail": "Not supported"}
        key = adapter.get_privkey_hex()
        if not key:
            return {"status": "none", "detail": "No private key cached (serial only, retrieved at startup)"}
        return {"status": "ok", "privkey_hex": key, "length": len(key)}

    @app.get("/api/adapters/{adapter_name}/key_inventory")
    async def get_key_inventory(adapter_name: str):
        """Return the list of channel decryption keys currently loaded (channels + common defaults)."""
        adapter = router._adapters.get(adapter_name)
        if not adapter:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail=f"Adapter '{adapter_name}' not found")
        channel_keys = getattr(adapter, "_channel_keys", {})
        channels = getattr(adapter, "_channels", {})
        from ech.adapters.meshcore import MeshCoreAdapter
        if not MeshCoreAdapter._COMMON_KEYS:
            MeshCoreAdapter._COMMON_KEYS = MeshCoreAdapter._build_common_keys()
        return {
            "channel_keys": [
                {"idx": idx, "name": channels.get(idx, f"ch{idx}"),
                 "key_hex": key.hex()[:32]}   # first 16 bytes (AES-128 key) only
                for idx, key in sorted(channel_keys.items())
            ],
            "common_keys": [
                {"label": label, "key_hex": key.hex()}
                for label, key in MeshCoreAdapter._COMMON_KEYS
            ],
        }

    @app.post("/api/adapters/{adapter_name}/clear_nodes")
    async def clear_adapter_nodes(adapter_name: str):
        adapter = router._adapters.get(adapter_name)
        if not adapter:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail=f"Adapter '{adapter_name}' not found")
        if not hasattr(adapter, "clear_nodes"):
            return {"status": "error", "detail": "Adapter does not support node clearing"}
        count = await adapter.clear_nodes()
        return {"status": "ok", "cleared": count}

    @app.post("/api/adapters/{adapter_name}/beacon")
    async def send_aprs_beacon(adapter_name: str, request: Request):
        """Send an APRS position beacon from this adapter."""
        data = await request.json()
        adapter = router._adapters.get(adapter_name)
        if not adapter:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail=f"Adapter '{adapter_name}' not found")
        if not hasattr(adapter, "send_beacon"):
            return {"status": "error", "detail": "Adapter does not support position beacons"}
        lat = data.get("lat")
        lon = data.get("lon")
        comment = data.get("comment")
        return await adapter.send_beacon(
            lat=float(lat) if lat is not None else None,
            lon=float(lon) if lon is not None else None,
            comment=comment,
        )

    @app.get("/api/adapters/{adapter_name}/packets")
    async def get_adapter_packets(adapter_name: str, limit: int = 100):
        """Return recent raw frames/events received by an adapter (for diagnostics)."""
        adapter = router._adapters.get(adapter_name)
        if not adapter:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail=f"Adapter '{adapter_name}' not found")
        log_attr = getattr(adapter, "_packet_log", None)
        if log_attr is None:
            return {"packets": [], "note": "This adapter does not log raw frames"}
        packets = list(log_attr)[-limit:]
        packets.reverse()  # newest first
        return {"adapter": adapter_name, "count": len(packets), "packets": packets}

    @app.get("/api/meshcore/raw")
    async def get_meshcore_raw(limit: int = 200):
        """Merged raw packet log from all MeshCore adapters, newest first. For protocol debugging."""
        from ech.adapters.meshcore import MeshCoreAdapter
        all_packets = []
        for name, adapter in router._adapters.items():
            if isinstance(adapter, MeshCoreAdapter):
                for pkt in getattr(adapter, "_packet_log", []):
                    all_packets.append({**pkt, "adapter": name})
        all_packets.sort(key=lambda p: p.get("ts", 0), reverse=True)
        return {"count": len(all_packets[:limit]), "packets": all_packets[:limit]}

    @app.post("/api/adapters/{adapter_name}/discover")
    async def trigger_discovery(adapter_name: str):
        """Immediately send a discovery pulse (APP_START + DEVICE_QUERY) to solicit node adverts."""
        adapter = router._adapters.get(adapter_name)
        if not adapter:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail=f"Adapter '{adapter_name}' not found")
        if not hasattr(adapter, '_discovery_pulse'):
            return {"status": "error", "detail": "Adapter does not support discovery"}
        if not getattr(adapter, '_connected', False):
            return {"status": "error", "detail": "Adapter not connected — cannot send discovery pulse"}
        await adapter._discovery_pulse()
        return {"status": "ok", "node_count": len(adapter._nodes)}

    @app.post("/api/adapters/{adapter_name}/max_hops")
    async def set_max_hops(adapter_name: str, request: Request):
        """Set the outgoing message hop limit for a MeshCore adapter."""
        data = await request.json()
        adapter = router._adapters.get(adapter_name)
        if not adapter:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail=f"Adapter '{adapter_name}' not found")
        if not hasattr(adapter, '_max_hops'):
            return {"status": "error", "detail": "Adapter does not support max_hops"}
        try:
            hops = int(data.get("max_hops", 0))
        except (ValueError, TypeError):
            return {"status": "error", "detail": "max_hops must be an integer"}
        if not 0 <= hops <= 10:
            return {"status": "error", "detail": "max_hops must be 0–10"}
        adapter._max_hops = hops
        log.info("MeshCore %s: max_hops set to %d", adapter_name, hops)
        return {"status": "ok", "max_hops": hops}

    # ── Channels ──────────────────────────────────────────────────────────

    @app.get("/api/channels")
    async def get_channels():
        return {"channels": await router.all_health()}

    @app.get("/api/nodes/{adapter_name}")
    async def get_nodes(adapter_name: str):
        return {"nodes": await router.nodes_for(adapter_name)}

    # ── Contacts ──────────────────────────────────────────────────────────

    @app.get("/api/contacts")
    async def get_contacts(search: str | None = None):
        return {"contacts": await db.get_contacts(search=search)}

    @app.post("/api/contacts")
    async def upsert_contact(contact: dict):
        await db.upsert_contact(contact)
        return {"status": "ok"}

    @app.delete("/api/contacts/{contact_id}")
    async def delete_contact(contact_id: str):
        await db.delete_contact(contact_id)
        return {"status": "ok"}

    @app.post("/api/contacts/import")
    async def import_contacts(
        file: UploadFile = File(...),
        format: str = Query("auto", description="vcf | csv | auto"),
    ):
        """Import contacts from a vCard (.vcf) or CSV file upload."""
        import tempfile, os
        from ech.adapters.sms import import_vcf, import_csv

        suffix = Path(file.filename or "contacts.vcf").suffix.lower()
        detected = "vcf" if suffix == ".vcf" else "csv" if suffix == ".csv" else format

        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(await file.read())
            tmp_path = tmp.name

        try:
            if detected == "vcf":
                contacts = import_vcf(tmp_path)
            else:
                contacts = import_csv(tmp_path)

            imported = 0
            for c in contacts:
                await db.upsert_contact(c)
                imported += 1
            return {"status": "ok", "imported": imported}
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}
        finally:
            os.unlink(tmp_path)

    # ── Anomaly findings ──────────────────────────────────────────────────

    @app.get("/api/anomalies")
    async def get_anomalies(acknowledged: bool | None = None, limit: int = 200):
        findings = await db.get_anomalies(acknowledged=acknowledged, limit=limit)
        active = anomaly_engine.active_findings() if anomaly_engine else []
        return {
            "findings": findings,
            "active_count": len(active),
            "total": len(findings),
        }

    @app.post("/api/anomalies/{finding_id}/acknowledge")
    async def acknowledge_anomaly(finding_id: str):
        if anomaly_engine:
            anomaly_engine.acknowledge(finding_id)
        await db.acknowledge_anomaly(finding_id)
        return {"status": "ok"}

    @app.post("/api/anomalies/clear-all")
    async def clear_all_anomalies():
        if anomaly_engine:
            anomaly_engine.clear_all()
        # rowcount is authoritative — engine may have empty in-memory list after restart
        db_count = await db.execute_raw("UPDATE anomaly_findings SET acknowledged=1 WHERE acknowledged=0")
        return {"status": "ok", "cleared": db_count}

    @app.post("/api/anomalies/{finding_id}/broadcast")
    async def broadcast_anomaly(finding_id: str):
        """Broadcast a warning about this finding to all mesh adapters."""
        findings = await db.get_anomalies(limit=500)
        finding = next((f for f in findings if f["id"] == finding_id), None)
        if not finding:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Finding not found")
        body = (f"NOTICE: Node {finding['node_id'][:12]} on {finding['adapter']} "
                f"flagged for {finding['rule']} — {finding['summary'][:80]}")
        results = await router.send(body=body[:200], priority=1)
        await db.execute_raw(
            "UPDATE anomaly_findings SET broadcast_sent=1 WHERE id=?", (finding_id,)
        )
        return {"status": "ok", "results": results}

    # ── Weather service ────────────────────────────────────────────────────

    @app.get("/api/weather")
    async def get_weather():
        if not wx_service:
            return {"enabled": False}
        return wx_service.status()

    @app.post("/api/weather/broadcast")
    async def broadcast_weather(adapters: list[str] = Query(default=None)):
        if not wx_service:
            return {"status": "error", "detail": "Weather service not configured"}
        ok = await wx_service.broadcast_wx_summary(adapter_names=adapters or None)
        return {"status": "ok" if ok else "partial"}

    @app.post("/api/weather/share")
    async def share_weather(adapters: list[str] = Query(default=None)):
        """Broadcast current conditions + 12h forecast to all adapters."""
        if not wx_service:
            return {"status": "error", "detail": "Weather service not configured"}
        summary = await wx_service.fetch_conditions_and_forecast()
        results = await router.send(body=summary, adapter_names=adapters or None)
        return {"status": "ok", "summary": summary, "sent": results}

    @app.post("/api/time-sync")
    async def time_sync_broadcast(adapters: list[str] = Query(default=None),
                                  incident: str = ""):
        if wx_service:
            ok = await wx_service.broadcast_time_sync(
                adapter_names=adapters, incident_name=incident
            )
        else:
            results = await router.send(
                body=f"TIME SYNC: {__import__('datetime').datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}",
                adapter_names=adapters,
            )
            ok = all(results.values())
        targets = adapters or list(router._adapters.keys())
        synced = {}
        for name in targets:
            a = router._adapters.get(name)
            if a and a._connected:
                synced[name] = await a.time_sync()
        return {"status": "ok" if ok else "partial", "synced": synced}

    @app.post("/api/announce")
    async def announce_nodes(adapters: list[str] | None = None):
        targets = adapters or list(router._adapters.keys())
        results = {}
        for name in targets:
            a = router._adapters.get(name)
            if a and a._connected:
                results[name] = await a.announce()
        return {"status": "ok", "results": results}

    @app.post("/api/ping")
    async def ping_node(request: Request):
        data = await request.json()
        adapter_name = data.get("adapter")
        node_id      = data.get("node_id", "").strip()
        if not adapter_name or not node_id:
            return {"status": "error", "detail": "adapter and node_id required"}
        a = router._adapters.get(adapter_name)
        if not a:
            return {"status": "error", "detail": f"adapter '{adapter_name}' not found"}
        if not a._connected:
            return {"status": "error", "detail": "adapter not connected"}
        result = await a.ping(node_id)
        return result

    # ── Prometheus metrics ────────────────────────────────────────────────

    @app.get("/metrics")
    async def prometheus_metrics():
        from ech.core.metrics import get_metrics_output
        body, content_type = get_metrics_output()
        return Response(content=body, media_type=content_type)

    # ── Anomaly dashboard page ────────────────────────────────────────────

    @app.get("/anomalies", response_class=HTMLResponse)
    async def anomaly_page():
        template = UI_DIR / "templates" / "anomalies.html"
        if template.exists():
            return HTMLResponse(content=_render_template("anomalies.html"), headers=_NO_CACHE)
        return HTMLResponse(content="<h1>Anomaly dashboard not found</h1>")

    # ── Auth ──────────────────────────────────────────────────────────────

    @app.get("/login", response_class=HTMLResponse)
    async def login_page():
        template = UI_DIR / "templates" / "login.html"
        return HTMLResponse(content=template.read_text() if template.exists() else "<h1>Login</h1>",
                            headers=_NO_CACHE)

    @app.get("/change-password", response_class=HTMLResponse)
    async def change_password_page():
        template = UI_DIR / "templates" / "change_password.html"
        return HTMLResponse(content=template.read_text() if template.exists() else "<h1>Change Password</h1>",
                            headers=_NO_CACHE)

    @app.post("/api/auth/change-password")
    async def do_change_password(request: Request):
        from fastapi.responses import JSONResponse, RedirectResponse
        from ech.core.auth import SESSION_COOKIE
        token = request.cookies.get(SESSION_COOKIE, "")
        session = await auth.get_session(token) if auth else {"username": "admin", "role": "admin"}
        if not session:
            raise HTTPException(status_code=401, detail="Not authenticated")
        content_type = request.headers.get("content-type", "")
        if "application/x-www-form-urlencoded" in content_type:
            form = await request.form()
            new_password = form.get("new_password", "")
            confirm = form.get("confirm_password", "")
            use_redirect = True
        else:
            data = await request.json()
            new_password = data.get("new_password", "")
            confirm = data.get("confirm_password", "")
            use_redirect = False
        if not new_password or len(new_password) < 8:
            if use_redirect:
                return RedirectResponse(url="/change-password?error=short", status_code=303)
            raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
        if new_password != confirm:
            if use_redirect:
                return RedirectResponse(url="/change-password?error=mismatch", status_code=303)
            raise HTTPException(status_code=400, detail="Passwords do not match")
        if auth:
            await auth.change_password(session["username"], new_password)
        log.info("AUTH: %s changed password via forced change flow", session["username"])
        if use_redirect:
            resp = RedirectResponse(url="/", status_code=303)
        else:
            resp = JSONResponse({"status": "ok"})
        return resp

    # Simple in-memory brute-force guard: track failed attempts per IP.
    _login_failures: dict[str, list[float]] = {}
    _LOGIN_MAX_ATTEMPTS = 10
    _LOGIN_WINDOW_SEC   = 60.0

    def _check_login_rate(ip: str) -> bool:
        """Return True if the IP is allowed to attempt login, False if rate-limited."""
        import time as _time
        now = _time.monotonic()
        attempts = _login_failures.get(ip, [])
        attempts = [t for t in attempts if now - t < _LOGIN_WINDOW_SEC]
        _login_failures[ip] = attempts
        return len(attempts) < _LOGIN_MAX_ATTEMPTS

    def _record_login_failure(ip: str) -> None:
        import time as _time
        _login_failures.setdefault(ip, []).append(_time.monotonic())

    @app.post("/api/auth/login")
    async def do_login(request: Request):
        from ech.core.auth import SESSION_COOKIE, SESSION_EXPIRE_HOURS
        from fastapi.responses import JSONResponse, RedirectResponse

        client_ip = (request.client.host if request.client else "unknown")
        if not _check_login_rate(client_ip):
            log.warning("AUTH: rate-limit hit from %s", client_ip)
            raise HTTPException(status_code=429, detail="Too many login attempts — wait 60 seconds")

        content_type = request.headers.get("content-type", "")
        if "application/x-www-form-urlencoded" in content_type:
            form = await request.form()
            username = form.get("username", "")
            password = form.get("password", "")
            next_url = form.get("next", "/")
            use_redirect = True
        else:
            data = await request.json()
            username = data.get("username", "")
            password = data.get("password", "")
            next_url = data.get("next", "/")
            use_redirect = False

        # Reject open-redirect payloads — next must be a relative path
        if not next_url or not next_url.startswith("/") or next_url.startswith("//"):
            next_url = "/"

        token = await auth.login(username, password) if auth else None
        if not token:
            _record_login_failure(client_ip)
            if use_redirect:
                return RedirectResponse(url="/login?error=1", status_code=303)
            raise HTTPException(status_code=401, detail="Invalid credentials")

        if use_redirect:
            resp = RedirectResponse(url=next_url, status_code=303)
        else:
            resp = JSONResponse({"status": "ok", "username": username})

        resp.set_cookie(
            SESSION_COOKIE, token,
            max_age=SESSION_EXPIRE_HOURS * 3600,
            httponly=True,
            samesite="lax",
            secure=secure_cookies,
            path="/",
        )
        return resp

    @app.post("/api/auth/logout")
    async def do_logout(request: Request):
        from fastapi.responses import JSONResponse
        from ech.core.auth import SESSION_COOKIE
        token = request.cookies.get(SESSION_COOKIE, "")
        if auth and token:
            await auth.logout(token)
        resp = JSONResponse({"status": "ok"})
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    @app.get("/api/auth/me")
    async def whoami(request: Request):
        session = await auth.require_session(request) if auth else {"username":"guest","role":"admin"}
        if not session:
            return {"authenticated": False}
        return {"authenticated": True, **session}

    @app.get("/api/auth/sessions")
    async def active_sessions():
        sessions = await db.get_active_sessions()
        return {"sessions": sessions, "count": len(sessions)}

    @app.delete("/api/auth/sessions/{username}")
    async def force_logout_user(username: str, request: Request):
        """Admin-only: revoke all sessions for a user, forcing them to re-login."""
        session = await auth.require_session(request) if auth else {"role": "admin"}
        if not session or session.get("role") != "admin":
            from starlette.responses import JSONResponse as _JR
            return _JR({"status": "error", "detail": "admin required"}, status_code=403)
        count = await db.delete_sessions_for_user(username)
        log.info("AUTH: admin '%s' force-logged-out '%s' (%d session(s) revoked)",
                 session.get("username", "?"), username, count)
        return {"status": "ok", "username": username, "sessions_revoked": count}

    @app.get("/api/users")
    async def get_users(request: Request):
        return {"users": await db.get_users()}

    @app.post("/api/users")
    async def create_user(request: Request):
        data = await request.json()
        if not auth:
            return {"status": "error", "detail": "auth not configured"}
        ok = await auth.create_user(data["username"], data["password"], data.get("role","operator"))
        return {"status": "ok" if ok else "error"}

    @app.delete("/api/users/{username}")
    async def delete_user(username: str):
        await db.delete_user(username)
        return {"status": "ok"}

    @app.post("/api/users/{username}/password")
    async def change_password(username: str, request: Request):
        data = await request.json()
        if auth:
            await auth.change_password(username, data["password"])
        return {"status": "ok"}

    # ── ECH State / Mode ──────────────────────────────────────────────────

    @app.get("/api/state")
    async def get_state():
        return ech_state.snapshot() if ech_state else {}

    @app.post("/api/state/mode")
    async def set_mode(request: Request):
        data = await request.json()
        if ech_state:
            await ech_state.set_mode(data.get("mode", "standard"))
        return {"status": "ok", "mode": ech_state.mode if ech_state else "unknown"}

    @app.post("/api/state/simulation")
    async def set_simulation(request: Request):
        data = await request.json()
        if ech_state:
            await ech_state.set_simulation(bool(data.get("enabled", True)))
        return {"status": "ok"}

    @app.post("/api/state/incident")
    async def set_incident(request: Request):
        data = await request.json()
        if ech_state:
            await ech_state.set_incident(data.get("incident_name", ""))
        return {"status": "ok"}

    @app.post("/api/state/operator")
    async def set_operator(request: Request):
        data = await request.json()
        if ech_state:
            await ech_state.set_operator(data.get("callsign", ""))
        return {"status": "ok"}

    # ── System GPS ───────────────────────────────────────────────────────

    @app.get("/api/gps")
    async def get_gps_status():
        if gps_reader is None:
            return {"enabled": False, "detail": "No GPS configured (add 'gps:' section to config.yaml)"}
        return {"enabled": True, **gps_reader.status}

    # ── Adapter config management ─────────────────────────────────────────

    @app.get("/api/adapter-config")
    async def get_adapter_config():
        import yaml
        from pathlib import Path
        config_path = Path("/etc/ech/config.yaml")
        if not config_path.exists():
            config_path = Path("config.yaml")
        try:
            with open(config_path) as f:
                cfg = yaml.safe_load(f)
            return {"adapters": cfg.get("adapters", []), "bridge_rules": cfg.get("bridge_rules", [])}
        except Exception as exc:
            return {"error": str(exc)}

    @app.post("/api/adapter-config")
    async def save_adapter_config(request: Request):
        import yaml
        from pathlib import Path
        data = await request.json()
        config_path = Path("/etc/ech/config.yaml")
        if not config_path.exists():
            config_path = Path("config.yaml")
        # Pre-build yaml snippet in case write fails (permission error etc.)
        new_adapters = data.get("adapters", [])
        yaml_snippet = yaml.dump(
            {"adapters": new_adapters}, default_flow_style=False, allow_unicode=True
        ).strip()
        try:
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            cfg["adapters"] = new_adapters
            if "bridge_rules" in data:
                cfg["bridge_rules"] = data["bridge_rules"]
            with open(config_path, "w") as f:
                yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
            return {"status": "ok", "note": "Restart ECH to apply adapter changes"}
        except PermissionError as exc:
            return {
                "status": "error",
                "detail": str(exc),
                "yaml_snippet": yaml_snippet,
                "fix_hint": f"sudo chown $(whoami) {config_path}",
            }
        except Exception as exc:
            return {"status": "error", "detail": str(exc), "yaml_snippet": yaml_snippet}

    @app.post("/api/bridge-rules")
    async def save_bridge_rules(request: Request):
        """Save bridge rules to config.yaml and apply them live without restart."""
        import yaml
        from pathlib import Path
        data = await request.json()
        rules = data.get("rules", [])
        # Validate: each rule must have from_adapter and to_adapter strings
        for r in rules:
            if not isinstance(r.get("from_adapter"), str) or not isinstance(r.get("to_adapter"), str):
                return {"status": "error", "detail": "Each rule needs from_adapter and to_adapter strings"}
        # Apply live to running router
        router._bridge_rules = rules
        # Persist to config.yaml
        config_path = Path("/etc/ech/config.yaml")
        if not config_path.exists():
            config_path = Path("config.yaml")
        try:
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            cfg["bridge_rules"] = rules
            with open(config_path, "w") as f:
                yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
            return {"status": "ok", "rules": rules,
                    "note": f"{len(rules)} rule(s) applied live and saved to config"}
        except Exception as exc:
            # Applied live but couldn't persist — warn rather than fail
            return {"status": "ok", "rules": rules, "applied_live": True,
                    "warning": f"Applied live but could not save to config: {exc}"}

    # ── Base location ──────────────────────────────────────────────────────

    @app.post("/api/base-location")
    async def set_base_location(request: Request):
        """Set the global base location — live-updates weather coords and mock adapter positions."""
        data = await request.json()
        lat = float(data.get("lat", 0))
        lon = float(data.get("lon", 0))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return {"status": "error", "detail": "Invalid coordinates"}
        # Live update: weather service, running mock adapters, DB
        if ech_state:
            await ech_state.set_base_location(lat, lon)
        return {"status": "ok", "lat": lat, "lon": lon,
                "note": "Mock adapter positions and weather coordinates updated live"}

    # ── Weather config ────────────────────────────────────────────────────

    @app.get("/api/weather/config")
    async def get_wx_config():
        if not wx_service:
            return {"enabled": False}
        return {
            "enabled": wx_service.enabled,
            "nws_area": wx_service._area,
            "nws_lat": wx_service._lat,
            "nws_lon": wx_service._lon,
            "poll_interval_sec": wx_service._poll_interval,
            "severity_filter": list(wx_service._severity_filter),
            "auto_broadcast_extreme": wx_service._auto_broadcast,
            "auto_broadcast_adapters": wx_service._auto_adapters,
        }

    @app.post("/api/weather/config")
    async def update_wx_config(request: Request):
        data = await request.json()
        if ech_state:
            await ech_state.update_weather_config(data)
        return {"status": "ok"}

    @app.get("/api/weather/alerts")
    async def get_weather_alerts():
        """Return currently active NWS alerts with seen/unseen status."""
        if not wx_service:
            return {"enabled": False, "alerts": []}
        alerts = [
            {**a, "seen": a["id"] in wx_service._seen_alert_ids}
            for a in wx_service._active_alerts
        ]
        using_coords = wx_service._lat is not None and wx_service._lon is not None
        return {
            "enabled": wx_service.enabled,
            "nws_area": wx_service._area,
            "poll_source": f"point:{wx_service._lat},{wx_service._lon}" if using_coords else f"area:{wx_service._area}",
            "last_poll": wx_service._last_poll.isoformat() if wx_service._last_poll else None,
            "alerts": alerts,
        }

    @app.post("/api/weather/poll")
    async def trigger_weather_poll():
        """Trigger an immediate NWS alert poll."""
        if not wx_service:
            return {"status": "error", "detail": "Weather service not configured"}
        await wx_service.trigger_poll()
        return {
            "status": "ok",
            "active_alerts": len(wx_service._active_alerts),
            "last_poll": wx_service._last_poll.isoformat() if wx_service._last_poll else None,
        }

    # ── Mesh bot config + test ───────────────────────────────────────────────

    @app.get("/api/bot/config")
    async def get_bot_config():
        wx_bot = getattr(router, "_weather_bot", None)
        if not wx_bot:
            return {"enabled": False}
        cfg = wx_bot._config.get("mesh_bot", {})
        return {
            "enabled":               wx_bot.enabled,
            "channels":              wx_bot._channels,
            "adapters":              wx_bot._adapter_filter,
            "reply_dm":              wx_bot._reply_dm,
            "per_user_cooldown_sec": wx_bot._user_cooldown,
            "global_cooldown_sec":   wx_bot._global_cooldown,
            "max_reply_len":         wx_bot._max_len,
            "dump1090_path":         wx_bot._dump1090,
            "overhead_radius_nm":    wx_bot._radius_nm,
            "ships_radius_nm":       float(cfg.get("ships_radius_nm", 50)),
            "tle_targets":           wx_bot._tle_targets,
            "solar_cache_sec":       wx_bot._solar_cache_sec,
            "lat":                   wx_bot._lat,
            "lon":                   wx_bot._lon,
            "aprs_fi_key":           cfg.get("aprs_fi_key", ""),
        }

    @app.post("/api/bot/config")
    async def update_bot_config(request: Request):
        data = await request.json()
        wx_bot = getattr(router, "_weather_bot", None)
        if not wx_bot:
            return {"status": "error", "detail": "Bot not running"}
        # Apply in-memory (survives until restart; use config save to persist)
        if "enabled" in data:             wx_bot.enabled               = bool(data["enabled"])
        if "channels" in data:            wx_bot._channels             = [c.lower() for c in data["channels"]]
        if "adapters" in data:            wx_bot._adapter_filter       = data["adapters"]
        if "reply_dm" in data:            wx_bot._reply_dm             = bool(data["reply_dm"])
        if "per_user_cooldown_sec" in data: wx_bot._user_cooldown      = int(data["per_user_cooldown_sec"])
        if "global_cooldown_sec" in data: wx_bot._global_cooldown      = int(data["global_cooldown_sec"])
        if "max_reply_len" in data:       wx_bot._max_len              = int(data["max_reply_len"])
        if "dump1090_path" in data:       wx_bot._dump1090             = data["dump1090_path"]
        if "overhead_radius_nm" in data:  wx_bot._radius_nm            = float(data["overhead_radius_nm"])
        if "solar_cache_sec" in data:     wx_bot._solar_cache_sec      = int(data["solar_cache_sec"])
        if "lat" in data:                 wx_bot._lat                  = float(data["lat"]) if data["lat"] not in (None, "") else None
        if "lon" in data:                 wx_bot._lon                  = float(data["lon"]) if data["lon"] not in (None, "") else None
        if "tle_targets" in data:         wx_bot._tle_targets          = [t.upper() for t in data["tle_targets"]]
        if "aprs_fi_key" in data:
            wx_bot._config.setdefault("mesh_bot", {})["aprs_fi_key"] = data["aprs_fi_key"]
        if "ships_radius_nm" in data:
            wx_bot._config.setdefault("mesh_bot", {})["ships_radius_nm"] = float(data["ships_radius_nm"])
        return {"status": "ok"}

    @app.post("/api/bot/test")
    async def test_bot_command(request: Request):
        data      = await request.json()
        command   = (data.get("command") or "").strip()
        if not command:
            return {"error": "command required"}
        wx_bot = getattr(router, "_weather_bot", None)
        if not wx_bot:
            return {"error": "Bot not running"}
        if not wx_bot._client:
            return {"error": "Bot HTTP client not started — is bot enabled?"}
        # Parse command + args the same way the bot does
        import re as _re
        m = _re.match(r'^(\S+)\s*(.*)', command, _re.IGNORECASE)
        if not m:
            return {"error": "Could not parse command"}
        cmd  = m.group(1).lower().rstrip("?")
        args = m.group(2).strip()
        try:
            # Dispatch directly without cooldown / routing
            if cmd == "ping":
                from ech.core.models import NormalizedMessage
                fake = NormalizedMessage(source_adapter="test", source_channel="test", body=command)
                result = wx_bot._cmd_ping(fake)
            elif cmd in ("weather", "wx"):   result = await wx_bot._cmd_weather(args)
            elif cmd == "overhead":          result = await wx_bot._cmd_overhead()
            elif cmd in ("satpass", "sat"):  result = await wx_bot._cmd_satpass(args)
            elif cmd in ("solar", "space"):  result = await wx_bot._cmd_solar()
            elif cmd == "ships":             result = await wx_bot._cmd_ships()
            elif cmd == "fcc":               result = await wx_bot._cmd_fcc(args)
            elif cmd == "trivia":            result = await wx_bot._cmd_trivia()
            elif cmd == "dad":               result = await wx_bot._cmd_dad()
            elif cmd == "alerts":            result = await wx_bot._cmd_alerts()
            elif cmd == "metar":             result = await wx_bot._cmd_metar(args)
            elif cmd == "sun":               result = await wx_bot._cmd_sun()
            elif cmd == "nodes":             result = await wx_bot._cmd_nodes()
            elif cmd == "aprs":              result = await wx_bot._cmd_aprs(args)
            elif cmd == "anomalies":         result = await wx_bot._cmd_anomalies()
            elif cmd == "help":              result = wx_bot._cmd_help()
            else:                            result = f"unknown command: {cmd}"
        except Exception as exc:
            result = f"error: {type(exc).__name__}: {exc}"
        return {"command": command, "response": result}

    # ── Config file read / write ──────────────────────────────────────────────

    @app.get("/api/config")
    async def get_config_file():
        """Return the parsed config.yaml as JSON (secrets like passwords redacted)."""
        import copy, yaml
        path = app.state.config_path
        if not path:
            return {"error": "config_path not set"}
        try:
            with open(path) as f:
                cfg = yaml.safe_load(f) or {}
        except FileNotFoundError:
            return {"error": f"config file not found: {path}"}
        # Redact sensitive fields before sending to browser
        safe = copy.deepcopy(cfg)
        for adapter in safe.get("adapters", []):
            for key in ("password", "secret", "private_key", "token"):
                if key in adapter:
                    adapter[key] = "••••••••"
        return {"config": safe, "path": path}

    @app.post("/api/config/section")
    async def save_config_section(request: Request):
        """Merge a partial config dict into the live config.yaml and reload affected services."""
        import yaml
        data    = await request.json()
        section = data.get("section")   # top-level key e.g. "mesh_bot", "server", "gps"
        values  = data.get("values", {})
        if not section or not isinstance(values, dict):
            return {"status": "error", "detail": "section and values required"}
        path = app.state.config_path
        if not path:
            return {"status": "error", "detail": "config_path not set on server"}
        try:
            with open(path) as f:
                cfg = yaml.safe_load(f) or {}
        except FileNotFoundError:
            cfg = {}
        cfg[section] = {**(cfg.get(section) or {}), **values}
        with open(path, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
        return {"status": "ok", "saved": path}

    # ── Simulation management ─────────────────────────────────────────────

    @app.get("/api/simulation/nodes")
    async def get_sim_nodes():
        return {"nodes": await db.get_sim_nodes()}

    @app.post("/api/simulation/nodes")
    async def upsert_sim_node(request: Request):
        data = await request.json()
        await db.upsert_sim_node(data)
        return {"status": "ok"}

    @app.delete("/api/simulation/nodes/{node_id}")
    async def delete_sim_node(node_id: str):
        await db.delete_sim_node(node_id)
        return {"status": "ok"}

    @app.get("/api/simulation/messages")
    async def get_sim_messages():
        return {"messages": await db.get_sim_messages()}

    @app.post("/api/simulation/messages")
    async def upsert_sim_message(request: Request):
        data = await request.json()
        await db.upsert_sim_message(data)
        return {"status": "ok"}

    @app.delete("/api/simulation/messages/{msg_id}")
    async def delete_sim_message(msg_id: str):
        await db.delete_sim_message(msg_id)
        return {"status": "ok"}

    # ── Log viewer ────────────────────────────────────────────────────────

    @app.get("/api/logs")
    async def get_logs(limit: int = 500, level: str | None = None):
        limit = min(max(limit, 1), 2000)
        return {"logs": await db.get_log_entries(limit=limit, level=level)}

    @app.get("/logs", response_class=HTMLResponse)
    async def logs_page():
        tpl = UI_DIR / "templates" / "logs.html"
        return HTMLResponse(content=_render_template("logs.html") if tpl.exists() else "<h1>Logs</h1>",
                            headers=_NO_CACHE)

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        await _admin_required(request)
        tpl = UI_DIR / "templates" / "settings.html"
        return HTMLResponse(content=_render_template("settings.html") if tpl.exists() else "<h1>Settings</h1>",
                            headers=_NO_CACHE)

    @app.get("/simulation", response_class=HTMLResponse)
    async def simulation_page():
        tpl = UI_DIR / "templates" / "simulation.html"
        return HTMLResponse(content=_render_template("simulation.html") if tpl.exists() else "<h1>Simulation</h1>",
                            headers=_NO_CACHE)

    @app.get("/map", response_class=HTMLResponse)
    async def map_page():
        tpl = UI_DIR / "templates" / "map.html"
        return HTMLResponse(content=_render_template("map.html") if tpl.exists() else "<h1>Map</h1>",
                            headers=_NO_CACHE)

    # ── System stats ─────────────────────────────────────────────────────

    @app.get("/api/system/services")
    async def system_services():
        """
        Check status of all ECH-related system services via systemctl.
        All service checks run concurrently so the endpoint responds in ~1s
        regardless of how many services are checked.
        """
        import asyncio as _aio
        import os as _os
        _systemctl = "/bin/systemctl" if _os.path.exists("/bin/systemctl") else "/usr/bin/systemctl"

        services = [
            {"name": "ech",         "label": "ECH",           "port": "8765"},
            {"name": "pat",         "label": "Pat (Winlink)",  "port": "8080"},
            {"name": "asterisk",    "label": "Asterisk PBX",   "port": "5060"},
            {"name": "mosquitto",   "label": "MQTT Broker",    "port": "1883"},
            {"name": "prometheus",  "label": "Prometheus",     "port": "9090"},
            {"name": "avahi-daemon","label": "mDNS (avahi)",   "port": "mdns"},
        ]

        systemctl_present = _os.path.exists(_systemctl)

        async def _check_one(svc: dict) -> dict:
            name = svc["name"]
            if not systemctl_present:
                return {**svc, "active": "unknown", "enabled": "unknown", "ok": None,
                        "note": "systemctl not available on this host"}
            try:
                async def _run(arg):
                    p = await _aio.create_subprocess_exec(
                        _systemctl, arg, name,
                        stdout=_aio.subprocess.PIPE,
                        stderr=_aio.subprocess.PIPE,
                    )
                    stdout, _ = await _aio.wait_for(p.communicate(), timeout=5.0)
                    return stdout.decode().strip() or "unknown"

                active, enabled = await _aio.gather(_run("is-active"), _run("is-enabled"))
                return {**svc, "active": active, "enabled": enabled, "ok": active == "active"}
            except Exception as exc:
                return {**svc, "active": "error", "enabled": "unknown", "ok": False,
                        "error": str(exc)}

        results = await _aio.gather(*[_check_one(s) for s in services])
        return {"services": list(results)}

    @app.post("/api/system/services/{service}/restart")
    async def restart_service(service: str):
        """Restart a named systemd service (admin only)."""
        import asyncio as _aio
        # Whitelist to prevent arbitrary command injection
        allowed = {"ech", "pat", "asterisk", "mosquitto", "prometheus", "avahi-daemon"}
        if service not in allowed:
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail=f"Service '{service}' not in allowed list")
        try:
            p = await _aio.create_subprocess_exec(
                "sudo", "systemctl", "restart", service,
                stdout=_aio.subprocess.PIPE,
                stderr=_aio.subprocess.PIPE,
            )
            stdout, stderr = await _aio.wait_for(p.communicate(), timeout=15.0)
            ok = p.returncode == 0
            return {
                "status": "ok" if ok else "error",
                "service": service,
                "stdout": stdout.decode(),
                "stderr": stderr.decode(),
            }
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}

    @app.get("/api/system/stats")
    async def system_stats():
        try:
            import psutil, time
            cpu   = psutil.cpu_percent(interval=None)
            mem   = psutil.virtual_memory()
            net   = psutil.net_io_counters()
            disk  = psutil.disk_usage("/")
            temps = {}
            try:
                for name, entries in psutil.sensors_temperatures().items():
                    if entries:
                        temps[name] = round(entries[0].current, 1)
            except Exception:
                pass
            return {
                "cpu_pct":        round(cpu, 1),
                "mem_total_mb":   round(mem.total / 1024**2),
                "mem_used_mb":    round(mem.used  / 1024**2),
                "mem_pct":        round(mem.percent, 1),
                "net_bytes_sent": net.bytes_sent,
                "net_bytes_recv": net.bytes_recv,
                "net_pkts_sent":  net.packets_sent,
                "net_pkts_recv":  net.packets_recv,
                "disk_total_gb":  round(disk.total / 1024**3, 1),
                "disk_used_gb":   round(disk.used  / 1024**3, 1),
                "disk_pct":       round(disk.percent, 1),
                "temps":          temps,
            }
        except ImportError:
            return {"error": "psutil not installed"}
        except Exception as exc:
            return {"error": str(exc)}

    # ── MeshCore bridge status ────────────────────────────────────────────

    @app.get("/api/meshcore-bridge")
    async def get_bridge_status():
        if not mc_bridge:
            return {"enabled": False}
        return mc_bridge.status()

    @app.post("/api/meshcore-bridge/enable")
    async def enable_bridge(request: Request):
        import yaml
        from pathlib import Path
        data = await request.json()
        enabled = bool(data.get("enabled", True))
        # Persist to config.yaml
        config_path = Path("/etc/ech/config.yaml")
        if not config_path.exists():
            config_path = Path("config.yaml")
        try:
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            bridge_cfg = cfg.get("meshcore_bridge", {})
            bridge_cfg["enabled"] = enabled
            if enabled and "mqtt_host" not in bridge_cfg:
                bridge_cfg.setdefault("mqtt_host", "localhost")
                bridge_cfg.setdefault("mqtt_port", 1883)
                bridge_cfg.setdefault("topic_prefix", "meshcore")
            cfg["meshcore_bridge"] = bridge_cfg
            with open(config_path, "w") as f:
                yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}
        # Update in-memory state
        if mc_bridge:
            mc_bridge.enabled = enabled
        return {"status": "ok", "enabled": enabled, "note": "Restart ECH to fully start the bridge"}

    # ── PSKReporter proxy ────────────────────────────────────────────────────

    @app.get("/api/pskreporter/spots")
    async def pskreporter_spots(callsign: str = "", band: str = "", limit: int = 200):
        """
        Proxy for PSKReporter JSON API.
        PSKReporter doesn't support CORS so browser can't fetch directly.
        Rate limited to 1 upstream request per 5 minutes (PSKReporter TOS).
        """
        import time as _time
        cache_key = f"{callsign.upper()}|{band}"
        cached = _psk_cache.get(cache_key)
        if cached:
            age = _time.monotonic() - cached[0]
            if age < _PSK_CACHE_TTL:
                payload = cached[1]
                return {"spots": payload["spots"][:limit], "total": payload["total"],
                        "cached": True, "cache_age_sec": round(age)}

        try:
            import httpx as _httpx
            import xml.etree.ElementTree as _ET
            params = {
                "active": "1",
                "flowStartSeconds": "-900",
                "statistics": "1",
                "lastSequence": "-1",
                "mode": "0",
            }
            if callsign:
                params["senderCallsign"] = callsign.upper()
            if band:
                params["band"] = band
            async with _httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(
                    "https://retrieve.pskreporter.info/query",
                    params=params,
                    headers={"User-Agent": "(ECH Emergency Communications Hub, ech@emergency.local)"},
                )
                r.raise_for_status()
                root = _ET.fromstring(r.text)
                spots = []
                for rx in root.findall(".//receptionReport"):
                    grid = rx.get("receiverLocator", "")
                    if len(grid) < 4:
                        continue
                    lat, lon = _grid_to_latlon(grid)
                    spots.append({
                        "senderCallsign": rx.get("senderCallsign", ""),
                        "receiverCallsign": rx.get("receiverCallsign", ""),
                        "mode": rx.get("mode", ""),
                        "band": int(rx.get("frequency", 0)),
                        "sNR": int(rx.get("sNR", 0)),
                        "rxLat": lat,
                        "rxLon": lon,
                        "receiverLocator": grid,
                        "flowStartSeconds": int(rx.get("flowStartSeconds", 0)),
                    })
                payload = {"spots": spots, "total": len(spots)}
                _psk_cache[cache_key] = (_time.monotonic(), payload)
                _psk_stats["last_success"] = __import__("datetime").datetime.utcnow().isoformat() + "Z"
                _psk_stats["total_fetches"] = _psk_stats.get("total_fetches", 0) + 1
                return {"spots": spots[:limit], "total": len(spots), "cached": False,
                        "cache_age_sec": 0}
        except Exception as exc:
            _psk_stats["last_failure"] = __import__("datetime").datetime.utcnow().isoformat() + "Z"
            _psk_stats["last_error"] = str(exc)
            return {"spots": [], "error": str(exc), "cached": False}

    @app.get("/api/pskreporter/status")
    async def pskreporter_status():
        import time as _time
        best_key = next(iter(_psk_cache), None)
        cache_age = None
        spot_count = 0
        if best_key and best_key in _psk_cache:
            cache_age = round(_time.monotonic() - _psk_cache[best_key][0])
            spot_count = _psk_cache[best_key][1].get("total", 0)
            if cache_age >= _PSK_CACHE_TTL:
                cache_age = None  # expired
        return {
            "last_success": _psk_stats.get("last_success"),
            "last_failure": _psk_stats.get("last_failure"),
            "last_error": _psk_stats.get("last_error"),
            "total_fetches": _psk_stats.get("total_fetches", 0),
            "cache_age_sec": cache_age,
            "cache_ttl_sec": _PSK_CACHE_TTL,
            "spot_count": spot_count,
            "cache_valid": cache_age is not None,
        }

    def _grid_to_latlon(grid: str):
        """Convert Maidenhead locator to center lat/lon."""
        g = grid.upper()
        if len(g) < 4:
            return 0.0, 0.0
        lon = (ord(g[0]) - ord('A')) * 20 - 180
        lat = (ord(g[1]) - ord('A')) * 10 - 90
        lon += (int(g[2])) * 2
        lat += (int(g[3])) * 1
        if len(g) >= 6:
            lon += (ord(g[4]) - ord('A')) * (2/24) + (1/24)
            lat += (ord(g[5]) - ord('A')) * (1/24) + (1/48)
        else:
            lon += 1.0   # center of 2-degree cell
            lat += 0.5   # center of 1-degree cell
        return round(lat, 4), round(lon, 4)

    # ── PBX / Asterisk AMI ───────────────────────────────────────────────

    def _find_pbx_adapter():
        """Return first adapter that connects to Asterisk AMI (any type)."""
        for a in router._adapters.values():
            # AsteriskAdapter, AREDNAMIAdapter, MockAsteriskAdapter all qualify
            if hasattr(a, "recent_calls") and (
                hasattr(a, "originate") or hasattr(a, "_pbx_version")
            ):
                return a
        return None

    @app.get("/api/pbx/status")
    async def pbx_status():
        """AMI connection state, active and recent calls."""
        a = _find_pbx_adapter()
        if not a:
            return {"enabled": False, "detail": "No PBX adapter configured"}
        return {
            "enabled":      True,
            "adapter":      a.name,
            "connected":    a._connected,
            "active_calls": a.active_calls(),
            "recent_calls": a.recent_calls()[:20],
            "detail":       a._health_detail(),
        }

    @app.post("/api/pbx/call")
    async def pbx_call(request: Request):
        """Click-to-call: ring local phone then bridge to destination."""
        data = await request.json()
        destination = data.get("destination", "").strip()
        caller_ext  = data.get("caller_extension", None)
        if not destination:
            return {"status": "error", "detail": "destination required"}
        a = _find_pbx_adapter()
        if not a:
            return {"status": "error", "detail": "No PBX adapter configured"}
        if not a._connected:
            return {"status": "error", "detail": "PBX adapter not connected"}
        ok = await a.originate(destination, caller_ext)
        return {"status": "ok" if ok else "error", "destination": destination}

    @app.post("/api/pbx/announce")
    async def pbx_announce(request: Request):
        """Page / announce to all configured extensions."""
        data = await request.json()
        target = data.get("target", None)
        a = _find_pbx_adapter()
        if not a:
            return {"status": "error", "detail": "No PBX adapter configured"}
        if not a._connected:
            return {"status": "error", "detail": "PBX adapter not connected"}
        ok = await a.page(target)
        return {"status": "ok" if ok else "error"}

    @app.post("/api/phone/push")
    async def phone_push(request: Request):
        """Push a short text notification to the screen phone display."""
        data = await request.json()
        text = data.get("text", "").strip()[:100]
        if not text:
            return {"status": "error", "detail": "text required"}
        a = _find_pbx_adapter()
        if not a or not hasattr(a, "push_to_screen"):
            return {"status": "error", "detail": "No screen phone adapter configured"}
        ok = await a.push_to_screen(text)
        return {"status": "ok" if ok else "not_configured"}

    @app.get("/api/phone/directory")
    async def phone_directory():
        """Return Yealink XML remote phone book built from ECH contacts."""
        a = _find_pbx_adapter()
        contacts = await db.get_contacts()
        if not a or not hasattr(a, "xml_directory"):
            return Response(
                content='<?xml version="1.0"?><YealinkIPPhoneDirectory></YealinkIPPhoneDirectory>',
                media_type="text/xml",
            )
        return Response(content=a.xml_directory(contacts), media_type="text/xml")

    # ── Health check ──────────────────────────────────────────────────────

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/system/storage")
    async def system_storage():
        """Return disk usage stats for the volume where ECH is running."""
        import shutil
        usage = shutil.disk_usage(".")
        free_gb  = usage.free  / (1024 ** 3)
        total_gb = usage.total / (1024 ** 3)
        free_pct = usage.free / usage.total * 100
        # Thresholds sized for a small SSD (8 GB thin-client target).
        # Warning  : < 1 GB free  OR  < 10 % free (whichever triggers first)
        # Critical : < 300 MB free
        return {
            "free_gb":  round(free_gb,  1),
            "used_gb":  round(usage.used / (1024 ** 3), 1),
            "total_gb": round(total_gb, 1),
            "free_pct": round(free_pct, 1),
            "warning":  free_gb < 1.0 or free_pct < 10,
            "critical": free_gb < 0.3,
        }

    # ── TLS / HTTPS support ───────────────────────────────────────────────

    @app.get("/ca.crt")
    async def download_ca_cert():
        """Serve the ECH CA cert so operators can trust it once in their browser/OS."""
        pem = getattr(app.state, "ca_cert_pem", None)
        if pem is None:
            return PlainTextResponse(
                "TLS is not enabled on this ECH server.\n"
                "Set  tls: enabled: true  in config.yaml to enable HTTPS.",
                status_code=404,
            )
        return Response(
            content=pem,
            media_type="application/x-pem-file",
            headers={"Content-Disposition": 'attachment; filename="ech-ca.crt"'},
        )

    @app.get("/tls-setup", response_class=HTMLResponse)
    async def tls_setup_page():
        """HTTPS setup guide — explains how to trust the CA cert and use Web Serial."""
        tls_enabled = getattr(app.state, "ca_cert_pem", None) is not None
        status_color = "#3fb950" if tls_enabled else "#d29922"
        status_text = "HTTPS is active on this server" if tls_enabled else "HTTPS is not enabled — TLS is disabled in config.yaml"
        dl_section = (
            '<p><a href="/ca.crt" style="color:#388bfd;font-weight:600">'
            '⬇ Download ECH CA Certificate (ech-ca.crt)</a></p>'
        ) if tls_enabled else '<p style="color:#d29922">Enable TLS first, then reload this page.</p>'
        return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ECH HTTPS Setup</title>
<style>
body{{font-family:'IBM Plex Sans',sans-serif;background:#0d1117;color:#e6edf3;max-width:700px;margin:40px auto;padding:20px;line-height:1.6}}
h1{{color:#3fb950;font-size:1.4rem}}h2{{color:#8b949e;font-size:1rem;text-transform:uppercase;letter-spacing:.08em;margin-top:2rem}}
.status{{padding:8px 14px;border-radius:6px;border:1px solid {status_color};color:{status_color};display:inline-block;margin-bottom:1.5rem;font-size:.9rem}}
code{{background:#161b22;padding:2px 6px;border-radius:4px;font-family:'IBM Plex Mono',monospace;font-size:.85rem;color:#79c0ff}}
ol li{{margin-bottom:.5rem}}a{{color:#388bfd}}
.box{{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:16px;margin:12px 0}}
</style></head><body>
<h1>ECH HTTPS / Web Serial Setup</h1>
<div class="status">{status_text}</div>
{dl_section}
<p>Web Serial API (used for browser-side CAT radio control) requires HTTPS.
ECH uses a self-signed CA so the cert works on any IP address — operators trust the CA once
and every future deployment is trusted automatically.</p>

<h2>Step 1 — Enable TLS in config.yaml</h2>
<div class="box"><code>tls:<br>&nbsp;&nbsp;enabled: true<br>&nbsp;&nbsp;https_port: 8766&nbsp;&nbsp;&nbsp;# HTTPS port (keep 8765 for HTTP)<br>&nbsp;&nbsp;data_dir: "."&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;# where ech-ca.crt is stored</code></div>
<p>Restart ECH. The CA cert is generated once and reused across deployments.</p>

<h2>Step 2 — Trust the CA cert (one-time per device)</h2>
<ol>
<li><strong>Windows:</strong> Double-click <code>ech-ca.crt</code> → Install Certificate → Local Machine → Trusted Root Certification Authorities</li>
<li><strong>macOS:</strong> Double-click to import into Keychain, then set Trust to "Always Trust"</li>
<li><strong>Linux / Chrome:</strong> chrome://settings/certificates → Authorities → Import <code>ech-ca.crt</code></li>
<li><strong>Firefox:</strong> Settings → Privacy → View Certificates → Authorities → Import</li>
<li><strong>Android Chrome:</strong> Copy cert to phone → Settings → Security → Install certificate</li>
</ol>

<h2>Step 3 — Connect</h2>
<p>Browse to <code>https://&lt;server-ip&gt;:8766</code> (or <code>https://ech.local:8766</code> if mDNS is working).
The Ham Log page will show a <strong>🔌 Connect Radio</strong> button when Web Serial is available.</p>

<h2>Supported radios (Web Serial CAT)</h2>
<div class="box">
<strong>Icom CI-V protocol</strong> — Xiegu G90 (0x70), IC-7300 (0x94), IC-705 (0x91), IC-9700 (0x98)<br>
<strong>Kenwood text CAT</strong> — Elecraft K3/K4/KX3, TS-590/2000, Yaesu FT-991A (Kenwood emulation)
</div>

<p><a href="/">← Back to ECH</a></p>
</body></html>"""

    # ── CAT radio control (rigctld / Hamlib) ──────────────────────────────

    def _get_cat() -> "CATController | None":
        return getattr(app.state, "cat_ctrl", None)

    @app.get("/api/cat/status")
    async def cat_status():
        cat = _get_cat()
        if cat is None:
            return {"enabled": False, "connected": False, "detail": "CAT not configured"}
        return cat.status()

    @app.post("/api/cat/set_freq")
    async def cat_set_freq(request: Request):
        cat = _get_cat()
        if cat is None:
            return {"status": "error", "detail": "CAT not configured"}
        data = await request.json()
        hz = data.get("hz") or int(float(data.get("mhz", 0)) * 1_000_000)
        if not hz:
            return {"status": "error", "detail": "provide hz or mhz"}
        ok = await cat.set_freq(int(hz))
        return {"status": "ok" if ok else "error", "freq_hz": int(hz),
                "freq_mhz": round(int(hz) / 1e6, 6)}

    @app.post("/api/cat/set_mode")
    async def cat_set_mode(request: Request):
        cat = _get_cat()
        if cat is None:
            return {"status": "error", "detail": "CAT not configured"}
        data = await request.json()
        mode = data.get("mode", "").upper()
        bw   = int(data.get("bw", 0))
        if not mode:
            return {"status": "error", "detail": "mode required"}
        ok = await cat.set_mode(mode, bw)
        return {"status": "ok" if ok else "error", "mode": mode}

    @app.post("/api/cat/set")
    async def cat_set_both(request: Request):
        """Convenience: set freq and mode in one call (e.g. from ham log QSO row)."""
        cat = _get_cat()
        if cat is None:
            return {"status": "error", "detail": "CAT not configured"}
        data = await request.json()
        results: dict = {}
        hz = data.get("hz") or (int(float(data.get("mhz", 0)) * 1_000_000) if data.get("mhz") else None)
        if hz:
            results["freq"] = await cat.set_freq(int(hz))
        mode = data.get("mode", "").upper()
        if mode:
            bw = int(data.get("bw", 0))
            results["mode"] = await cat.set_mode(mode, bw)
        return {"status": "ok" if all(results.values()) else "partial", "results": results}

    # ── Ham Log ───────────────────────────────────────────────────────────

    def _hamlog_config(cfg: dict) -> dict:
        """Extract the hamlog config block, merging defaults."""
        h = cfg.get("hamlog", {}) if cfg else {}
        return {
            "callsign":           h.get("callsign", cfg.get("operator", {}).get("callsign", "N0CALL") if cfg else "N0CALL"),
            "operator":           h.get("operator", ""),
            "grid":               h.get("grid", ""),
            "power":              h.get("power", "LOW"),
            "contest":            h.get("contest", "ARRL-FIELD-DAY"),
            "field_day_class":    h.get("field_day_class", "1A"),
            "field_day_section":  h.get("field_day_section", ""),
            "pota_ref":           h.get("pota_ref", ""),
            "sota_ref":           h.get("sota_ref", ""),
            "qrz_api_key":        h.get("qrz_api_key", ""),
            "clublog_api_key":    h.get("clublog_api_key", ""),
            "clublog_email":      h.get("clublog_email", ""),
            "pota_username":      h.get("pota_username", ""),
            "sota_username":      h.get("sota_username", ""),
        }

    # Load config once at route-definition time via closure
    import yaml as _yaml
    _cfg_path = None
    for _arg in __import__("sys").argv:
        if _arg.endswith(".yaml") or _arg.endswith(".yml"):
            _cfg_path = _arg
            break

    def _load_raw_config() -> dict:
        import yaml
        path = _cfg_path or "config.yaml"
        try:
            with open(path) as f:
                return yaml.safe_load(f) or {}
        except Exception:
            return {}

    @app.get("/api/hamlog/config")
    async def get_hamlog_config():
        cfg = _load_raw_config()
        hc = _hamlog_config(cfg)
        # Don't expose passwords/keys in the GET response
        safe = {k: v for k, v in hc.items()
                if not any(x in k for x in ("key", "password", "username"))}
        safe["qrz_configured"]     = bool(hc.get("qrz_api_key"))
        safe["clublog_configured"]  = bool(hc.get("clublog_api_key") and hc.get("clublog_email"))
        safe["pota_configured"]     = bool(hc.get("pota_username"))
        safe["sota_configured"]     = bool(hc.get("sota_username"))
        return safe

    # Allowed fields operators can update without full admin config access
    _HAMLOG_OPERATOR_FIELDS = {"grid", "callsign", "operator", "field_day_class",
                                "field_day_section", "contest", "power",
                                "pota_ref", "sota_ref"}

    @app.patch("/api/hamlog/config")
    async def patch_hamlog_config(request: Request):
        import yaml
        from pathlib import Path
        body = await request.json()
        allowed = {k: v for k, v in body.items() if k in _HAMLOG_OPERATOR_FIELDS}
        if not allowed:
            return {"status": "ok", "updated": []}
        config_path = Path("/etc/ech/config.yaml")
        try:
            cfg = yaml.safe_load(config_path.read_text()) or {} if config_path.exists() else {}
            if "hamlog" not in cfg:
                cfg["hamlog"] = {}
            cfg["hamlog"].update(allowed)
            config_path.write_text(yaml.dump(cfg, default_flow_style=False, allow_unicode=True))
            return {"status": "ok", "updated": list(allowed.keys())}
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}

    @app.get("/api/hamlog/qsos")
    async def get_hamlog_qsos(
        contest: str | None = None,
        limit: int = Query(1000, le=5000),
        since: str | None = None,
        source: str | None = None,
    ):
        qsos  = await db.get_qsos(contest=contest, limit=limit, since=since, source=source)
        total = await db.get_qso_count(contest=contest)
        return {"qsos": qsos, "total": total}

    @app.post("/api/hamlog/qsos")
    async def add_hamlog_qso(request: Request):
        import uuid
        data = await request.json()
        if not data.get("callsign"):
            return {"status": "error", "detail": "callsign required"}
        if not data.get("band"):
            return {"status": "error", "detail": "band required"}
        if not data.get("mode"):
            return {"status": "error", "detail": "mode required"}
        qso = {
            "id":             data.get("id") or str(uuid.uuid4()),
            "station_id":     str(data.get("station_id", "1")),
            "callsign":       data["callsign"].upper().strip(),
            "band":           data["band"].upper(),
            "mode":           data["mode"].upper(),
            "freq_mhz":       data.get("freq_mhz"),
            "sent_rst":       data.get("sent_rst", "59") or "59",
            "rcvd_rst":       data.get("rcvd_rst", "59") or "59",
            "sent_exch":      data.get("sent_exch", ""),
            "rcvd_exch":      data.get("rcvd_exch", ""),
            "notes":          data.get("notes", ""),
            "timestamp":      data.get("timestamp") or __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
            "source":         data.get("source", "manual"),
            "source_adapter": data.get("source_adapter"),
            "contest":        data.get("contest", "GENERAL"),
            "pota_ref":       data.get("pota_ref", ""),
            "sota_ref":       data.get("sota_ref", ""),
            "name":           data.get("name", ""),
            "power":          data.get("power", ""),
            "state":          data.get("state", ""),
            "country":        data.get("country", ""),
            "county":         data.get("county", ""),
            "time_off":       data.get("time_off", ""),
            "grid":           data.get("grid", ""),
        }
        await db.save_qso(qso)
        await router.broadcast_ws_event("qso", qso)
        log.info("Ham log: QSO added %s on %s/%s (%s)", qso["callsign"], qso["band"], qso["mode"], qso["contest"])
        return {"status": "ok", "qso": qso}

    @app.delete("/api/hamlog/qsos/{qso_id}")
    async def delete_hamlog_qso(qso_id: str):
        ok = await db.delete_qso(qso_id)
        if ok:
            await router.broadcast_ws_event("qso_deleted", {"id": qso_id})
        return {"status": "ok" if ok else "not_found"}

    @app.patch("/api/hamlog/qsos/{qso_id}")
    async def update_hamlog_qso(qso_id: str, request: Request):
        from fastapi import HTTPException
        body = await request.json()
        qso = await db.update_qso(qso_id, body)
        if qso is None:
            raise HTTPException(status_code=404, detail="QSO not found")
        await router.broadcast_ws_event("qso_updated", {"qso": qso})
        return {"status": "ok", "qso": qso}

    @app.get("/api/hamlog/stations")
    async def get_hamlog_stations():
        return {"stations": await db.get_stations()}

    @app.post("/api/hamlog/stations")
    async def upsert_hamlog_station(request: Request):
        data = await request.json()
        sid  = data.get("station_id", "")[:64]
        op   = data.get("operator", "")[:16].upper()
        band = data.get("band", "20M")[:8]
        mode = data.get("mode", "PH")[:8]
        if not sid:
            return {"error": "station_id required"}
        st = await db.upsert_station(sid, op, band, mode)
        await router.broadcast_ws_event("hamlog_station", st)
        return {"status": "ok", "station": st}

    @app.get("/api/hamlog/chat")
    async def get_hamlog_chat(since_id: int = 0, limit: int = 200):
        msgs = await db.get_chat_msgs(since_id=since_id, limit=limit)
        return {"messages": msgs}

    @app.post("/api/hamlog/chat")
    async def send_hamlog_chat(request: Request):
        data = await request.json()
        sid  = data.get("station_id", "")[:64]
        op   = data.get("operator", "?")[:16].upper()
        text = data.get("text", "").strip()[:300]
        if not text:
            return {"error": "text required"}
        msg = await db.save_chat_msg(sid, op, text)
        await router.broadcast_ws_event("hamlog_chat", msg)
        return {"status": "ok", "message": msg}

    @app.get("/api/hamlog/export/{fmt}")
    async def export_hamlog(fmt: str, contest: str | None = None, bonuses: str | None = None):
        import json as _json
        from ech.core.hamlog import format_adif, format_cabrillo, format_pota_csv, format_sota_csv
        qsos = await db.get_qsos(contest=contest, limit=10000)
        cfg  = _hamlog_config(_load_raw_config())
        if contest:
            cfg["contest"] = contest

        bonus_dict: dict = {}
        if bonuses:
            try:
                bonus_dict = _json.loads(bonuses)
            except Exception:
                pass

        fmt = fmt.lower()
        if fmt == "adif":
            content = format_adif(qsos, cfg)
            media   = "text/plain"
            fname   = f"hamlog_{contest or 'all'}.adi"
        elif fmt == "cabrillo":
            content = format_cabrillo(qsos, cfg, bonus_dict or None)
            media   = "text/plain"
            fname   = f"hamlog_{contest or 'all'}.cbr"
        elif fmt == "pota":
            content = format_pota_csv(qsos, cfg)
            media   = "text/csv"
            fname   = "pota_log.csv"
        elif fmt == "sota":
            content = format_sota_csv(qsos, cfg)
            media   = "text/csv"
            fname   = "sota_log.csv"
        else:
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail=f"Unknown format: {fmt}")

        return Response(
            content=content,
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    @app.post("/api/hamlog/upload/{service}")
    async def upload_hamlog(service: str, request: Request):
        from ech.core import hamlog as hl
        data    = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        contest = data.get("contest") or request.query_params.get("contest")
        qsos    = await db.get_qsos(contest=contest, limit=10000)
        if not qsos:
            return {"status": "error", "detail": "No QSOs to upload"}
        cfg  = _hamlog_config(_load_raw_config())
        if contest:
            cfg["contest"] = contest

        service = service.lower()
        if service == "qrz":
            result = await hl.upload_qrz(qsos, cfg)
        elif service == "clublog":
            result = await hl.upload_clublog(qsos, cfg)
        elif service == "pota":
            result = await hl.upload_pota(qsos, cfg)
        elif service == "sota":
            result = await hl.upload_sota(qsos, cfg)
        else:
            return {"status": "error", "detail": f"Unknown service: {service}"}

        if result["status"] == "ok":
            for q in qsos:
                await db.mark_qso_uploaded(q["id"], service)
            log.info("Ham log: uploaded %d QSOs to %s", len(qsos), service)
        return result

    @app.post("/api/hamlog/import")
    async def import_hamlog(request: Request):
        from ech.core.hamlog import import_from_messages
        data    = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        contest = data.get("contest", "GENERAL")
        # Pull recent messages (last 7 days)
        from datetime import datetime, timezone, timedelta
        since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        messages = await db.get_messages(limit=2000, since=since)
        cfg = _hamlog_config(_load_raw_config())
        cfg["contest"] = contest
        new_qsos = import_from_messages(messages, cfg)
        # Deduplicate against existing — skip if same callsign+band+mode already in log
        existing = await db.get_qsos(contest=contest, limit=10000)
        existing_keys = {(q["callsign"], q["band"], q["mode"]) for q in existing}
        added = 0
        for qso in new_qsos:
            key = (qso["callsign"], qso["band"], qso["mode"])
            if key not in existing_keys:
                await db.save_qso(qso)
                await router.broadcast_ws_event("qso", qso)
                existing_keys.add(key)
                added += 1
        return {"status": "ok", "added": added, "skipped": len(new_qsos) - added}

    @app.post("/api/hamlog/import/file")
    async def import_hamlog_file(request: Request):
        """Import QSOs from an uploaded ADIF, Cabrillo, or CSV file."""
        from fastapi import HTTPException as _HE
        form = await request.form()
        file_obj = form.get("file")
        fmt      = str(form.get("format", "adif")).lower().strip()
        contest  = str(form.get("contest", "GENERAL")).strip() or "GENERAL"

        if not file_obj or not hasattr(file_obj, "read"):
            raise _HE(status_code=400, detail="No file provided")

        content = await file_obj.read()
        try:
            text = content.decode("utf-8", errors="replace")
        except Exception:
            text = content.decode("latin-1", errors="replace")

        from ech.core.hamlog import (
            parse_adif_file, adif_to_ech_qso,
            parse_cabrillo_file, cabrillo_to_ech_qso,
            parse_csv_file, csv_to_ech_qso,
        )

        if fmt == "adif":
            raw  = parse_adif_file(text)
            conv = lambda r: adif_to_ech_qso(r, contest)
        elif fmt == "cabrillo":
            raw  = parse_cabrillo_file(text)
            conv = lambda r: cabrillo_to_ech_qso(r, contest)
        elif fmt == "csv":
            raw  = parse_csv_file(text)
            conv = lambda r: csv_to_ech_qso(r, contest)
        else:
            raise _HE(status_code=400, detail=f"Unknown format: {fmt!r} — use adif/cabrillo/csv")

        parsed = [q for r in raw if (q := conv(r))]

        # Deduplicate: same callsign + band + mode + UTC date
        existing = await db.get_qsos(limit=50000)
        existing_keys = {
            (q["callsign"], q["band"], q["mode"], (q["timestamp"] or "")[:10])
            for q in existing
        }
        added = skipped = 0
        for qso in parsed:
            key = (qso["callsign"], qso["band"], qso["mode"], (qso["timestamp"] or "")[:10])
            if key not in existing_keys:
                await db.save_qso(qso)
                await router.broadcast_ws_event("qso", qso)
                existing_keys.add(key)
                added += 1
            else:
                skipped += 1

        log.info("hamlog import: fmt=%s parsed=%d added=%d skipped=%d contest=%s",
                 fmt, len(parsed), added, skipped, contest)
        return {"status": "ok", "added": added, "skipped": skipped, "parsed": len(parsed)}

    def _freq_to_band(khz: float) -> str:
        if khz < 2000:   return "160m"
        if khz < 4000:   return "80m"
        if khz < 5500:   return "60m"
        if khz < 7500:   return "40m"
        if khz < 11000:  return "30m"
        if khz < 15000:  return "20m"
        if khz < 18500:  return "17m"
        if khz < 21500:  return "15m"
        if khz < 25000:  return "12m"
        if khz < 30000:  return "10m"
        if khz < 60000:  return "6m"
        if khz < 150000: return "2m"
        return ""

    _local_spots: list[dict] = []   # in-process store; clears on restart

    @app.get("/api/dx/spots")
    async def get_dx_spots(band: str | None = None, limit: int = 75):
        import httpx as _hx
        from datetime import datetime as _dt
        url = f"https://www.dxsummit.fi/api/v1/spots?limit={min(limit,200)}"
        if band:
            url += f"&band={band.lower().replace('m','m')}"
        try:
            async with _hx.AsyncClient(timeout=10) as client:
                r = await client.get(url, headers={"User-Agent": "ECH/1.0"})
            remote = r.json() if r.status_code == 200 else []
        except Exception as e:
            remote = []
        # Prepend our locally submitted spots (newest first)
        band_filter = (band or "").lower().replace("m", "m")
        local = [s for s in reversed(_local_spots)
                 if not band_filter or s.get("band", "").lower() == band_filter]
        spots = local + remote
        return {"spots": spots, "error": None}

    @app.post("/api/dx/spots")
    async def submit_dx_spot(request: Request):
        import re
        from datetime import datetime as _dt, timezone as _tz
        body = await request.json()
        dx   = (body.get("dx") or "").upper().strip()
        freq = (body.get("freq") or "").strip()
        spotter = (body.get("spotter") or "").upper().strip()
        comment = (body.get("comment") or "").strip()[:80]
        if not dx or not freq:
            return {"error": "dx and freq required"}
        try:
            freq_khz = float(freq)
        except ValueError:
            return {"error": "freq must be numeric kHz"}
        band = _freq_to_band(freq_khz)
        spot = {
            "dx_call":     dx,
            "callsign":    spotter,
            "freq":        freq,
            "band":        band,
            "info":        comment,
            "time":        _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source":      "local",
        }
        _local_spots.append(spot)
        if len(_local_spots) > 200:
            _local_spots.pop(0)
        return {"status": "ok", "spot": spot}

    @app.get("/api/callsign/{cs}")
    async def lookup_callsign(cs: str):
        import httpx as _hx
        import re
        cs = cs.upper().strip()
        if not re.match(r'^[A-Z0-9]{3,10}$', cs):
            return {"error": "invalid callsign"}

        us_prefix = re.match(r'^[WKNA][A-Z0-9]', cs)

        async def try_callook(call):
            async with _hx.AsyncClient(timeout=8) as client:
                r = await client.get(f"https://callook.info/api/{call}/json",
                                     headers={"User-Agent": "ECH/1.0"})
            if r.status_code != 200:
                return None
            d = r.json()
            if d.get("status") != "VALID":
                return None
            cur = d.get("current", {})
            addr = cur.get("address", {})
            loc  = cur.get("location", {})
            name_raw = cur.get("name", "")
            # FCC stores "LASTNAME, FIRSTNAME" or just club name
            if "," in name_raw:
                parts = [p.strip().title() for p in name_raw.split(",", 1)]
                fname = parts[1] if len(parts) > 1 else ""
                lname = parts[0]
                full_name = f"{fname} {lname}".strip()
            else:
                full_name = name_raw.title()
            line2 = addr.get("line2", "")
            state = ""
            if line2:
                m2 = re.search(r',\s*([A-Z]{2})\s', line2)
                if m2:
                    state = m2.group(1)
            return {
                "call":    call,
                "name":    full_name,
                "state":   state,
                "country": "USA",
                "county":  "",
                "grid":    loc.get("gridsquare", ""),
                "lat":     loc.get("latitude", ""),
                "lon":     loc.get("longitude", ""),
                "source":  "callook.info",
            }

        async def try_hamdb(call):
            async with _hx.AsyncClient(timeout=8) as client:
                r = await client.get(f"https://api.hamdb.org/v1/{call}/json/ech-logger",
                                     headers={"User-Agent": "ECH/1.0"})
            if r.status_code != 200:
                return None
            d = r.json().get("hamdb", {}).get("callsign", {})
            if not d or d.get("call", "").upper() != call:
                return None
            fname = d.get("fname", "").strip().title()
            lname = d.get("name", "").strip().title()
            full  = f"{fname} {lname}".strip() if fname or lname else ""
            return {
                "call":    call,
                "name":    full,
                "state":   d.get("state", ""),
                "country": d.get("country", ""),
                "county":  d.get("county", ""),
                "grid":    d.get("grid", ""),
                "lat":     d.get("lat", ""),
                "lon":     d.get("lon", ""),
                "source":  "hamdb.org",
            }

        try:
            result = None
            if us_prefix:
                result = await try_callook(cs)
            if result is None:
                result = await try_hamdb(cs)
            if result is None and us_prefix:
                result = await try_hamdb(cs)
            if result is None:
                return {"error": f"callsign {cs} not found"}
            result["error"] = None
            return result
        except Exception as e:
            return {"error": str(e)}

    @app.get("/api/propagation")
    async def get_propagation():
        import re
        import httpx as _hx
        try:
            async with _hx.AsyncClient(timeout=12) as client:
                r = await client.get("https://www.hamqsl.com/solarxml.php",
                                     headers={"User-Agent": "ECH/1.0"})
            text = r.text

            def tag(*names, default=""):
                for name in names:
                    m = re.search(
                        rf"<{re.escape(name)}[^>]*>\s*([^<]*?)\s*</{re.escape(name)}>",
                        text, re.IGNORECASE
                    )
                    if m and m.group(1).strip():
                        return m.group(1).strip()
                return default

            bands = {}
            # Try both formats: <band...><condition>X</condition></band>
            #                   <band...>X</band>
            for pat in [
                r'<band\s+[^>]*name="([^"]+)"[^>]*time="([^"]+)"[^>]*>\s*<condition>\s*([^<]+?)\s*</condition>',
                r'<band\s+[^>]*time="([^"]+)"[^>]*name="([^"]+)"[^>]*>\s*<condition>\s*([^<]+?)\s*</condition>',
                r'<band\s+[^>]*name="([^"]+)"[^>]*time="([^"]+)"[^>]*>\s*([A-Za-z][^<]*?)\s*</band>',
                r'<band\s+[^>]*time="([^"]+)"[^>]*name="([^"]+)"[^>]*>\s*([A-Za-z][^<]*?)\s*</band>',
            ]:
                for m in re.finditer(pat, text, re.DOTALL | re.IGNORECASE):
                    g1, g2, g3 = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
                    # First two patterns: g1=name, g2=time; swapped patterns: g1=time, g2=name
                    if pat.startswith(r'<band\s+[^>]*name'):
                        bname, btime, cond = g1, g2, g3
                    else:
                        btime, bname, cond = g1, g2, g3
                    if bname not in bands:
                        bands[bname] = {}
                    bands[bname][btime] = cond
                if bands:
                    break

            return {
                "sfi":         tag("SFI", "solarflux"),
                "sunspots":    tag("sunspots"),
                "aindex":      tag("aindex"),
                "kindex":      tag("kindex"),
                "xray":        tag("xray"),
                "geomagfield": tag("geomagfield"),
                "signalnoise": tag("signalnoise"),
                "solarwind":   tag("solarwind"),
                "created":     tag("created", "updated"),
                "bands":       bands,
                "error":       None,
            }
        except Exception as e:
            return {"error": str(e), "bands": {}}

    return app
