"""Price the imported energy series by a Home Assistant price entity."""

from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from functools import partial
from math import isfinite

from homeassistant.core import HomeAssistant

HOUR = timedelta(hours=1)

# Price entities are usually per kWh, but spot price sensors often report per MWh.
_ENERGY_UNIT_FACTORS = {"kwh": 1.0, "mwh": 0.001, "wh": 1000.0}


def price_unit_factor(unit: str | None) -> float:
    """Return the factor converting a `<currency>/<energy unit>` price to per kWh."""
    if not unit or "/" not in unit:
        return 1.0
    return _ENERGY_UNIT_FACTORS.get(unit.rsplit("/", 1)[1].strip().casefold(), 1.0)


def parse_price(value: object) -> float | None:
    """Return a numeric price, or None for unknown/unavailable states."""
    try:
        price = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return price if isfinite(price) else None


def time_weighted_hourly_prices(
    changes: list[tuple[datetime, float | None]],
    hours: list[datetime],
) -> dict[datetime, float]:
    """Return the time-weighted price over each hour [h, h + 1h).

    `changes` are the price entity's state changes in time order; a None value
    (unknown/unavailable) keeps the previous price, as a tariff does not stop
    applying because its sensor briefly dropped out. An hour is only priced when
    a numeric price is known at its start, so hours before the entity's history
    begins are left out rather than guessed.
    """
    known: list[tuple[datetime, float]] = []
    for timestamp, price in changes:
        if price is None:
            continue
        if known and known[-1][0] == timestamp:
            known[-1] = (timestamp, price)
        else:
            known.append((timestamp, price))
    if not known:
        return {}

    times = [timestamp for timestamp, _ in known]
    prices: dict[datetime, float] = {}
    for hour_start in hours:
        index = bisect_right(times, hour_start) - 1
        if index < 0:
            continue
        hour_end = hour_start + HOUR
        weighted = 0.0
        segment_start = hour_start
        price = known[index][1]
        index += 1
        while index < len(known) and known[index][0] < hour_end:
            weighted += price * (known[index][0] - segment_start).total_seconds()
            segment_start, price = known[index]
            index += 1
        weighted += price * (hour_end - segment_start).total_seconds()
        prices[hour_start] = weighted / HOUR.total_seconds()
    return prices


def fill_missing_prices(
    prices: dict[datetime, float],
    hours: list[datetime],
) -> dict[datetime, float]:
    """Return prices for hours the Recorder could not price.

    Each gets the latest known price before it, or the earliest known price when
    the gap precedes all of them (energy history usually starts long before the
    price entity's). Returns an empty dict when no price is known at all.
    """
    if not prices:
        return {}
    times = sorted(prices)
    filled: dict[datetime, float] = {}
    for hour_start in hours:
        if hour_start in prices:
            continue
        index = bisect_right(times, hour_start) - 1
        filled[hour_start] = prices[times[max(index, 0)]]
    return filled


async def async_fetch_hourly_prices(
    hass: HomeAssistant,
    entity_id: str,
    hours: list[datetime],
) -> dict[datetime, float]:
    """Price the given closed UTC hours from the Recorder, per kWh.

    State history gives the exact time-weighted price but is purged after the
    Recorder's `purge_keep_days`. For hours it no longer covers, the hourly mean
    from long-term statistics is used, which exists when the price entity has a
    `state_class` and is kept indefinitely.
    """
    if not hours:
        return {}

    from homeassistant.components.recorder import get_instance, history
    from homeassistant.components.recorder.statistics import statistics_during_period

    start = min(hours)
    end = max(hours) + HOUR
    instance = get_instance(hass)
    state = hass.states.get(entity_id)
    factor = price_unit_factor(
        state.attributes.get("unit_of_measurement") if state is not None else None
    )

    states = await instance.async_add_executor_job(
        partial(
            history.state_changes_during_period,
            hass,
            start,
            end,
            entity_id,
            no_attributes=True,
            include_start_time_state=True,
        )
    )
    changes = [
        (item.last_changed.astimezone(timezone.utc), parse_price(item.state))
        for item in states.get(entity_id.lower(), [])
    ]
    prices = time_weighted_hourly_prices(changes, hours)

    missing = [hour_start for hour_start in hours if hour_start not in prices]
    if missing:
        stats = await instance.async_add_executor_job(
            statistics_during_period,
            hass,
            min(missing),
            max(missing) + HOUR,
            {entity_id},
            "hour",
            None,
            {"mean"},
        )
        means = {
            datetime.fromtimestamp(row["start"], timezone.utc): parse_price(row.get("mean"))
            for row in stats.get(entity_id, [])
        }
        for hour_start in missing:
            mean = means.get(hour_start)
            if mean is not None:
                prices[hour_start] = mean

    return {hour_start: price * factor for hour_start, price in prices.items()}
