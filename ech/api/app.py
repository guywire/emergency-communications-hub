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
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ech.core.models import Priority

log = logging.getLogger(__name__)

# PSKReporter asks for no more than 1 request per 5 minutes per IP.
_psk_cache: dict = {}          # key → (timestamp, payload)
_PSK_CACHE_TTL = 300           # seconds

UI_DIR = Path(__file__).parent.parent / "ui"


def create_app(router, db, anomaly_engine=None, wx_service=None, auth=None, ech_state=None, mc_bridge=None) -> FastAPI:
    app = FastAPI(title="Emergency Communications Hub", version="0.1.0")

    # ── Auth middleware ───────────────────────────────────────────────────
    # Public paths that don't require login
    PUBLIC_PATHS = {"/login", "/api/auth/login", "/api/health", "/ws",
                    "/static", "/favicon.ico", "/metrics"}

    @app.middleware("http")
    async def auth_middleware(request, call_next):
        path = request.url.path
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
        return await call_next(request)

    # ── Static files ──────────────────────────────────────────────────────
    static_dir = UI_DIR / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # ── WebSocket ─────────────────────────────────────────────────────────

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
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

    @app.get("/", response_class=HTMLResponse)
    async def index():
        template = UI_DIR / "templates" / "index.html"
        return HTMLResponse(content=template.read_text())

    # ── Messages ──────────────────────────────────────────────────────────

    @app.get("/api/messages")
    async def get_messages(
        limit: int = Query(100, le=500),
        offset: int = 0,
        adapter: str | None = None,
        since: str | None = None,
        priority_min: int | None = None,
    ):
        msgs = await db.get_messages(
            limit=limit, offset=offset,
            adapter=adapter, since=since,
            priority_min=priority_min,
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
        results = await router.send(
            body=body,
            adapter_names=adapters,
            to_id=to_id,
            priority=priority,
        )
        return {"results": results}

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
    async def broadcast_weather(adapters: list[str] | None = None):
        if not wx_service:
            return {"status": "error", "detail": "Weather service not configured"}
        ok = await wx_service.broadcast_wx_summary(adapter_names=adapters)
        return {"status": "ok" if ok else "partial"}

    @app.post("/api/time-sync")
    async def time_sync_broadcast(adapters: list[str] | None = None,
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
        return {"status": "ok" if ok else "partial"}

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
            return HTMLResponse(content=template.read_text())
        return HTMLResponse(content="<h1>Anomaly dashboard not found</h1>")

    # ── Auth ──────────────────────────────────────────────────────────────

    @app.get("/login", response_class=HTMLResponse)
    async def login_page():
        template = UI_DIR / "templates" / "login.html"
        return HTMLResponse(content=template.read_text() if template.exists() else "<h1>Login</h1>")

    @app.post("/api/auth/login")
    async def do_login(request: Request):
        from ech.core.auth import SESSION_COOKIE, SESSION_EXPIRE_HOURS
        from fastapi.responses import JSONResponse, RedirectResponse

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

        token = await auth.login(username, password) if auth else None
        if not token:
            if use_redirect:
                return RedirectResponse(url="/login?error=1", status_code=303)
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="Invalid credentials")

        if use_redirect:
            resp = RedirectResponse(url=next_url or "/", status_code=303)
        else:
            resp = JSONResponse({"status": "ok", "username": username})

        resp.set_cookie(
            SESSION_COOKIE, token,
            max_age=SESSION_EXPIRE_HOURS * 3600,
            httponly=True,
            samesite="lax",
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
        try:
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            cfg["adapters"] = data.get("adapters", cfg.get("adapters", []))
            if "bridge_rules" in data:
                cfg["bridge_rules"] = data["bridge_rules"]
            with open(config_path, "w") as f:
                yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
            return {"status": "ok", "note": "Restart ECH to apply adapter changes"}
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}

    # ── Base location ──────────────────────────────────────────────────────

    @app.post("/api/base-location")
    async def set_base_location(request: Request):
        """Persist base lat/lon to config and patch all mock adapters."""
        import yaml
        from pathlib import Path
        data = await request.json()
        lat = float(data.get("lat", 0))
        lon = float(data.get("lon", 0))
        config_path = Path("/etc/ech/config.yaml")
        if not config_path.exists():
            config_path = Path("config.yaml")
        try:
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            for adapter in cfg.get("adapters", []):
                if adapter.get("type", "").startswith("mock_"):
                    adapter["base_lat"] = lat
                    adapter["base_lon"] = lon
            if "operator" not in cfg:
                cfg["operator"] = {}
            cfg["operator"]["base_lat"] = lat
            cfg["operator"]["base_lon"] = lon
            with open(config_path, "w") as f:
                yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
            return {"status": "ok", "lat": lat, "lon": lon,
                    "note": "Restart ECH to reposition mock adapter nodes"}
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}

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
        return {"logs": await db.get_log_entries(limit=limit, level=level)}

    @app.get("/logs", response_class=HTMLResponse)
    async def logs_page():
        template = UI_DIR / "templates" / "logs.html"
        return HTMLResponse(content=template.read_text() if template.exists() else "<h1>Logs</h1>")

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page():
        template = UI_DIR / "templates" / "settings.html"
        return HTMLResponse(content=template.read_text() if template.exists() else "<h1>Settings</h1>")

    @app.get("/simulation", response_class=HTMLResponse)
    async def simulation_page():
        template = UI_DIR / "templates" / "simulation.html"
        return HTMLResponse(content=template.read_text() if template.exists() else "<h1>Simulation</h1>")

    @app.get("/map", response_class=HTMLResponse)
    async def map_page():
        template = UI_DIR / "templates" / "map.html"
        return HTMLResponse(content=template.read_text() if template.exists() else "<h1>Map</h1>")

    # ── System stats ─────────────────────────────────────────────────────

    @app.get("/api/system/services")
    async def system_services():
        """
        Check status of all ECH-related system services via systemctl.
        Returns list of {name, status, active, enabled, description} dicts.
        """
        import asyncio as _aio
        services = [
            {"name": "ech",        "label": "ECH",          "port": "8765"},
            {"name": "pat",        "label": "Pat (Winlink)", "port": "8080"},
            {"name": "asterisk",   "label": "Asterisk PBX", "port": "5060"},
            {"name": "mosquitto",  "label": "MQTT Broker",  "port": "1883"},
            {"name": "prometheus", "label": "Prometheus",   "port": "9090"},
            {"name": "avahi-daemon","label": "mDNS (avahi)", "port": "mdns"},
        ]
        results = []
        for svc in services:
            name = svc["name"]
            try:
                # is-active
                # Use full path; systemctl may not be in uvicorn's PATH
                systemctl = "/bin/systemctl"
                import os
                if not os.path.exists(systemctl):
                    systemctl = "/usr/bin/systemctl"

                p = await _aio.create_subprocess_exec(
                    systemctl, "is-active", name,
                    stdout=_aio.subprocess.PIPE,
                    stderr=_aio.subprocess.PIPE,
                )
                stdout, _ = await _aio.wait_for(p.communicate(), timeout=5.0)
                active = stdout.decode().strip() or "unknown"

                p2 = await _aio.create_subprocess_exec(
                    systemctl, "is-enabled", name,
                    stdout=_aio.subprocess.PIPE,
                    stderr=_aio.subprocess.PIPE,
                )
                stdout2, _ = await _aio.wait_for(p2.communicate(), timeout=5.0)
                enabled = stdout2.decode().strip() or "unknown"

                results.append({
                    **svc,
                    "active":  active,
                    "enabled": enabled,
                    "ok": active == "active",
                })
            except Exception as exc:
                results.append({
                    **svc,
                    "active":  "error",
                    "enabled": "unknown",
                    "ok": False,
                    "error": str(exc),
                })
        return {"services": results}

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
        data = await request.json()
        if mc_bridge:
            mc_bridge.enabled = bool(data.get("enabled", True))
        return {"status": "ok", "enabled": mc_bridge.enabled if mc_bridge else False}

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
                return {"spots": spots[:limit], "total": len(spots), "cached": False}
        except Exception as exc:
            return {"spots": [], "error": str(exc)}

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

    # ── Health check ──────────────────────────────────────────────────────

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    return app
