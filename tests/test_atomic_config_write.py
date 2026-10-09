"""
tests/test_atomic_config_write.py
------------------------------------
Real incident, 2026-10-09: every config.yaml write endpoint in app.py used
to open the live file directly in "w" mode and stream yaml.dump(cfg, f, ...)
into it. open(path, "w") truncates the file the instant it's opened — if
the dump then threw partway through serializing (or a second request
raced this one with no locking), the file was left truncated/corrupted.
This happened for real via the new adapter-config GUI editor: an
operator's save left config.yaml unparseable ("unexpected end of file"),
and ECH could not restart at all.

Fix: ech.api.app.atomic_write_yaml() renders the full YAML to a string
FIRST (so a serialization error happens before the live file is touched),
then writes to a sibling .tmp file and os.replace()s it into place. These
tests cover the actual guarantee: a failed write must never corrupt or
even partially modify the existing file.
"""

import yaml
import pytest

from ech.api.app import atomic_write_yaml


def test_atomic_write_produces_valid_yaml(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("adapters: []\n")
    atomic_write_yaml(path, {"adapters": [{"name": "a", "type": "mock_aprs"}]})
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert loaded == {"adapters": [{"name": "a", "type": "mock_aprs"}]}


def test_atomic_write_leaves_no_tmp_file_behind(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("adapters: []\n")
    atomic_write_yaml(path, {"adapters": []})
    assert not (tmp_path / "config.yaml.tmp").exists()
    assert list(tmp_path.iterdir()) == [path]


def test_failed_dump_never_touches_the_live_file(tmp_path):
    """The actual guarantee this fix provides: if serialization fails for
    any reason, the existing file must be completely untouched — not
    truncated, not partially overwritten."""
    path = tmp_path / "config.yaml"
    original = "adapters:\n  - name: real-adapter\n    type: meshcore\n"
    path.write_text(original)

    class _Unserializable:
        """PyYAML's default (safe) Dumper has no representer for this and
        will raise mid-dump — exactly the failure mode that used to
        truncate the live file."""
        pass

    with pytest.raises(yaml.representer.RepresenterError):
        atomic_write_yaml(path, {"adapters": [], "bad": _Unserializable()})

    # The live file must be byte-for-byte unchanged — not truncated.
    assert path.read_text() == original
    # And no leftover .tmp file from the aborted write.
    assert not (tmp_path / "config.yaml.tmp").exists()


def test_atomic_write_round_trips_unicode(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("adapters: []\n")
    atomic_write_yaml(path, {"operator": {"callsign": "N0CALL", "note": "em—dash and café"}})
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert loaded["operator"]["note"] == "em—dash and café"


def test_atomic_write_accepts_string_path(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("adapters: []\n")
    atomic_write_yaml(str(path), {"adapters": [{"name": "x", "type": "mock_aprs"}]})
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert loaded == {"adapters": [{"name": "x", "type": "mock_aprs"}]}
