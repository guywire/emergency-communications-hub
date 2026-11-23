"""
AREDN cameras: the cameras and video streams advertised as services on the
mesh, checked once a day and placed at the node that advertises them.

Source: the mesh-wide service list in a local AREDN node's sysinfo
(?services=1&hosts=1, same URL as the MeshPhone map). A service counts as a
camera when its owner tagged it [camera] or [video], or its name / link
looks like one (cam, rtsp://, snapshot, NVR, motionEye, Frigate ...).

Each camera gets one check per refresh, nothing more than a viewer would do:
  * http/https link: one GET without credentials (beyond any the owner put in
    the advertised link). 200 image = snapshot, multipart = live MJPEG,
    HTML = a web page (software named from the page where we can), a password
    field or 401 = password needed.
  * rtsp link: an RTSP DESCRIBE. 200 = open stream (VLC), 401 = password.
  * a dedicated camera host (not the AREDN node itself): a TCP connect to the
    handful of ports camera apps use (RTSP 554/8554, Hikvision 8000, Dahua
    37777, Reolink 9000, XMEye 34567), so the map can say which app views it.

Config (radio_directory.aredn_cameras, all optional):
  enabled         bool   default true
  refresh_hours   float  default 24 (at least 6)
  hosts_url       str    default radio_directory.meshphone.hosts_url, else
                         radio_directory.aredn_discovery.sysinfo_url
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

REFRESH_S = 24 * 3600
FIRST_DELAY_S = 300
RETRY_S = 1800
MAX_CAMERAS = 400
_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_TAG_RE = re.compile(r"\[(\w+)\]\s*$")
_CAM_RE = re.compile(r"\bcam\b|cam\d|-cam|cam-|camera|webcam|ipcam|snapshot|\bsnap\b|nvr\b|\bdvr\b|motioneye|frigate|"
                     r"blue ?iris|zoneminder|reolink|hikvision|dahua|amcrest|\bptz\b|videoproxy|surveil", re.I)
_NOT_CAM_TAGS = {"phone", "radio", "router", "switch", "winlink", "mail", "chat", "time", "wiki", "power",
                 "solar", "battery", "internet"}

# port -> (what listens there, what to view it with)
CAMERA_PORTS = {554: ("RTSP", "VLC or any RTSP player"),
                8554: ("RTSP", "VLC or any RTSP player"),
                8000: ("Hikvision SDK", "Hik-Connect / iVMS-4200"),
                37777: ("Dahua TCP", "DMSS / SmartPSS"),
                9000: ("Reolink media", "Reolink app / client"),
                34567: ("XMEye / NETSurveillance", "XMEye / VMS")}

# (pattern in headers / page, software, how to view)
_SOFTWARE = [
    (r"motioneye", "motionEye", "browser"),
    (r"frigate", "Frigate NVR", "browser"),
    (r"blue ?iris", "Blue Iris", "browser or Blue Iris app"),
    (r"zoneminder", "ZoneMinder", "browser or zmNinja"),
    (r"unifi|protect", "UniFi Protect", "UniFi Protect app"),
    (r"reolink", "Reolink", "Reolink app / client (or browser)"),
    (r"hikvision|doc/page/login|webcomponents|isapi", "Hikvision", "Hik-Connect / iVMS-4200 (or browser)"),
    (r"dahua|\bweb service\b|/RPC2", "Dahua", "DMSS / SmartPSS (or browser)"),
    (r"amcrest", "Amcrest", "Amcrest View Pro (or browser)"),
    (r"axis", "Axis", "browser or AXIS Camera Station"),
    (r"foscam", "Foscam", "Foscam app (or browser)"),
    (r"webrtcapp|liveapp|ant ?media", "Ant Media Server", "browser (WebRTC)"),
    (r"/a/videoproxy", "AREDN video proxy", "browser"),
    (r"go2rtc", "go2rtc", "browser"),
    (r"mediamtx|rtsp-simple-server", "MediaMTX", "browser or VLC"),
]


def is_camera(service: dict) -> bool:
    name, link = str(service.get("name") or ""), str(service.get("link") or "")
    m = _TAG_RE.search(name)
    tag = m.group(1).lower() if m else ""
    if tag in ("camera", "video"):
        return True
    if tag in _NOT_CAM_TAGS:
        return False
    return bool(link.lower().startswith("rtsp://") or _CAM_RE.search(name) or _CAM_RE.search(link))


def camera_services(services: list[dict]) -> list[dict]:
    out, seen = [], set()
    for s in services:
        link = str(s.get("link") or "")
        if not is_camera(s) or not re.match(r"(https?|rtsp)://", link, re.I) or link in seen:
            continue
        seen.add(link)
        out.append(s)
    return out[:MAX_CAMERAS]


def link_target(link: str, hosts: list[dict]) -> tuple[str, str, int]:
    """(scheme, ip, port) the advertised link points at; ip "" if unknown."""
    u = urlsplit(link)
    scheme = (u.scheme or "").lower()
    host = (u.hostname or "").lower().removesuffix(".local.mesh")
    try:
        port = u.port or {"http": 80, "https": 443, "rtsp": 554}.get(scheme, 0)
    except ValueError:
        port = 0
    if _IP_RE.match(host):
        return scheme, host, port
    for h in hosts:
        if str(h.get("name") or "").lower() == host:
            return scheme, str(h.get("ip") or ""), port
    return scheme, "", port


def software_from(text: str) -> tuple[str, str]:
    for pat, sw, view in _SOFTWARE:
        if re.search(pat, text or "", re.I):
            return sw, view
    return "", ""


def classify_http(status: int, ctype: str, body: str, headers_text: str, url: str) -> dict:
    """What a GET of the advertised link tells a viewer."""
    ctype = (ctype or "").lower()
    sw, view = software_from(" ".join((url, headers_text, body[:20000])))
    if status == 401:
        return {"status": "password", "kind": "login", "software": sw, "view": view or "browser (with the password)"}
    if status in (403,):
        return {"status": "blocked", "kind": "forbidden", "software": sw, "view": view}
    if 200 <= status < 300:
        if ctype.startswith("image/"):
            return {"status": "open", "kind": "snapshot", "software": sw, "view": "browser (still image)"}
        if "multipart/x-mixed-replace" in ctype:
            return {"status": "open", "kind": "mjpeg", "software": sw, "view": "browser (live MJPEG)"}
        if re.search(r"<input[^>]+type=[\"']?password", body, re.I):
            return {"status": "password", "kind": "login", "software": sw, "view": view or "browser (with the password)"}
        if re.search(r"<video|<img|webrtc|hls|\.m3u8|mjpe?g", body, re.I) or sw:
            return {"status": "open", "kind": "page", "software": sw, "view": view or "browser"}
        return {"status": "open", "kind": "page", "software": sw, "view": view or "browser"}
    if 300 <= status < 400:
        if re.search(r"location:\s*\S*(login|signin|sign-in|auth)", headers_text, re.I):
            return {"status": "password", "kind": "login", "software": sw, "view": view or "browser (with the password)"}
        return {"status": "open", "kind": "redirect", "software": sw, "view": view or "browser"}
    return {"status": "error", "kind": f"http {status}", "software": sw, "view": view}


def describe_request(link: str) -> bytes:
    return (f"DESCRIBE {link} RTSP/1.0\r\nCSeq: 2\r\nAccept: application/sdp\r\n"
            "User-Agent: SignalMatrix\r\n\r\n").encode()


async def rtsp_check(ip: str, port: int, link: str, timeout: float = 6.0) -> dict:
    t0 = time.monotonic()
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
    except (OSError, asyncio.TimeoutError):
        return {"status": "closed", "kind": "rtsp", "view": "VLC or any RTSP player"}
    try:
        w.write(describe_request(link))
        await w.drain()
        line = await asyncio.wait_for(r.readline(), timeout)
    except (OSError, asyncio.TimeoutError):
        line = b""
    finally:
        w.close()
    ms = round((time.monotonic() - t0) * 1000)
    m = re.match(rb"RTSP/1\.\d (\d{3})", line or b"")
    code = int(m.group(1)) if m else 0
    out = {"kind": "rtsp", "view": "VLC or any RTSP player", "ms": ms}
    if code == 200:
        out["status"] = "open"
    elif code == 401:
        out["status"] = "password"
    elif code:
        out["status"], out["kind"] = "error", f"rtsp {code}"
    else:
        out["status"] = "no_reply"           # port open, didn't speak RTSP to us
    return out


async def port_open(ip: str, port: int, timeout: float = 3.0) -> bool:
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    w.close()
    return True


def mask_secrets(link: str) -> str:
    """The advertised link for display: credentials in it masked."""
    s = re.sub(r"(?i)((?:password|pwd|pass)=)[^&]*", r"\1***", link)
    return re.sub(r"(//[^/:@]+:)[^@/]+@", r"\1***@", s)


class ArednCameras:
    def __init__(self) -> None:
        self.data: dict | None = None
        self.last_error: str | None = None
        self.enabled = True
        self._config: dict = {}
        self._task: asyncio.Task | None = None
        self.cache_path: str | None = None
        self.refresh_s = REFRESH_S
        self.running = False

    def configure(self, config: dict) -> None:
        self._config = config or {}
        rd = self._config.get("radio_directory") or {}
        cfg = rd.get("aredn_cameras") or {}
        self.enabled = bool(cfg.get("enabled", True))
        try:
            self.refresh_s = max(6 * 3600, int(float(cfg.get("refresh_hours", REFRESH_S / 3600)) * 3600))
        except (TypeError, ValueError):
            self.refresh_s = REFRESH_S
        db_path = (self._config.get("database") or {}).get("path", "ech.db")
        base = os.path.dirname(os.path.abspath(db_path)) if db_path != ":memory:" else os.getcwd()
        self.cache_path = os.path.join(base, "aredn_cameras.json")
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and "cameras" in d:
                self.data = d
        except (OSError, ValueError):
            pass

    def _hosts_url(self) -> str:
        rd = self._config.get("radio_directory") or {}
        return str((rd.get("aredn_cameras") or {}).get("hosts_url")
                   or (rd.get("meshphone") or {}).get("hosts_url")
                   or (rd.get("aredn_discovery") or {}).get("sysinfo_url") or "")

    async def start(self, config: dict) -> None:
        self.configure(config)
        if self.enabled and self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        age = time.time() - float((self.data or {}).get("generated_at") or 0)
        await asyncio.sleep(max(FIRST_DELAY_S, self.refresh_s - age))
        while True:
            wait = self.refresh_s
            try:
                await self.refresh()
            except Exception as exc:
                self.last_error = f"{exc.__class__.__name__}: {exc}"
                log.warning("AREDN cameras refresh failed (retrying in 30 min): %s", self.last_error)
                wait = RETRY_S
            await asyncio.sleep(wait)

    async def _check(self, client, s: dict, hosts: list[dict], probe_ports: bool = True) -> dict:
        link = str(s.get("link") or "")
        scheme, ip, port = link_target(link, hosts)
        res: dict = {"status": "unreachable" if not ip else "", "kind": "", "software": "", "view": ""}
        if ip and scheme == "rtsp":
            res.update(await rtsp_check(ip, port, link))
        elif ip:
            # by IP with the advertised name as Host: this box may not resolve .local.mesh
            u = urlsplit(link)
            host = u.hostname or ""
            url = link.replace(f"//{u.netloc}", f"//{ip}:{port}", 1) if "@" not in u.netloc else link
            hdr = {"Host": host if port in (80, 443) else f"{host}:{port}"}
            t0 = time.monotonic()
            for attempt in (url, re.sub(r"^https://", "http://", url) if port == 80 else None):
                if not attempt:
                    continue
                try:
                    async with client.stream("GET", attempt, timeout=10, headers=hdr, follow_redirects=False) as r:
                        body = b""
                        ctype = r.headers.get("content-type", "")
                        if not ctype.lower().startswith(("image/", "multipart/")):
                            async for chunk in r.aiter_bytes():
                                body += chunk
                                if len(body) > 65536:
                                    break
                        hdrs = " ".join(f"{k}: {v}" for k, v in r.headers.items()
                                        if k.lower() in ("server", "www-authenticate", "location"))
                        res.update(classify_http(r.status_code, ctype, body.decode("utf-8", "replace"), hdrs,
                                                 str(r.url)))
                        res["ms"] = round((time.monotonic() - t0) * 1000)
                    break
                except Exception as exc:
                    res["status"] = "closed" if "refused" in str(exc).lower() else "no_reply"
                    res["kind"] = scheme
        # a dedicated camera host: which camera-app ports it answers on
        ports = []
        if probe_ports and ip and ip != s.get("ip"):
            opens = await asyncio.gather(*(port_open(ip, p) for p in CAMERA_PORTS if p != port))
            ports = [p for p, ok in zip([p for p in CAMERA_PORTS if p != port], opens) if ok]
        apps = []
        for p in ports:
            if CAMERA_PORTS[p][1] not in apps and not (res.get("view") or "").startswith(CAMERA_PORTS[p][1]):
                apps.append(CAMERA_PORTS[p][1])
        name = _TAG_RE.sub("", str(s.get("name") or "")).strip()[:80]
        m = _TAG_RE.search(str(s.get("name") or ""))
        return {"name": name, "tag": m.group(1).lower() if m else "", "link": mask_secrets(link)[:300],
                "href": link[:500], "scheme": scheme, "ip": ip, "port": port, "node_ip": s.get("ip") or "",
                "open_ports": ports, "other_apps": apps, **res}

    async def refresh(self) -> dict | None:
        import httpx
        from ech.core.meshphone_map import service as mp_map
        url = self._hosts_url()
        if not url:
            raise RuntimeError("set radio_directory.meshphone.hosts_url (or aredn_discovery.sysinfo_url)")
        for q in ("hosts=1", "services=1"):
            if q not in url:
                url += ("&" if "?" in url else "?") + q
        self.running = True
        try:
            async with httpx.AsyncClient(follow_redirects=True, verify=False,
                                         headers={"User-Agent": "SignalMatrix"}) as client:
                j = (await client.get(url, timeout=60)).json()
                hosts = [h for h in j.get("hosts") or [] if isinstance(h, dict)]
                cams = camera_services([s for s in j.get("services") or [] if isinstance(s, dict)])
                sem = asyncio.Semaphore(12)

                async def one(s):
                    async with sem:
                        return await self._check(client, s, hosts)
                checked = await asyncio.gather(*(one(s) for s in cams))
                node_names = {str(h.get("ip") or ""): str(h.get("name") or "") for h in hosts}
                sem2 = asyncio.Semaphore(8)

                async def ninfo(ip):
                    async with sem2:
                        return ip, await mp_map._node_info(client, ip)
                nodes = dict(await asyncio.gather(*(ninfo(ip) for ip in {c["node_ip"] for c in checked if c["node_ip"]})))
                for n in nodes.values():
                    if n.get("lat") is not None:
                        n["place"] = await mp_map._place(client, n["lat"], n["lon"])
        finally:
            self.running = False
        out = []
        for c in checked:
            n = nodes.get(c["node_ip"]) or {}
            out.append({**c, "node": n.get("name") or node_names.get(c["node_ip"], ""),
                        "lat": n.get("lat"), "lon": n.get("lon"), "grid": n.get("grid", ""),
                        "place": n.get("place", "")})
        counts: dict[str, int] = {}
        for c in out:
            counts[c["status"] or "unknown"] = counts.get(c["status"] or "unknown", 0) + 1
        data = {"generated_at": time.time(), "cameras": out,
                "summary": {"cameras": len(out), "located": sum(1 for c in out if c["lat"] is not None),
                            "status": counts}}
        self.data, self.last_error = data, None
        log.info("AREDN cameras: %d advertised, %d located, %s", len(out), data["summary"]["located"], counts)
        if self.cache_path:
            try:
                tmp = self.cache_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                os.replace(tmp, self.cache_path)
            except OSError as exc:
                log.debug("AREDN cameras: cache write failed: %s", exc)
        return data


service = ArednCameras()
