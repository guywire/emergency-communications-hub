"""AREDN cameras: picking cameras from the mesh service list and reading a check (data made up)."""
import asyncio

from ech.core import aredn_cameras as ac

HOSTS = [{"name": "N0CAM-NODE", "ip": "10.1.0.1"}, {"name": "n0cam-ptz", "ip": "10.1.0.5"},
         {"name": "VIDEO-SRV-1", "ip": "10.2.0.9"}]


def test_which_services_are_cameras():
    svcs = [{"name": "Yard [camera]", "link": "http://n0cam-ptz:80/", "ip": "10.1.0.1"},
            {"name": "Stream [video]", "link": "rtsp://VIDEO-SRV-1:8554/LOW", "ip": "10.2.0.1"},
            {"name": "Cambium to City Hall [radio]", "link": "http://w6yx-cambium:80/", "ip": "10.3.0.1"},
            {"name": "OHSU Cam1 Still Picture", "link": "http://MCS7-motioneye:9083/current/", "ip": "10.4.0.1"},
            {"name": "MeshChat [chat]", "link": "http://n0cam-node:8080/meshchat", "ip": "10.1.0.1"},
            {"name": "Yard again [camera]", "link": "http://n0cam-ptz:80/", "ip": "10.1.0.1"},
            {"name": "Odd [camera]", "link": "javascript:alert(1)", "ip": "10.1.0.1"}]
    names = [s["name"] for s in ac.camera_services(svcs)]
    assert names == ["Yard [camera]", "Stream [video]", "OHSU Cam1 Still Picture"]


def test_link_target_resolves_mesh_names_and_default_ports():
    assert ac.link_target("http://n0cam-ptz.local.mesh/", HOSTS) == ("http", "10.1.0.5", 80)
    assert ac.link_target("rtsp://VIDEO-SRV-1:8554/LOW", HOSTS) == ("rtsp", "10.2.0.9", 8554)
    assert ac.link_target("rtsp://10.9.9.9/s0", HOSTS) == ("rtsp", "10.9.9.9", 554)
    assert ac.link_target("http://nowhere-cam/", HOSTS)[1] == ""


def test_classify_http():
    assert ac.classify_http(200, "image/jpeg", "", "", "http://x/snap.jpg")["kind"] == "snapshot"
    assert ac.classify_http(200, "multipart/x-mixed-replace; boundary=x", "", "", "http://x/")["kind"] == "mjpeg"
    r = ac.classify_http(401, "text/html", "", 'WWW-Authenticate: Digest realm="Login to 4L0123"', "http://x/")
    assert r["status"] == "password"
    r = ac.classify_http(200, "text/html", '<title>motionEye</title><input type="password">', "", "http://x/")
    assert r["status"] == "password" and r["software"] == "motionEye"
    r = ac.classify_http(200, "text/html", "<script src='/doc/page/login.asp'>", "", "http://x/")
    assert r["software"] == "Hikvision" and "Hik-Connect" in r["view"]
    # Reolink's page is a script app that draws its login form in the browser
    r = ac.classify_http(200, "text/html", "<title></title><script src='js/reolink.min.js'></script>", "", "https://cam/")
    assert (r["status"], r["software"]) == ("password", "Reolink")
    assert ac.classify_http(200, "text/html", "<title>Camera Login</title><div id=app>", "", "http://x/")["status"] == "password"
    assert ac.classify_http(200, "text/html", "<title>Live view</title><video>", "", "http://x/")["status"] == "open"
    assert ac.classify_http(500, "text/html", "", "", "http://x/")["status"] == "error"
    assert ac.classify_http(302, "", "", "location: /login.html", "http://x/")["status"] == "password"
    assert ac.classify_http(302, "", "", "location: /live/", "http://x/")["status"] == "open"


def test_mask_secrets():
    link = "http://cam:80/cgi-bin/api.cgi?cmd=Snap&user=guest1&password=guest1"
    assert ac.mask_secrets(link).endswith("user=guest1&password=***")
    assert ac.mask_secrets("rtsp://admin:hunter2@10.1.1.1/s0") == "rtsp://admin:***@10.1.1.1/s0"


def test_rtsp_check_against_a_local_server():
    async def run():
        async def handle(r, w):
            req = await r.readuntil(b"\r\n\r\n")
            w.write(b"RTSP/1.0 401 Unauthorized\r\nCSeq: 2\r\n\r\n" if b"/private" in req
                    else b"RTSP/1.0 200 OK\r\nCSeq: 2\r\n\r\n")
            await w.drain()
            w.close()
        srv = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            ok = await ac.rtsp_check("127.0.0.1", port, f"rtsp://127.0.0.1:{port}/s0", timeout=2)
            pw = await ac.rtsp_check("127.0.0.1", port, f"rtsp://127.0.0.1:{port}/private", timeout=2)
            assert await ac.port_open("127.0.0.1", port)
        finally:
            srv.close()
        closed = await ac.rtsp_check("127.0.0.1", 1, "rtsp://127.0.0.1:1/x", timeout=1)
        return ok, pw, closed
    ok, pw, closed = asyncio.run(run())
    assert ok["status"] == "open" and pw["status"] == "password" and closed["status"] == "closed"


def test_http_check_goes_by_ip_with_the_mesh_name_as_host():
    seen = {}

    class Resp:
        status_code, url = 200, "http://10.1.0.5:80/snap.jpg"
        headers = {"content-type": "image/jpeg"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Client:
        def stream(self, method, url, **kw):
            seen.update(url=url, **kw)
            return Resp()
    svc = {"name": "Yard [camera]", "link": "http://n0cam-ptz:80/snap.jpg", "ip": "10.1.0.1"}
    old = ac.CAMERA_PORTS
    ac.CAMERA_PORTS = {}
    try:
        c = asyncio.run(ac.ArednCameras()._check(Client(), svc, HOSTS))
    finally:
        ac.CAMERA_PORTS = old
    assert seen["url"] == "http://10.1.0.5:80/snap.jpg" and seen["headers"] == {"Host": "n0cam-ptz"}
    assert seen["follow_redirects"] is False
    assert (c["status"], c["kind"], c["ip"]) == ("open", "snapshot", "10.1.0.5")


def test_rtsp_probe_finds_an_open_stream_on_the_brands_path():
    async def run():
        seen = []

        async def handle(r, w):
            req = await r.readuntil(b"\r\n\r\n")
            seen.append(req.split(b" ")[1].decode())
            w.write(b"RTSP/1.0 200 OK\r\nCSeq: 2\r\n\r\n" if b"/h264Preview_01_main" in req
                    else b"RTSP/1.0 404 Not Found\r\nCSeq: 2\r\n\r\n")
            await w.drain()
            w.close()
        srv = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            found = await ac.rtsp_probe("127.0.0.1", port, "Reolink")
        finally:
            srv.close()
        return found, seen
    found, seen = asyncio.run(run())
    assert found == {"port": found["port"], "path": "/h264Preview_01_main", "status": "open"}
    assert len(seen) == 1                                   # stops at the first stream that plays


def test_rtsp_probe_reports_a_password_and_tries_at_most_three_paths():
    async def run():
        n = []

        async def handle(r, w):
            await r.readuntil(b"\r\n\r\n")
            n.append(1)
            w.write(b"RTSP/1.0 401 Unauthorized\r\nCSeq: 2\r\n\r\n")
            await w.drain()
            w.close()
        srv = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            found = await ac.rtsp_probe("127.0.0.1", port, "")
        finally:
            srv.close()
        return found, len(n)
    found, tries = asyncio.run(run())
    assert found["status"] == "password" and found["path"] == "/" and tries == ac.MAX_RTSP_TRIES



def test_a_region_check_counts_its_requests_and_bytes(monkeypatch):
    import asyncio
    import httpx
    body = b"x" * 5000

    async def handle(self, request):
        return httpx.Response(200, headers={"content-type": "text/plain"}, stream=httpx.ByteStream(body))
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)

    async def run():
        t = {"requests": 0, "bytes": 0}
        tok = ac.TRAFFIC.set(t)
        try:
            async with httpx.AsyncClient(transport=ac.counting_transport()) as c:
                for _ in range(3):
                    r = await c.get("http://10.1.2.3/")
                    assert r.content == body
            ac.note_traffic(1, 48)                     # e.g. an NTP request
        finally:
            ac.TRAFFIC.reset(tok)
        ac.note_traffic(5, 999)                        # no check running: not counted
        return t
    t = asyncio.run(run())
    assert t["requests"] == 4 and 3 * 5000 + 48 < t["bytes"] < 3 * 5000 + 48 + 3 * 400
