"""
ech/core/self_update.py
--------------------------
O103: let ECH update its own code from GitHub, triggered and run entirely
from the live running system (Settings -> System) — not the external
Windows deploy/build_and_scp.ps1 pipeline.

Deliberately does NOT reuse install.sh / sudo tar-extraction. Checked the
live deployment's actual file ownership first (2026-10-08) rather than
assuming: /opt/ech, /opt/ech/ech (the code), and /opt/ech/.venv are all
owned by the `ech` service account itself — the same account this code runs
as. A few peripheral files under /opt/ech (VERSION, pyproject.toml,
INSTALLED_VERSION, deploy/*) are owned by a different uid from the original
tarball extraction, but Unix delete/recreate permission is governed by the
*directory's* write bit, not the target file's owner — and /opt/ech itself
is ech-owned — so even those are replaceable without sudo via write-temp +
os.replace() (atomic, avoids partial-write corruption).

Net effect: a routine code update needs exactly ONE privileged operation —
restarting the systemd service — and `ech` already has narrowly-scoped
NOPASSWD sudo for exactly that (`systemctl restart ech`, configured by
install.sh's existing sudoers block). This deliberately does NOT widen that
sudoers scope — no new file-system root access, no sudo tar-extraction, no
pip-as-root. deploy/install.sh, deploy/ech.service, and
deploy/ech-sim.service are NOT touched by self-update (those live under a
directory `ech` doesn't own) — a genuine change to the service unit file or
installer still goes through the full Windows deploy pipeline, which is the
right amount of ceremony for something that can prevent the service from
starting at all if gotten wrong.

Self-restart hazard: `systemctl restart ech` is issued by code running
*inside* the ech process it's restarting. ech.service uses KillMode=mixed
(SIGTERM to the main process only; only escalates to SIGKILL-the-whole-
cgroup after TimeoutStopSec=10s if the main process hasn't exited — see
deploy/ech.service) and this is the exact same pattern the already-existing
`POST /api/system/services/{service}/restart` endpoint uses for "ech" today
— not new risk introduced here, just reused.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tarfile
import tempfile
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

GITHUB_REPO = "guywire/emergency-communications-hub"
# ech/ (the code) and pyproject.toml (dependency list) get applied live;
# config.yaml/deploy/*/VERSION are downloaded for completeness/version
# reporting but VERSION is the only one of those actually written — see
# module docstring for why deploy/* isn't touched.
TARBALL_MEMBERS = ["ech", "pyproject.toml", "VERSION"]

INSTALL_DIR = Path("/opt/ech")
# Deliberately NOT /tmp/ech_deploy.tar.gz — that's deploy/build_and_scp.ps1's
# path, written there by the "mesh" SSH user. The two mechanisms colliding on
# the same filename, owned by a different user each time (ech vs mesh), was
# a real bug found live: whichever ran second couldn't overwrite the other's
# file (plain 644 perms, not writable by a non-owner), so a Windows-pipeline
# deploy right after a self-update silently failed to upload and install.sh
# re-installed the STALE leftover tarball instead with no clear error.
DEPLOY_TARBALL = Path("/tmp/ech_selfupdate_deploy.tar.gz")
SELFUPDATE_LOG = Path("/tmp/ech_selfupdate.log")

_state: dict = {"running": False, "started_at": None, "finished_at": None,
                "exit_code": None, "step": "", "error": None}


async def check_latest_commit(branch: str = "main") -> dict:
    """GET the latest commit on `branch` from the public GitHub API — no
    auth needed for a public repo (rate-limited to 60 req/hr unauthenticated,
    fine for an admin manually clicking "Check for updates"). Also fetches
    the branch's VERSION file so the UI can show ECH's own rc-style version
    number (the same one the messaging page displays) instead of only a
    raw git commit hash, which means nothing to an operator at a glance."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/commits/{branch}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers={"Accept": "application/vnd.github+json"})
        resp.raise_for_status()
        data = resp.json()
        version = await _fetch_remote_version(client, branch)
    commit = data.get("commit", {}) or {}
    return {
        "sha": (data.get("sha") or "")[:12],
        "message": (commit.get("message") or "").split("\n", 1)[0],
        "date": (commit.get("committer") or {}).get("date", ""),
        "author": (commit.get("author") or {}).get("name", ""),
        "version": version,
    }


async def _fetch_remote_version(client: httpx.AsyncClient, branch: str) -> str:
    """Best-effort fetch of the raw VERSION file at the tip of `branch`.
    Returns '' (not raised) on any failure — the commit info above is
    still useful on its own, and this is only ever shown as supplementary
    context in the "check for updates" UI."""
    url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{branch}/VERSION"
    try:
        resp = await client.get(url, timeout=10.0)
        resp.raise_for_status()
        return resp.text.strip()
    except httpx.HTTPError:
        return ""


def _safe_extract(archive_path: Path, dest: Path) -> None:
    """Extract archive_path into dest, refusing any member whose resolved
    path would land outside dest (defense in depth against a maliciously
    crafted archive — not expected from GitHub's own export, but cheap
    insurance). Works on Python versions without tarfile's PEP 706 filter
    (the live deployment runs 3.11.2, which predates the backport)."""
    dest = dest.resolve()
    with tarfile.open(archive_path) as tf:
        safe_members = []
        for member in tf.getmembers():
            target = (dest / member.name).resolve()
            if dest not in target.parents and target != dest:
                raise RuntimeError(f"refusing unsafe archive member: {member.name}")
            safe_members.append(member)
        try:
            tf.extractall(dest, members=safe_members, filter="data")
        except TypeError:
            # Python <3.12 (no PEP 706 filter kwarg) — our own member-path
            # check above already did the equivalent safety filtering.
            tf.extractall(dest, members=safe_members)


async def download_and_repackage(branch: str = "main") -> Path:
    """Download the GitHub archive for `branch` and repackage just the
    pieces this module actually applies (ech/, pyproject.toml, VERSION)
    into a local tarball."""
    url = f"https://github.com/{GITHUB_REPO}/archive/refs/heads/{branch}.tar.gz"
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        archive_bytes = resp.content

    tmp_dir = Path(tempfile.mkdtemp(prefix="ech_selfupdate_"))
    try:
        archive_path = tmp_dir / "source.tar.gz"
        archive_path.write_bytes(archive_bytes)
        _safe_extract(archive_path, tmp_dir)

        # GitHub's archive export extracts into one top-level "{repo}-{branch}" dir.
        extracted_dirs = [p for p in tmp_dir.iterdir() if p.is_dir()]
        if len(extracted_dirs) != 1:
            raise RuntimeError(f"unexpected archive layout: {[p.name for p in tmp_dir.iterdir()]}")
        src_root = extracted_dirs[0]

        missing = [m for m in TARBALL_MEMBERS if not (src_root / m).exists()]
        if missing:
            raise RuntimeError(f"downloaded source is missing expected path(s): {missing}")

        with tarfile.open(DEPLOY_TARBALL, "w:gz") as out:
            for member in TARBALL_MEMBERS:
                out.add(src_root / member, arcname=member)

        return DEPLOY_TARBALL
    finally:
        # A real bug found live: this was never cleaned up, leaving a full
        # extracted copy of the source tree in /tmp after every single run.
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _replace_file(dest: Path, content: bytes) -> None:
    """Atomically replace dest's content, even if dest is currently owned
    by a different uid than this process — Unix delete/recreate permission
    is governed by the PARENT directory's write bit, not the target file's
    owner, and every path this is called on lives directly under
    INSTALL_DIR, which the ech account owns. Writes to a temp file in the
    same directory first so a crash mid-write can't leave a half-written
    file in place of the original."""
    tmp = dest.with_suffix(dest.suffix + f".tmp{os.getpid()}")
    tmp.write_bytes(content)
    os.replace(tmp, dest)  # atomic on the same filesystem


def _apply_update(tarball: Path) -> list[str]:
    """Extract the downloaded tarball over the live /opt/ech/ech tree and
    update VERSION/pyproject.toml. Returns a short log of what changed.
    Synchronous/blocking — called via asyncio.to_thread from _run_update so
    it doesn't block the event loop during the file copy."""
    log_lines = []
    tmp_dir = Path(tempfile.mkdtemp(prefix="ech_selfupdate_apply_"))
    try:
        with tarfile.open(tarball) as tf:
            try:
                tf.extractall(tmp_dir, filter="data")
            except TypeError:
                tf.extractall(tmp_dir)

        new_ech_dir = tmp_dir / "ech"
        if not new_ech_dir.is_dir():
            raise RuntimeError("extracted tarball has no ech/ directory")

        live_ech_dir = INSTALL_DIR / "ech"
        # Copy new/changed files in, then remove anything in the live tree
        # that no longer exists upstream (a renamed/deleted module should
        # actually disappear, not linger as stale bytecode-importable cruft).
        shutil.copytree(new_ech_dir, live_ech_dir, dirs_exist_ok=True)
        log_lines.append(f"synced {new_ech_dir} -> {live_ech_dir}")

        new_files = {p.relative_to(new_ech_dir) for p in new_ech_dir.rglob("*") if p.is_file()}
        removed = 0
        for p in list(live_ech_dir.rglob("*")):
            if p.is_file() and p.relative_to(live_ech_dir) not in new_files:
                p.unlink()
                removed += 1
        if removed:
            log_lines.append(f"removed {removed} file(s) no longer present upstream")

        # Clear stale bytecode so Python re-compiles against the new source
        # (mirrors install.sh's own `find ... -name '*.pyc' -delete` step).
        pyc_removed = 0
        for p in live_ech_dir.rglob("*.pyc"):
            p.unlink()
            pyc_removed += 1
        if pyc_removed:
            log_lines.append(f"cleared {pyc_removed} stale .pyc file(s)")

        version_text = (tmp_dir / "VERSION").read_bytes()
        _replace_file(INSTALL_DIR / "VERSION", version_text)
        log_lines.append(f"VERSION -> {version_text.decode(errors='replace').strip()}")

        pyproject_src = tmp_dir / "pyproject.toml"
        if pyproject_src.exists():
            _replace_file(INSTALL_DIR / "pyproject.toml", pyproject_src.read_bytes())
            log_lines.append("pyproject.toml updated")

        return log_lines
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def _pip_sync() -> tuple[int, str]:
    """`pip install -e .` against the live venv — ech owns .venv directly,
    no sudo needed. Picks up any new/changed dependency from the updated
    pyproject.toml. Best-effort: a failure here is logged but doesn't abort
    the update, since the existing venv may already satisfy everything."""
    pip = INSTALL_DIR / ".venv" / "bin" / "pip"
    if not pip.exists():
        return 0, "(no .venv/bin/pip found — skipped)"
    proc = await asyncio.create_subprocess_exec(
        str(pip), "install", "-e", str(INSTALL_DIR),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        cwd=str(INSTALL_DIR),
    )
    out, _ = await proc.communicate()
    return proc.returncode, out.decode(errors="replace")


def status() -> dict:
    log_tail = ""
    if SELFUPDATE_LOG.exists():
        try:
            log_tail = "\n".join(SELFUPDATE_LOG.read_text(errors="replace").splitlines()[-200:])
        except OSError:
            pass
    return {**_state, "log_tail": log_tail}


async def start_update(branch: str = "main") -> dict:
    """Kick off the update in the background. Returns immediately; poll
    status() for progress. Safe to call again only once running=False."""
    if _state["running"]:
        return {"status": "error", "detail": "Update already running"}

    _state.update(running=True, started_at=time.time(), finished_at=None,
                  exit_code=None, step="downloading", error=None)
    try:
        SELFUPDATE_LOG.write_text(f"[ech] self-update started, branch={branch}\n")
    except OSError:
        pass

    asyncio.ensure_future(_run_update(branch))
    return {"status": "ok"}


async def _run_update(branch: str) -> None:
    try:
        _state["step"] = "downloading"
        _append_log(f"[ech] downloading https://github.com/{GITHUB_REPO}/archive/refs/heads/{branch}.tar.gz ...")
        tarball = await download_and_repackage(branch)
        _append_log(f"[ech] repackaged {tarball} ({tarball.stat().st_size} bytes)")

        _state["step"] = "applying (writing files under /opt/ech)"
        applied_log = await asyncio.to_thread(_apply_update, tarball)
        for line in applied_log:
            _append_log(f"[ech] {line}")

        _state["step"] = "syncing python dependencies"
        pip_rc, pip_out = await _pip_sync()
        _append_log(f"[ech] pip install -e . exit={pip_rc}")
        if pip_rc != 0:
            _append_log(pip_out[-2000:])
            _append_log("[ech] WARNING: dependency sync failed — continuing with the "
                        "existing venv, which may already satisfy everything")

        _state["step"] = "restarting ech (this request will be interrupted)"
        _append_log("[ech] restarting ech via the existing admin restart path — "
                    "connection will drop; poll /api/system/selfupdate/status after a few seconds")
        _state["exit_code"] = 0
        _state["running"] = False
        _state["finished_at"] = time.time()

        # Detached + file-redirected for the same reason the module docstring
        # explains: this process is about to be killed by the very restart
        # it's issuing, so nothing here can be a pipe this process reads from.
        with open(SELFUPDATE_LOG, "ab") as logf:
            await asyncio.create_subprocess_exec(
                "sudo", "-n", "systemctl", "restart", "ech",
                stdout=logf, stderr=logf,
                start_new_session=True,
            )
        # Deliberately don't await/wait_for here — a successful restart means
        # THIS process is what gets killed next; there's nothing left to do.
        return
    except Exception as exc:
        log.exception("self_update failed")
        _state["error"] = str(exc)
        _state["step"] = "failed"
        _state["exit_code"] = -1
        _state["running"] = False
        _state["finished_at"] = time.time()
        _append_log(f"[ech] self-update failed: {exc}")


def _append_log(line: str) -> None:
    try:
        with open(SELFUPDATE_LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass
