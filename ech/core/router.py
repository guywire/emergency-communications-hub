"""
ech/core/router.py
------------------
Central message router. Owns:
  - Adapter lifecycle (connect / supervise / reconnect)
  - Inbound message fan-in from all adapters → SQLite log + WS broadcast
  - Outbound dispatch from API → correct adapter(s)
  - Bridge rules: forward messages between adapters
  - WebSocket client registry
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ech.adapters.base import Adapter
from ech.core.models import NormalizedMessage, Priority
from ech.core import metrics as M

if TYPE_CHECKING:
    from starlette.websockets import WebSocket

log = logging.getLogger(__name__)

# De-duplication window in seconds
DEDUP_WINDOW = 5.0


class Router:
    def __init__(self, db, anomaly_engine=None):
        self._db = db
        self._anomaly_engine = anomaly_engine
        self._adapters: dict[str, Adapter] = {}
        self._ws_clients: set["WebSocket"] = set()
        self._dedup_cache: dict[str, float] = {}   # hash → timestamp
        self._bridge_rules: list[dict] = []        # loaded from config
        self._tasks: list[asyncio.Task] = []
        self._adapter_tasks: dict[str, asyncio.Task] = {}  # name → supervise task
        self._metrics_task: asyncio.Task | None = None

    # ── Adapter management ────────────────────────────────────────────────

    def register(self, adapter: Adapter) -> None:
        self._adapters[adapter.name] = adapter
        adapter._router_notify = self._handle_delivery_status
        # Let mesh adapters push a lightweight "nodes_updated" WS event when their
        # node count changes so the UI refreshes the node panel without polling.
        adapter._router_notify_nodes = self._handle_nodes_updated
        # Generic broadcast for telemetry, topology, and other real-time events.
        adapter._router_broadcast = self.broadcast_ws_event
        log.info("Router: registered adapter '%s'", adapter.name)

    async def start_adapter(self, adapter: Adapter) -> None:
        """Register and start supervising a single adapter (used for dynamic sim adapters)."""
        self.register(adapter)
        task = asyncio.create_task(
            self._supervise(adapter), name=f"supervise-{adapter.name}"
        )
        self._adapter_tasks[adapter.name] = task
        self._tasks.append(task)
        log.info("Router: dynamically started adapter '%s'", adapter.name)

    async def stop_adapter(self, name: str) -> None:
        """Stop and unregister a single adapter by name."""
        task = self._adapter_tasks.pop(name, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            if task in self._tasks:
                self._tasks.remove(task)
        adapter = self._adapters.pop(name, None)
        if adapter:
            try:
                await adapter.disconnect()
            except Exception:
                pass
            log.info("Router: stopped and removed adapter '%s'", name)

    async def start(self) -> None:
        """Connect all adapters and start their receive loops."""
        for adapter in self._adapters.values():
            task = asyncio.create_task(
                self._supervise(adapter), name=f"supervise-{adapter.name}"
            )
            self._tasks.append(task)
        self._metrics_task = asyncio.create_task(
            self._metrics_loop(), name="metrics-update"
        )
        log.info("Router: started, supervising %d adapter(s)", len(self._adapters))

    async def stop(self) -> None:
        if self._metrics_task:
            self._metrics_task.cancel()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for adapter in self._adapters.values():
            await adapter.disconnect()
        log.info("Router: stopped")

    async def _supervise(self, adapter: Adapter) -> None:
        """Connect and restart adapter on failure with exponential back-off."""
        backoff = 2.0
        while True:
            try:
                await adapter.connect()
                backoff = 2.0
                async for msg in adapter.receive():
                    await self._handle_inbound(msg)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                exc_str = str(exc)
                log.error("Adapter '%s' error: %s — retry in %.0fs", adapter.name, exc, backoff)
                if exc_str.startswith("[PORT CONFLICT]"):
                    await self.broadcast_ws_event("adapter_error", {
                        "adapter": adapter.name,
                        "error_type": "port_conflict",
                        "message": exc_str,
                    })
                adapter._connected = False
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    # ── Inbound pipeline ──────────────────────────────────────────────────

    async def _handle_inbound(self, msg: NormalizedMessage) -> None:
        if self._is_duplicate(msg):
            log.debug("Router: dropped duplicate from %s", msg.from_id)
            M.record_message_dropped(msg.source_adapter, "dedup")
            return

        # Persist
        await self._db.save_message(msg)

        # Update metrics
        M.record_message_received(msg.source_adapter, int(msg.priority))

        # Broadcast to WebSocket clients
        await self._broadcast_ws(msg)

        # Anomaly detection (mesh adapters only)
        if self._anomaly_engine:
            findings = await self._anomaly_engine.process(msg)
            for finding in findings:
                M.record_anomaly(finding.adapter, finding.rule, finding.severity.value)
                # Push anomaly event to WebSocket clients
                import json
                payload = json.dumps({"type": "anomaly", "data": finding.to_dict()})
                dead = set()
                for ws in self._ws_clients:
                    try:
                        await ws.send_text(payload)
                    except Exception:
                        dead.add(ws)
                self._ws_clients -= dead

        # Apply bridge rules
        await self._apply_bridge_rules(msg)

        # Weather bot — respond to "weather? <zip>" requests
        wx_bot = getattr(self, '_weather_bot', None)
        if wx_bot:
            await wx_bot.handle(msg)

        # MeshCore → MeshMapper MQTT bridge
        bridge = getattr(self, '_meshcore_bridge', None)
        if bridge and bridge.enabled and 'meshcore' in msg.source_adapter.lower():
            await bridge.enqueue(msg)

    def _is_duplicate(self, msg: NormalizedMessage) -> bool:
        key = hashlib.md5(
            f"{msg.source_adapter}|{msg.from_id}|{msg.body}".encode()
        ).hexdigest()
        now = time.monotonic()
        # Prune stale entries
        stale = [k for k, t in self._dedup_cache.items() if now - t > DEDUP_WINDOW]
        for k in stale:
            del self._dedup_cache[k]
        if key in self._dedup_cache:
            return True
        self._dedup_cache[key] = now
        return False

    async def _metrics_loop(self) -> None:
        """Periodically update Prometheus gauges from adapter/db state."""
        import os
        while True:
            try:
                await asyncio.sleep(15)
                # Adapter health
                for adapter in self._adapters.values():
                    h = await adapter.health()
                    M.update_adapter_health(
                        adapter.name,
                        h.state == "connected",
                        h.last_rx, h.last_tx,
                    )
                    # Node stats
                    nodes = await adapter.nodes()
                    for node in nodes:
                        M.update_node_stats(
                            adapter.name, node.node_id, node.display_name,
                            node.snr, node.rssi, node.battery_level, node.last_heard,
                        )
                # Message log size
                total = await self._db.message_count()
                M.msg_log_total.set(total)
                # DB file size
                try:
                    db_path = self._db._path
                    if os.path.exists(db_path):
                        M.db_size_bytes.set(os.path.getsize(db_path))
                except Exception:
                    pass
                # Anomaly active counts
                if self._anomaly_engine:
                    from collections import Counter as Ctr
                    active = self._anomaly_engine.active_findings()
                    rule_counts = dict(Ctr(f.rule for f in active))
                    M.update_anomaly_active(rule_counts)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.debug("Metrics loop error: %s", exc)

    async def broadcast_ws_event(self, event_type: str, data: dict) -> None:
        """Broadcast a typed event (non-message) to all WS clients."""
        if not self._ws_clients:
            return
        payload = json.dumps({"type": event_type, "data": data})
        dead = set()
        for ws in self._ws_clients:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.add(ws)
        self._ws_clients -= dead

    async def _broadcast_ws(self, msg: NormalizedMessage) -> None:
        if not self._ws_clients:
            return
        payload = json.dumps({"type": "message", "data": msg.to_dict()})
        dead = set()
        for ws in self._ws_clients:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.add(ws)
        self._ws_clients -= dead

    async def _apply_bridge_rules(self, msg: NormalizedMessage) -> None:
        """Forward a message to another adapter if a bridge rule matches."""
        for rule in self._bridge_rules:
            if rule.get("from_adapter") == msg.source_adapter:
                target_name = rule.get("to_adapter")
                target = self._adapters.get(target_name)
                if target and target._connected and not target._paused:
                    fwd = NormalizedMessage(
                        source_adapter=msg.source_adapter,
                        source_channel=msg.source_channel,
                        from_id=msg.from_id,
                        from_display=msg.from_display,
                        body=msg.body,
                        priority=msg.priority,
                        lat=msg.lat,
                        lon=msg.lon,
                        raw={**(msg.raw or {}), "_bridged_via": target_name},
                    )
                    await target.send(fwd)
                    log.debug("Bridge: forwarded %s → %s", msg.source_adapter, target_name)

    async def _handle_nodes_updated(self, adapter_name: str, node_count: int) -> None:
        """Broadcast a lightweight event so the UI can refresh its node panel."""
        await self.broadcast_ws_event("nodes_updated", {
            "adapter": adapter_name,
            "node_count": node_count,
        })

    async def _handle_delivery_status(self, adapter_name: str, msg_id: str,
                                       status: str, detail: str = "",
                                       path: list | None = None) -> None:
        """Called by adapters when send confirmation or delivery ACK arrives."""
        event: dict = {
            "msg_id": msg_id,
            "adapter": adapter_name,
            "status": status,
            "detail": detail,
        }
        if path:
            event["path"] = path
        await self.broadcast_ws_event("msg_status", event)
        # Persist so status survives page navigation
        if self._db:
            try:
                await self._db.update_delivery_status(msg_id, status, path=path or None)
            except Exception:
                pass
        log.debug("Delivery status: %s %s → %s (%s)", adapter_name, msg_id[:8], status, detail)

    # ── Outbound dispatch ─────────────────────────────────────────────────

    async def send(
        self,
        body: str,
        adapter_names: list[str] | None = None,
        to_id: str | None = None,
        priority: Priority = Priority.NORMAL,
        raw: dict | None = None,
    ) -> dict[str, bool]:
        """
        Send a message via one or more adapters.
        Returns {adapter_name: success_bool} for each target.

        ``raw`` is an optional metadata dict passed through to the adapter via
        NormalizedMessage.raw.  MeshCore uses ``raw["channel_idx"]`` to route
        a reply back to the originating channel instead of the adapter default.
        """
        targets = adapter_names or list(self._adapters.keys())
        results = {}
        for name in targets:
            adapter = self._adapters.get(name)
            if not adapter or not adapter._connected or adapter._paused:
                results[name] = False
                continue
            msg = NormalizedMessage(
                source_adapter=name,
                source_channel="outbound",
                from_id="local",
                from_display="ECH Operator",
                to_id=to_id,
                body=body,
                priority=priority,
                raw=raw or {},
            )
            ok = await adapter.send(msg)
            results[name] = ok
            if ok:
                M.record_message_sent(name)
            await self._mark_tx_in_db(msg)
        return results

    async def send_tracked(
        self,
        body: str,
        adapter_names: list[str] | None = None,
        to_id: str | None = None,
        priority: Priority = Priority.NORMAL,
    ) -> dict[str, dict]:
        """
        Like send() but returns {adapter: {"ok": bool, "msg_id": str}} so the
        UI can track each message and update its status when ACKs arrive.
        """
        targets = adapter_names or list(self._adapters.keys())
        results = {}
        for name in targets:
            adapter = self._adapters.get(name)
            if not adapter or not adapter._connected or adapter._paused:
                results[name] = {"ok": False, "msg_id": None}
                continue
            if to_id:
                source_channel = "DM"
            else:
                source_channel = getattr(adapter, 'tx_channel', None) or "outbound"
            msg = NormalizedMessage(
                source_adapter=name,
                source_channel=source_channel,
                from_id="local",
                from_display="ECH Operator",
                to_id=to_id,
                body=body,
                priority=priority,
            )
            adapter._last_sent_uuid = msg.id
            ok = await adapter.send(msg)
            results[name] = {"ok": ok, "msg_id": msg.id}
            await self._mark_tx_in_db(msg)  # INSERT first so delivery UPDATE finds the row
            if ok:
                M.record_message_sent(name)
                await self._handle_delivery_status(name, msg.id, "sent_to_air", "sent")
            else:
                await self._handle_delivery_status(name, msg.id, "failed", "send error")
        return results

    async def _mark_tx_in_db(self, msg: NormalizedMessage) -> None:
        await self._db.save_message(msg)

    # ── WebSocket registry ────────────────────────────────────────────────

    def add_ws_client(self, ws: "WebSocket") -> None:
        self._ws_clients.add(ws)

    def remove_ws_client(self, ws: "WebSocket") -> None:
        self._ws_clients.discard(ws)

    # ── Health / node queries ─────────────────────────────────────────────

    async def all_health(self) -> list[dict]:
        return [
            (await adapter.health()).to_dict()
            for adapter in self._adapters.values()
        ]

    async def nodes_for(self, adapter_name: str) -> list[dict]:
        adapter = self._adapters.get(adapter_name)
        if not adapter:
            return []
        return [(n.to_dict()) for n in await adapter.nodes()]
