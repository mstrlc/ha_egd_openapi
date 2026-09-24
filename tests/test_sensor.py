"""Unit tests for EG.D sensor entities."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")
from homeassistant.components.sensor import SensorStateClass

from custom_components.ha_egd_openapi.const import CONF_EAN
from custom_components.ha_egd_openapi.coordinator import EnergyState
from custom_components.ha_egd_openapi.sensor import EgdEnergySensor, SENSORS


class _FakeCoordinator:
    """Minimal coordinator stub for entity tests."""

    def __init__(self, state: EnergyState) -> None:
        self.data = state
        self.last_update_success = True

    def async_add_listener(self, update_callback):
        """Match coordinator listener API."""
        return lambda: None


def _build_entry() -> SimpleNamespace:
    """Create a lightweight stand-in for a config entry."""
    return SimpleNamespace(
        entry_id="entry-1",
        title="My EG.D",
        data={CONF_EAN: "859182400000000000"},
    )


def _build_state() -> EnergyState:
    """Build a representative runtime state."""
    return EnergyState(
        total_import_kwh=123.456,
        total_export_kwh=78.9,
        last_valid_import_timestamp="2026-04-11T23:45:00Z",
        last_valid_export_timestamp="2026-04-11T23:45:00Z",
        last_import_status="IU012",
        last_export_status="IU012",
        last_api_sync_utc="2026-04-12T06:00:00Z",
        last_update_utc="2026-04-12T06:00:00Z",
        sync_status="ok",
        last_error=None,
        last_check_started_utc="2026-04-12T05:59:58Z",
        last_check_finished_utc="2026-04-12T06:00:00Z",
        next_sync_attempt_utc="2026-04-12T16:17:00Z",
        next_sync_reason="scheduled_daily",
        last_manual_refresh_utc=None,
        last_manual_refresh_result=None,
    )


def _build_sensor(sensor_key: str, state: EnergyState) -> EgdEnergySensor:
    """Create a sensor entity with a fake coordinator."""
    description = next(description for description in SENSORS if description.key == sensor_key)
    coordinator = _FakeCoordinator(state)
    return EgdEnergySensor(coordinator, _build_entry(), description)


def test_sync_status_sensor_exposes_diagnostic_attributes() -> None:
    """Sync status sensor should surface diagnostic metadata including errors."""
    state = replace(_build_state(), sync_status="error", last_error="Timeout")
    sensor = _build_sensor("sync_status", state)

    assert sensor.native_value == "error"
    assert sensor.device_info["serial_number"] == "859182400000000000"
    assert sensor.extra_state_attributes["last_error"] == "Timeout"
    assert sensor.extra_state_attributes["last_valid_import_timestamp"] == "2026-04-11T23:45:00Z"
    assert sensor.extra_state_attributes["last_check_started_utc"] == "2026-04-12T05:59:58Z"


def test_last_api_sync_sensor_returns_datetime_value() -> None:
    """Timestamp diagnostic sensor should expose a parsed datetime."""
    sensor = _build_sensor("last_api_sync", _build_state())

    assert sensor.native_value == datetime(2026, 4, 12, 6, 0, tzinfo=timezone.utc)
    assert sensor.extra_state_attributes["sync_status"] == "ok"


def test_total_import_sensor_keeps_energy_value_and_import_attributes() -> None:
    """Energy sensor should keep kWh value and only relevant import metadata."""
    sensor = _build_sensor("total_import", _build_state())

    assert sensor.native_value == 123.456
    assert sensor.entity_description.state_class is SensorStateClass.TOTAL
    assert sensor.extra_state_attributes["last_import_status"] == "IU012"
    assert "last_export_status" not in sensor.extra_state_attributes


def test_total_export_sensor_uses_total_state_class() -> None:
    """Export energy sensor should be eligible for long-term statistics.

    The value is a self-computed cumulative total, not a hardware counter, so
    `total` is used: `total_increasing` would read any transient drop (restart,
    reload, rebuilt cache) as a meter reset and double-count the whole total.
    """
    sensor = _build_sensor("total_export", _build_state())

    assert sensor.native_value == 78.9
    assert sensor.entity_description.state_class is SensorStateClass.TOTAL


def test_next_sync_attempt_sensor_is_timestamp_and_exposes_reason() -> None:
    """Disabled-by-default diagnostic sensor should expose the next retry plan."""
    sensor = _build_sensor("next_sync_attempt", _build_state())

    assert sensor.entity_description.entity_registry_enabled_default is False
    assert sensor.native_value == datetime(2026, 4, 12, 16, 17, tzinfo=timezone.utc)
    assert sensor.extra_state_attributes["next_sync_reason"] == "scheduled_daily"


def test_all_diagnostic_sensors_are_disabled_by_default() -> None:
    """Diagnostic sensors should stay opt-in in the entity registry."""
    diagnostic_keys = {"sync_status", "last_api_sync", "next_sync_attempt"}

    for description in SENSORS:
        if description.key in diagnostic_keys:
            assert description.entity_registry_enabled_default is False
