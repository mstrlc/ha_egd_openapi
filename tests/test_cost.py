"""Tests for pricing the import series by a price entity."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("homeassistant")

from test_coordinator import _build_c1_coordinator

from custom_components.ha_egd_openapi import cost as cost_module
from custom_components.ha_egd_openapi import statistics as statistics_module
from custom_components.ha_egd_openapi.api import IntervalRecord
from custom_components.ha_egd_openapi.cost import (
    dominant_values,
    fill_missing_prices,
    fill_missing_tariffs,
    parse_tariff,
    price_unit_factor,
    slot_starts,
    time_weighted_hourly_prices,
)

H0 = datetime(2026, 9, 23, 20, tzinfo=timezone.utc)
H1 = H0 + timedelta(hours=1)
H2 = H0 + timedelta(hours=2)
LOW, HIGH = 4.58, 5.317


def test_hdo_switch_at_half_hour_splits_the_hour() -> None:
    """A tariff switch at :30 prices each half of the hour at its own rate."""
    changes = [(H0 - timedelta(hours=3), HIGH), (H0 + timedelta(minutes=30), LOW)]
    prices = time_weighted_hourly_prices(changes, [H0, H1])
    assert prices[H0] == pytest.approx((HIGH + LOW) / 2)
    assert prices[H1] == pytest.approx(LOW)


def test_change_exactly_at_hour_start_applies_to_whole_hour() -> None:
    prices = time_weighted_hourly_prices([(H0 - timedelta(hours=1), HIGH), (H0, LOW)], [H0])
    assert prices[H0] == pytest.approx(LOW)


def test_unavailable_keeps_previous_price() -> None:
    changes = [
        (H0 - timedelta(hours=1), HIGH),
        (H0 + timedelta(minutes=10), None),
        (H0 + timedelta(minutes=40), LOW),
    ]
    prices = time_weighted_hourly_prices(changes, [H0])
    assert prices[H0] == pytest.approx((40 * HIGH + 20 * LOW) / 60)


def test_hours_before_first_known_price_are_not_priced() -> None:
    prices = time_weighted_hourly_prices([(H1 + timedelta(minutes=5), LOW)], [H0, H1, H2])
    assert set(prices) == {H2}
    assert time_weighted_hourly_prices([(H0, None)], [H0]) == {}


@pytest.mark.parametrize(
    "unit,factor",
    [("CZK/kWh", 1.0), ("EUR/MWh", 0.001), ("Kč/Wh", 1000.0), ("CZK", 1.0), (None, 1.0)],
)
def test_price_unit_factor(unit, factor) -> None:
    assert price_unit_factor(unit) == factor


def test_fill_missing_prices_uses_nearest_earlier_then_earliest() -> None:
    known = {H1: LOW}
    assert fill_missing_prices(known, [H0, H1, H2]) == {H0: LOW, H2: LOW}
    assert fill_missing_prices({}, [H0]) == {}


def _q(hour: datetime, index: int) -> datetime:
    return hour + timedelta(minutes=15 * index)


@pytest.mark.asyncio
async def test_fetch_prices_falls_back_to_hourly_statistics(monkeypatch) -> None:
    """History prices each quarter-hour; long-term means price purged hours; MWh is converted."""
    entity = "sensor.spot_price"
    hass = MagicMock()
    hass.states.get.return_value = SimpleNamespace(attributes={"unit_of_measurement": "EUR/MWh"})
    instance = SimpleNamespace(
        async_add_executor_job=AsyncMock(
            side_effect=[
                {
                    entity: [
                        SimpleNamespace(state="100", last_changed=H1),
                        SimpleNamespace(state="200", last_changed=_q(H1, 2)),
                    ]
                },
                {entity: [{"start": H0.timestamp(), "mean": 80.0}]},
            ]
        )
    )
    from homeassistant.components import recorder

    monkeypatch.setattr(recorder, "get_instance", lambda _hass: instance)
    prices = await cost_module.async_fetch_slot_prices(hass, entity, [H0, H1])
    assert prices == {
        H0: pytest.approx([0.08] * 4),
        H1: pytest.approx([0.1, 0.1, 0.2, 0.2]),
    }


@pytest.mark.asyncio
async def test_fetch_tariffs_takes_the_state_covering_most_of_each_slot(monkeypatch) -> None:
    """A sensor flipping 90 s after the :30 switch still classifies the slots cleanly."""
    entity = "binary_sensor.hdo"
    changes = [
        SimpleNamespace(state="off", last_changed=H0 - timedelta(hours=1)),
        SimpleNamespace(state="on", last_changed=_q(H0, 2) + timedelta(seconds=90)),
        SimpleNamespace(state="unavailable", last_changed=_q(H0, 3)),
    ]
    instance = SimpleNamespace(async_add_executor_job=AsyncMock(return_value={entity: changes}))
    from homeassistant.components import recorder

    monkeypatch.setattr(recorder, "get_instance", lambda _hass: instance)
    tariffs = await cost_module.async_fetch_slot_tariffs(MagicMock(), entity, slot_starts(H0))
    assert [tariffs[slot] for slot in slot_starts(H0)] == ["V", "V", "N", "N"]


def test_quarter_hour_takes_the_price_in_effect_for_most_of_it() -> None:
    """A price sensor flipping 90 s after the :30 switch must not bill the old price."""
    slots = slot_starts(H0)
    changes = [(H0 - timedelta(hours=1), HIGH), (_q(H0, 2) + timedelta(seconds=90), LOW)]
    prices = dominant_values(changes, slots, timedelta(minutes=15))
    assert [prices[slot] for slot in slots] == [HIGH, HIGH, LOW, LOW]
    assert dominant_values([(H1, LOW)], [H0], timedelta(minutes=15)) == {}


def test_parse_tariff() -> None:
    assert parse_tariff("on") == 0.0
    assert parse_tariff("off") == 1.0
    assert parse_tariff("unavailable") is None


def test_missing_tariffs_follow_the_weekly_then_daily_schedule() -> None:
    week = timedelta(days=7)
    known = {
        H0 - week: "N",  # same weekday and time a week earlier
        H0 - timedelta(days=1): "V",  # same time, previous day
        H1 + timedelta(days=1): "V",  # same time as H1, next day
        H2 - timedelta(minutes=15): "N",  # nearest slot to H2
    }
    assert fill_missing_tariffs(known, [H0, H1, H2 + timedelta(hours=5)]) == {
        H0: "N",
        H1: "V",
        H2 + timedelta(hours=5): "N",
    }
    assert fill_missing_tariffs({}, [H0]) == {}


def _cost_coordinator(
    monkeypatch, *, price_entity="sensor.electricity_price", tariff_entity=None
):
    end = H0 + timedelta(minutes=45)
    imports = [
        IntervalRecord(H0 + timedelta(minutes=i * 15), value, "W")
        for i, value in enumerate([0.1, 0.1, 0.1, 0.1])
    ]
    coordinator = _build_c1_coordinator(monkeypatch, H0, end, None)
    coordinator.client.async_get_profile_data = AsyncMock(
        side_effect=lambda **kw: imports if kw["profile"] == "DCQC" else []
    )
    coordinator.hass.config.currency = "CZK"
    if price_entity:
        coordinator.config_entry.data["price_entity"] = price_entity
    if tariff_entity:
        coordinator.config_entry.data["tariff_entity"] = tariff_entity
    add_statistics = MagicMock()
    monkeypatch.setattr(statistics_module, "async_add_external_statistics", add_statistics)
    fetch = AsyncMock(return_value={H0: [5.0] * 4})
    monkeypatch.setattr(
        "custom_components.ha_egd_openapi.coordinator.async_fetch_slot_prices", fetch
    )
    return coordinator, add_statistics, fetch


def _cost_calls(add_statistics, suffix="_import_cost"):
    return [
        call.args
        for call in add_statistics.call_args_list
        if call.args[1]["statistic_id"].endswith(suffix)
    ]


def _sums(add_statistics, suffix):
    [(_, _, rows)] = _cost_calls(add_statistics, suffix)
    return [row["sum"] for row in rows]


@pytest.mark.asyncio
async def test_refresh_writes_cost_statistic_once(monkeypatch) -> None:
    coordinator, add_statistics, fetch = _cost_coordinator(monkeypatch)

    await coordinator._async_refresh_energy_state()
    [(_, metadata, rows)] = _cost_calls(add_statistics)
    assert metadata["statistic_id"] == "ha_egd_openapi:meter_859182400000000000_import_cost"
    assert metadata["unit_of_measurement"] == "CZK"
    assert metadata["unit_class"] is None
    assert metadata["has_sum"] is True
    assert rows == [{"start": H0, "state": 2.0, "sum": 2.0}]

    # Cached prices are reused, and an unchanged series writes nothing.
    add_statistics.reset_mock()
    fetch.reset_mock()
    await coordinator._async_refresh_energy_state()
    assert _cost_calls(add_statistics) == []
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_revalidated_energy_is_repriced_at_cached_price(monkeypatch) -> None:
    coordinator, add_statistics, fetch = _cost_coordinator(monkeypatch)
    await coordinator._async_refresh_energy_state()

    corrected = [IntervalRecord(H0, 0.6, "W")]
    coordinator.client.async_get_profile_data = AsyncMock(
        side_effect=lambda **kw: corrected if kw["profile"] == "DCQC" else []
    )
    add_statistics.reset_mock()
    fetch.reset_mock()
    await coordinator._async_refresh_energy_state()
    [(_, _, rows)] = _cost_calls(add_statistics)
    assert rows == [{"start": H0, "state": 3.0, "sum": 3.0}]
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_changing_price_entity_reprices_everything(monkeypatch) -> None:
    coordinator, add_statistics, fetch = _cost_coordinator(monkeypatch)
    await coordinator._async_refresh_energy_state()

    coordinator.config_entry.options["price_entity"] = "sensor.other_price"
    fetch.return_value = {H0: [4.0] * 4}
    add_statistics.reset_mock()
    await coordinator._async_refresh_energy_state()
    assert fetch.await_args.args[1] == "sensor.other_price"
    [(_, _, rows)] = _cost_calls(add_statistics)
    assert rows == [{"start": H0, "state": 1.6, "sum": 1.6}]


@pytest.mark.asyncio
async def test_cleared_price_entity_in_options_stops_cost(monkeypatch) -> None:
    coordinator, add_statistics, fetch = _cost_coordinator(monkeypatch)
    coordinator.config_entry.options["import_profile"] = "DCQC"
    await coordinator._async_refresh_energy_state()
    assert _cost_calls(add_statistics) == []
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_hours_without_price_history_use_nearest_price(monkeypatch) -> None:
    coordinator, add_statistics, fetch = _cost_coordinator(monkeypatch)
    records = [IntervalRecord(H0, 1.0, "W"), IntervalRecord(H1, 1.0, "W")]
    coordinator.client.async_get_profile_data = AsyncMock(
        side_effect=lambda **kw: records if kw["profile"] == "DCQC" else []
    )
    monkeypatch.setattr(coordinator, "_get_latest_available_utc", lambda: H1)
    fetch.return_value = {H1: [5.0] * 4}
    await coordinator._async_refresh_energy_state()
    [(_, _, rows)] = _cost_calls(add_statistics)
    assert [row["sum"] for row in rows] == [5.0, 10.0]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("recorder not ready"), None])
async def test_price_problems_never_block_energy_import(monkeypatch, failure) -> None:
    coordinator, add_statistics, fetch = _cost_coordinator(monkeypatch)
    if failure:
        fetch.side_effect = failure
    else:
        fetch.return_value = {}
    state = await coordinator._async_refresh_energy_state()
    assert state.total_import_kwh == 0.4
    assert _cost_calls(add_statistics) == []
    assert "import_hourly_costs" not in coordinator._persisted
    # The next run tries again.
    fetch.side_effect = None
    fetch.return_value = {H0: [5.0] * 4}
    await coordinator._async_refresh_energy_state()
    assert len(_cost_calls(add_statistics)) == 1


@pytest.mark.asyncio
async def test_no_price_entity_writes_no_cost(monkeypatch) -> None:
    coordinator, add_statistics, fetch = _cost_coordinator(monkeypatch, price_entity=None)
    await coordinator._async_refresh_energy_state()
    assert _cost_calls(add_statistics) == []
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_price_entity_triggers_refresh_on_startup(monkeypatch) -> None:
    coordinator, _, _ = _cost_coordinator(monkeypatch)
    coordinator._persisted["next_sync_attempt_utc"] = "2099-01-01T00:00:00Z"
    await coordinator._async_refresh_energy_state()
    coordinator.data = coordinator._build_state_from_persisted()
    coordinator._persisted["next_sync_attempt_utc"] = "2099-01-01T00:00:00Z"
    assert not coordinator.should_refresh_on_startup()

    coordinator.config_entry.options.update(
        {"import_profile": "DCQC", "price_entity": "sensor.other_price"}
    )
    assert coordinator.should_refresh_on_startup()


@pytest.mark.asyncio
async def test_each_quarter_hour_is_priced_at_its_own_price(monkeypatch) -> None:
    """Uneven use across a :30 switch is priced exactly, not at the hourly mean."""
    coordinator, add_statistics, fetch = _cost_coordinator(monkeypatch)
    records = [IntervalRecord(_q(H0, 3), 1.0, "W")]
    coordinator.client.async_get_profile_data = AsyncMock(
        side_effect=lambda **kw: records if kw["profile"] == "DCQC" else []
    )
    fetch.return_value = {H0: [HIGH, HIGH, LOW, LOW]}
    await coordinator._async_refresh_energy_state()
    assert _sums(add_statistics, "_import_cost") == [pytest.approx(LOW)]


def _tariff_coordinator(monkeypatch, *, price_entity="sensor.electricity_price"):
    coordinator, add_statistics, fetch = _cost_coordinator(
        monkeypatch, price_entity=price_entity, tariff_entity="binary_sensor.hdo"
    )
    records = [
        IntervalRecord(_q(H0, index), value, "W")
        for index, value in enumerate([0.1, 0.2, 0.3, 0.4])
    ]
    coordinator.client.async_get_profile_data = AsyncMock(
        side_effect=lambda **kw: records if kw["profile"] == "DCQC" else []
    )
    fetch.return_value = {H0: [HIGH, HIGH, LOW, LOW]}
    tariffs = AsyncMock(
        side_effect=lambda _hass, _entity, slots: dict(zip(slots, "VVNN"))
    )
    monkeypatch.setattr(
        "custom_components.ha_egd_openapi.coordinator.async_fetch_slot_tariffs", tariffs
    )
    return coordinator, add_statistics, tariffs


@pytest.mark.asyncio
async def test_tariff_entity_splits_energy_and_cost_by_quarter_hour(monkeypatch) -> None:
    coordinator, add_statistics, tariffs = _tariff_coordinator(monkeypatch)
    await coordinator._async_refresh_energy_state()

    assert _sums(add_statistics, "_import_nt") == [pytest.approx(0.7)]
    assert _sums(add_statistics, "_import_vt") == [pytest.approx(0.3)]
    assert _sums(add_statistics, "_import_nt_cost") == [pytest.approx(0.7 * LOW)]
    assert _sums(add_statistics, "_import_vt_cost") == [pytest.approx(0.3 * HIGH)]
    assert _sums(add_statistics, "_import_cost") == [pytest.approx(0.7 * LOW + 0.3 * HIGH)]
    [(_, metadata, _)] = _cost_calls(add_statistics, "_import_nt")
    assert metadata["statistic_id"] == "ha_egd_openapi:meter_859182400000000000_import_nt"
    assert metadata["name"] == "Fake C1 meter Odběr NT"
    assert metadata["unit_of_measurement"] == "kWh"
    [(_, metadata, _)] = _cost_calls(add_statistics, "_import_vt_cost")
    assert metadata["unit_of_measurement"] == "CZK"
    assert coordinator._persisted["import_slot_tariffs"] == {"2026-09-23T20:00:00Z": "VVNN"}

    # Cached tariffs are reused, and an unchanged series writes nothing.
    add_statistics.reset_mock()
    tariffs.reset_mock()
    await coordinator._async_refresh_energy_state()
    add_statistics.assert_not_called()
    tariffs.assert_not_awaited()


@pytest.mark.asyncio
async def test_tariff_split_without_price_entity_writes_only_energy(monkeypatch) -> None:
    coordinator, add_statistics, _ = _tariff_coordinator(monkeypatch, price_entity=None)
    await coordinator._async_refresh_energy_state()
    assert _sums(add_statistics, "_import_nt") == [pytest.approx(0.7)]
    assert _sums(add_statistics, "_import_vt") == [pytest.approx(0.3)]
    assert _cost_calls(add_statistics, "_cost") == []


@pytest.mark.asyncio
async def test_slots_without_tariff_history_follow_the_weekly_schedule(monkeypatch) -> None:
    coordinator, add_statistics, tariffs = _tariff_coordinator(monkeypatch)
    week_ago = H0 - timedelta(days=7)
    coordinator._persisted.update(
        {
            "import_tariff_entity": "binary_sensor.hdo",
            "import_slot_tariffs": {"2026-09-16T20:00:00Z": "NNNV"},
        }
    )
    assert coordinator._parse_dt("2026-09-16T20:00:00Z") == week_ago
    # History covers only the first two quarter-hours.
    tariffs.side_effect = lambda _hass, _entity, slots: dict(zip(slots[:2], "VV"))
    await coordinator._async_refresh_energy_state()
    assert coordinator._persisted["import_slot_tariffs"]["2026-09-23T20:00:00Z"] == "VVNV"
    assert _sums(add_statistics, "_import_vt") == [pytest.approx(0.7)]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("recorder not ready"), None])
async def test_tariff_problems_never_block_energy_or_cost(monkeypatch, failure) -> None:
    coordinator, add_statistics, tariffs = _tariff_coordinator(monkeypatch)
    if failure:
        tariffs.side_effect = failure
    else:
        tariffs.side_effect = None
        tariffs.return_value = {}
    state = await coordinator._async_refresh_energy_state()
    assert state.total_import_kwh == 1.0
    assert len(_cost_calls(add_statistics, "_import_cost")) == 1
    assert _cost_calls(add_statistics, "_import_nt") == []
    assert "import_tariff_entity" not in coordinator._persisted


@pytest.mark.asyncio
async def test_changing_tariff_entity_resplits_everything(monkeypatch) -> None:
    coordinator, add_statistics, tariffs = _tariff_coordinator(monkeypatch)
    await coordinator._async_refresh_energy_state()

    coordinator.config_entry.options.update(
        {"price_entity": "sensor.electricity_price", "tariff_entity": "binary_sensor.other"}
    )
    tariffs.side_effect = lambda _hass, _entity, slots: dict(zip(slots, "NNNN"))
    add_statistics.reset_mock()
    await coordinator._async_refresh_energy_state()
    assert tariffs.await_args.args[1] == "binary_sensor.other"
    assert _sums(add_statistics, "_import_nt") == [pytest.approx(1.0)]
    assert _sums(add_statistics, "_import_vt") == [0.0]
    # The total cost does not depend on the tariff entity.
    assert _cost_calls(add_statistics, "_import_cost") == []


@pytest.mark.asyncio
async def test_v12_cache_is_refetched_once_and_seeded_from_hourly_prices(monkeypatch) -> None:
    """A v1.2 store has hourly kWh and prices only; one full refetch restores quarter-hours."""
    coordinator, add_statistics, fetch = _cost_coordinator(monkeypatch)
    records = [IntervalRecord(_q(H0, 3), 1.0, "W")]
    coordinator.client.async_get_profile_data = AsyncMock(
        side_effect=lambda **kw: records if kw["profile"] == "DCQC" else []
    )
    coordinator._persisted.update(
        {
            "import_profile": "DCQC",
            "export_profile": "DSQC",
            "import_hourly_deltas": {"2026-09-23T20:00:00Z": 1.0},
            "import_hourly_deltas_complete": True,
            "last_valid_import_timestamp": "2026-09-23T20:45:00Z",
            "import_cost_price_entity": "sensor.electricity_price",
            "import_hourly_prices": {"2026-09-23T20:00:00Z": 4.9},
            "import_hourly_costs": {"2026-09-23T20:00:00Z": 4.9},
        }
    )
    fetch.return_value = {}  # the hour's history is purged
    probe = AsyncMock(return_value=True)
    coordinator.client.async_probe_access = probe
    await coordinator._async_refresh_energy_state()

    probe.assert_awaited()
    assert coordinator._persisted["import_slot_kwh"] == {
        "2026-09-23T20:00:00Z": [0.0, 0.0, 0.0, 1.0]
    }
    assert coordinator._persisted["import_slot_prices"] == {"2026-09-23T20:00:00Z": [4.9] * 4}
    assert "import_hourly_prices" not in coordinator._persisted
    # Same price, same kWh: nothing to rewrite.
    assert _cost_calls(add_statistics) == []

    probe.reset_mock()
    await coordinator._async_refresh_energy_state()
    probe.assert_not_awaited()


def test_v12_store_triggers_refresh_on_startup() -> None:
    from test_coordinator import _build_coordinator

    coordinator = _build_coordinator()
    coordinator.data = object()
    coordinator.config_entry.data.update({"import_profile": "DCQC", "export_profile": "DSQC"})
    coordinator._persisted.update(
        {
            "import_profile": "DCQC",
            "export_profile": "DSQC",
            "next_sync_attempt_utc": "2099-01-01T00:00:00Z",
            "import_hourly_deltas": {"2026-09-23T20:00:00Z": 1.0},
        }
    )
    assert coordinator.should_refresh_on_startup()
    coordinator._persisted["import_slot_kwh"] = {}
    assert not coordinator.should_refresh_on_startup()


def test_hours_without_quarter_hours_are_spread_evenly() -> None:
    from test_coordinator import _build_coordinator

    coordinator = _build_coordinator()
    coordinator._persisted["import_slot_kwh"] = {"2026-09-23T21:00:00Z": [0.0, 0.0, 0.0, 0.4]}
    assert coordinator._load_slot_kwh({H0: 0.8, H1: 0.4}) == {
        H0: [0.2] * 4,
        H1: [0.0, 0.0, 0.0, 0.4],
    }


@pytest.mark.asyncio
async def test_new_tariff_entity_triggers_refresh_on_startup(monkeypatch) -> None:
    coordinator, _, _ = _tariff_coordinator(monkeypatch)
    await coordinator._async_refresh_energy_state()
    coordinator.data = coordinator._build_state_from_persisted()
    coordinator._persisted["next_sync_attempt_utc"] = "2099-01-01T00:00:00Z"
    assert not coordinator.should_refresh_on_startup()

    coordinator.config_entry.options.update(
        {
            "import_profile": "DCQC",
            "price_entity": "sensor.electricity_price",
            "tariff_entity": "binary_sensor.other",
        }
    )
    assert coordinator.should_refresh_on_startup()
