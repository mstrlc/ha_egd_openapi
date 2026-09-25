"""EG.D OpenAPI integration."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging

import voluptuous as vol

from homeassistant.components.recorder import get_instance
from homeassistant.helpers import config_validation as cv
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import aiohttp_client, event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import EgdApiClient, EgdApiError, EgdAuthError
from .const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_EAN,
    CONF_UPDATE_HOUR,
    CONF_UPDATE_MINUTE,
    DOMAIN,
    PLATFORMS,
    STORE_KEY,
    STORE_VERSION,
)
from .coordinator import IMPORT_SERIES_NAMES, EgdDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

SERVICE_REMOVE_STATISTICS = "egd_remove_statistics_entity"
SERVICE_FORCE_REFRESH = "force_refresh"
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_SCHEMA_REMOVE_STATISTICS = vol.Schema(
    {
        vol.Optional("entry_id"): str,
        vol.Optional("ean"): str,
    }
)

SERVICE_SCHEMA_FORCE_REFRESH = vol.Schema(
    {
        vol.Optional("entry_id"): str,
        vol.Optional("ean"): str,
    }
)


def _build_statistic_ids_for_ean(ean: str) -> list[str]:
    """Build statistic IDs for a single EAN."""
    clean_ean = str(ean).strip()
    return [
        f"{DOMAIN}:meter_{clean_ean}_import",
        f"{DOMAIN}:meter_{clean_ean}_export",
        *(f"{DOMAIN}:meter_{clean_ean}_{suffix}" for suffix in IMPORT_SERIES_NAMES),
    ]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up EG.D integration domain."""
    hass.data.setdefault(DOMAIN, {})

    def _match_entries(call: ServiceCall) -> list[ConfigEntry]:
        """Return config entries targeted by a service call."""
        entry_id: str | None = call.data.get("entry_id")
        ean: str | None = call.data.get("ean")
        matched_entries: list[ConfigEntry] = []

        for entry in hass.config_entries.async_entries(DOMAIN):
            entry_ean = str(entry.data.get(CONF_EAN, "")).strip()
            if not entry_ean:
                continue

            if entry_id and entry.entry_id != entry_id:
                continue
            if ean and entry_ean != str(ean).strip():
                continue

            matched_entries.append(entry)

        return matched_entries

    async def _handle_remove_statistics(call: ServiceCall) -> None:
        """Remove external statistics and matching integration store."""
        entry_id: str | None = call.data.get("entry_id")
        ean: str | None = call.data.get("ean")

        statistic_ids: list[str] = []
        matched_entries = _match_entries(call)

        for entry in matched_entries:
            entry_ean = str(entry.data.get(CONF_EAN, "")).strip()
            statistic_ids.extend(_build_statistic_ids_for_ean(entry_ean))

        # Allow cleanup by explicit EAN even if no config entry currently exists
        if not statistic_ids and ean:
            statistic_ids.extend(_build_statistic_ids_for_ean(str(ean).strip()))

        if not statistic_ids:
            _LOGGER.warning(
                "No EG.D statistics matched for removal (entry_id=%s, ean=%s)",
                entry_id,
                ean,
            )
            return

        _LOGGER.warning("Removing EG.D statistics: %s", statistic_ids)
        get_instance(hass).async_clear_statistics(statistic_ids)

        # Remove persisted checkpoints so next refresh rebuilds history.
        for entry in matched_entries:
            store = Store[dict](hass, STORE_VERSION, f"{STORE_KEY}_{entry.entry_id}")
            await store.async_remove()
            _LOGGER.warning(
                "Removed EG.D store for entry_id=%s ean=%s",
                entry.entry_id,
                entry.data.get(CONF_EAN),
            )

            # Also clear coordinator in-memory cache if currently loaded.
            coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
            if coordinator is not None:
                coordinator._persisted = {}  # noqa: SLF001

    async def _handle_force_refresh(call: ServiceCall) -> dict[str, object]:
        """Force a refresh outside the configured daily schedule."""
        matched_entries = _match_entries(call)
        if not matched_entries:
            entry_id: str | None = call.data.get("entry_id")
            ean: str | None = call.data.get("ean")
            return {
                "ok": False,
                "results": [],
                "message": f"No EG.D entries matched for force refresh (entry_id={entry_id}, ean={ean})",
            }

        results: list[dict[str, object]] = []
        overall_ok = True

        for entry in matched_entries:
            coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
            entry_ean = str(entry.data.get(CONF_EAN, "")).strip()
            if coordinator is None:
                overall_ok = False
                results.append(
                    {
                        "entry_id": entry.entry_id,
                        "ean": entry_ean,
                        "result": "error",
                        "message": "Coordinator is not loaded",
                    }
                )
                continue

            previous_sync = coordinator.data.last_api_sync_utc if coordinator.data else None
            automatic_state_snapshot = coordinator.snapshot_automatic_state()
            coordinator._record_diagnostic_event(  # noqa: SLF001
                "info",
                "manual_refresh_requested",
                {"entry_id": entry.entry_id},
            )

            try:
                await coordinator.async_request_refresh()
                state = coordinator.data
                if state is None:
                    result = "error"
                    message = "Refresh finished without state"
                    overall_ok = False
                elif state.sync_status == "error":
                    result = "error"
                    message = state.last_error or "Refresh failed"
                    overall_ok = False
                elif state.last_api_sync_utc != previous_sync:
                    result = "new_data_loaded"
                    message = "New data loaded successfully"
                elif state.sync_status == "waiting_for_data":
                    result = "waiting_for_data"
                    message = "No newer data are available yet"
                else:
                    result = "up_to_date"
                    message = "Data are already up to date"

                await coordinator.async_store_manual_refresh_result(result)
                if result != "new_data_loaded":
                    await coordinator.async_restore_automatic_state(automatic_state_snapshot)
                    state = coordinator.data
                coordinator._record_diagnostic_event(  # noqa: SLF001
                    "info" if result != "error" else "error",
                    "manual_refresh_completed",
                    {
                        "entry_id": entry.entry_id,
                        "result": result,
                        "message": message,
                    },
                )
                await coordinator._store.async_save(coordinator._persisted)  # noqa: SLF001

                results.append(
                    {
                        "entry_id": entry.entry_id,
                        "ean": entry_ean,
                        "result": result,
                        "message": message,
                        "sync_status": state.sync_status if state is not None else None,
                        "last_api_sync_utc": state.last_api_sync_utc if state is not None else None,
                        "last_error": state.last_error if state is not None else None,
                    }
                )
            except Exception as err:  # noqa: BLE001
                overall_ok = False
                await coordinator.async_store_manual_refresh_result("error")
                await coordinator.async_restore_automatic_state(automatic_state_snapshot)
                coordinator._record_diagnostic_event(  # noqa: SLF001
                    "error",
                    "manual_refresh_failed",
                    {
                        "entry_id": entry.entry_id,
                        "reason": str(err),
                    },
                )
                await coordinator._store.async_save(coordinator._persisted)  # noqa: SLF001
                results.append(
                    {
                        "entry_id": entry.entry_id,
                        "ean": entry_ean,
                        "result": "error",
                        "message": str(err),
                    }
                )

        return {
            "ok": overall_ok,
            "results": results,
        }

    if not hass.services.has_service(DOMAIN, SERVICE_REMOVE_STATISTICS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_REMOVE_STATISTICS,
            _handle_remove_statistics,
            schema=SERVICE_SCHEMA_REMOVE_STATISTICS,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_FORCE_REFRESH):
        hass.services.async_register(
            DOMAIN,
            SERVICE_FORCE_REFRESH,
            _handle_force_refresh,
            schema=SERVICE_SCHEMA_FORCE_REFRESH,
            supports_response=SupportsResponse.OPTIONAL,
        )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up EG.D from a config entry."""
    session = aiohttp_client.async_get_clientsession(hass)
    client = EgdApiClient(
        session=session,
        client_id=entry.data[CONF_CLIENT_ID],
        client_secret=entry.data[CONF_CLIENT_SECRET],
    )
    coordinator = EgdDataUpdateCoordinator(hass, entry, client)
    await coordinator.async_load()

    if coordinator.should_refresh_on_startup():
        try:
            await coordinator.async_config_entry_first_refresh()
        except EgdAuthError as err:
            raise ConfigEntryNotReady(f"Authentication not ready yet: {err}") from err
        except EgdApiError as err:
            raise ConfigEntryNotReady(str(err)) from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _handle_time(now: datetime) -> None:
        _LOGGER.debug("Scheduled EG.D refresh at %s", now)
        await coordinator.async_request_refresh()

    async def _handle_watchdog(now: datetime) -> None:
        """Recover from missed refreshes or delayed EG.D publication."""
        scheduled_hour = entry.options.get(CONF_UPDATE_HOUR, entry.data[CONF_UPDATE_HOUR])
        scheduled_minute = entry.options.get(CONF_UPDATE_MINUTE, entry.data[CONF_UPDATE_MINUTE])
        now_local = dt_util.as_local(now)

        # Do not retry before the user-configured daily sync time.
        if (now_local.hour, now_local.minute) < (scheduled_hour, scheduled_minute):
            await coordinator.async_refresh_next_sync_projection(now.astimezone(timezone.utc))
            return

        if not coordinator.should_retry_refresh():
            return

        _LOGGER.debug(
            "EG.D watchdog requesting catch-up refresh at %s because latest import data are still missing",
            now_local,
        )
        await coordinator.async_request_refresh()

    @callback
    def _schedule_daily_refresh() -> None:
        daily_unsub = event.async_track_time_change(
            hass,
            _handle_time,
            hour=entry.options.get(CONF_UPDATE_HOUR, entry.data[CONF_UPDATE_HOUR]),
            minute=entry.options.get(CONF_UPDATE_MINUTE, entry.data[CONF_UPDATE_MINUTE]),
            second=0,
        )
        watchdog_unsub = event.async_track_time_interval(
            hass,
            _handle_watchdog,
            timedelta(hours=1),
        )
        entry.async_on_unload(daily_unsub)
        entry.async_on_unload(watchdog_unsub)

    _schedule_daily_refresh()

    async def _reload_entry(hass_: HomeAssistant, updated_entry: ConfigEntry) -> None:
        await hass_.config_entries.async_reload(updated_entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
