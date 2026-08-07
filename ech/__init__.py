# ech package
from pathlib import Path as _Path

def _read_version() -> str:
    try:
        return (_Path(__file__).parent.parent / "VERSION").read_text().strip()
    except Exception:
        return "unknown"

__version__ = _read_version()
