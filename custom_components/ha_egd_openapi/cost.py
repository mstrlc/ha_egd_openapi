"""Price and tariff-split the imported energy series by Home Assistant entities."""

from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from functools import partial
from math import isfinite

from typing import Callable, TypeVar

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

HOUR = timedelta(hours=1)
# EG.D meters report quarter-hours, and HDO switches on quarter-hour boundaries.
SLOT = timedelta(minutes=15)
SLOTS_PER_HOUR = 4
TARIFF_LOW = "N"
TARIFF_HIGH = "V"

_T = TypeVar("_T")

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


def parse_tariff(value: object) -> float | None:
    """Return the high-tariff share of an HDO state: `on` (signal active) is low."""
    return {"on": 0.0, "off": 1.0}.get(str(value).casefold())


def slot_starts(hour_start: datetime) -> list[datetime]:
    """Return the quarter-hour slot starts of one hour."""
    return [hour_start + SLOT * index for index in range(SLOTS_PER_HOUR)]


def time_weighted_hourly_prices(
    changes: list[tuple[datetime, float | None]],
    hours: list[datetime],
) -> dict[datetime, float]:
    """Return the time-weighted price over each hour [h, h + 1h)."""
    return time_weighted_values(changes, hours, HOUR)


def time_weighted_values(
    changes: list[tuple[datetime, float | None]],
    starts: list[datetime],
    length: timedelta,
) -> dict[datetime, float]:
    """Return the time-weighted value over each period [start, start + length).

    `changes` are an entity's state changes in time order; a None value
    (unknown/unavailable) keeps the previous value, as a tariff does not stop
    applying because its sensor briefly dropped out. A period is only valued when
    a value is known at its start, so periods before the entity's history begins
    are left out rather than guessed.
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
    values: dict[datetime, float] = {}
    for start in starts:
        index = bisect_right(times, start) - 1
        if index < 0:
            continue
        end = start + length
        weighted = 0.0
        segment_start = start
        value = known[index][1]
        index += 1
        while index < len(known) and known[index][0] < end:
            weighted += value * (known[index][0] - segment_start).total_seconds()
            segment_start, value = known[index]
            index += 1
        weighted += value * (end - segment_start).total_seconds()
        values[start] = weighted / length.total_seconds()
    return values


def fill_missing_prices(
    prices: dict[datetime, _T],
    hours: list[datetime],
) -> dict[datetime, _T]:
    """Return prices for hours the Recorder could not price.

    Each gets the latest known price before it, or the earliest known price when
    the gap precedes all of them (energy history usually starts long before the
    price entity's). Returns an empty dict when no price is known at all.
    """
    if not prices:
        return {}
    times = sorted(prices)
    filled: dict[datetime, _T] = {}
    for hour_start in hours:
        if hour_start in prices:
            continue
        index = bisect_right(times, hour_start) - 1
        filled[hour_start] = prices[times[max(index, 0)]]
    return filled


def fill_missing_tariffs(
    known: dict[datetime, str],
    slots: list[datetime],
) -> dict[datetime, str]:
    """Return tariffs for slots the Recorder could not classify.

    HDO follows a weekly schedule in local time, so a slot takes the tariff of
    the nearest known slot at the same local weekday and time, then at the same
    local time on any day, then the nearest known slot at all. Returns an empty
    dict when no tariff is known at all.
    """
    if not known:
        return {}

    def weekly(slot: datetime) -> tuple[int, int, int]:
        local = dt_util.as_local(slot)
        return local.weekday(), local.hour, local.minute

    def daily(slot: datetime) -> tuple[int, int]:
        local = dt_util.as_local(slot)
        return local.hour, local.minute

    indexes: list[tuple[Callable[[datetime], object], dict[object, list[datetime]]]] = []
    for key in (weekly, daily):
        index: dict[object, list[datetime]] = {}
        for slot in sorted(known):
            index.setdefault(key(slot), []).append(slot)
        indexes.append((key, index))
    everything = sorted(known)

    filled: dict[datetime, str] = {}
    for slot in slots:
        if slot in known:
            continue
        candidates = everything
        for key, index in indexes:
            if key(slot) in index:
                candidates = index[key(slot)]
                break
        filled[slot] = known[_nearest(candidates, slot)]
    return filled


def dominant_values(
    changes: list[tuple[datetime, float | None]],
    starts: list[datetime],
    length: timedelta,
) -> dict[datetime, float]:
    """Return the value in effect for the longest part of each period.

    Tariff prices change on quarter-hour boundaries, but their sensors often
    flip a minute late; time-weighting would bill that minute at the old price.
    None values keep the previous value, and a period is only valued when a
    value is known at its start, as in `time_weighted_values`.
    """
    known: list[tuple[datetime, float]] = []
    for timestamp, value in changes:
        if value is None:
            continue
        if known and known[-1][0] == timestamp:
            known[-1] = (timestamp, value)
        else:
            known.append((timestamp, value))
    times = [timestamp for timestamp, _ in known]
    values: dict[datetime, float] = {}
    for start in starts:
        index = bisect_right(times, start) - 1
        if index < 0:
            continue
        end = start + length
        durations: dict[float, float] = {}
        segment_start = start
        value = known[index][1]
        index += 1
        while index < len(known) and known[index][0] < end:
            durations[value] = durations.get(value, 0.0) + (
                known[index][0] - segment_start
            ).total_seconds()
            segment_start, value = known[index]
            index += 1
        durations[value] = durations.get(value, 0.0) + (end - segment_start).total_seconds()
        values[start] = max(durations, key=durations.__getitem__)
    return values


def _nearest(times: list[datetime], target: datetime) -> datetime:
    """Return the element of sorted, non-empty `times` closest to `target`."""
    index = bisect_right(times, target)
    before = times[index - 1] if index > 0 else None
    after = times[index] if index < len(times) else None
    if before is None:
        return after  # type: ignore[return-value]
    if after is None or target - before <= after - target:
        return before
    return after


async def _async_state_changes(
    hass: HomeAssistant,
    entity_id: str,
    start: datetime,
    end: datetime,
    parse: Callable[[object], float | None],
) -> list[tuple[datetime, float | None]]:
    """Return an entity's parsed state changes over [start, end) from the Recorder."""
    from homeassistant.components.recorder import get_instance, history

    states = await get_instance(hass).async_add_executor_job(
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
    return [
        (item.last_changed.astimezone(timezone.utc), parse(item.state))
        for item in states.get(entity_id.lower(), [])
    ]


async def async_fetch_slot_prices(
    hass: HomeAssistant,
    entity_id: str,
    hours: list[datetime],
) -> dict[datetime, list[float]]:
    """Price the quarter-hours of the given closed UTC hours from the Recorder, per kWh.

    State history gives the price in effect for most of each quarter-hour but
    is purged after the Recorder's `purge_keep_days`. For hours it no longer
    covers, all four quarter-hours take the hourly mean from long-term
    statistics, which exists when the price entity has a `state_class` and is
    kept indefinitely.
    """
    if not hours:
        return {}

    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period

    state = hass.states.get(entity_id)
    factor = price_unit_factor(
        state.attributes.get("unit_of_measurement") if state is not None else None
    )

    changes = await _async_state_changes(
        hass, entity_id, min(hours), max(hours) + HOUR, parse_price
    )
    slots = [slot for hour_start in hours for slot in slot_starts(hour_start)]
    values = dominant_values(changes, slots, SLOT)
    prices = {
        hour_start: [values[slot] for slot in slot_starts(hour_start)]
        for hour_start in hours
        if all(slot in values for slot in slot_starts(hour_start))
    }

    missing = [hour_start for hour_start in hours if hour_start not in prices]
    if missing:
        stats = await get_instance(hass).async_add_executor_job(
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
                prices[hour_start] = [mean] * SLOTS_PER_HOUR

    return {
        hour_start: [price * factor for price in slot_prices]
        for hour_start, slot_prices in prices.items()
    }


async def async_fetch_slot_tariffs(
    hass: HomeAssistant,
    entity_id: str,
    slots: list[datetime],
) -> dict[datetime, str]:
    """Classify quarter-hour slots as low or high tariff from the HDO entity's history.

    Each slot takes the state that covers most of it, which absorbs the delay
    of an HDO sensor that flips shortly after the actual switch.
    """
    if not slots:
        return {}
    changes = await _async_state_changes(
        hass, entity_id, min(slots), max(slots) + SLOT, parse_tariff
    )
    return {
        slot: TARIFF_HIGH if share >= 0.5 else TARIFF_LOW
        for slot, share in time_weighted_values(changes, slots, SLOT).items()
    }
