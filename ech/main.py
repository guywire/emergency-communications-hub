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
import signal
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
        raw = f.read()
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        # Print a human-readable error pointing to the problem line
        import sys
        mark = getattr(exc, 'problem_mark', None)
        loc = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        print(
            f"\n{'='*60}\n"
            f"CONFIG ERROR: {path}{loc}\n"
            f"{exc.problem if hasattr(exc, 'problem') else exc}\n\n"
            "Common cause: YAML indentation mismatch when manually pasting\n"
            "adapter blocks. Adapter list items must have NO leading spaces:\n\n"
            "  adapters:\n"
            "  - type: mqtt        ← correct (0 indent before dash)\n"
            "    name: my-adapter\n\n"
            "  adapters:\n"
            "    - type: mqtt      ← wrong (extra indent causes parse error)\n"
            "      name: my-adapter\n\n"
            "Use the Settings page → MQTT section → 'Add to config' button\n"
            "to add adapters safely without manual YAML editing.\n"
            f"{'='*60}\n",
            file=sys.stderr,
        )
        sys.exit(1)


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
    log.info("build_adapter: type=%r name=%r", adapter_type, cfg.get("name"))
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
        "mock_asterisk":   ("ech.adapters.mock_asterisk",  "MockAsteriskAdapter"),
    }
    # Real adapters — may require optional packages
    _real = {
        "meshcore":    ("ech.adapters.meshcore",         "MeshCoreAdapter",    "serial_asyncio"),
        "meshtastic":  ("ech.adapters.meshtastic_adapter","MeshtasticAdapter", "meshtastic"),
        "aprs_is":     ("ech.adapters.aprs_is",          "APRSISAdapter",      "aprslib"),
        "adsb":        ("ech.adapters.adsb_adapter",     "ADSBAdapter",        "aiohttp"),
        "ais_catcher": ("ech.adapters.ais_catcher_adapter", "AISCatcherAdapter", "aiohttp"),
        "aprs_kiss":   ("ech.adapters.aprs_kiss",        "APRSKISSAdapter",    "serial_asyncio, aprslib"),
        "js8call":     ("ech.adapters.js8call",          "JS8CallAdapter",     None),
        "sms":         ("ech.adapters.sms",              "SMSAdapter",         "serial_asyncio"),
        "pat_winlink": ("ech.adapters.pat_winlink",      "PatWinlinkAdapter",  "httpx"),
        "reticulum":   ("ech.adapters.reticulum_adapter","ReticulumAdapter",   "rns, lxmf"),
        "mqtt":        ("ech.adapters.mqtt_adapter",     "MQTTAdapter",        "aiomqtt"),
        "aredn_ami":   ("ech.adapters.aredn_ami",        "AREDNAMIAdapter",    None),
        "asterisk":    ("ech.adapters.asterisk_adapter", "AsteriskAdapter",    None),
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
            msg += f"  Install missing package(s):  pip install {hint}"
        log.error("build_adapter IMPORT ERROR: %s", msg)
        raise ImportError(msg) from exc

    instance = cls(cfg)
    log.info("build_adapter: created %s (name=%r)", cls.__name__, instance.name)
    return instance


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
        def __init__(self, database, loop):
            super().__init__()
            self._db = database
            self._loop = loop
        def emit(self, record):
            if self._loop is None or self._loop.is_closed():
                return
            try:
                msg = self.format(record)
                self._loop.call_soon_threadsafe(
                    self._loop.create_task,
                    self._db.save_log_entry(record.levelname, record.name, msg[:500]),
                )
            except Exception:
                pass

    db_handler = DBLogHandler(db, asyncio.get_running_loop())
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

    # Router — register adapters but don't start yet
    router = Router(db, anomaly_engine=anomaly_engine)
    adapter_cfgs = config.get("adapters", [])
    log.info("Router: %d adapter config(s) found in config", len(adapter_cfgs))
    for adapter_cfg in adapter_cfgs:
        try:
            adapter = build_adapter(adapter_cfg)
            router.register(adapter)
            log.info("Router: registered '%s' (%s)", adapter.name, type(adapter).__name__)
        except Exception as exc:
            log.error(
                "Router: FAILED to load adapter type=%r name=%r — %s: %s — skipping",
                adapter_cfg.get("type"), adapter_cfg.get("name"),
                type(exc).__name__, exc,
            )

    # Weather service (created before state so state can wire it in)
    from ech.core.weather import WeatherService
    wx_service = WeatherService(config, router=router)

    # ECH state — init BEFORE router.start() so adapters are paused before connecting
    from ech.core.state import ECHState
    state = ECHState(db, router=router, wx_service=wx_service)
    await state.init()

    # Load bridge rules from config before starting
    router._bridge_rules = config.get("bridge_rules") or []
    if router._bridge_rules:
        log.info("Router: %d bridge rule(s) loaded from config", len(router._bridge_rules))

    # Now start router (adapters already have correct pause state)
    await router.start()

    # System GPS reader (optional — only if 'gps:' section present in config)
    gps_reader = None
    gps_cfg = config.get("gps")
    if gps_cfg and gps_cfg.get("port"):
        from ech.core.gps import GpsReader

        async def _on_gps_fix(lat: float, lon: float, alt) -> None:
            await state.set_base_location(lat, lon)
            log.info("GPS: ECH base location updated to (%.6f, %.6f)", lat, lon)

        gps_reader = GpsReader(gps_cfg, on_fix=_on_gps_fix)
        await gps_reader.start()

    # Start weather service after router is live
    await wx_service.start()

    # CAT radio control via rigctld (Hamlib)
    from ech.core.cat_rigctld import CATController
    cat_ctrl = CATController(config, router=router)
    await cat_ctrl.start()

    # Mesh bot — responds to ping, weather, overhead, satpass, solar on mesh channels
    from ech.core.mesh_bot import MeshBot
    wx_bot = MeshBot(config, router=router, state=state)
    router._weather_bot = wx_bot
    await wx_bot.start()

    # MeshCore → MeshMapper MQTT bridge
    from ech.core.meshcore_bridge import MeshCoreMQTTBridge
    mc_bridge = MeshCoreMQTTBridge(config)
    await mc_bridge.start(router)

    server_cfg = config.get("server", {})
    host = server_cfg.get("host", "0.0.0.0")
    port = server_cfg.get("port", 8765)
    secure_cookies = bool(server_cfg.get("secure_cookies", False))

    # TLS setup (optional — controlled by config tls: section)
    tls_cfg = config.get("tls", {})
    mdns_handle = None
    if tls_cfg.get("enabled"):
        from ech.core.tls import ensure_ca, ensure_server_cert, start_mdns
        _data_dir = Path(tls_cfg.get("data_dir", "."))
        _ca_cert_pem, _ca_key_pem = ensure_ca(_data_dir)
        _crt_path, _key_path = ensure_server_cert(_data_dir, _ca_cert_pem, _ca_key_pem)
        _tls_port = int(tls_cfg.get("https_port", 8766))
        secure_cookies = True  # HTTPS means secure cookies are safe
        mdns_handle = start_mdns(_tls_port)
    else:
        _ca_cert_pem = _crt_path = _key_path = _tls_port = None

    app = create_app(router, db, anomaly_engine=anomaly_engine,
                     wx_service=wx_service if 'wx_service' in dir() else None,
                     auth=auth, ech_state=state, mc_bridge=mc_bridge,
                     gps_reader=gps_reader, secure_cookies=secure_cookies,
                     cat_ctrl=cat_ctrl if 'cat_ctrl' in dir() else None,
                     ca_cert_pem=_ca_cert_pem)

    # Build server list: always HTTP, optionally HTTPS on a second port
    _servers: list[uvicorn.Server] = []

    _http_cfg = uvicorn.Config(app, host=host, port=port,
                               log_level=log_level.lower(), access_log=False)
    http_server = uvicorn.Server(_http_cfg)
    http_server.install_signal_handlers = False
    _servers.append(http_server)
    log.info("ECH starting on http://%s:%d", host, port)

    if _crt_path:
        _tls_cfg = uvicorn.Config(app, host=host, port=_tls_port,
                                  ssl_certfile=str(_crt_path), ssl_keyfile=str(_key_path),
                                  log_level=log_level.lower(), access_log=False)
        https_server = uvicorn.Server(_tls_cfg)
        https_server.install_signal_handlers = False
        _servers.append(https_server)
        log.info("ECH TLS starting on https://%s:%d", host, _tls_port)

    # Combined signal handler stops all servers gracefully
    def _stop_all(*_):
        for s in _servers:
            s.should_exit = True

    _loop = asyncio.get_running_loop()
    for _sig in (signal.SIGINT, signal.SIGTERM):
        try:
            _loop.add_signal_handler(_sig, _stop_all)
        except (NotImplementedError, AttributeError):
            pass  # Windows — uvicorn handles it via KeyboardInterrupt

    try:
        await asyncio.gather(*(_s.serve() for _s in _servers))
    finally:
        if mdns_handle:
            from ech.core.tls import stop_mdns
            stop_mdns(mdns_handle)
        if 'mc_bridge' in dir():
            await mc_bridge.stop()
        if gps_reader:
            await gps_reader.stop()
        if 'wx_service' in dir():
            await wx_service.stop()
        if 'wx_bot' in dir():
            await wx_bot.stop()
        if 'cat_ctrl' in dir():
            await cat_ctrl.stop()
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
