"""Sensors for EG.D OpenAPI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_EAN,
    ATTR_LAST_API_SYNC_UTC,
    ATTR_LAST_CHECK_FINISHED_UTC,
    ATTR_LAST_CHECK_STARTED_UTC,
    ATTR_LAST_ERROR,
    ATTR_LAST_EXPORT_STATUS,
    ATTR_LAST_IMPORT_STATUS,
    ATTR_LAST_MANUAL_REFRESH_RESULT,
    ATTR_LAST_MANUAL_REFRESH_UTC,
    ATTR_LAST_UPDATE_UTC,
    ATTR_LAST_VALID_EXPORT_TS,
    ATTR_LAST_VALID_IMPORT_TS,
    ATTR_NEXT_SYNC_ATTEMPT_UTC,
    ATTR_NEXT_SYNC_REASON,
    ATTR_SYNC_STATUS,
    CONF_EAN,
    DOMAIN,
)
from .coordinator import EgdDataUpdateCoordinator


@dataclass(frozen=True, kw_only=True)
class EgdSensorDescription(SensorEntityDescription):
    value_key: str


SENSORS: tuple[EgdSensorDescription, ...] = (
    EgdSensorDescription(
        key="total_import",
        translation_key="total_import",
        name="Total import",
        value_key="total_import_kwh",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=3,
    ),
    EgdSensorDescription(
        key="total_export",
        translation_key="total_export",
        name="Total export",
        value_key="total_export_kwh",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=3,
    ),
    EgdSensorDescription(
        key="sync_status",
        translation_key="sync_status",
        name="Sync status",
        value_key="sync_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        icon="mdi:sync",
    ),
    EgdSensorDescription(
        key="last_api_sync",
        translation_key="last_api_sync",
        name="Last successful sync",
        value_key="last_api_sync_utc",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        icon="mdi:clock-check-outline",
    ),
    EgdSensorDescription(
        key="next_sync_attempt",
        translation_key="next_sync_attempt",
        name="Next sync attempt",
        value_key="next_sync_attempt_utc",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        icon="mdi:clock-start",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EgdDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EgdEnergySensor(coordinator, entry, description) for description in SENSORS])


class EgdEnergySensor(CoordinatorEntity[EgdDataUpdateCoordinator], SensorEntity):
    """EG.D cumulative energy sensor."""

    entity_description: EgdSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EgdDataUpdateCoordinator,
        entry: ConfigEntry,
        description: EgdSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_translation_key = description.translation_key

        ean = entry.data[CONF_EAN]

        self._attr_device_info = {
            "identifiers": {(DOMAIN, ean)},
            "name": entry.title,
            "manufacturer": "EG.D",
            "model": "OpenAPI Smart Meter",
            "serial_number": ean,
        }

        if description.key == "sync_status":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = ["checking_for_updates", "ok", "waiting_for_data", "error"]

    @property
    def native_value(self) -> Any | None:
        data = self.coordinator.data
        if data is None:
            return None
        value = getattr(data, self.entity_description.value_key)
        if self.entity_description.device_class is SensorDeviceClass.TIMESTAMP and isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        attrs: dict[str, Any] = {ATTR_EAN: self._entry.data[CONF_EAN]}
        if data is None:
            return attrs

        attrs[ATTR_LAST_API_SYNC_UTC] = data.last_api_sync_utc
        attrs[ATTR_LAST_UPDATE_UTC] = data.last_update_utc
        attrs[ATTR_SYNC_STATUS] = data.sync_status
        attrs[ATTR_LAST_CHECK_STARTED_UTC] = data.last_check_started_utc
        attrs[ATTR_LAST_CHECK_FINISHED_UTC] = data.last_check_finished_utc
        attrs[ATTR_NEXT_SYNC_ATTEMPT_UTC] = data.next_sync_attempt_utc
        attrs[ATTR_NEXT_SYNC_REASON] = data.next_sync_reason

        if self.entity_description.key in {"sync_status", "last_api_sync", "next_sync_attempt"}:
            attrs[ATTR_LAST_ERROR] = data.last_error
            attrs[ATTR_LAST_VALID_IMPORT_TS] = data.last_valid_import_timestamp
            attrs[ATTR_LAST_VALID_EXPORT_TS] = data.last_valid_export_timestamp
            attrs[ATTR_LAST_IMPORT_STATUS] = data.last_import_status
            attrs[ATTR_LAST_EXPORT_STATUS] = data.last_export_status
            attrs[ATTR_LAST_MANUAL_REFRESH_UTC] = data.last_manual_refresh_utc
            attrs[ATTR_LAST_MANUAL_REFRESH_RESULT] = data.last_manual_refresh_result
            return attrs

        if self.entity_description.key == "total_import":
            attrs[ATTR_LAST_VALID_IMPORT_TS] = data.last_valid_import_timestamp
            attrs[ATTR_LAST_IMPORT_STATUS] = data.last_import_status
        else:
            attrs[ATTR_LAST_VALID_EXPORT_TS] = data.last_valid_export_timestamp
            attrs[ATTR_LAST_EXPORT_STATUS] = data.last_export_status
        return attrs
