"""Helpers for importing external statistics into Home Assistant recorder."""

from __future__ import annotations

from typing import Any

from homeassistant.components.recorder.statistics import async_add_external_statistics

try:
    from homeassistant.components.recorder.models import StatisticMeanType
except ImportError:  # pragma: no cover
    class StatisticMeanType:  # type: ignore[override]
        NONE = 0


STAT_UNIT_CLASS_ENERGY = "energy"
STAT_MEAN_NONE = StatisticMeanType.NONE


def build_energy_metadata(*, statistic_id: str, name: str, source: str) -> dict[str, Any]:
    """Build recorder metadata for cumulative energy statistics."""
    return {
        "has_mean": False,
        "has_sum": True,
        "mean_type": STAT_MEAN_NONE,
        "name": name,
        "source": source,
        "statistic_id": statistic_id,
        "unit_class": STAT_UNIT_CLASS_ENERGY,
        "unit_of_measurement": "kWh",
    }


def build_cost_metadata(
    *, statistic_id: str, name: str, source: str, currency: str
) -> dict[str, Any]:
    """Build recorder metadata for a cumulative cost statistic."""
    return {
        "has_mean": False,
        "has_sum": True,
        "mean_type": STAT_MEAN_NONE,
        "name": name,
        "source": source,
        "statistic_id": statistic_id,
        "unit_class": None,
        "unit_of_measurement": currency,
    }


async def async_import_external_statistics(
    hass,
    *,
    statistic_id: str,
    name: str,
    source: str,
    rows: list[dict[str, Any]],
    currency: str | None = None,
) -> None:
    """Import external statistics rows if there is anything to import.

    Rows are energy in kWh, or a cost in `currency` when one is given.
    """
    if not rows:
        return

    # source MUST match the domain part of statistic_id
    # e.g. statistic_id "egd_openapi:meter_xxx_import" -> source "egd_openapi"
    domain = statistic_id.split(":", 1)[0]

    if currency is None:
        metadata = build_energy_metadata(
            statistic_id=statistic_id,
            name=name,
            source=domain,
        )
    else:
        metadata = build_cost_metadata(
            statistic_id=statistic_id,
            name=name,
            source=domain,
            currency=currency,
        )
    async_add_external_statistics(hass, metadata, rows)
