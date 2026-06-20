"""
ech/main.py
-----------
Application entry point. Loads config.yaml, instantiates adapters,
wires the router and database, and starts uvicorn.

Usage:
    python -m ech.main                  # uses ./config.yaml
    python -m ech.main --config /etc/ech/config.yaml
    ech                                 # via pyproject.toml script entry
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import uvicorn
import yaml

log = logging.getLogger("ech")


def load_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        log.warning("Config not found at %s, using defaults", path)
        return _default_config()
    with open(p) as f:
        return yaml.safe_load(f)


def _default_config() -> dict:
    return {
        "server": {"host": "0.0.0.0", "port": 8765},
        "database": {"path": "ech.db"},
        "logging": {"level": "INFO"},
        "adapters": [
            {"type": "mock_meshtastic", "name": "meshtastic-mock",
             "channel": 0, "interval_sec": 8.0},
            {"type": "mock_aprs",       "name": "aprs-mock",
             "source": "aprsis", "interval_sec": 12.0},
            {"type": "mock_meshcore",   "name": "meshcore-mock",
             "channel": "TAC-1", "interval_sec": 15.0},
            {"type": "mock_sms",        "name": "sms-mock",
             "interval_sec": 25.0},
            {"type": "mock_pat_winlink","name": "winlink-mock",
             "callsign": "W1ABC", "interval_sec": 45.0},
            {"type": "mock_reticulum",   "name": "reticulum-mock",
             "display_name": "ECH Node", "interval_sec": 35.0},
            {"type": "mock_mqtt",        "name": "mqtt-mock",
             "host": "localhost", "topics": ["msh/US/ME/#"], "interval_sec": 18.0},
            {"type": "mock_aredn_ami",   "name": "aredn-pbx-mock", "interval_sec": 60.0},
        ],
    }


def build_adapter(cfg: dict):
    """Instantiate an adapter by its 'type' key in config.

    Each real adapter is imported lazily so a missing optional dependency
    (e.g. serial_asyncio, meshtastic, aiomqtt) only blocks that specific
    adapter — not the entire startup or any mock adapters.
    """
    adapter_type = cfg.get("type")
    if not adapter_type:
        raise ValueError("Adapter config missing required 'type' key")

    # Mock adapters — no external dependencies, always importable
    _mocks = {
        "mock_meshtastic": ("ech.adapters.mock_meshtastic", "MockMeshtasticAdapter"),
        "mock_aprs":       ("ech.adapters.mock_aprs",       "MockAPRSAdapter"),
        "mock_meshcore":   ("ech.adapters.mock_meshcore",   "MockMeshCoreAdapter"),
        "mock_js8call":    ("ech.adapters.mock_js8call",    "MockJS8CallAdapter"),
        "mock_sms":        ("ech.adapters.mock_sms",        "MockSMSAdapter"),
        "mock_pat_winlink":("ech.adapters.mock_pat_winlink","MockPatWinlinkAdapter"),
        "mock_reticulum":  ("ech.adapters.reticulum_adapter","MockReticulumAdapter"),
        "mock_mqtt":       ("ech.adapters.mqtt_adapter",    "MockMQTTAdapter"),
        "mock_aredn_ami":  ("ech.adapters.aredn_ami",       "MockAREDNAMIAdapter"),
    }
    # Real adapters — may require optional packages
    _real = {
        "meshcore":    ("ech.adapters.meshcore",         "MeshCoreAdapter",    "serial_asyncio"),
        "meshtastic":  ("ech.adapters.meshtastic_adapter","MeshtasticAdapter", "meshtastic"),
        "aprs_is":     ("ech.adapters.aprs_is",          "APRSISAdapter",      "aprslib"),
        "aprs_kiss":   ("ech.adapters.aprs_kiss",        "APRSKISSAdapter",    "serial_asyncio, aprslib"),
        "js8call":     ("ech.adapters.js8call",          "JS8CallAdapter",     None),
        "sms":         ("ech.adapters.sms",              "SMSAdapter",         "serial_asyncio"),
        "pat_winlink": ("ech.adapters.pat_winlink",      "PatWinlinkAdapter",  "httpx"),
        "reticulum":   ("ech.adapters.reticulum_adapter","ReticulumAdapter",   "rns, lxmf"),
        "mqtt":        ("ech.adapters.mqtt_adapter",     "MQTTAdapter",        "aiomqtt"),
        "aredn_ami":   ("ech.adapters.aredn_ami",        "AREDNAMIAdapter",    None),
    }

    entry = _mocks.get(adapter_type) or _real.get(adapter_type)
    if not entry:
        all_types = sorted(list(_mocks) + list(_real))
        raise ValueError(f"Unknown adapter type: {adapter_type!r}. Available: {all_types}")

    module_path, class_name = entry[0], entry[1]
    hint = entry[2] if len(entry) > 2 else None

    try:
        import importlib
        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
    except ImportError as exc:
        msg = f"Cannot load adapter '{adapter_type}': {exc}"
        if hint:
            msg += f"\n  Install missing package(s):  pip install {hint}"
        raise ImportError(msg) from exc

    return cls(cfg)


async def run(config: dict) -> None:
    from ech.core.database import Database
    from ech.core.router import Router
    from ech.api.app import create_app

    # Logging
    log_level = config.get("logging", {}).get("level", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    # Database
    db_path = config.get("database", {}).get("path", "ech.db")
    db = Database(db_path)
    await db.connect()

    # Add DB log handler so /logs page shows entries
    class DBLogHandler(logging.Handler):
        def __init__(self, database):
            super().__init__()
            self._db = database
            self._loop = None
        def emit(self, record):
            if self._loop is None:
                try:
                    self._loop = asyncio.get_event_loop()
                except Exception:
                    return
            try:
                msg = self.format(record)
                asyncio.ensure_future(
                    self._db.save_log_entry(record.levelname, record.name, msg[:500]),
                    loop=self._loop,
                )
            except Exception:
                pass

    db_handler = DBLogHandler(db)
    db_handler.setLevel(logging.INFO)
    db_handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(db_handler)

    # Auth manager
    from ech.core.auth import AuthManager
    auth = AuthManager(db)
    await auth.init()

    # Anomaly engine
    from ech.core.anomaly import AnomalyEngine
    anomaly_engine = AnomalyEngine(config, db)

    # Router
    router = Router(db, anomaly_engine=anomaly_engine)
    for adapter_cfg in config.get("adapters", []):
        adapter = build_adapter(adapter_cfg)
        router.register(adapter)

    await router.start()

    # Weather service
    from ech.core.weather import WeatherService
    wx_service = WeatherService(config, router=router)
    await wx_service.start()

    # FastAPI
    # MeshCore → MeshMapper MQTT bridge
    from ech.core.meshcore_bridge import MeshCoreMQTTBridge
    mc_bridge = MeshCoreMQTTBridge(config)
    await mc_bridge.start(router)

    # ECH state manager
    from ech.core.state import ECHState
    state = ECHState(db, router=router, wx_service=wx_service if 'wx_service' in dir() else None)
    await state.init()

    app = create_app(router, db, anomaly_engine=anomaly_engine,
                     wx_service=wx_service if 'wx_service' in dir() else None,
                     auth=auth, ech_state=state, mc_bridge=mc_bridge)
    server_cfg = config.get("server", {})
    host = server_cfg.get("host", "0.0.0.0")
    port = server_cfg.get("port", 8765)

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level=log_level.lower(),
            access_log=False,
        )
    )

    log.info("ECH starting on http://%s:%d", host, port)
    try:
        await server.serve()
    finally:
        if 'mc_bridge' in dir():
            await mc_bridge.stop()
        if 'state' in dir():
            pass
        if 'wx_service' in dir():
            await wx_service.stop()
        await router.stop()
        await db.close()
        log.info("ECH shutdown complete")


def main():
    parser = argparse.ArgumentParser(description="Emergency Communications Hub")
    parser.add_argument("--config", default="config.yaml",
                        help="Path to config.yaml (default: ./config.yaml)")
    args = parser.parse_args()
    config = load_config(args.config)
    asyncio.run(run(config))


if __name__ == "__main__":
    main()
