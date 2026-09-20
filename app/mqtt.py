"""HiveMQ MQTT client for ingesting bus positions."""

from __future__ import annotations

import json
import logging
import math
from typing import Any, Optional
from uuid import UUID

import paho.mqtt.client as mqtt
from supabase import create_client

from .config import get_settings
from .positions import _maybe_emit_stop_transition

logger = logging.getLogger(__name__)


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
            identifier = str(payload.get("bus_id", "")).strip()
            bus_id = _resolve_bus_id(self.supabase, identifier) if identifier else None

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

            # Upsert into Supabase (reusing logic from positions.py)
            previous = (
                self.supabase.table("bus_positions")
                .select("latitude, longitude")
                .eq("bus_id", bus_id)
                .maybe_single()
                .execute()
            )
            previous_position = previous.data if getattr(previous, "data", None) else None

            self.supabase.table("bus_positions").upsert({
                "bus_id": bus_id,
                "latitude": lat,
                "longitude": lng,
                "speed": speed,
                "altitude": altitude,
            }).execute()

            _maybe_emit_stop_transition(
                self.supabase,
                bus_id,
                float(lat),
                float(lng),
                previous_position,
            )

            # Log to history
            self.supabase.table("bus_position_history").insert({
                "bus_id": bus_id,
                "latitude": lat,
                "longitude": lng,
                "speed": speed,
                "altitude": altitude,
            }).execute()

            logger.info(f"Updated position for bus {bus_id}: {lat}, {lng} | Speed: {speed} km/h | Alt: {altitude}m")

        except Exception as e:
            logger.error(f"Error processing MQTT message: {e}")

    def start(self):
        # HiveMQ default public broker for testing
        # In production, these would be in settings.py
        broker = "broker.hivemq.com"
        port = 1883

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
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
            logger.info("MQTT Client stopped")

# Singleton instance
mqtt_manager = MQTTPositionClient()
