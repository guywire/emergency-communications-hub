"""
tests/test_base_path.py
-------------------------
Covers O100: ECH reverse-proxied under a sub-path (e.g. https://host/ech/
behind Caddy's `handle_path /ech/*`) instead of a dedicated host/port at
"/". Tests the two pure helpers app.py uses to make every template's
hardcoded absolute paths (fetch, WebSocket, href/src/action) prefix-aware,
without needing to construct the full FastAPI app.
"""

from ech.api.app import base_path_patch_script, rewrite_base_path_links


# ── rewrite_base_path_links ─────────────────────────────────────────────

def test_rewrite_noop_when_base_path_empty():
    html = '<a href="/map">Map</a><script src="/static/x.js"></script>'
    assert rewrite_base_path_links(html, "") == html


def test_rewrite_prefixes_href_src_action():
    html = '<a href="/map">Map</a><img src="/icon.png"><form action="/login">'
    out = rewrite_base_path_links(html, "/ech")
    assert 'href="/ech/map"' in out
    assert 'src="/ech/icon.png"' in out
    assert 'action="/ech/login"' in out


def test_rewrite_does_not_double_prefix_protocol_relative():
    html = '<script src="//cdn.example.com/lib.js"></script>'
    out = rewrite_base_path_links(html, "/ech")
    assert out == html  # unchanged — // is protocol-relative, not root-relative


def test_rewrite_leaves_external_absolute_urls_alone():
    html = '<a href="https://github.com/guywire/emergency-communications-hub">repo</a>'
    out = rewrite_base_path_links(html, "/ech")
    assert out == html


def test_rewrite_handles_multiple_occurrences():
    html = '<a href="/a">A</a><a href="/b">B</a><a href="/c">C</a>'
    out = rewrite_base_path_links(html, "/sub")
    assert out == '<a href="/sub/a">A</a><a href="/sub/b">B</a><a href="/sub/c">C</a>'


# ── base_path_patch_script ──────────────────────────────────────────────

def test_patch_script_sets_window_base_path_global():
    script = base_path_patch_script("/ech")
    assert "window.ECH_BASE_PATH='/ech';" in script


def test_patch_script_empty_base_path_is_inert():
    script = base_path_patch_script("")
    # Still sets the global (so pages can always read window.ECH_BASE_PATH),
    # but the IIFE short-circuits before touching fetch/WebSocket at all.
    assert "window.ECH_BASE_PATH='';" in script
    assert "if(!false)return;" in script


def test_patch_script_is_valid_javascript():
    """Executes the generated script in a real JS engine (Node, via the
    project's existing node.exe) and verifies it rewrites fetch/WebSocket
    URLs exactly as app.py's docstring claims — regression guard against
    the real bug this caught during development (repr(bool(x)) emits
    Python's True/False, not JS's true/false, which throws ReferenceError)."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        import pytest
        pytest.skip("node.js not available in this environment")

    script = base_path_patch_script("/ech")
    harness = (
        "global.window = {fetch: (u) => u, WebSocket: function (u, p) { this.url = u; }};\n"
        + script + "\n"
        "const r = window.fetch('/api/foo');\n"
        "if (r !== '/ech/api/foo') throw new Error('fetch rewrite wrong: ' + r);\n"
        "const ws = new window.WebSocket('wss://example.com/ws');\n"
        "if (ws.url !== 'wss://example.com/ech/ws') throw new Error('ws rewrite wrong: ' + ws.url);\n"
        "const r2 = window.fetch('//external.com/x');\n"
        "if (r2 !== '//external.com/x') throw new Error('protocol-relative should be untouched: ' + r2);\n"
        "console.log('OK');\n"
    )
    result = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "OK" in result.stdout
