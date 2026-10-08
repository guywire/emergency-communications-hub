"""
tests/test_self_update.py
----------------------------
Covers O103: ECH updating its own code from GitHub, run from the live
system itself. Exercises the pure/testable pieces — GitHub API parsing,
archive download+repackage, the path-traversal guard, and the direct
file-level apply (ech/, VERSION, pyproject.toml) — without actually
restarting any service. See self_update.py's module docstring for why
this applies files directly instead of invoking install.sh with sudo.
"""

import io
import tarfile
from pathlib import Path

import httpx
import pytest

from ech.core import self_update


def _make_github_archive_bytes(repo_dir_name: str, files: dict[str, bytes]) -> bytes:
    """Build a tar.gz matching GitHub's archive export shape: everything
    nested under one top-level '{repo}-{branch}' directory."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for rel_path, content in files.items():
            data = io.BytesIO(content)
            info = tarfile.TarInfo(name=f"{repo_dir_name}/{rel_path}")
            info.size = len(content)
            tf.addfile(info, data)
    return buf.getvalue()


def _fake_source_files() -> dict[str, bytes]:
    files = {
        "pyproject.toml": b"[project]\nname = \"ech\"\n",
        "VERSION": b"1.0.0-rcTEST\n",
    }
    for extra in ("__init__.py", "main.py", "api/app.py"):
        files[f"ech/{extra}"] = b"# placeholder\n"
    return files


def _mock_client_factory(handler):
    """self_update.py constructs its own httpx.AsyncClient(...) internally
    (no injectable client param) — patch the AsyncClient class it sees so
    every instance it creates is wired to a MockTransport, regardless of
    whatever other kwargs (timeout, follow_redirects, ...) it passes."""
    class _MockAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)
    return _MockAsyncClient


@pytest.mark.asyncio
async def test_check_latest_commit_parses_github_response(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/guywire/emergency-communications-hub/commits/main"
        return httpx.Response(200, json={
            "sha": "abc123def456abc123def456abc123def456abc1",
            "commit": {
                "message": "Fix something\n\nLonger body here",
                "committer": {"date": "2026-10-08T12:00:00Z"},
                "author": {"name": "someone"},
            },
        })

    monkeypatch.setattr(self_update.httpx, "AsyncClient", _mock_client_factory(handler))
    result = await self_update.check_latest_commit("main")
    assert result["sha"] == "abc123def456"
    assert result["message"] == "Fix something"
    assert result["author"] == "someone"
    assert result["date"] == "2026-10-08T12:00:00Z"


@pytest.mark.asyncio
async def test_download_and_repackage_builds_install_compatible_tarball(monkeypatch, tmp_path):
    archive_bytes = _make_github_archive_bytes(
        "emergency-communications-hub-main", _fake_source_files())

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/archive/refs/heads/main.tar.gz")
        return httpx.Response(200, content=archive_bytes)

    monkeypatch.setattr(self_update.httpx, "AsyncClient", _mock_client_factory(handler))
    monkeypatch.setattr(self_update, "DEPLOY_TARBALL", tmp_path / "ech_deploy.tar.gz")

    out_path = await self_update.download_and_repackage("main")
    assert out_path.exists()

    with tarfile.open(out_path) as tf:
        names = set(tf.getnames())
    for expected in self_update.TARBALL_MEMBERS:
        assert any(n == expected or n.startswith(expected + "/") for n in names), expected


@pytest.mark.asyncio
async def test_download_and_repackage_raises_on_missing_paths(monkeypatch, tmp_path):
    # Missing ech/ entirely — must fail loudly, not silently ship a broken update
    incomplete_files = {"pyproject.toml": b"[project]\n", "VERSION": b"1.0.0-rcTEST\n"}
    archive_bytes = _make_github_archive_bytes("emergency-communications-hub-main", incomplete_files)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=archive_bytes)

    monkeypatch.setattr(self_update.httpx, "AsyncClient", _mock_client_factory(handler))
    monkeypatch.setattr(self_update, "DEPLOY_TARBALL", tmp_path / "ech_deploy.tar.gz")

    with pytest.raises(RuntimeError, match="missing expected path"):
        await self_update.download_and_repackage("main")


def test_safe_extract_rejects_path_traversal(tmp_path):
    archive_path = tmp_path / "evil.tar.gz"
    dest = tmp_path / "dest"
    dest.mkdir()
    with tarfile.open(archive_path, "w:gz") as tf:
        data = io.BytesIO(b"pwned")
        info = tarfile.TarInfo(name="../../etc/passwd")
        info.size = len(b"pwned")
        tf.addfile(info, data)

    with pytest.raises(RuntimeError, match="unsafe archive member"):
        self_update._safe_extract(archive_path, dest)


def test_safe_extract_allows_normal_archive(tmp_path):
    archive_path = tmp_path / "ok.tar.gz"
    dest = tmp_path / "dest"
    dest.mkdir()
    with tarfile.open(archive_path, "w:gz") as tf:
        data = io.BytesIO(b"hello")
        info = tarfile.TarInfo(name="repo-main/VERSION")
        info.size = len(b"hello")
        tf.addfile(info, data)

    self_update._safe_extract(archive_path, dest)
    assert (dest / "repo-main" / "VERSION").read_bytes() == b"hello"


def _make_local_tarball(path: Path, files: dict[str, bytes]) -> None:
    """A tarball already in self_update's OWN repackaged shape (flat
    ech/..., pyproject.toml, VERSION — no repo-name wrapper dir), i.e. what
    download_and_repackage() produces and _apply_update() consumes."""
    with tarfile.open(path, "w:gz") as tf:
        for rel_path, content in files.items():
            data = io.BytesIO(content)
            info = tarfile.TarInfo(name=rel_path)
            info.size = len(content)
            tf.addfile(info, data)


def test_replace_file_overwrites_atomically(tmp_path):
    target = tmp_path / "VERSION"
    target.write_bytes(b"old\n")
    self_update._replace_file(target, b"new\n")
    assert target.read_bytes() == b"new\n"
    # no leftover temp file
    assert list(tmp_path.glob("*.tmp*")) == []


def test_apply_update_syncs_code_updates_version_removes_stale_files(tmp_path, monkeypatch):
    install_dir = tmp_path / "opt_ech"
    live_ech = install_dir / "ech"
    live_ech.mkdir(parents=True)
    (live_ech / "main.py").write_text("old main\n")
    (live_ech / "stale_module.py").write_text("should be removed\n")
    (install_dir / "VERSION").write_text("1.0.0-rcOLD\n")

    monkeypatch.setattr(self_update, "INSTALL_DIR", install_dir)

    tarball = tmp_path / "update.tar.gz"
    _make_local_tarball(tarball, {
        "ech/main.py": b"new main\n",
        "ech/new_module.py": b"brand new\n",
        "pyproject.toml": b"[project]\nname='ech'\n",
        "VERSION": b"1.0.0-rcNEW\n",
    })

    log_lines = self_update._apply_update(tarball)

    assert (live_ech / "main.py").read_text() == "new main\n"
    assert (live_ech / "new_module.py").read_text() == "brand new\n"
    assert not (live_ech / "stale_module.py").exists()  # removed — no longer upstream
    assert (install_dir / "VERSION").read_text() == "1.0.0-rcNEW\n"
    assert (install_dir / "pyproject.toml").read_text() == "[project]\nname='ech'\n"
    assert any("removed 1 file" in line for line in log_lines)


def test_apply_update_clears_stale_pyc_files(tmp_path, monkeypatch):
    install_dir = tmp_path / "opt_ech"
    live_ech = install_dir / "ech"
    live_ech.mkdir(parents=True)
    (live_ech / "main.py").write_text("old\n")
    (live_ech / "__pycache__").mkdir()
    (live_ech / "__pycache__" / "main.cpython-311.pyc").write_bytes(b"stale bytecode")

    monkeypatch.setattr(self_update, "INSTALL_DIR", install_dir)

    tarball = tmp_path / "update.tar.gz"
    _make_local_tarball(tarball, {
        "ech/main.py": b"new\n",
        "VERSION": b"1.0.0-rcNEW\n",
    })

    self_update._apply_update(tarball)
    assert list(live_ech.rglob("*.pyc")) == []


def test_apply_update_raises_without_ech_dir(tmp_path, monkeypatch):
    install_dir = tmp_path / "opt_ech"
    (install_dir / "ech").mkdir(parents=True)
    monkeypatch.setattr(self_update, "INSTALL_DIR", install_dir)

    tarball = tmp_path / "update.tar.gz"
    _make_local_tarball(tarball, {"VERSION": b"1.0.0-rcNEW\n"})  # no ech/ at all

    with pytest.raises(RuntimeError, match="no ech/ directory"):
        self_update._apply_update(tarball)


def test_status_reports_defaults_when_never_run():
    self_update._state.update(running=False, started_at=None, finished_at=None,
                              exit_code=None, step="", error=None)
    s = self_update.status()
    assert s["running"] is False
    assert s["exit_code"] is None


@pytest.mark.asyncio
async def test_start_update_refuses_concurrent_runs(monkeypatch):
    self_update._state.update(running=True)
    try:
        result = await self_update.start_update("main")
        assert result["status"] == "error"
        assert "already running" in result["detail"]
    finally:
        self_update._state.update(running=False)
