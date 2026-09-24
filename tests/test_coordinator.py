"""Unit tests for EG.D coordinator logic."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("homeassistant")

from types import SimpleNamespace

from custom_components.ha_egd_openapi import coordinator as coordinator_module
from custom_components.ha_egd_openapi import statistics as statistics_module
from custom_components.ha_egd_openapi.api import EgdApiError, IntervalRecord
from custom_components.ha_egd_openapi.const import (
    ATTR_LAST_ERROR,
    ATTR_SYNC_STATUS,
    CONF_ENABLE_DIAGNOSTICS,
    DIAGNOSTICS_EVENTS_KEY,
    MAX_DIAGNOSTIC_EVENTS,
)
from custom_components.ha_egd_openapi.coordinator import EgdDataUpdateCoordinator


class _ProbeClient:
    """Probe test double that allows access from a configured day."""

    def __init__(self, accessible_from: datetime) -> None:
        self.accessible_from = accessible_from
        self.probes: list[tuple[datetime, datetime]] = []

    async def async_probe_access(
        self,
        *,
        ean: str,
        profile: str,
        from_dt: datetime,
        to_dt: datetime,
    ) -> bool:
        self.probes.append((from_dt, to_dt))
        return from_dt >= self.accessible_from


def _build_coordinator() -> EgdDataUpdateCoordinator:
    """Create an uninitialized coordinator instance for pure method tests."""
    coordinator = EgdDataUpdateCoordinator.__new__(EgdDataUpdateCoordinator)
    coordinator._persisted = {}  # noqa: SLF001
    coordinator.config_entry = SimpleNamespace(
        options={},
        data={"update_hour": 16, "update_minute": 17},
    )
    return coordinator


def test_process_records_hourly_filters_invalid_statuses() -> None:
    """Only valid EG.D statuses should contribute to statistics."""
    coordinator = _build_coordinator()
    records = [
        IntervalRecord(
            timestamp=datetime(2026, 4, 10, 0, 0, tzinfo=timezone.utc),
            value=1.0,
            status="W",
        ),
        IntervalRecord(
            timestamp=datetime(2026, 4, 10, 0, 15, tzinfo=timezone.utc),
            value=2.0,
            status="INVALID",
        ),
        IntervalRecord(
            timestamp=datetime(2026, 4, 10, 0, 30, tzinfo=timezone.utc),
            value=3.0,
            status="IU012",
        ),
    ]

    hourly, meta = coordinator._process_records_hourly(records=records, profile="ICQ2")  # noqa: SLF001

    assert hourly == {datetime(2026, 4, 10, 0, 0, tzinfo=timezone.utc): 4.0}
    assert meta["last_valid_ts"] == datetime(2026, 4, 10, 0, 30, tzinfo=timezone.utc)
    assert meta["last_status"] == "IU012"


def test_process_records_hourly_converts_quarter_hour_profiles_to_kwh() -> None:
    """ICC1/ISC1 profiles should be converted from quarter-hour power values."""
    coordinator = _build_coordinator()
    records = [
        IntervalRecord(
            timestamp=datetime(2026, 4, 10, 1, 0, tzinfo=timezone.utc),
            value=400.0,
            status="IU012",
        ),
        IntervalRecord(
            timestamp=datetime(2026, 4, 10, 1, 15, tzinfo=timezone.utc),
            value=200.0,
            status="IU012",
        ),
    ]

    hourly, _meta = coordinator._process_records_hourly(records=records, profile="ICC1")  # noqa: SLF001

    assert hourly == {datetime(2026, 4, 10, 1, 0, tzinfo=timezone.utc): 150.0}


def test_waiting_for_latest_data_detects_missing_latest_day() -> None:
    """Diagnostic waiting state should reflect missing import or export data for the latest day."""
    latest_available = datetime(2026, 4, 10, 23, 45, tzinfo=timezone.utc)

    assert EgdDataUpdateCoordinator._is_waiting_for_latest_data(  # noqa: SLF001
        latest_available_utc=latest_available,
        last_valid_import_ts=None,
        last_valid_export_ts=datetime(2026, 4, 10, 23, 45, tzinfo=timezone.utc),
    )
    assert EgdDataUpdateCoordinator._is_waiting_for_latest_data(  # noqa: SLF001
        latest_available_utc=latest_available,
        last_valid_import_ts=datetime(2026, 4, 9, 23, 45, tzinfo=timezone.utc),
        last_valid_export_ts=datetime(2026, 4, 10, 23, 45, tzinfo=timezone.utc),
    )
    assert EgdDataUpdateCoordinator._is_waiting_for_latest_data(  # noqa: SLF001
        latest_available_utc=latest_available,
        last_valid_import_ts=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
        last_valid_export_ts=datetime(2026, 4, 9, 23, 45, tzinfo=timezone.utc),
    )
    assert not EgdDataUpdateCoordinator._is_waiting_for_latest_data(  # noqa: SLF001
        latest_available_utc=latest_available,
        last_valid_import_ts=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
        last_valid_export_ts=datetime(2026, 4, 10, 18, 0, tzinfo=timezone.utc),
    )


def test_latest_available_timestamp_is_yesterdays_final_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Newest sync window should end with yesterday's final 23:45 local slot."""
    coordinator = _build_coordinator()
    prague = ZoneInfo("Europe/Prague")
    monkeypatch.setattr(
        coordinator_module.dt_util,
        "now",
        lambda: datetime(2026, 5, 23, 8, 33, tzinfo=prague),
    )

    assert coordinator._get_latest_available_utc() == datetime(  # noqa: SLF001
        2026,
        5,
        22,
        21,
        45,
        tzinfo=timezone.utc,
    )


@pytest.mark.parametrize("last_success", [None, "2026-04-09T18:00:00Z"])
def test_store_error_state_updates_diagnostic_persisted_values(
    last_success: str | None,
) -> None:
    """A failed refresh should leave a clear diagnostic footprint in persisted state."""
    coordinator = _build_coordinator()

    if last_success is not None:
        coordinator._persisted["last_api_sync_utc"] = last_success

    coordinator._store_error_state("Boom")  # noqa: SLF001

    assert coordinator._persisted[ATTR_SYNC_STATUS] == "error"  # noqa: SLF001
    assert coordinator._persisted[ATTR_LAST_ERROR] == "Boom"  # noqa: SLF001
    assert coordinator._persisted.get("last_api_sync_utc") == last_success
    assert coordinator._persisted["last_check_finished_utc"].endswith("Z")


def test_did_timestamp_advance_only_when_value_moves_forward() -> None:
    """Successful sync timestamp should advance only on real data progress."""
    previous = datetime(2026, 4, 10, 23, 45, tzinfo=timezone.utc)

    assert EgdDataUpdateCoordinator._did_timestamp_advance(  # noqa: SLF001
        current=datetime(2026, 4, 11, 23, 45, tzinfo=timezone.utc),
        previous=previous,
    )
    assert not EgdDataUpdateCoordinator._did_timestamp_advance(  # noqa: SLF001
        current=previous,
        previous=previous,
    )
    assert not EgdDataUpdateCoordinator._did_timestamp_advance(  # noqa: SLF001
        current=None,
        previous=previous,
    )


def test_successful_sync_timestamp_does_not_advance_while_waiting() -> None:
    """Revalidation writes must not make a waiting refresh look successful."""
    previous = datetime(2026, 5, 20, 21, 45, tzinfo=timezone.utc)

    assert not EgdDataUpdateCoordinator._should_advance_successful_sync(  # noqa: SLF001
        sync_status="waiting_for_data",
        import_stats_written=True,
        export_stats_written=True,
        import_last_valid_ts=previous,
        export_last_valid_ts=previous,
        previous_import_ts=previous,
        previous_export_ts=previous,
    )


def test_successful_sync_timestamp_advances_when_refresh_is_complete() -> None:
    """Completed refreshes with written statistics should update last success."""
    previous = datetime(2026, 5, 20, 21, 45, tzinfo=timezone.utc)

    assert EgdDataUpdateCoordinator._should_advance_successful_sync(  # noqa: SLF001
        sync_status="ok",
        import_stats_written=True,
        export_stats_written=False,
        import_last_valid_ts=previous,
        export_last_valid_ts=previous,
        previous_import_ts=previous,
        previous_export_ts=previous,
    )


def test_diagnostic_events_are_stored_only_when_enabled() -> None:
    """Structured diagnostics should respect the user toggle."""
    coordinator = _build_coordinator()

    coordinator._record_diagnostic_event("info", "disabled")  # noqa: SLF001
    assert coordinator.get_diagnostic_events() == []

    coordinator.config_entry.options[CONF_ENABLE_DIAGNOSTICS] = True
    coordinator._record_diagnostic_event("info", "enabled", {"step": "refresh"})  # noqa: SLF001

    assert coordinator.get_diagnostic_events()[0]["message"] == "enabled"
    assert coordinator.get_diagnostic_events()[0]["details"] == {"step": "refresh"}


@pytest.mark.asyncio
async def test_determine_start_timestamp_skips_unauthorized_history() -> None:
    """Initial sync should begin at the first day accepted by EG.D."""
    coordinator = _build_coordinator()
    coordinator.client = _ProbeClient(
        accessible_from=datetime(2026, 4, 5, 0, 0, tzinfo=timezone.utc)
    )

    start = await coordinator._determine_start_timestamp(  # noqa: SLF001
        ean="859182400000000000",
        profile="ICQ2",
        latest_available_utc=datetime(2026, 4, 10, 23, 45, tzinfo=timezone.utc),
        cache_complete_key="cache_complete",
        accessible_start_key="accessible_start",
        last_valid_key="last_valid",
        profile_key="import_profile",
    )

    assert start == datetime(2026, 4, 5, 0, 0, tzinfo=timezone.utc)
    assert coordinator._persisted["accessible_start"] == "2026-04-05T00:00:00Z"  # noqa: SLF001
    assert coordinator.client.probes[0] == (  # type: ignore[attr-defined]
        datetime(2024, 7, 1, 0, 0, tzinfo=timezone.utc),
        datetime(2024, 7, 1, 23, 45, tzinfo=timezone.utc),
    )
    assert all(
        probe_to - probe_from <= timedelta(days=1)
        for probe_from, probe_to in coordinator.client.probes  # type: ignore[attr-defined]
    )


def test_merge_statistics_completes_cache_from_accessible_start() -> None:
    """Totals should be computed once the cache starts at the first authorized day."""
    coordinator = _build_coordinator()
    coordinator._persisted["accessible_start"] = "2026-04-05T00:00:00Z"  # noqa: SLF001

    total, rows = coordinator._merge_statistics(  # noqa: SLF001
        cache_key="hourly_deltas",
        cache_complete_key="cache_complete",
        persisted_total_key="total_kwh",
        fetched_from=datetime(2026, 4, 5, 0, 0, tzinfo=timezone.utc),
        latest_available_utc=datetime(2026, 4, 5, 23, 45, tzinfo=timezone.utc),
        profile="ICQ2",
        accessible_start_key="accessible_start",
        hourly_deltas={
            datetime(2026, 4, 5, 0, 0, tzinfo=timezone.utc): 1.25,
            datetime(2026, 4, 5, 1, 0, tzinfo=timezone.utc): 2.5,
        },
    )

    assert total == 3.75
    assert len(rows) == 2
    assert coordinator._persisted["cache_complete"] is True  # noqa: SLF001


def test_merge_statistics_repairs_missing_accessible_start_on_initial_fetch() -> None:
    """Initial full fetch should complete the cache even if the start checkpoint is absent."""
    coordinator = _build_coordinator()

    total, _rows = coordinator._merge_statistics(  # noqa: SLF001
        cache_key="hourly_deltas",
        cache_complete_key="cache_complete",
        persisted_total_key="total_kwh",
        fetched_from=datetime(2026, 4, 5, 0, 0, tzinfo=timezone.utc),
        latest_available_utc=datetime(2026, 4, 5, 23, 45, tzinfo=timezone.utc),
        profile="ICQ2",
        accessible_start_key="accessible_start",
        hourly_deltas={
            datetime(2026, 4, 5, 0, 0, tzinfo=timezone.utc): 1.25,
            datetime(2026, 4, 5, 1, 0, tzinfo=timezone.utc): 2.5,
        },
    )

    assert total == 3.75
    assert coordinator._persisted["accessible_start"] == "2026-04-05T00:00:00Z"  # noqa: SLF001
    assert coordinator._persisted["cache_complete"] is True  # noqa: SLF001


def test_incomplete_cache_total_advances_from_cached_sum() -> None:
    """Incomplete caches should still repair frozen totals when cached data grows."""
    coordinator = _build_coordinator()
    coordinator._persisted["total_kwh"] = 3.0  # noqa: SLF001
    coordinator._persisted["hourly_deltas"] = {  # noqa: SLF001
        "2026-04-05T00:00:00Z": 1.25,
        "2026-04-05T01:00:00Z": 2.5,
    }

    total, _rows = coordinator._merge_statistics(  # noqa: SLF001
        cache_key="hourly_deltas",
        cache_complete_key="cache_complete",
        persisted_total_key="total_kwh",
        fetched_from=datetime(2026, 4, 6, 0, 0, tzinfo=timezone.utc),
        latest_available_utc=datetime(2026, 4, 6, 23, 45, tzinfo=timezone.utc),
        profile="ICQ2",
        accessible_start_key="accessible_start",
        hourly_deltas={
            datetime(2026, 4, 6, 0, 0, tzinfo=timezone.utc): 0.5,
        },
    )

    assert total == 4.25
    assert "cache_complete" not in coordinator._persisted  # noqa: SLF001


def test_incomplete_cache_total_does_not_decrease_from_partial_cache() -> None:
    """Partial cached sums must not lower an already persisted cumulative total."""
    coordinator = _build_coordinator()
    coordinator._persisted["total_kwh"] = 10.0  # noqa: SLF001
    coordinator._persisted["hourly_deltas"] = {  # noqa: SLF001
        "2026-04-05T00:00:00Z": 0.2,
    }

    total, _rows = coordinator._merge_statistics(  # noqa: SLF001
        cache_key="hourly_deltas",
        cache_complete_key="cache_complete",
        persisted_total_key="total_kwh",
        fetched_from=datetime(2026, 4, 6, 0, 0, tzinfo=timezone.utc),
        latest_available_utc=datetime(2026, 4, 6, 23, 45, tzinfo=timezone.utc),
        profile="ICQ2",
        accessible_start_key="accessible_start",
        hourly_deltas={
            datetime(2026, 4, 6, 0, 0, tzinfo=timezone.utc): 0.5,
        },
    )

    assert total == 10.0
    assert "cache_complete" not in coordinator._persisted  # noqa: SLF001


def test_persisted_total_falls_back_to_hourly_cache() -> None:
    """Sensors should recover totals from local cached hourly deltas."""
    coordinator = _build_coordinator()
    coordinator._persisted["hourly_deltas"] = {  # noqa: SLF001
        "2026-04-05T00:00:00Z": 1.25,
        "2026-04-05T01:00:00Z": 2.5,
    }

    assert (
        coordinator._get_persisted_total(  # noqa: SLF001
            persisted_total_key="total_kwh",
            cache_key="hourly_deltas",
        )
        == 3.75
    )


def test_diagnostic_events_buffer_is_bounded() -> None:
    """Diagnostics buffer should keep only the most recent events."""
    coordinator = _build_coordinator()
    coordinator.config_entry.options[CONF_ENABLE_DIAGNOSTICS] = True

    for idx in range(MAX_DIAGNOSTIC_EVENTS + 5):
        coordinator._record_diagnostic_event("debug", f"event-{idx}")  # noqa: SLF001

    events = coordinator._persisted[DIAGNOSTICS_EVENTS_KEY]  # noqa: SLF001
    assert len(events) == MAX_DIAGNOSTIC_EVENTS
    assert events[0]["message"] == "event-5"
    assert events[-1]["message"] == f"event-{MAX_DIAGNOSTIC_EVENTS + 4}"


def test_next_sync_attempt_prefers_watchdog_when_waiting_for_data() -> None:
    """Waiting for data should expose the next hourly retry, not tomorrow's daily run."""
    coordinator = _build_coordinator()
    now = datetime(2026, 4, 12, 18, 0, tzinfo=timezone.utc)

    next_attempt, reason = coordinator._get_next_sync_attempt(  # noqa: SLF001
        now_utc=now,
        sync_status="waiting_for_data",
    )

    assert next_attempt == datetime(2026, 4, 12, 19, 0, tzinfo=timezone.utc)
    assert reason == "watchdog_retry"


@pytest.mark.parametrize(
    "profile,status,factor",
    [
        ("DCQC", "W", 1),
        ("DSQC", "W", 1),
        ("ICQ2", "W", 1),
        ("ISQ2", "W", 1),
        ("ICC1", "W", 0.25),
        ("ISC1", "W", 0.25),
        ("DCQC", "IU012", 1),
    ],
)
def test_valid_energy_values(profile, status, factor) -> None:
    """C1 and A/B energy are summed directly; only power is divided by four."""
    coordinator = _build_coordinator()
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    records = [
        IntervalRecord(start + timedelta(minutes=i * 15), value, status)
        for i, value in enumerate([0.07, 0.08, 0.09, 0.1])
    ]
    hourly, meta = coordinator._process_records_hourly(records=records, profile=profile)
    assert hourly == {start: pytest.approx(0.34 * factor)}
    assert meta == {
        "last_valid_ts": start + timedelta(minutes=45),
        "last_status": status,
    }


@pytest.mark.parametrize(
    "status,value",
    [
        ("G", 10.0),
        ("F", None),
        ("UNKNOWN", 10.0),
        ("B", 10.0),
        ("E", 10.0),
        ("M", 10.0),
        ("N", 10.0),
    ],
)
def test_unfinalized_or_missing_records_do_not_contribute(status, value) -> None:
    coordinator = _build_coordinator()
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    hourly, meta = coordinator._process_records_hourly(
        records=[IntervalRecord(start, value, status)],
        profile="DCQC",
    )
    assert hourly == {start: 0.0}
    assert meta == {"last_valid_ts": None, "last_status": status}


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), float("-inf")])
def test_missing_or_nonfinite_final_values_do_not_corrupt_statistics(value) -> None:
    coordinator = _build_coordinator()
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    hourly, meta = coordinator._process_records_hourly(
        records=[IntervalRecord(start, value, "W")],
        profile="DCQC",
    )
    assert hourly == {start: 0.0}
    assert meta["last_valid_ts"] is None


@pytest.mark.parametrize("initial_status,final_status", [("G", "W"), ("F", "IU012")])
def test_revalidation_repairs_cumulative_statistics(
    initial_status, final_status
) -> None:
    """Valid corrections replace an hour and update every following sum once."""
    coordinator = _build_coordinator()
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    records = [
        IntervalRecord(start, 10.0, initial_status),
        IntervalRecord(end, 0.5, "W"),
    ]
    total, rows = _merge_records(coordinator, records, start, end)
    assert total == 0.5
    assert [row["sum"] for row in rows] == [0.0, 0.5]

    records[0] = IntervalRecord(start, 0.25, final_status)
    total, rows = _merge_records(coordinator, records, start, end)
    assert total == 0.75
    assert rows == [
        {"start": start, "state": 0.25, "sum": 0.25},
        {"start": end, "state": 0.75, "sum": 0.75},
    ]
    assert _merge_records(coordinator, records, start, end) == (0.75, [])

    # A subsequent explicit missing value removes the old contribution.
    records[0] = IntervalRecord(start, None, "F")
    total, rows = _merge_records(coordinator, records, start, end)
    assert total == 0.5
    assert [row["sum"] for row in rows] == [0.0, 0.5]
    # An empty response does not claim that previously fetched energy was zero.
    assert _merge_records(coordinator, [], start, end) == (0.5, [])


@pytest.mark.parametrize(
    "direction,old_profile,new_profile",
    [("import", "ICQ2", "DCQC"), ("export", "ISQ2", "DSQC")],
)
@pytest.mark.parametrize("has_profile_checkpoint", [False, True])
@pytest.mark.asyncio
async def test_profile_switch_replaces_history_and_retries_failed_fetch(
    monkeypatch, direction, old_profile, new_profile, has_profile_checkpoint
) -> None:
    """Switch profiles with cached history, retry failures, and avoid double counting."""
    start = datetime(2026, 4, 5, tzinfo=timezone.utc)
    end = datetime(2026, 8, 31, 23, tzinfo=timezone.utc)
    records = [
        IntervalRecord(start, 0.25, "W"),
        IntervalRecord(start + timedelta(hours=1), None, "F"),
        IntervalRecord(end, 0.75, "W"),
    ]
    coordinator = _build_c1_coordinator(monkeypatch, start, end, [])
    profile_key = f"{direction}_profile"
    cache_key = f"{direction}_hourly_deltas"
    total_key = f"total_{direction}_kwh"
    old_cache = coordinator._serialize_hourly_deltas(
        {
            start - timedelta(hours=1): 0.5,
            start: 1.0,
            start + timedelta(hours=1): 0.5,
            end: 0.75,
        }
    )
    coordinator.config_entry.data[profile_key] = old_profile
    coordinator.config_entry.options[profile_key] = new_profile
    coordinator._persisted.update(
        {
            cache_key: old_cache,
            f"{cache_key}_complete": True,
            f"last_valid_{direction}_timestamp": coordinator._iso(end),
            total_key: 2.75,
            "next_sync_attempt_utc": "2099-01-01T00:00:00Z",
        }
    )
    if has_profile_checkpoint:
        coordinator._persisted[profile_key] = old_profile
    coordinator.data = coordinator._build_state_from_persisted()
    assert coordinator.should_refresh_on_startup()
    add_statistics = MagicMock()
    monkeypatch.setattr(
        statistics_module, "async_add_external_statistics", add_statistics
    )

    async def fail_selected_profile(**kwargs):
        if kwargs["profile"] == new_profile:
            raise EgdApiError("Incomplete page")
        return []

    coordinator.client.async_get_profile_data.side_effect = fail_selected_profile
    with pytest.raises(EgdApiError, match="Incomplete"):
        await coordinator._async_refresh_energy_state()
    assert coordinator._persisted[cache_key] == old_cache
    assert coordinator._persisted.get(profile_key) == (
        old_profile if has_profile_checkpoint else None
    )
    add_statistics.assert_not_called()

    async def respond(**kwargs):
        if kwargs["profile"] != new_profile:
            return []
        return [record for record in records if record.timestamp >= kwargs["from_dt"]]

    coordinator.client.async_get_profile_data.reset_mock(side_effect=True)
    coordinator.client.async_get_profile_data.side_effect = respond
    state = await coordinator._async_refresh_energy_state()
    assert getattr(state, total_key) == 1.5
    assert coordinator._persisted[profile_key] == new_profile
    assert coordinator._persisted[f"{cache_key}_complete"] is True
    selected_call = next(
        call
        for call in coordinator.client.async_get_profile_data.call_args_list
        if call.kwargs["profile"] == new_profile
    )
    assert selected_call.kwargs["from_dt"] == start
    add_statistics.assert_called_once()
    _, metadata, rows = add_statistics.call_args.args
    assert metadata["statistic_id"].endswith(f"_{direction}")
    assert rows == [
        {"start": start, "state": 0.75, "sum": 0.75},
        {"start": start + timedelta(hours=1), "state": 0.75, "sum": 0.75},
        {"start": end, "state": 1.5, "sum": 1.5},
    ]

    add_statistics.reset_mock()
    coordinator.client.async_get_profile_data.reset_mock()
    again = await coordinator._async_refresh_energy_state()
    assert getattr(again, total_key) == 1.5
    add_statistics.assert_not_called()
    assert coordinator.client.async_get_profile_data.call_count == 2
    assert all(
        call.kwargs["from_dt"] == datetime(2026, 8, 1, tzinfo=timezone.utc)
        for call in coordinator.client.async_get_profile_data.call_args_list
    )


@pytest.mark.parametrize("has_export", [True, False])
@pytest.mark.asyncio
async def test_c1_refresh_imports_recorder_energy_and_handles_empty_export(
    monkeypatch, has_export
) -> None:
    """Exercise history, Recorder metadata, totals, diagnostics and a repeat refresh."""
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=45)
    imports = [
        IntervalRecord(start + timedelta(minutes=i * 15), value, "W")
        for i, value in enumerate([0.07, 0.08, 0.09, 0.1])
    ]
    exports = [IntervalRecord(end, 0.05, "W")] if has_export else []
    coordinator = _build_c1_coordinator(
        monkeypatch, start, end, [imports, exports, imports, exports]
    )
    add_statistics = MagicMock()
    monkeypatch.setattr(
        statistics_module, "async_add_external_statistics", add_statistics
    )

    state = await coordinator._async_refresh_energy_state()
    assert state.total_import_kwh == 0.34
    assert state.total_export_kwh == (0.05 if has_export else 0.0)
    assert state.last_import_status == "W"
    assert state.last_valid_import_timestamp == "2026-08-01T00:45:00Z"
    assert state.sync_status == ("ok" if has_export else "waiting_for_data")
    assert bool(state.last_api_sync_utc) == has_export
    assert coordinator._persisted["import_profile"] == "DCQC"
    assert coordinator._persisted["export_profile"] == "DSQC"
    assert add_statistics.call_count == (2 if has_export else 1)
    for call, direction, total in zip(
        add_statistics.call_args_list, ["import", "export"], [0.34, 0.05]
    ):
        _, metadata, rows = call.args
        assert (
            metadata["statistic_id"]
            == f"ha_egd_openapi:meter_859182400000000000_{direction}"
        )
        assert metadata["unit_of_measurement"] == "kWh"
        assert metadata["unit_class"] == "energy"
        assert metadata["has_sum"] is True
        assert metadata["has_mean"] is False
        assert rows == [{"start": start, "state": total, "sum": total}]

    add_statistics.reset_mock()
    again = await coordinator._async_refresh_energy_state()
    assert again.total_import_kwh == state.total_import_kwh
    assert again.total_export_kwh == state.total_export_kwh
    add_statistics.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_failure_does_not_checkpoint_partial_history(monkeypatch) -> None:
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    coordinator = _build_c1_coordinator(
        monkeypatch, start, start, EgdApiError("Incomplete profile response")
    )
    with pytest.raises(EgdApiError, match="Incomplete"):
        await coordinator._async_refresh_energy_state()
    assert "import_profile" not in coordinator._persisted
    assert "import_hourly_deltas" not in coordinator._persisted
    assert "import_hourly_deltas_complete" not in coordinator._persisted
    coordinator._store.async_save.assert_not_awaited()


def _build_c1_coordinator(monkeypatch, start, end, responses):
    coordinator = _build_coordinator()
    coordinator.hass = MagicMock()
    coordinator.config_entry.title = "Fake C1 meter"
    coordinator.config_entry.data.update(
        {
            "ean": "859182400000000000",
            "import_profile": "DCQC",
            "export_profile": "DSQC",
        }
    )
    coordinator._store = SimpleNamespace(async_save=AsyncMock())
    coordinator.client = SimpleNamespace(
        async_probe_access=AsyncMock(return_value=True),
        async_get_profile_data=AsyncMock(side_effect=responses),
    )
    monkeypatch.setattr(coordinator, "_hard_min_for_profile", lambda *args: start)
    monkeypatch.setattr(coordinator, "_get_latest_available_utc", lambda: end)
    return coordinator


def _merge_records(coordinator, records, start, end):
    hourly, _ = coordinator._process_records_hourly(records=records, profile="DCQC")
    return coordinator._merge_statistics(
        cache_key="hourly_deltas",
        cache_complete_key="complete",
        persisted_total_key="total",
        fetched_from=start,
        latest_available_utc=end,
        profile="DCQC",
        accessible_start_key="accessible_start",
        hourly_deltas=hourly,
    )
