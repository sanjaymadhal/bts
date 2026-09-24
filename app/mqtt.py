"""HiveMQ MQTT client for ingesting bus positions.

Ingestion is decoupled from Supabase writes: `on_message` only parses
and enqueues the payload on paho's network thread; a dedicated worker
thread owns every DB round-trip. Without this, a slow Supabase call
stalls the MQTT loop and queued messages pile up on the broker.

Per-bus state is cached in memory (bus-id resolution, last position for
stop-transition diffing, bus metadata) so the steady state is ~2 DB
calls per message instead of ~5, and nothing re-resolves a UUID on
every 3-second tick.
"""

from __future__ import annotations

import json
import logging
import math
import queue
import threading
import time
from typing import Any, Optional
from uuid import UUID

import paho.mqtt.client as mqtt
from supabase import create_client

from .config import get_settings
from .positions import _maybe_emit_stop_transition

logger = logging.getLogger(__name__)

#: How long a resolved bus-id stays valid before we re-query the DB.
_BUS_ID_CACHE_TTL_SECONDS = 300.0

#: How long a cached bus metadata (number + schedule) stays valid.
_METADATA_CACHE_TTL_SECONDS = 600.0

#: Bounded ingest queue. If the worker can't keep up we drop messages
#: with a warning rather than grow memory unboundedly.
_QUEUE_MAXSIZE = 1000


def _resolve_bus_id(supabase, identifier: str) -> str | None:
    """Resolve either a database UUID or the human-readable bus number."""
    try:
        UUID(identifier)
        column = "id"
    except ValueError:
        column = "number"
    row = (
        supabase.table("buses")
        .select("id")
        .eq(column, identifier)
        .limit(1)
        .execute()
        .data
    )
    return row[0]["id"] if row else None


class MQTTPositionClient:
    def __init__(self):
        self.settings = get_settings()
        self.client: Optional[mqtt.Client] = None

        # Supabase admin client for bypassing RLS
        self.supabase = create_client(
            self.settings.SUPABASE_URL,
            self.settings.SUPABASE_SERVICE_ROLE_KEY
        )

        # In-memory caches keyed by the raw payload identifier / bus id.
        # These collapse the per-message DB round-trips (2-3) into a
        # rare re-fetch (TTL'd) + a single upsert + single history insert.
        self._bus_id_cache: dict[str, tuple[str, float]] = {}
        self._last_positions: dict[str, dict[str, float]] = {}
        self._metadata_cache: dict[str, tuple[tuple[str, list], float]] = {}

        self._queue: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._worker: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _cached_bus_id(self, identifier: str) -> str | None:
        now = time.monotonic()
        hit = self._bus_id_cache.get(identifier)
        if hit and now - hit[1] < _BUS_ID_CACHE_TTL_SECONDS:
            return hit[0]
        resolved = _resolve_bus_id(self.supabase, identifier) if identifier else None
        if resolved:
            self._bus_id_cache[identifier] = (resolved, now)
        return resolved

    def _cached_metadata(self, bus_id: str) -> tuple[str, list] | None:
        now = time.monotonic()
        hit = self._metadata_cache.get(bus_id)
        if hit and now - hit[1] < _METADATA_CACHE_TTL_SECONDS:
            return hit[0]
        return None

    def _last_position(self, bus_id: str) -> dict[str, float] | None:
        return self._last_positions.get(bus_id)

    # ------------------------------------------------------------------
    # Paho callbacks (network thread — must stay fast)
    # ------------------------------------------------------------------

    def on_connect(self, client: mqtt.Client, userdata: Any, flags: dict, rc: int):
        if rc == 0:
            logger.info("Connected to HiveMQ MQTT Broker")
            # Subscribe to position updates
            client.subscribe("trackr/positions")
        else:
            logger.error(f"Failed to connect to MQTT Broker, return code {rc}")

    def on_message(self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage):
        try:
            payload = json.loads(msg.payload.decode())
            if not isinstance(payload, dict):
                raise ValueError("payload must be a JSON object")
        except Exception:
            logger.warning(f"Malformed MQTT payload: {msg.payload[:200]!r}")
            return
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            logger.warning("MQTT ingest queue full; dropping a position message")

    # ------------------------------------------------------------------
    # Worker (owns every Supabase call)
    # ------------------------------------------------------------------

    def _worker_loop(self):
        while not self._stop_event.is_set():
            try:
                payload = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._process(payload)
            except Exception as e:
                logger.error(f"Error processing MQTT message: {e}")

    def _process(self, payload: dict[str, Any]):
        identifier = str(payload.get("bus_id", "")).strip()
        bus_id = self._cached_bus_id(identifier)

        lat = float(payload["latitude"])
        lng = float(payload["longitude"])
        speed = float(payload.get("speed", 0.0))
        altitude = float(payload.get("altitude", 0.0))

        if (
            not bus_id
            or not all(math.isfinite(value) for value in (lat, lng, speed, altitude))
            or not -90 <= lat <= 90
            or not -180 <= lng <= 180
        ):
            logger.warning(f"Malformed MQTT payload: {payload}")
            return

        previous_position = self._last_position(bus_id)

        # Upsert the live position.
        self.supabase.table("bus_positions").upsert({
            "bus_id": bus_id,
            "latitude": lat,
            "longitude": lng,
            "speed": speed,
            "altitude": altitude,
        }).execute()

        # Track in memory so the next tick can diff without a SELECT.
        self._last_positions[bus_id] = {"latitude": lat, "longitude": lng}

        # Persist every valid device fix before notification side effects.
        # A stop-transition failure must never discard the history record.
        self.supabase.table("bus_position_history").insert({
            "bus_id": bus_id,
            "latitude": lat,
            "longitude": lng,
            "speed": speed,
            "altitude": altitude,
        }).execute()

        # Stop-transition fan-out. Pass cached metadata to avoid a
        # bus lookup per message; the helper loads it on cache miss.
        metadata = self._cached_metadata(bus_id)
        loaded = _maybe_emit_stop_transition(
            self.supabase,
            bus_id,
            float(lat),
            float(lng),
            previous_position,
            metadata,
        )
        if loaded is not None and self._cached_metadata(bus_id) is None:
            self._metadata_cache[bus_id] = (loaded, time.monotonic())

        logger.info(f"Updated position for bus {bus_id}: {lat}, {lng} | Speed: {speed} km/h | Alt: {altitude}m")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        # HiveMQ default public broker for testing
        # In production, these would be in settings.py
        broker = "broker.hivemq.com"
        port = 1883

        self._stop_event.clear()
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="mqtt-position-worker",
            daemon=True,
        )
        self._worker.start()

        self.client = mqtt.Client()
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

        try:
            self.client.connect(broker, port, 60)
            self.client.loop_start()
            logger.info(f"MQTT Client started. Connected to {broker}:{port}")
        except Exception as e:
            logger.error(f"Could not start MQTT client: {e}")

    def stop(self):
        self._stop_event.set()
        if self._worker:
            self._worker.join(timeout=3.0)
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
            logger.info("MQTT Client stopped")

# Singleton instance
mqtt_manager = MQTTPositionClient()