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
    fill_missing_prices,
    price_unit_factor,
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


@pytest.mark.asyncio
async def test_fetch_prices_falls_back_to_hourly_statistics(monkeypatch) -> None:
    """History prices recent hours; long-term means price purged ones; MWh is converted."""
    entity = "sensor.spot_price"
    hass = MagicMock()
    hass.states.get.return_value = SimpleNamespace(attributes={"unit_of_measurement": "EUR/MWh"})
    instance = SimpleNamespace(
        async_add_executor_job=AsyncMock(
            side_effect=[
                {entity: [SimpleNamespace(state="100", last_changed=H1)]},
                {entity: [{"start": H0.timestamp(), "mean": 80.0}]},
            ]
        )
    )
    from homeassistant.components import recorder

    monkeypatch.setattr(recorder, "get_instance", lambda _hass: instance)
    prices = await cost_module.async_fetch_hourly_prices(hass, entity, [H0, H1])
    assert prices == {H0: pytest.approx(0.08), H1: pytest.approx(0.1)}


def _cost_coordinator(monkeypatch, *, price_entity="sensor.electricity_price"):
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
    add_statistics = MagicMock()
    monkeypatch.setattr(statistics_module, "async_add_external_statistics", add_statistics)
    fetch = AsyncMock(return_value={H0: 5.0})
    monkeypatch.setattr(
        "custom_components.ha_egd_openapi.coordinator.async_fetch_hourly_prices", fetch
    )
    return coordinator, add_statistics, fetch


def _cost_calls(add_statistics):
    return [
        call.args
        for call in add_statistics.call_args_list
        if call.args[1]["statistic_id"].endswith("_import_cost")
    ]


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
    fetch.return_value = {H0: 4.0}
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
    fetch.return_value = {H1: 5.0}
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
    fetch.return_value = {H0: 5.0}
    await coordinator._async_refresh_energy_state()
    assert len(_cost_calls(add_statistics)) == 1


@pytest.mark.asyncio
async def test_no_price_entity_writes_no_cost(monkeypatch) -> None:
    coordinator, add_statistics, fetch = _cost_coordinator(monkeypatch, price_entity=None)
    await coordinator._async_refresh_energy_state()
    assert _cost_calls(add_statistics) == []
    fetch.assert_not_awaited()
