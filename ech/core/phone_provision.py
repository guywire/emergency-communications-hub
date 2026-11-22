"""Push ECH settings (phone directory, voicemail key, apps) to desk phones.

Yealink Android phones (T67/T58/T56/...) have a JSON web API behind an RSA
login: the same calls their own web UI makes. ECH logs in as the phone's
admin and

  * exports the phone's full config ("All Settings", passwords left out by
    the phone itself) and keeps it as a backup before every change,
  * builds a small .cfg holding only ECH's keys, reusing what the phone
    already has (which account is the ECH line, which remote-phonebook
    slots are free), and
  * imports it the way "Settings > Configuration > Import CFG" does.

The phone's own provisioning server is left alone on purpose: a carrier
phone (Verizon One Talk) is re-provisioned weekly from the carrier's server,
and pointing it at ECH would cut that off. Settings imported through the web
UI are kept across that re-provisioning (static.auto_provision.custom.protect).

Importing a cfg makes the phone restart (~40 s). Directory *contents* never
need a push: the phone re-reads /phonebook.xml on its own, at least every
features.remote_phonebook.flash_time seconds.

Config (config.yaml, live only: holds the phone's admin password):

  pbx_phones:
    phones:
      - name: Desk phone
        host: 192.0.2.36
        username: admin
        password: "..."
    http_port: 8766          # ECH's plain-HTTP port the phones can reach
    flash_time: 3600         # directory refresh interval pushed to phones
    voicemail_code: "*98"
    show_browser: true       # un-hide the phone's built-in Browser app
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_ENC_PREFIX = "__WUI_ENC__:"
_PHONEBOOKS = (("ECH", "/phonebook.xml"), ("MeshPhone", "/phonebook-meshphone.xml"),
               ("Audio Streams", "/phonebook-streams.xml"))
_PB_SLOTS = 5
# Phone web API resource types (Yealink web UI's RES_TYPE) ECH offers to upload.
RESOURCE_KINDS = {
    "wallpaper": (".jpg", ".jpeg", ".png", ".bmp"),
    "screensaver": (".jpg", ".jpeg", ".png", ".bmp"),
    "ringtone": (".wav",),
}


def rsa_encrypt(message: str, n_hex: str, e_hex: str) -> str:
    """PKCS#1 v1.5 RSA, hex out: what the phone's login page does (jsbn)."""
    n, e = int(n_hex, 16), int(e_hex, 16)
    k = (n.bit_length() + 7) // 8
    m = message.encode()
    if len(m) > k - 11:
        raise ValueError("password too long for the phone's RSA key")
    pad = bytearray()
    while len(pad) < k - len(m) - 3:
        b = secrets.token_bytes(1)
        if b != b"\x00":
            pad += b
    em = b"\x00\x02" + bytes(pad) + b"\x00" + m
    c = pow(int.from_bytes(em, "big"), e, n)
    h = format(c, "x")
    return _ENC_PREFIX + ("0" + h if len(h) % 2 else h)


def parse_cfg(text: str) -> dict[str, str]:
    """`key = value` lines of a Yealink .cfg (comments and blanks skipped)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def ech_accounts(cfg: dict[str, str], ech_hosts: set[str]) -> list[tuple[int, str]]:
    """(account index, server address) for every enabled line registered to ECH."""
    found = []
    for k, v in cfg.items():
        m = re.fullmatch(r"account\.(\d+)\.sip_server\.1\.address", k)
        if m and v in ech_hosts and cfg.get(f"account.{m.group(1)}.enable") == "1":
            found.append((int(m.group(1)), v))
    return sorted(found)


def plan_settings(cfg: dict[str, str], phonebooks: list[dict], ech_hosts: set[str],
                  opts: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    """The ECH keys this phone should have, given its current config.

    Returns (settings, notes). Only keys whose value differs from the phone's
    are worth importing; the caller compares.
    """
    notes: list[str] = []
    want: dict[str, str] = {}
    accts = ech_accounts(cfg, ech_hosts)
    if not accts:
        notes.append("no enabled line on this phone registers to ECH; directory not set")
        return want, notes
    host = accts[0][1]
    base = f"http://{host}:{int(opts.get('http_port') or 8766)}"
    notes.append("ECH line(s): " + ", ".join(f"account {i} ({cfg.get(f'account.{i}.label') or cfg.get(f'account.{i}.user_name', '?')})" for i, _ in accts))

    # Remote phonebooks: keep a slot that already points at an ECH book (any
    # ECH address), else take the first empty one. Never overwrite another book.
    slots = {int(p.get("id")): (p.get("name") or "", p.get("url") or "") for p in phonebooks if str(p.get("id", "")).isdigit()}
    if not slots:
        slots = {i: (cfg.get(f"remote_phonebook.data.{i}.name", ""), cfg.get(f"remote_phonebook.data.{i}.url", ""))
                 for i in range(1, _PB_SLOTS + 1)}
    used: set[int] = set()
    for title, path in _PHONEBOOKS:
        slot = next((i for i, (_, u) in sorted(slots.items())
                     if u.endswith(path) and i not in used and any(h in u for h in ech_hosts)), None)
        if slot is None:
            slot = next((i for i, (_, u) in sorted(slots.items()) if not u and i not in used), None)
        if slot is None:
            notes.append(f"no free remote phonebook slot for {title}")
            continue
        used.add(slot)
        want[f"remote_phonebook.data.{slot}.url"] = base + path
        want[f"remote_phonebook.data.{slot}.name"] = title
    if used:
        want["features.remote_phonebook.enable"] = "1"
        want["features.remote_phonebook.flash_time"] = str(int(opts.get("flash_time") or 3600))

    vm = str(opts.get("voicemail_code") or "*98")
    for i, _ in accts:
        want[f"voice_mail.number.{i}"] = vm
        # Caller ID from P-Asserted-Identity / Remote-Party-ID before From, so
        # mid-call connected-line updates show (who is talking on M17/AllStar
        # listen calls). The default (0 = From only) never changes mid-call.
        if opts.get("cid_source", True):
            want[f"account.{i}.cid_source"] = "6"

    # Line keys opening XML Browser pages (key type 27): the ECH menu, plus the
    # AllStar and M17 panels. Each reuses its own key if the phone already has
    # one (matched by page name), else takes the first free key.
    if opts.get("xml_key") and opts.get("menu_key", True):
        pages = [(str(opts.get("menu_label") or "ECH"), "menu.xml")]
        if opts.get("allstar_key", True):
            pages.append(("AllStar", "allstar.xml"))
        if opts.get("m17_key", True):
            pages.append(("M17", "m17.xml"))
        used_keys = {int(m.group(1)) for k in cfg for m in [re.fullmatch(r"linekey\.(\d+)\.type", k)]
                     if m and cfg[k] not in ("", "0")}
        taken: set[int] = set()
        for label, page in pages:
            url = f"{base}/yealink/{opts['xml_key']}/{page}"
            mine = next((int(m.group(1)) for k, v in cfg.items()
                         for m in [re.fullmatch(r"linekey\.(\d+)\.value", k)]
                         if m and "/yealink/" in v and v.endswith("/" + page)
                         and cfg.get(f"linekey.{m.group(1)}.type") == "27"), None)
            slot = mine or next((i for i in range(1, 30) if i not in used_keys and i not in taken), None)
            if not slot:
                notes.append(f"no free line key for {label}")
                continue
            taken.add(slot)
            want[f"linekey.{slot}.type"] = "27"
            want[f"linekey.{slot}.value"] = url
            want[f"linekey.{slot}.label"] = label
            notes.append(f"{label} {'menu' if page == 'menu.xml' else 'panel'} on line key {slot}")

    if opts.get("show_browser", True):
        hidden = [a for a in (cfg.get("app.hide.icon") or "").split("/") if a]
        if "Browser" in hidden:
            want["app.hide.icon"] = "/".join(a for a in hidden if a != "Browser")
    return want, notes


def generic_cfg(server: str, account: int, opts: dict[str, Any]) -> str:
    """ECH's settings as a .cfg for a phone ECH can't log into (Yealink T4x/T5x
    classic web UI, not the Android JSON API): import it by hand under
    Settings > Configuration > Import CFG. Assumes remote phonebook slots 1-3
    and line keys from 1 are free; adjust in the file if they aren't."""
    fake = {f"account.{account}.sip_server.1.address": server, f"account.{account}.enable": "1",
            "linekey.1.type": "15"}          # line key 1 stays the phone's own line
    want, _ = plan_settings(fake, [], {server}, {**opts, "show_browser": False})
    head = (f"## For a Yealink without the Android web API (T4x/T5x): import under\n"
            f"## Settings > Configuration > Import CFG. ECH line = account {account}, ECH = {server}.\n")
    return render_cfg(want).replace("\n", "\n" + head, 1)


def render_cfg(settings: dict[str, str]) -> str:
    lines = ["#!version:1.0.0.1", f"## ECH phone settings, {time.strftime('%Y-%m-%d %H:%M')}"]
    lines += [f"{k} = {v}" for k, v in sorted(settings.items())]
    return "\n".join(lines) + "\n"


MAX_LOCAL_CONTACTS = 1000      # Yealink local directory limit


def render_contacts_xml(groups: list[tuple[str, list[tuple[str, str]]]]) -> tuple[str, int]:
    """Yealink local-directory XML (the phone's own export format) from
    [(group name, [(display name, number)])]. Duplicate numbers within a group
    are dropped; stops at MAX_LOCAL_CONTACTS. Returns (xml, contact count)."""
    from xml.sax.saxutils import quoteattr
    gx, cx, n = [], [], 0
    for gname, entries in groups:
        if not entries:
            continue
        gx.append(f"\t<group display_name={quoteattr(gname)} ring=\"Auto\" />")
        seen = set()
        for name, num in entries:
            num = str(num or "").strip()
            if not num or num in seen or n >= MAX_LOCAL_CONTACTS:
                continue
            seen.add(num)
            n += 1
            cx.append(f"\t<contact display_name={quoteattr(str(name or num)[:60])} office_number={quoteattr(num)} "
                      f"mobile_number=\"\" other_number=\"\" line=\"-1\" ring=\"Auto\" group_id_name={quoteattr(gname)} />")
    xml = ('<?xml version="1.0" encoding="utf-8"?>\n<root_group>\n' + "\n".join(gx) + "\n</root_group>\n"
           "<root_contact>\n" + "\n".join(cx) + "\n</root_contact>\n")
    return xml, n


class PhoneError(Exception):
    pass


class YealinkPhone:
    """One login session to one phone's web API."""

    def __init__(self, host: str, username: str, password: str, timeout: float = 20.0):
        self.host, self.username, self.password = host, username, password
        self._c = httpx.AsyncClient(base_url=f"https://{host}", verify=False, timeout=timeout)
        self._token = ""
        self.info: dict[str, Any] = {}

    async def __aenter__(self):
        await self.login()
        return self

    async def __aexit__(self, *exc):
        try:
            await self._c.post("/api/auth/logout", headers=self._hdr())
        except Exception:
            pass
        await self._c.aclose()

    def _hdr(self, extra: dict | None = None) -> dict:
        h = {"X-CSRFToken": self._token or self._c.cookies.get("csrftoken", "")}
        h.update(extra or {})
        return h

    @staticmethod
    def _q(path: str, page: str | None) -> str:
        sep = "&" if "?" in path else "?"
        path = f"{path}{sep}t={int(time.time() * 1000)}"
        return f"{path}&p={page}" if page else path

    async def _json(self, method: str, path: str, page: str | None = None, body: Any = None) -> Any:
        kw: dict[str, Any] = {"headers": self._hdr()}
        if body is not None:
            kw["json"] = body
        r = await self._c.request(method, self._q(path, page), **kw)
        try:
            d = r.json()
        except ValueError:
            raise PhoneError(f"{path}: HTTP {r.status_code}")
        if d.get("ret") not in ("ok", True, "true"):
            err = d.get("error") or {}
            raise PhoneError(f"{path}: {err.get('msg') or err.get('webStatus') or r.status_code}")
        return d.get("data")

    async def login(self):
        try:
            r = await self._c.get("/api/system/info")
        except httpx.HTTPError as e:
            raise PhoneError(f"phone not reachable: {e.__class__.__name__}")
        err = (r.json() or {}).get("error") or {}
        n, e = err.get("rsaN"), err.get("rsaE")
        if not n or not e:
            raise PhoneError("phone did not offer a login key (not a supported Yealink web UI?)")
        r = await self._c.post("/api/auth/login",
                               data={"username": self.username, "pwd": rsa_encrypt(self.password, n, e)})
        d = r.json()
        if d.get("ret") != "ok":
            st = (d.get("error") or {}).get("webStatus") or d
            raise PhoneError(f"phone login refused ({st})")
        data = await self._json("POST", "/api/common/info", "Status", {"idlist": ["wui"]})
        nested: dict[str, Any] = {}
        for k, v in (data or {}).items():
            cur = nested
            parts = k.split(".")
            for p in parts[:-1]:
                cur = cur.setdefault(p, {})
            cur[parts[-1]] = v
        wui = nested.get("wui", {})
        self._token = (wui.get("common") or {}).get("token", "") or self._token
        dev = wui.get("device") or {}
        self.info = {"model": dev.get("phoneName", ""), "firmware": dev.get("firmware", ""),
                     "mac": dev.get("mac", "")}

    async def export_cfg(self, kind: str = "all") -> str:
        tok = self._hdr()["X-CSRFToken"]
        r = await self._c.post(self._q(f"/api/diagnosis/cfg/file?action=export&type={kind}", None),
                               data={"csrfmiddlewaretoken": tok}, headers=self._hdr())
        text = r.text
        if r.status_code != 200 or text.lstrip().startswith("{"):
            raise PhoneError(f"config export failed: {text[:120]}")
        return text

    async def import_cfg(self, text: str):
        await self._upload("/api/diagnosis/cfg/file?action=import", "SettingConfig",
                           "ech.cfg", text.encode(), "application/octet-stream")

    async def local_contacts_total(self) -> int:
        d = await self._json("GET", "/api/contacts/localcontacts?page=1", "ContactsLocal") or {}
        return int(d.get("total") or 0)

    async def local_contacts(self) -> list[dict]:
        """Every local contact. The phone pages 10 at a time whatever pagesize says."""
        out: list[dict] = []
        for page in range(1, 200):
            d = await self._json("GET", f"/api/contacts/localcontacts?page={page}", "ContactsLocal") or {}
            rows = d.get("list") or []
            out += rows
            if not rows or len(out) >= int(d.get("total") or 0):
                break
        return out

    async def import_contacts(self, xml_text: str):
        """Replace the phone's whole local directory (Yealink's import replaces, not appends)."""
        await self._upload("/api/contacts/uploadxml", "ContactsLocal", "contact.xml", xml_text.encode("utf-8"), "text/xml")

    async def install_apk(self, filename: str, blob: bytes):
        await self._upload("/api/apk/manage?action=install", "SettingApk",
                           filename, blob, "application/vnd.android.package-archive", timeout=300)

    async def upload_resource(self, kind: str, filename: str, blob: bytes):
        """Wallpaper / screensaver image / ringtone / contact photo onto the phone.
        It then shows up in the phone's own picker (Settings > Display / Sound)."""
        if kind not in RESOURCE_KINDS:
            raise PhoneError(f"unsupported file kind {kind}")
        ctype = "audio/wav" if kind == "ringtone" else ("image/png" if filename.lower().endswith(".png") else "image/jpeg")
        await self._upload(f"/api/resource/file?action=upload&type={kind}", "SettingGeneral",
                           filename, blob, ctype, timeout=120)

    async def _upload(self, path, page, filename, blob, ctype, timeout=60):
        r = await self._c.post(self._q(path, page), files={"file": (filename, blob, ctype)},
                               headers=self._hdr(), timeout=timeout)
        try:
            d = r.json()
        except ValueError:
            raise PhoneError(f"upload: HTTP {r.status_code}")
        if d.get("ret") != "ok":
            err = d.get("error") or {}
            msg = err.get("msg") or err.get("webStatus") or str(r.status_code)
            if msg == "error_invalid_request" and "apk" in path:
                msg += " (app install is turned off on this phone: enable developer mode / unknown sources on the handset, then retry)"
            raise PhoneError(f"phone refused upload: {msg}")

    async def phonebooks(self) -> list[dict]:
        try:
            return await self._json("GET", "/api/contacts/remotephonebook/list", "ContactsRemote") or []
        except PhoneError:
            return []

    async def account_status(self) -> dict:
        try:
            return await self._json("GET", "/api/account/status", "Status") or {}
        except PhoneError:
            return {}


class PhoneManager:
    """The configured phones, plus the last result for each (for the Settings page)."""

    def __init__(self, cfg: dict, ech_hosts: set[str], backup_dir: str | os.PathLike):
        pc = (cfg or {}).get("pbx_phones") or {}
        self.opts = {k: v for k, v in pc.items() if k != "phones"}
        self.phones = [p for p in (pc.get("phones") or []) if p.get("host")]
        self.ech_hosts = set(ech_hosts)
        self.backup_dir = Path(backup_dir)
        self.last: dict[str, dict] = {}

    def public_list(self) -> list[dict]:
        return [{"name": p.get("name") or p["host"], "host": p["host"], "vendor": p.get("vendor", "yealink"),
                 "last": self.last.get(p["host"])} for p in self.phones]

    def _find(self, host: str | None) -> list[dict]:
        if not host:
            return list(self.phones)
        ph = [p for p in self.phones if p["host"] == host]
        if not ph:
            raise PhoneError(f"unknown phone {host}")
        return ph

    def _client(self, p: dict) -> YealinkPhone:
        if not p.get("password"):
            raise PhoneError("no admin password set for this phone in config.yaml")
        return YealinkPhone(p["host"], p.get("username") or "admin", str(p["password"]))

    def _backup(self, mac: str, host: str, text: str) -> str:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.backup_dir, 0o700)
        except OSError:
            pass
        f = self.backup_dir / f"{(mac or host).replace(':', '')}-{time.strftime('%Y%m%d-%H%M%S')}.cfg"
        f.write_text(text)
        try:
            os.chmod(f, 0o600)
        except OSError:
            pass
        return f.name

    async def inspect(self, p: dict) -> dict:
        """Pull the phone's config and work out what a push would change."""
        async with self._client(p) as ph:
            text = await ph.export_cfg("all")
            cfg = parse_cfg(text)
            books = await ph.phonebooks()
            want, notes = plan_settings(cfg, books, self.ech_hosts, self.opts)
            changes = {k: v for k, v in want.items() if cfg.get(k, "") != v}
            # remote phonebook values may be missing from the export; trust the list API
            for b in books:
                i = b.get("id")
                if want.get(f"remote_phonebook.data.{i}.url") == b.get("url") and \
                   want.get(f"remote_phonebook.data.{i}.name") == b.get("name"):
                    changes.pop(f"remote_phonebook.data.{i}.url", None)
                    changes.pop(f"remote_phonebook.data.{i}.name", None)
            return {"info": ph.info, "text": text, "cfg": cfg, "want": want, "changes": changes,
                    "notes": notes, "phonebooks": [b for b in books if b.get("url")],
                    "accounts": await ph.account_status()}

    async def check(self, host: str | None = None) -> list[dict]:
        out = []
        for p in self._find(host):
            try:
                r = await self.inspect(p)
                res = {"ok": True, "host": p["host"], **r["info"], "pending": r["changes"],
                       "notes": r["notes"], "phonebooks": r["phonebooks"], "checked": time.time()}
            except Exception as e:
                res = {"ok": False, "host": p["host"], "error": str(e), "checked": time.time()}
            self.last[p["host"]] = {**(self.last.get(p["host"]) or {}), "check": res}
            out.append(res)
        return out

    async def _push_contacts(self, p: dict, mac: str, contacts) -> str:
        """Back up the phone's local directory, then replace it with ECH's."""
        xml, n = render_contacts_xml(contacts)
        if not n:
            return "directory: nothing to load"
        async with self._client(p) as ph:
            old = await ph.local_contacts()
            self._backup((mac or p["host"]) + "-contacts", p["host"], json.dumps(old, indent=1))
            await ph.import_contacts(xml)
            got = await ph.local_contacts_total()
        return f"directory: {got} of {n} contacts loaded (replaced {len(old)})"

    async def push(self, host: str | None = None, force: bool = False, contacts=None) -> list[dict]:
        """contacts: [(group, [(name, number)])] to load as the phone's local directory, or None."""
        out = []
        for p in self._find(host):
            try:
                r = await self.inspect(p)
                backup = self._backup(r["info"].get("mac", ""), p["host"], r["text"])
                send = r["want"] if force else r["changes"]
                notes = list(r["notes"])
                if contacts:
                    try:
                        notes.append(await self._push_contacts(p, r["info"].get("mac", ""), contacts))
                    except Exception as e:
                        notes.append(f"directory load failed: {e}")
                if not send:
                    res = {"ok": True, "host": p["host"], **r["info"], "changed": {}, "backup": backup,
                           "notes": notes, "detail": "settings already up to date (no restart needed)"}
                else:
                    async with self._client(p) as ph:
                        await ph.import_cfg(render_cfg(send))
                    res = {"ok": True, "host": p["host"], **r["info"], "changed": send, "backup": backup,
                           "notes": notes, "detail": "settings imported; the phone restarts to apply them (~40 s)"}
                logger.info("Phone push %s: %s", p["host"], res["detail"])
            except Exception as e:
                logger.warning("Phone push %s failed: %s", p["host"], e)
                res = {"ok": False, "host": p["host"], "error": str(e)}
            res["at"] = time.time()
            self.last[p["host"]] = {**(self.last.get(p["host"]) or {}), "push": res}
            out.append(res)
        return out

    async def send_file(self, host: str, kind: str, filename: str, blob: bytes) -> dict:
        """kind 'apk' installs an app; otherwise one of RESOURCE_KINDS."""
        p = self._find(host)[0]
        low = filename.lower()
        try:
            if kind == "apk":
                if not low.endswith(".apk"):
                    raise PhoneError("choose an .apk file")
            elif kind in RESOURCE_KINDS:
                if not low.endswith(RESOURCE_KINDS[kind]):
                    raise PhoneError(f"{kind} must be one of: {', '.join(RESOURCE_KINDS[kind])}")
            else:
                raise PhoneError(f"unsupported file kind {kind}")
            async with self._client(p) as ph:
                if kind == "apk":
                    await ph.install_apk(filename, blob)
                    detail = f"{filename} installed"
                else:
                    await ph.upload_resource(kind, filename, blob)
                    detail = f"{filename} added to the phone's {kind} list (pick it on the phone)"
            res = {"ok": True, "host": host, "kind": kind, "detail": detail}
        except Exception as e:
            res = {"ok": False, "host": host, "kind": kind, "error": str(e)}
        res["at"] = time.time()
        self.last[host] = {**(self.last.get(host) or {}), "file": res}
        return res
