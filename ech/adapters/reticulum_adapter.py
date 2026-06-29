"""
ech/adapters/reticulum_adapter.py
----------------------------------
Reticulum Network Stack / LXMF adapter.

Reticulum (https://reticulum.network) is a cryptography-based networking
stack that works over LoRa, AX.25 packet radio, WiFi, I2P, TCP, and more.
LXMF (Lightweight Extensible Message Format) provides store-and-forward
encrypted messaging built on top of RNS.

This adapter embeds an LXMF router in-process. The RNS daemon (rnsd) does
NOT need to be running separately — RNS is a library, not a service.

Install:  pip install rns lxmf

Config keys:
  name              str     adapter name (default: reticulum)
  display_name      str     LXMF display name broadcast in announces
  config_dir        str     RNS config directory (default: ~/.reticulum)
  identity_path     str     ECH identity file (default: ~/.ech/rns_identity)
  announce_interval int     seconds between LXMF announces (default: 300)
  propagation_node  str     optional propagation node dest hash for sync
  storage_path      str     LXMF router storage path (default: ~/.ech/lxmf)
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from ech.adapters.base import Adapter
from ech.core.models import NormalizedMessage, Priority

log = logging.getLogger(__name__)

EMRG_WORDS = {"emergency", "mayday", "sos", "urgent help", "life safety"}
ELVT_WORDS = {"urgent", "priority", "immediate"}


def _priority(text: str) -> Priority:
    lower = text.lower()
    if any(w in lower for w in EMRG_WORDS):
        return Priority.EMERGENCY
    if any(w in lower for w in ELVT_WORDS):
        return Priority.ELEVATED
    return Priority.NORMAL


class ReticulumAdapter(Adapter):
    """
    Reticulum/LXMF adapter.
    Requires: pip install rns lxmf
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.name             = config.get("name", "reticulum")
        self._display_name    = config.get("display_name", "ECH Node")
        self._config_dir      = config.get("config_dir", os.path.expanduser("~/.reticulum"))
        self._identity_path   = config.get("identity_path", os.path.expanduser("~/.ech/rns_identity"))
        self._announce_interval = int(config.get("announce_interval", 300))
        self._propagation_node  = config.get("propagation_node", None)
        self._storage_path    = config.get("storage_path", os.path.expanduser("~/.ech/lxmf"))
        self._rns = None
        self._router = None
        self._lxmf_dest = None
        self._identity = None
        self._peers: dict[str, dict] = {}   # dest_hash → {name, last_heard}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._announce_task: asyncio.Task | None = None
        self._run_task: asyncio.Task | None = None
        self._rx_count = 0
        self._tx_count = 0

    async def connect(self) -> None:
        self._loop = asyncio.get_running_loop()
        log.info("Reticulum %s: initialising RNS stack", self.name)

        try:
            import RNS
            import LXMF
        except ImportError:
            raise ImportError(
                "rns and lxmf packages required: pip install rns lxmf"
            )

        # Run blocking RNS init in executor
        await self._loop.run_in_executor(None, self._sync_init, RNS, LXMF)
        self._connected = True
        self._run_task = asyncio.create_task(self._run(), name=f"{self.name}-run")
        self._announce_task = asyncio.create_task(
            self._announce_loop(), name=f"{self.name}-announce"
        )
        log.info(
            "Reticulum %s: ready. LXMF address: %s",
            self.name,
            RNS.hexrep(self._lxmf_dest.hash) if self._lxmf_dest else "unknown",
        )

    def _sync_init(self, RNS, LXMF) -> None:
        """Blocking RNS + LXMF setup. Runs in executor thread."""
        Path(self._storage_path).mkdir(parents=True, exist_ok=True)
        Path(os.path.dirname(self._identity_path)).mkdir(parents=True, exist_ok=True)

        # Load or create identity
        if os.path.exists(self._identity_path):
            self._identity = RNS.Identity.from_file(self._identity_path)
            log.info("Reticulum %s: loaded existing identity", self.name)
        else:
            self._identity = RNS.Identity()
            self._identity.to_file(self._identity_path)
            log.info("Reticulum %s: created new identity, saved to %s",
                     self.name, self._identity_path)

        # Start RNS (uses config_dir for interface config)
        self._rns = RNS.Reticulum(configdir=self._config_dir)

        # Create LXMF router and delivery destination
        self._router = LXMF.LXMRouter(
            storagepath=self._storage_path,
        )
        self._router.register_delivery_callback(self._on_lxmf_delivery)

        self._lxmf_dest = self._router.register_delivery_identity(
            self._identity,
            display_name=self._display_name,
        )
        self._lxmf_dest.set_proof_strategy(RNS.Destination.PROVE_ALL)

        # Connect to propagation node if configured
        if self._propagation_node:
            try:
                prop_hash = bytes.fromhex(self._propagation_node)
                self._router.request_messages_from_propagation_node(prop_hash)
                log.info("Reticulum %s: syncing from propagation node %s",
                         self.name, self._propagation_node[:16])
            except Exception as exc:
                log.warning("Reticulum %s: propagation node sync failed: %s", self.name, exc)

        # Listen for announces from other LXMF nodes
        RNS.Transport.register_announce_handler(self._announce_handler_wrapper())

    def _announce_handler_wrapper(self):
        """Returns an announce handler object for RNS.Transport.register_announce_handler."""
        import RNS

        adapter = self
        class LXMFAnnounceHandler:
            aspect_filter = "lxmf.delivery"
            def received_announce(self, destination_hash, announced_identity, app_data):
                try:
                    name = app_data.decode("utf-8") if app_data else ""
                    dest_hex = RNS.hexrep(destination_hash, delimit=False)
                    adapter._peers[dest_hex] = {
                        "name": name,
                        "last_heard": datetime.now(timezone.utc).isoformat(),
                        "dest_hash": dest_hex,
                    }
                    log.debug("Reticulum %s: announce from %s (%s)", adapter.name, dest_hex[:16], name)
                except Exception as exc:
                    log.debug("Reticulum %s: announce handler error: %s", adapter.name, exc)

        return LXMFAnnounceHandler()

    def _on_lxmf_delivery(self, message) -> None:
        """Called by LXMF router on message delivery. May be called from RNS thread."""
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(
            asyncio.ensure_future,
            self._process_lxmf(message),
        )

    async def _process_lxmf(self, message) -> None:
        self._rx_count += 1
        try:
            import RNS
            source_hash = RNS.hexrep(message.source_hash, delimit=False)
            peer = self._peers.get(source_hash, {})
            display = peer.get("name") or source_hash[:16]

            title   = (message.title or b"").decode("utf-8", errors="replace").strip()
            content = (message.content or b"").decode("utf-8", errors="replace").strip()
            body = f"[{title}] {content}" if title else content

            ts = datetime.fromtimestamp(message.timestamp, tz=timezone.utc) \
                 if message.timestamp else datetime.now(timezone.utc)

            nm = NormalizedMessage(
                source_adapter=self.name,
                source_channel="LXMF",
                from_id=source_hash,
                from_display=display,
                body=body or "(empty)",
                timestamp=ts,
                priority=_priority(body),
                raw={
                    "source_hash": source_hash,
                    "title": title,
                    "rssi": getattr(message, "rssi", None),
                    "snr": getattr(message, "snr", None),
                },
            )
            await self._enqueue(nm)
            log.debug("Reticulum %s: RX from %s: %s", self.name, display, body[:60])

        except Exception as exc:
            log.error("Reticulum %s: message processing error: %s", self.name, exc)

    async def send(self, message: NormalizedMessage) -> bool:
        """Send an LXMF message to message.to_id (hex destination hash)."""
        if not message.to_id:
            log.warning("Reticulum %s: to_id (LXMF dest hash) required", self.name)
            return False
        if not self._router or not self._connected:
            return False

        try:
            import RNS, LXMF
            dest_hash = bytes.fromhex(message.to_id)
            dest_identity = RNS.Identity.recall(dest_hash)

            if dest_identity is None:
                log.warning("Reticulum %s: unknown destination %s — path request sent",
                            self.name, message.to_id[:16])
                RNS.Transport.request_path(dest_hash)
                return False

            dest = RNS.Destination(
                dest_identity,
                RNS.Destination.OUT,
                RNS.Destination.SINGLE,
                "lxmf",
                "delivery",
            )
            lxm = LXMF.LXMessage(
                dest,
                self._lxmf_dest,
                message.body,
                title="ECH",
                desired_method=LXMF.LXMessage.DIRECT,
            )
            await self._loop.run_in_executor(None, self._router.handle_outbound, lxm)
            self._tx_count += 1
            self._mark_tx(message)
            log.debug("Reticulum %s: TX to %s: %s", self.name, message.to_id[:16], message.body[:60])
            return True

        except Exception as exc:
            log.error("Reticulum %s: send error: %s", self.name, exc)
            return False

    async def _announce_loop(self) -> None:
        try:
            while self._connected:
                if self._lxmf_dest:
                    await self._loop.run_in_executor(None, self._lxmf_dest.announce)
                    log.debug("Reticulum %s: announced LXMF address", self.name)
                await asyncio.sleep(self._announce_interval)
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        """Reticulum is event-driven; _run just idles while connected."""
        try:
            while self._connected:
                await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            pass

    async def disconnect(self) -> None:
        self._connected = False
        for task in (self._run_task, self._announce_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        log.info("Reticulum %s: disconnected", self.name)

    async def nodes(self):
        from ech.core.models import MeshNode
        return [
            MeshNode(
                node_id=h,
                display_name=p.get("name", h[:16]),
                short_name=h[:8],
                last_heard=datetime.fromisoformat(p["last_heard"]) if p.get("last_heard") else None,
            )
            for h, p in self._peers.items()
        ]

    def _health_detail(self) -> dict:
        lxmf_addr = ""
        if self._lxmf_dest:
            try:
                import RNS
                lxmf_addr = RNS.hexrep(self._lxmf_dest.hash, delimit=False)
            except Exception:
                pass
        return {
            "lxmf_address": lxmf_addr,
            "display_name": self._display_name,
            "peers_known": len(self._peers),
            "rx_count": self._rx_count,
            "tx_count": self._tx_count,
            "config_dir": self._config_dir,
        }


class MockReticulumAdapter(Adapter):
    """Mock Reticulum adapter — no RNS library needed."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.name = config.get("name", "reticulum-mock")
        self._display_name = config.get("display_name", "ECH Node")
        self._interval = config.get("interval_sec", 35.0)
        self._run_task: asyncio.Task | None = None
        self._peers = {
            "a1b2c3d4e5f60001": {"name": "Nomad-Node-1"},
            "a1b2c3d4e5f60002": {"name": "Sideband-Alpha"},
            "a1b2c3d4e5f60003": {"name": "RNS-Relay-Maine"},
        }
        self._rx_count = 0

    async def connect(self) -> None:
        await asyncio.sleep(0.2)
        self._connected = True
        self._run_task = asyncio.create_task(self._run(), name=f"{self.name}-run")
        log.info("%s: mock Reticulum connected", self.name)

    async def disconnect(self) -> None:
        self._connected = False
        if self._run_task:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass

    async def send(self, message: NormalizedMessage) -> bool:
        if not message.to_id:
            return False
        await asyncio.sleep(0.3)
        self._mark_tx(message)
        return True

    async def _run(self) -> None:
        import random
        msgs = [
            "RNS mesh node online, all interfaces up",
            "Propagation node sync complete, 3 new messages",
            "Path established to Nomad-Node-1 via LoRa",
            "LXMF delivery confirmed",
            "Link quality report: SNR +8dB",
        ]
        peers = list(self._peers.items())
        try:
            while self._connected:
                if self.is_paused():
                    await asyncio.sleep(1.0)
                    continue
                await asyncio.sleep(self._interval + random.uniform(-8, 8))
                self._rx_count += 1
                dest_hash, peer = random.choice(peers)
                nm = NormalizedMessage(
                    source_adapter=self.name,
                    source_channel="LXMF",
                    from_id=dest_hash,
                    from_display=peer["name"],
                    body=random.choice(msgs),
                    raw={"source_hash": dest_hash},
                )
                await self._enqueue(nm)
        except asyncio.CancelledError:
            pass

    async def nodes(self):
        from ech.core.models import MeshNode
        return [
            MeshNode(node_id=h, display_name=p["name"], short_name=h[:8])
            for h, p in self._peers.items()
        ]

    def _health_detail(self) -> dict:
        return {
            "lxmf_address": "a1b2c3d4e5f6MOCK",
            "display_name": self._display_name,
            "peers_known": len(self._peers),
            "rx_count": self._rx_count,
            "mode": "mock",
        }
