"""Coordinator for EG.D OpenAPI."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from math import isfinite
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    EgdApiClient,
    EgdApiError,
    EgdAuthError,
    IntervalRecord,
    get_history_start,
)
from .const import (
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
    CONF_ENABLE_DIAGNOSTICS,
    CONF_EXPORT_PROFILE,
    CONF_IMPORT_PROFILE,
    CONF_PRICE_ENTITY,
    CONF_REVALIDATE_DAYS,
    CONF_UPDATE_HOUR,
    CONF_UPDATE_MINUTE,
    DEFAULT_ENABLE_DIAGNOSTICS,
    DEFAULT_REVALIDATE_DAYS,
    DIAGNOSTICS_EVENTS_KEY,
    DOMAIN,
    MAX_DIAGNOSTIC_EVENTS,
    PROFILE_UNITS,
    STORE_KEY,
    STORE_VERSION,
)
from .cost import async_fetch_hourly_prices, fill_missing_prices
from .statistics import async_import_external_statistics

_LOGGER = logging.getLogger(__name__)

PROFILE_MIN_DATES: dict[str, date] = {
    "ICQ2": date(2024, 7, 1),
    "ISQ2": date(2024, 7, 1),
}

# W is valid in the EG.D guide; IU012 is the legacy code.
ALLOWED_STATUSES = {"IU012", "W"}


@dataclass(slots=True)
class EnergyState:
    """Runtime state exposed to entities."""

    total_import_kwh: float
    total_export_kwh: float
    last_valid_import_timestamp: str | None
    last_valid_export_timestamp: str | None
    last_import_status: str | None
    last_export_status: str | None
    last_api_sync_utc: str | None
    last_update_utc: str | None
    sync_status: str
    last_error: str | None
    last_check_started_utc: str | None
    last_check_finished_utc: str | None
    next_sync_attempt_utc: str | None
    next_sync_reason: str | None
    last_manual_refresh_utc: str | None
    last_manual_refresh_result: str | None


class EgdDataUpdateCoordinator(DataUpdateCoordinator[EnergyState]):
    """Coordinates fetching, statistics import and cumulative totals."""

    _IMPORT_CACHE_KEY = "import_hourly_deltas"
    _EXPORT_CACHE_KEY = "export_hourly_deltas"
    _IMPORT_CACHE_COMPLETE_KEY = "import_hourly_deltas_complete"
    _EXPORT_CACHE_COMPLETE_KEY = "export_hourly_deltas_complete"
    _IMPORT_ACCESSIBLE_START_KEY = "import_accessible_start"
    _EXPORT_ACCESSIBLE_START_KEY = "export_accessible_start"
    _IMPORT_PRICES_KEY = "import_hourly_prices"
    _IMPORT_COSTS_KEY = "import_hourly_costs"
    _IMPORT_COST_PRICE_ENTITY_KEY = "import_cost_price_entity"
    _MANUAL_RESTORE_KEYS = (
        ATTR_LAST_API_SYNC_UTC,
        ATTR_LAST_UPDATE_UTC,
        ATTR_SYNC_STATUS,
        ATTR_LAST_ERROR,
        ATTR_LAST_CHECK_STARTED_UTC,
        ATTR_LAST_CHECK_FINISHED_UTC,
        ATTR_NEXT_SYNC_ATTEMPT_UTC,
        ATTR_NEXT_SYNC_REASON,
    )

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: EgdApiClient) -> None:
        self.hass = hass
        self.config_entry = entry
        self.client = client
        self._store = Store(hass, STORE_VERSION, f"{STORE_KEY}_{entry.entry_id}")
        self._persisted: dict[str, Any] = {}
        self.client.set_diagnostic_logger(self._record_diagnostic_event)

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(hours=24),
        )

    async def async_load(self) -> None:
        """Load persisted state."""
        self._persisted = await self._store.async_load() or {}
        self._persisted.setdefault(DIAGNOSTICS_EVENTS_KEY, [])
        self.data = self._build_state_from_persisted()

    def should_retry_refresh(self) -> bool:
        """Return whether latest expected import or export data are still missing.

        This is used by a lightweight watchdog to recover from missed daily
        schedule callbacks or from cases where EG.D publishes the previous day
        later than the configured refresh time.
        """
        latest_available_utc = self._get_latest_available_utc()
        last_valid_import = self._parse_dt(self._persisted.get(ATTR_LAST_VALID_IMPORT_TS))
        last_valid_export = self._parse_dt(self._persisted.get(ATTR_LAST_VALID_EXPORT_TS))
        return self._is_waiting_for_latest_data(
            latest_available_utc=latest_available_utc,
            last_valid_import_ts=last_valid_import,
            last_valid_export_ts=last_valid_export,
        )

    def should_refresh_on_startup(self) -> bool:
        """Return whether setup should trigger an immediate refresh.

        When a persisted state exists, respect the stored next sync attempt so
        a Home Assistant restart does not bypass the configured schedule.
        """
        if self.data is None:
            return True

        for profile_key in (CONF_IMPORT_PROFILE, CONF_EXPORT_PROFILE):
            profile = self.config_entry.options.get(
                profile_key, self.config_entry.data.get(profile_key)
            )
            previous_profile = self._persisted.get(
                profile_key, self.config_entry.data.get(profile_key)
            )
            if profile != previous_profile:
                return True

        next_sync_attempt = self._parse_dt(self._persisted.get(ATTR_NEXT_SYNC_ATTEMPT_UTC))
        if next_sync_attempt is None:
            return True

        return next_sync_attempt <= dt_util.utcnow().astimezone(timezone.utc)

    async def _async_update_data(self) -> EnergyState:
        """Fetch data from API and update cumulative totals."""
        check_started_utc = self._iso(dt_util.utcnow().astimezone(timezone.utc))
        self._record_diagnostic_event(
            "info",
            "refresh_started",
            {"entry_id": self.config_entry.entry_id},
        )
        self._persisted[ATTR_LAST_CHECK_STARTED_UTC] = check_started_utc
        if self.data is not None:
            self.data = replace(
                self.data,
                sync_status="checking_for_updates",
                last_error=None,
                last_check_started_utc=check_started_utc,
            )
            self.async_update_listeners()
        try:
            return await self._async_refresh_energy_state()
        except EgdAuthError as err:
            self._record_diagnostic_event(
                "error",
                "refresh_auth_failed",
                {"reason": str(err)},
            )
            self._store_error_state(str(err))
            await self._store.async_save(self._persisted)
            raise ConfigEntryAuthFailed(str(err)) from err
        except EgdApiError as err:
            self._record_diagnostic_event(
                "error",
                "refresh_failed",
                {"reason": str(err)},
            )
            self._store_error_state(str(err))
            await self._store.async_save(self._persisted)
            if self.data is not None:
                _LOGGER.warning("EG.D refresh failed, keeping last known data: %s", err)
                return replace(
                    self.data,
                    last_api_sync_utc=self._persisted.get(ATTR_LAST_API_SYNC_UTC),
                    sync_status="error",
                    last_error=str(err),
                    last_check_started_utc=self._persisted.get(ATTR_LAST_CHECK_STARTED_UTC),
                    last_check_finished_utc=self._persisted.get(ATTR_LAST_CHECK_FINISHED_UTC),
                )
            raise UpdateFailed(str(err)) from err

    async def _async_refresh_energy_state(self) -> EnergyState:
        """Refresh import/export state from EG.D."""
        ean = str(self.config_entry.data[CONF_EAN]).strip()

        import_profile = self.config_entry.options.get(
            CONF_IMPORT_PROFILE,
            self.config_entry.data[CONF_IMPORT_PROFILE],
        )
        export_profile = self.config_entry.options.get(
            CONF_EXPORT_PROFILE,
            self.config_entry.data[CONF_EXPORT_PROFILE],
        )

        now_utc = dt_util.utcnow().astimezone(timezone.utc)
        latest_available_utc = self._get_latest_available_utc()

        import_statistic_id = f"{DOMAIN}:meter_{ean}_import"
        export_statistic_id = f"{DOMAIN}:meter_{ean}_export"

        import_from = await self._determine_start_timestamp(
            ean=ean,
            profile=import_profile,
            latest_available_utc=latest_available_utc,
            cache_complete_key=self._IMPORT_CACHE_COMPLETE_KEY,
            accessible_start_key=self._IMPORT_ACCESSIBLE_START_KEY,
            last_valid_key=ATTR_LAST_VALID_IMPORT_TS,
            profile_key=CONF_IMPORT_PROFILE,
        )
        export_from = await self._determine_start_timestamp(
            ean=ean,
            profile=export_profile,
            latest_available_utc=latest_available_utc,
            cache_complete_key=self._EXPORT_CACHE_COMPLETE_KEY,
            accessible_start_key=self._EXPORT_ACCESSIBLE_START_KEY,
            last_valid_key=ATTR_LAST_VALID_EXPORT_TS,
            profile_key=CONF_EXPORT_PROFILE,
        )
        _LOGGER.debug(
            "Refreshing EG.D state for EAN %s: import profile %s from %s, export profile %s from %s, latest available %s",
            ean,
            import_profile,
            import_from.isoformat() if import_from else None,
            export_profile,
            export_from.isoformat() if export_from else None,
            latest_available_utc.isoformat(),
        )
        self._record_diagnostic_event(
            "debug",
            "refresh_window_resolved",
            {
                "ean_suffix": ean[-4:],
                "import_profile": import_profile,
                "import_from": import_from.isoformat() if import_from else None,
                "export_profile": export_profile,
                "export_from": export_from.isoformat() if export_from else None,
                "latest_available_utc": latest_available_utc.isoformat(),
            },
        )

        import_records: list[IntervalRecord] = []
        export_records: list[IntervalRecord] = []

        if import_from and import_from <= latest_available_utc:
            import_records = await self.client.async_get_profile_data(
                ean=ean,
                profile=import_profile,
                from_dt=import_from,
                to_dt=latest_available_utc,
            )

        if export_from and export_from <= latest_available_utc:
            export_records = await self.client.async_get_profile_data(
                ean=ean,
                profile=export_profile,
                from_dt=export_from,
                to_dt=latest_available_utc,
            )

        import_hourly, import_meta = self._process_records_hourly(
            records=import_records,
            profile=import_profile,
        )
        export_hourly, export_meta = self._process_records_hourly(
            records=export_records,
            profile=export_profile,
        )
        self._record_diagnostic_event(
            "debug",
            "records_processed",
            {
                "ean_suffix": ean[-4:],
                "import_records": len(import_records),
                "export_records": len(export_records),
                "import_hours": len(import_hourly),
                "export_hours": len(export_hourly),
                "last_import_status": import_meta["last_status"],
                "last_export_status": export_meta["last_status"],
            },
        )

        total_import, import_stats = self._merge_statistics(
            cache_key=self._IMPORT_CACHE_KEY,
            cache_complete_key=self._IMPORT_CACHE_COMPLETE_KEY,
            persisted_total_key="total_import_kwh",
            fetched_from=import_from,
            latest_available_utc=latest_available_utc,
            profile=import_profile,
            accessible_start_key=self._IMPORT_ACCESSIBLE_START_KEY,
            hourly_deltas=import_hourly,
        )
        total_export, export_stats = self._merge_statistics(
            cache_key=self._EXPORT_CACHE_KEY,
            cache_complete_key=self._EXPORT_CACHE_COMPLETE_KEY,
            persisted_total_key="total_export_kwh",
            fetched_from=export_from,
            latest_available_utc=latest_available_utc,
            profile=export_profile,
            accessible_start_key=self._EXPORT_ACCESSIBLE_START_KEY,
            hourly_deltas=export_hourly,
        )

        if import_stats:
            await async_import_external_statistics(
                self.hass,
                statistic_id=import_statistic_id,
                name=f"{self.config_entry.title} Odběr",
                source=DOMAIN,
                rows=import_stats,
            )
            _LOGGER.debug(
                "Imported %s hourly import rows for EAN %s starting from %s",
                len(import_stats),
                ean,
                import_from.isoformat() if import_from else None,
            )
        else:
            _LOGGER.debug(
                "No new import statistics for EAN %s (from=%s to=%s, last_valid=%s)",
                ean,
                import_from.isoformat() if import_from else None,
                latest_available_utc.isoformat(),
                self._persisted.get(ATTR_LAST_VALID_IMPORT_TS),
            )

        import_cost_stats = await self._async_refresh_import_cost(
            latest_available_utc=latest_available_utc,
        )
        if import_cost_stats:
            await async_import_external_statistics(
                self.hass,
                statistic_id=f"{DOMAIN}:meter_{ean}_import_cost",
                name=f"{self.config_entry.title} Náklady na odběr",
                source=DOMAIN,
                rows=import_cost_stats,
                currency=self.hass.config.currency,
            )

        if export_stats:
            await async_import_external_statistics(
                self.hass,
                statistic_id=export_statistic_id,
                name=f"{self.config_entry.title} Dodávka",
                source=DOMAIN,
                rows=export_stats,
            )
            _LOGGER.debug(
                "Imported %s hourly export rows for EAN %s starting from %s",
                len(export_stats),
                ean,
                export_from.isoformat() if export_from else None,
            )
        else:
            _LOGGER.debug(
                "No new export statistics for EAN %s (from=%s to=%s, last_valid=%s)",
                ean,
                export_from.isoformat() if export_from else None,
                latest_available_utc.isoformat(),
                self._persisted.get(ATTR_LAST_VALID_EXPORT_TS),
            )

        # Důležité:
        # last_valid_* se posouvá jen pokud opravdu přišla nová validní data.
        sync_status = (
            "waiting_for_data"
            if self._is_waiting_for_latest_data(
                latest_available_utc=latest_available_utc,
                last_valid_import_ts=import_meta["last_valid_ts"],
                last_valid_export_ts=export_meta["last_valid_ts"],
            )
            else "ok"
        )
        # last_api_sync_utc tracks completed syncs only. Revalidation may write
        # older corrected rows while the newest expected day is still missing;
        # that must not make the "last successful sync" sensor look fresh.
        should_advance_successful_sync = self._should_advance_successful_sync(
            sync_status=sync_status,
            import_stats_written=bool(import_stats),
            export_stats_written=bool(export_stats),
            import_last_valid_ts=import_meta["last_valid_ts"],
            export_last_valid_ts=export_meta["last_valid_ts"],
            previous_import_ts=self._parse_dt(self._persisted.get(ATTR_LAST_VALID_IMPORT_TS)),
            previous_export_ts=self._parse_dt(self._persisted.get(ATTR_LAST_VALID_EXPORT_TS)),
        )
        last_successful_sync_utc = (
            self._iso(now_utc)
            if should_advance_successful_sync
            else self._persisted.get(ATTR_LAST_API_SYNC_UTC)
        )
        next_sync_attempt_utc, next_sync_reason = self._get_next_sync_attempt(
            now_utc=now_utc,
            sync_status=sync_status,
        )
        state = EnergyState(
            total_import_kwh=round(total_import, 3),
            total_export_kwh=round(total_export, 3),
            last_valid_import_timestamp=(
                self._iso(import_meta["last_valid_ts"])
                or self._persisted.get(ATTR_LAST_VALID_IMPORT_TS)
            ),
            last_valid_export_timestamp=(
                self._iso(export_meta["last_valid_ts"])
                or self._persisted.get(ATTR_LAST_VALID_EXPORT_TS)
            ),
            last_import_status=import_meta["last_status"] or self._persisted.get(ATTR_LAST_IMPORT_STATUS),
            last_export_status=export_meta["last_status"] or self._persisted.get(ATTR_LAST_EXPORT_STATUS),
            last_api_sync_utc=last_successful_sync_utc,
            last_update_utc=self._iso(now_utc),
            sync_status=sync_status,
            last_error=None,
            last_check_started_utc=self._persisted.get(ATTR_LAST_CHECK_STARTED_UTC),
            last_check_finished_utc=self._iso(now_utc),
            next_sync_attempt_utc=self._iso(next_sync_attempt_utc),
            next_sync_reason=next_sync_reason,
            last_manual_refresh_utc=self._persisted.get(ATTR_LAST_MANUAL_REFRESH_UTC),
            last_manual_refresh_result=self._persisted.get(ATTR_LAST_MANUAL_REFRESH_RESULT),
        )
        self._record_diagnostic_event(
            "info",
            "refresh_completed",
            {
                "ean_suffix": ean[-4:],
                "sync_status": sync_status,
                "total_import_kwh": state.total_import_kwh,
                "total_export_kwh": state.total_export_kwh,
                "import_rows_written": len(import_stats),
                "export_rows_written": len(export_stats),
            },
        )

        self._persisted.update(
            {
                "total_import_kwh": state.total_import_kwh,
                "total_export_kwh": state.total_export_kwh,
                ATTR_LAST_VALID_IMPORT_TS: state.last_valid_import_timestamp,
                ATTR_LAST_VALID_EXPORT_TS: state.last_valid_export_timestamp,
                ATTR_LAST_IMPORT_STATUS: state.last_import_status,
                ATTR_LAST_EXPORT_STATUS: state.last_export_status,
                ATTR_LAST_API_SYNC_UTC: state.last_api_sync_utc,
                ATTR_LAST_UPDATE_UTC: state.last_update_utc,
                ATTR_SYNC_STATUS: state.sync_status,
                ATTR_LAST_ERROR: state.last_error,
                ATTR_LAST_CHECK_STARTED_UTC: state.last_check_started_utc,
                ATTR_LAST_CHECK_FINISHED_UTC: state.last_check_finished_utc,
                ATTR_NEXT_SYNC_ATTEMPT_UTC: state.next_sync_attempt_utc,
                ATTR_NEXT_SYNC_REASON: state.next_sync_reason,
                ATTR_LAST_MANUAL_REFRESH_UTC: state.last_manual_refresh_utc,
                ATTR_LAST_MANUAL_REFRESH_RESULT: state.last_manual_refresh_result,
            }
        )

        if import_from is not None:
            self._persisted[CONF_IMPORT_PROFILE] = import_profile
        if export_from is not None:
            self._persisted[CONF_EXPORT_PROFILE] = export_profile
        await self._store.async_save(self._persisted)
        return state

    def _process_records_hourly(
        self,
        *,
        records: list[IntervalRecord],
        profile: str,
    ) -> tuple[dict[datetime, float], dict[str, Any]]:
        """Aggregate quarter-hour records to hourly delta rows."""
        last_status: str | None = None
        newest_valid_ts: datetime | None = None

        hourly: dict[datetime, float] = defaultdict(float)

        for record in records:
            last_status = record.status
            hour_start = record.timestamp.replace(minute=0, second=0, microsecond=0)
            # Explicit invalid records must also replace previously valid hours.
            # An absent hour is left untouched by the merge.
            hourly.setdefault(hour_start, 0.0)

            if (
                record.status not in ALLOWED_STATUSES
                or record.value is None
                or not isfinite(record.value)
            ):
                continue

            value_kwh = self._record_to_kwh(record.value, profile)
            hourly[hour_start] += value_kwh
            newest_valid_ts = record.timestamp

        return (
            {hour_start: round(value, 6) for hour_start, value in hourly.items()},
            {"last_valid_ts": newest_valid_ts, "last_status": last_status},
        )

    async def _determine_start_timestamp(
        self,
        *,
        ean: str,
        profile: str,
        latest_available_utc: datetime,
        cache_complete_key: str,
        accessible_start_key: str,
        last_valid_key: str,
        profile_key: str,
    ) -> datetime | None:
        """Determine where next fetch should start.

        Behavior:
        - first sync (or migration without local hourly cache): fetch full history
        - subsequent daily runs: revalidate a rolling window from the configured day offset
        """
        hard_min = self._hard_min_for_profile(profile)
        cache_complete = bool(self._persisted.get(cache_complete_key))
        previous_profile = self._persisted.get(
            profile_key, self.config_entry.data.get(profile_key)
        )
        profile_changed = previous_profile is not None and previous_profile != profile
        if profile_changed or not cache_complete:
            if profile_changed or self._parse_dt(self._persisted.get(last_valid_key)) is None:
                accessible_start = await self._find_first_accessible_timestamp(
                    ean=ean,
                    profile=profile,
                    candidate_start=hard_min,
                    latest_available_utc=latest_available_utc,
                )
                self._persisted[accessible_start_key] = self._iso(accessible_start)
                if profile_changed:
                    self._persisted[cache_complete_key] = False
                if accessible_start is None:
                    self._record_diagnostic_event(
                        "info",
                        "start_timestamp_no_accessible_period",
                        {
                            "profile": profile,
                            "hard_min": hard_min.isoformat(),
                            "latest_available_utc": latest_available_utc.isoformat(),
                        },
                    )
                    return None
                self._record_diagnostic_event(
                    "debug",
                    "start_timestamp_full_history",
                    {
                        "profile": profile,
                        "hard_min": hard_min.isoformat(),
                        "accessible_start": accessible_start.isoformat(),
                    },
                )
                return accessible_start
            start = max(self._get_revalidation_start(latest_available_utc), hard_min)
            self._record_diagnostic_event(
                "debug",
                "start_timestamp_partial_cache",
                {
                    "profile": profile,
                    "start": start.isoformat(),
                    "hard_min": hard_min.isoformat(),
                },
            )
            return start

        revalidation_start = self._get_revalidation_start(latest_available_utc)
        start = max(revalidation_start, hard_min)
        self._record_diagnostic_event(
            "debug",
            "start_timestamp_revalidation",
            {
                "profile": profile,
                "start": start.isoformat(),
                "hard_min": hard_min.isoformat(),
            },
        )
        return start

    async def _find_first_accessible_timestamp(
        self,
        *,
        ean: str,
        profile: str,
        candidate_start: datetime,
        latest_available_utc: datetime,
    ) -> datetime | None:
        """Find the first day accepted by EG.D for the configured EAN/profile."""
        candidate_start = max(candidate_start, get_history_start())
        if candidate_start > latest_available_utc:
            return None
        first_day = candidate_start.date()
        last_day = latest_available_utc.date()

        if await self._probe_accessible_day(
            ean=ean,
            profile=profile,
            day=first_day,
            latest_available_utc=latest_available_utc,
        ):
            return candidate_start

        low = 0
        high = (last_day - first_day).days
        accessible_offset: int | None = None

        while low <= high:
            mid = (low + high) // 2
            probe_day = first_day + timedelta(days=mid)

            if await self._probe_accessible_day(
                ean=ean,
                profile=profile,
                day=probe_day,
                latest_available_utc=latest_available_utc,
            ):
                accessible_offset = mid
                high = mid - 1
            else:
                low = mid + 1

        if accessible_offset is None:
            return None

        accessible_day = first_day + timedelta(days=accessible_offset)
        return max(
            candidate_start,
            datetime.combine(accessible_day, time(0, 0), tzinfo=timezone.utc),
        )

    async def _probe_accessible_day(
        self,
        *,
        ean: str,
        profile: str,
        day: date,
        latest_available_utc: datetime,
    ) -> bool:
        """Return whether one UTC day is accepted by EG.D."""
        probe_from = max(
            datetime.combine(day, time(0, 0), tzinfo=timezone.utc),
            get_history_start(),
        )
        probe_to = min(
            datetime.combine(day, time(23, 45), tzinfo=timezone.utc),
            latest_available_utc,
        )
        if probe_from > probe_to:
            return False

        return await self.client.async_probe_access(
            ean=ean,
            profile=profile,
            from_dt=probe_from,
            to_dt=probe_to,
        )

    def _merge_statistics(
        self,
        *,
        cache_key: str,
        cache_complete_key: str,
        persisted_total_key: str,
        fetched_from: datetime | None,
        latest_available_utc: datetime,
        profile: str,
        accessible_start_key: str,
        hourly_deltas: dict[datetime, float],
    ) -> tuple[float, list[dict[str, Any]]]:
        """Merge fetched hourly deltas into local cache and build changed rows."""
        existing = self._load_hourly_deltas(cache_key)
        merged = dict(existing)
        merged.update(hourly_deltas)

        latest_hour = latest_available_utc.replace(minute=0, second=0, microsecond=0)
        window_start = (
            fetched_from or self._hard_min_for_profile(profile)
        ).replace(minute=0, second=0, microsecond=0)

        old_sums = self._build_cumulative_sum_map(existing, window_start, latest_hour)
        new_sums = self._build_cumulative_sum_map(merged, window_start, latest_hour)

        rows = [
            {
                "start": hour_start,
                "state": sum_value,
                "sum": sum_value,
            }
            for hour_start, sum_value in new_sums.items()
            if not self._numbers_equal(old_sums.get(hour_start), sum_value)
        ]

        self._persisted[cache_key] = self._serialize_hourly_deltas(merged)

        hard_min = self._hard_min_for_profile(profile)
        accessible_start = self._parse_dt(self._persisted.get(accessible_start_key))
        if accessible_start is None and not existing and fetched_from is not None:
            accessible_start = fetched_from
            self._persisted[accessible_start_key] = self._iso(accessible_start)
        effective_complete_from = accessible_start or hard_min
        cache_complete = bool(self._persisted.get(cache_complete_key))
        if fetched_from is not None and fetched_from <= effective_complete_from:
            self._persisted[cache_complete_key] = True
            cache_complete = True

        cache_total = round(sum(merged.values()), 6)
        if cache_complete:
            total = cache_total
        else:
            persisted_total = self._get_persisted_total(
                persisted_total_key=persisted_total_key,
                cache_key=cache_key,
            )
            total = max(persisted_total, cache_total)
        self._record_diagnostic_event(
            "debug",
            "statistics_merged",
            {
                "cache_key": cache_key,
                "fetched_from": fetched_from.isoformat() if fetched_from else None,
                "latest_available_utc": latest_available_utc.isoformat(),
                "hourly_deltas": len(hourly_deltas),
                "changed_rows": len(rows),
                "cache_complete": cache_complete,
                "total_kwh": round(total, 6),
            },
        )
        return total, rows

    async def _async_refresh_import_cost(
        self, *, latest_available_utc: datetime
    ) -> list[dict[str, Any]]:
        """Price the cached import series and return changed cumulative cost rows.

        Each hour's cost is its kWh times the price entity's time-weighted price
        over that hour. Prices are cached per hour once known: every EG.D hour is
        already closed, and the Recorder purges the state history they come from
        long before revalidation stops touching the hour.
        """
        # Once options exist they are authoritative, so the entity can be cleared.
        source = self.config_entry.options or self.config_entry.data
        price_entity = source.get(CONF_PRICE_ENTITY)
        if not price_entity:
            return []

        deltas = self._load_hourly_deltas(self._IMPORT_CACHE_KEY)
        if not deltas:
            return []

        same_entity = self._persisted.get(self._IMPORT_COST_PRICE_ENTITY_KEY) == price_entity
        # Prices of a previously selected entity must not leak into the new series.
        prices = self._load_hourly_deltas(self._IMPORT_PRICES_KEY) if same_entity else {}
        old_costs = self._load_hourly_deltas(self._IMPORT_COSTS_KEY) if same_entity else {}

        missing = sorted(hour_start for hour_start in deltas if hour_start not in prices)
        fallback: dict[datetime, float] = {}
        if missing:
            try:
                fetched = await async_fetch_hourly_prices(self.hass, price_entity, missing)
            except Exception as err:  # noqa: BLE001 - never fail the energy import
                _LOGGER.warning("Cannot read prices of %s, cost not updated: %s", price_entity, err)
                self._record_diagnostic_event(
                    "warning",
                    "cost_prices_failed",
                    {"price_entity": price_entity, "reason": str(err)},
                )
                return []
            prices.update(fetched)
            fallback = fill_missing_prices(prices, missing)
            if not prices:
                _LOGGER.warning("No price of %s is known yet, cost not updated", price_entity)
                return []
            # History never reappears for a closed hour, so a fallback is final too.
            prices.update(fallback)

        costs = {
            hour_start: round(kwh * prices[hour_start], 6)
            for hour_start, kwh in deltas.items()
        }
        latest_hour = latest_available_utc.replace(minute=0, second=0, microsecond=0)
        window_start = min(costs)
        old_sums = self._build_cumulative_sum_map(old_costs, window_start, latest_hour)
        new_sums = self._build_cumulative_sum_map(costs, window_start, latest_hour)
        rows = [
            {"start": hour_start, "state": sum_value, "sum": sum_value}
            for hour_start, sum_value in new_sums.items()
            if not self._numbers_equal(old_sums.get(hour_start), sum_value)
        ]

        self._persisted[self._IMPORT_COST_PRICE_ENTITY_KEY] = price_entity
        self._persisted[self._IMPORT_PRICES_KEY] = self._serialize_hourly_deltas(prices)
        self._persisted[self._IMPORT_COSTS_KEY] = self._serialize_hourly_deltas(costs)
        self._record_diagnostic_event(
            "debug",
            "cost_merged",
            {
                "price_entity": price_entity,
                "priced_hours": len(missing) - len(fallback),
                "fallback_hours": len(fallback),
                "changed_rows": len(rows),
                "total_cost": round(sum(costs.values()), 6),
            },
        )
        if fallback:
            _LOGGER.info(
                "Priced %s hour(s) without %s history by the nearest known price, first %s",
                len(fallback),
                price_entity,
                min(fallback).isoformat(),
            )
        return rows

    def _get_persisted_total(self, *, persisted_total_key: str, cache_key: str) -> float:
        """Return stored total, falling back to the local hourly cache."""
        persisted_total = round(float(self._persisted.get(persisted_total_key, 0.0)), 6)
        if persisted_total:
            return persisted_total
        return round(sum(self._load_hourly_deltas(cache_key).values()), 6)

    def _store_error_state(self, error_message: str) -> None:
        """Persist the last refresh error for diagnostic entities."""
        now_utc = self._iso(dt_util.utcnow().astimezone(timezone.utc))
        self._persisted[ATTR_LAST_ERROR] = error_message
        self._persisted[ATTR_SYNC_STATUS] = "error"
        self._persisted[ATTR_LAST_UPDATE_UTC] = now_utc
        self._persisted[ATTR_LAST_CHECK_FINISHED_UTC] = now_utc
        next_sync_attempt_utc, next_sync_reason = self._get_next_sync_attempt(
            now_utc=dt_util.utcnow().astimezone(timezone.utc),
            sync_status="error",
        )
        self._persisted[ATTR_NEXT_SYNC_ATTEMPT_UTC] = self._iso(next_sync_attempt_utc)
        self._persisted[ATTR_NEXT_SYNC_REASON] = next_sync_reason

    def _build_state_from_persisted(self) -> EnergyState | None:
        """Hydrate runtime state from persisted storage when available."""
        has_state = any(
            key in self._persisted
            for key in (
                "total_import_kwh",
                "total_export_kwh",
                ATTR_LAST_VALID_IMPORT_TS,
                ATTR_LAST_VALID_EXPORT_TS,
                ATTR_LAST_API_SYNC_UTC,
                ATTR_SYNC_STATUS,
                ATTR_LAST_ERROR,
            )
        )
        if not has_state:
            return None

        sync_status = str(self._persisted.get(ATTR_SYNC_STATUS) or "waiting_for_data")
        now_utc = dt_util.utcnow().astimezone(timezone.utc)
        next_sync_attempt_utc = self._persisted.get(ATTR_NEXT_SYNC_ATTEMPT_UTC)
        next_sync_reason = self._persisted.get(ATTR_NEXT_SYNC_REASON)
        next_sync_attempt_dt = self._parse_dt(next_sync_attempt_utc)
        if (
            next_sync_attempt_utc is None
            or next_sync_reason is None
            or (next_sync_attempt_dt is not None and next_sync_attempt_dt <= now_utc)
        ):
            next_attempt, next_reason = self._get_next_sync_attempt(
                now_utc=now_utc,
                sync_status=sync_status,
            )
            next_sync_attempt_utc = self._iso(next_attempt)
            next_sync_reason = next_reason

        return EnergyState(
            total_import_kwh=round(
                self._get_persisted_total(
                    persisted_total_key="total_import_kwh",
                    cache_key=self._IMPORT_CACHE_KEY,
                ),
                3,
            ),
            total_export_kwh=round(
                self._get_persisted_total(
                    persisted_total_key="total_export_kwh",
                    cache_key=self._EXPORT_CACHE_KEY,
                ),
                3,
            ),
            last_valid_import_timestamp=self._persisted.get(ATTR_LAST_VALID_IMPORT_TS),
            last_valid_export_timestamp=self._persisted.get(ATTR_LAST_VALID_EXPORT_TS),
            last_import_status=self._persisted.get(ATTR_LAST_IMPORT_STATUS),
            last_export_status=self._persisted.get(ATTR_LAST_EXPORT_STATUS),
            last_api_sync_utc=self._persisted.get(ATTR_LAST_API_SYNC_UTC),
            last_update_utc=self._persisted.get(ATTR_LAST_UPDATE_UTC),
            sync_status=sync_status,
            last_error=self._persisted.get(ATTR_LAST_ERROR),
            last_check_started_utc=self._persisted.get(ATTR_LAST_CHECK_STARTED_UTC),
            last_check_finished_utc=self._persisted.get(ATTR_LAST_CHECK_FINISHED_UTC),
            next_sync_attempt_utc=next_sync_attempt_utc,
            next_sync_reason=next_sync_reason,
            last_manual_refresh_utc=self._persisted.get(ATTR_LAST_MANUAL_REFRESH_UTC),
            last_manual_refresh_result=self._persisted.get(ATTR_LAST_MANUAL_REFRESH_RESULT),
        )

    async def async_store_manual_refresh_result(self, result: str) -> None:
        """Persist metadata about the last manual refresh attempt."""
        refreshed_at = self._iso(dt_util.utcnow().astimezone(timezone.utc))
        self._persisted[ATTR_LAST_MANUAL_REFRESH_UTC] = refreshed_at
        self._persisted[ATTR_LAST_MANUAL_REFRESH_RESULT] = result
        await self._store.async_save(self._persisted)

        if self.data is not None:
            self.data = replace(
                self.data,
                last_manual_refresh_utc=refreshed_at,
                last_manual_refresh_result=result,
            )
            self.async_update_listeners()

    def snapshot_automatic_state(self) -> tuple[EnergyState | None, dict[str, Any]]:
        """Capture coordinator state that manual refreshes must not overwrite."""
        persisted = {
            key: self._persisted.get(key)
            for key in self._MANUAL_RESTORE_KEYS
        }
        return self.data, persisted

    async def async_restore_automatic_state(
        self,
        snapshot: tuple[EnergyState | None, dict[str, Any]],
    ) -> None:
        """Restore automatic sync state after a non-authoritative manual refresh."""
        previous_data, persisted = snapshot
        for key, value in persisted.items():
            self._persisted[key] = value

        if previous_data is not None:
            self.data = replace(
                previous_data,
                last_manual_refresh_utc=self._persisted.get(ATTR_LAST_MANUAL_REFRESH_UTC),
                last_manual_refresh_result=self._persisted.get(ATTR_LAST_MANUAL_REFRESH_RESULT),
            )
        else:
            self.data = None

        await self._store.async_save(self._persisted)
        self.async_update_listeners()

    async def async_refresh_next_sync_projection(self, now_utc: datetime | None = None) -> None:
        """Refresh the projected next sync timestamp without fetching new data."""
        if self.data is None:
            return

        resolved_now = now_utc or dt_util.utcnow().astimezone(timezone.utc)
        next_sync_attempt_utc, next_sync_reason = self._get_next_sync_attempt(
            now_utc=resolved_now,
            sync_status=self.data.sync_status,
        )
        next_sync_attempt_iso = self._iso(next_sync_attempt_utc)

        if (
            self.data.next_sync_attempt_utc == next_sync_attempt_iso
            and self.data.next_sync_reason == next_sync_reason
        ):
            return

        self._persisted[ATTR_NEXT_SYNC_ATTEMPT_UTC] = next_sync_attempt_iso
        self._persisted[ATTR_NEXT_SYNC_REASON] = next_sync_reason
        self.data = replace(
            self.data,
            next_sync_attempt_utc=next_sync_attempt_iso,
            next_sync_reason=next_sync_reason,
        )
        await self._store.async_save(self._persisted)
        self.async_update_listeners()

    def diagnostics_enabled(self) -> bool:
        """Return whether structured diagnostics collection is enabled."""
        return bool(
            self.config_entry.options.get(
                CONF_ENABLE_DIAGNOSTICS,
                self.config_entry.data.get(
                    CONF_ENABLE_DIAGNOSTICS,
                    DEFAULT_ENABLE_DIAGNOSTICS,
                ),
            )
        )

    def _record_diagnostic_event(
        self,
        level: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Store bounded structured diagnostics for later download."""
        if not self.diagnostics_enabled():
            return

        events = self._persisted.setdefault(DIAGNOSTICS_EVENTS_KEY, [])
        if not isinstance(events, list):
            events = []
            self._persisted[DIAGNOSTICS_EVENTS_KEY] = events

        events.append(
            {
                "ts": self._iso(dt_util.utcnow().astimezone(timezone.utc)),
                "level": level,
                "message": message,
                "details": details or {},
            }
        )
        if len(events) > MAX_DIAGNOSTIC_EVENTS:
            del events[:-MAX_DIAGNOSTIC_EVENTS]

    def get_diagnostic_events(self) -> list[dict[str, Any]]:
        """Return collected diagnostic events."""
        events = self._persisted.get(DIAGNOSTICS_EVENTS_KEY, [])
        if not isinstance(events, list):
            return []
        return list(events)

    def _get_next_sync_attempt(
        self,
        *,
        now_utc: datetime,
        sync_status: str,
    ) -> tuple[datetime, str]:
        """Return the next automatic sync attempt and why it will happen."""
        daily_attempt = self._get_next_daily_sync_attempt(now_utc)
        if sync_status == "waiting_for_data":
            scheduled_hour = self.config_entry.options.get(
                CONF_UPDATE_HOUR,
                self.config_entry.data[CONF_UPDATE_HOUR],
            )
            scheduled_minute = self.config_entry.options.get(
                CONF_UPDATE_MINUTE,
                self.config_entry.data[CONF_UPDATE_MINUTE],
            )
            now_local = dt_util.as_local(now_utc)
            if (now_local.hour, now_local.minute) < (scheduled_hour, scheduled_minute):
                return daily_attempt, "scheduled_daily"
            return now_utc + timedelta(hours=1), "watchdog_retry"
        return daily_attempt, "scheduled_daily"

    def _get_next_daily_sync_attempt(self, now_utc: datetime) -> datetime:
        """Return the next daily sync timestamp converted to UTC."""
        scheduled_hour = self.config_entry.options.get(
            CONF_UPDATE_HOUR,
            self.config_entry.data[CONF_UPDATE_HOUR],
        )
        scheduled_minute = self.config_entry.options.get(
            CONF_UPDATE_MINUTE,
            self.config_entry.data[CONF_UPDATE_MINUTE],
        )
        now_local = dt_util.as_local(now_utc)
        scheduled_local = now_local.replace(
            hour=scheduled_hour,
            minute=scheduled_minute,
            second=0,
            microsecond=0,
        )
        if scheduled_local <= now_local:
            scheduled_local += timedelta(days=1)
        return scheduled_local.astimezone(timezone.utc)

    @staticmethod
    def _is_waiting_for_latest_data(
        *,
        latest_available_utc: datetime,
        last_valid_import_ts: datetime | None,
        last_valid_export_ts: datetime | None,
    ) -> bool:
        """Return whether the latest expected import or export day is still missing."""
        latest_available_day = latest_available_utc.date()
        return (
            last_valid_import_ts is None
            or last_valid_import_ts.date() < latest_available_day
            or last_valid_export_ts is None
            or last_valid_export_ts.date() < latest_available_day
        )

    @staticmethod
    def _did_timestamp_advance(
        *,
        current: datetime | None,
        previous: datetime | None,
    ) -> bool:
        """Return whether a valid data timestamp moved forward."""
        if current is None:
            return False
        if previous is None:
            return True
        return current > previous

    @classmethod
    def _should_advance_successful_sync(
        cls,
        *,
        sync_status: str,
        import_stats_written: bool,
        export_stats_written: bool,
        import_last_valid_ts: datetime | None,
        export_last_valid_ts: datetime | None,
        previous_import_ts: datetime | None,
        previous_export_ts: datetime | None,
    ) -> bool:
        """Return whether the successful-sync timestamp should move forward."""
        if sync_status != "ok":
            return False
        return (
            import_stats_written
            or export_stats_written
            or cls._did_timestamp_advance(
                current=import_last_valid_ts,
                previous=previous_import_ts,
            )
            or cls._did_timestamp_advance(
                current=export_last_valid_ts,
                previous=previous_export_ts,
            )
        )

    def _get_revalidation_start(self, latest_available_utc: datetime) -> datetime:
        """Return start of rolling revalidation window in UTC."""
        revalidate_days = max(
            1,
            int(
                self.config_entry.options.get(
                    CONF_REVALIDATE_DAYS,
                    self.config_entry.data.get(CONF_REVALIDATE_DAYS, DEFAULT_REVALIDATE_DAYS),
                )
            ),
        )
        start_date = latest_available_utc.date() - timedelta(days=revalidate_days - 1)
        return datetime.combine(start_date, time(0, 0), tzinfo=timezone.utc)

    def _load_hourly_deltas(self, cache_key: str) -> dict[datetime, float]:
        """Load cached hourly deltas from persistent storage."""
        raw = self._persisted.get(cache_key, {})
        if not isinstance(raw, dict):
            return {}

        deltas: dict[datetime, float] = {}
        for timestamp, value in raw.items():
            parsed = self._parse_dt(timestamp)
            if parsed is None:
                continue
            deltas[parsed] = round(float(value), 6)
        return deltas

    def _serialize_hourly_deltas(self, deltas: dict[datetime, float]) -> dict[str, float]:
        """Serialize cached hourly deltas for Home Assistant storage."""
        return {
            self._iso(timestamp): round(value, 6)
            for timestamp, value in sorted(deltas.items())
            if self._iso(timestamp) is not None
        }

    def _build_cumulative_sum_map(
        self,
        deltas: dict[datetime, float],
        window_start: datetime,
        latest_hour: datetime,
    ) -> dict[datetime, float]:
        """Build cumulative sums for a specific hourly window."""
        cumulative = round(
            sum(value for timestamp, value in deltas.items() if timestamp < window_start),
            6,
        )
        sums: dict[datetime, float] = {}

        for hour_start in sorted(
            timestamp
            for timestamp in deltas
            if window_start <= timestamp <= latest_hour
        ):
            cumulative = round(cumulative + deltas[hour_start], 6)
            sums[hour_start] = cumulative

        return sums

    @staticmethod
    def _numbers_equal(left: float | None, right: float | None, tolerance: float = 1e-6) -> bool:
        """Compare floats safely for recorder reimports."""
        if left is None or right is None:
            return left is right
        return abs(left - right) <= tolerance

    def _hard_min_for_profile(self, profile: str) -> datetime:
        """Return earliest allowed start for a profile."""
        hard_min = get_history_start()

        profile_min = PROFILE_MIN_DATES.get(profile)
        if profile_min is not None:
            profile_min_dt = datetime.combine(profile_min, time(0, 0), tzinfo=timezone.utc)
            hard_min = max(hard_min, profile_min_dt)

        return hard_min

    def _get_latest_available_utc(self) -> datetime:
        """Return latest allowed EG.D quarter-hour timestamp.

        EG.D allows querying only up to yesterday and the last slot is 23:45.
        The request for it ends at 23:59:59 local (see `_format_egd_profile_to`),
        which is still yesterday, so the whole day arrives on the first sync.
        """
        now_local = dt_util.now()
        latest_local = datetime.combine(
            now_local.date() - timedelta(days=1),
            time(23, 45),
            tzinfo=now_local.tzinfo,
        )
        return latest_local.astimezone(timezone.utc)

    @staticmethod
    def _record_to_kwh(value: float, profile: str) -> float:
        """Convert API value to kWh."""
        if PROFILE_UNITS[profile] == "kW":
            return value / 4
        return value

    @staticmethod
    def _parse_dt(value: str | None) -> datetime | None:
        """Parse stored ISO datetime."""
        if not value:
            return None
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        """Format datetime as UTC ISO string."""
        if value is None:
            return None
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
