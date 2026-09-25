"""Profile selection regressions for setup, options, and translations."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import frame

from custom_components.ha_egd_openapi import config_flow

PROFILE_PAIRS = [("DCQC", "DSQC"), ("ICQ2", "ISQ2"), ("ICC1", "ISC1")]


@pytest.mark.parametrize("import_profile,export_profile", PROFILE_PAIRS)
@pytest.mark.asyncio
async def test_setup_stores_raw_profile_codes(
    monkeypatch, import_profile, export_profile
) -> None:
    flow = config_flow.EgdConfigFlow()
    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_entries=lambda *args: [])
    )
    flow.async_set_unique_id = AsyncMock()
    monkeypatch.setattr(config_flow, "_validate_input", AsyncMock())
    data = config_flow._build_user_schema()(
        {
            "name": "Fake meter",
            "ean": "859182400000000000",
            "client_id": "fake-id",
            "client_secret": "fake-secret",
            "import_profile": import_profile,
            "export_profile": export_profile,
        }
    )
    result = await flow.async_step_user(data)
    assert result["type"] == "create_entry"
    assert result["data"]["import_profile"] == import_profile
    assert result["data"]["export_profile"] == export_profile


@pytest.mark.parametrize("import_profile,export_profile", PROFILE_PAIRS)
@pytest.mark.asyncio
async def test_options_select_and_preserve_profiles(
    monkeypatch, tmp_path, import_profile, export_profile
) -> None:
    entry = SimpleNamespace(
        options={"import_profile": import_profile, "export_profile": export_profile},
        data={"import_profile": "ICQ2", "export_profile": "ISQ2"},
    )
    hass = HomeAssistant(str(tmp_path))
    monkeypatch.setattr(frame._hass, "hass", hass)
    hass.config_entries = SimpleNamespace(async_get_known_entry=lambda entry_id: entry)
    flow = config_flow.EgdConfigFlow.async_get_options_flow(entry)
    flow.hass = hass
    flow.handler = "fake-entry-id"
    result = await flow.async_step_init()
    schema = result["data_schema"]
    defaults = schema({})
    assert defaults["import_profile"] == import_profile
    assert defaults["export_profile"] == export_profile
    defaults.update(import_profile="DCQC", export_profile="DSQC")
    result = await flow.async_step_init(schema(defaults))
    assert result["data"]["import_profile"] == "DCQC"
    assert result["data"]["export_profile"] == "DSQC"


@pytest.mark.parametrize(
    "key,value", [("import_profile", "DSQC"), ("export_profile", "DCQC")]
)
def test_profile_direction_is_validated(key, value) -> None:
    schema = config_flow._build_user_schema()
    with pytest.raises(config_flow.vol.Invalid):
        schema({"name": "Fake meter", key: value})


def test_ab_defaults_are_unchanged() -> None:
    defaults = config_flow._build_user_schema()({})
    assert defaults["import_profile"] == "ICQ2"
    assert defaults["export_profile"] == "ISQ2"


@pytest.mark.asyncio
async def test_options_keep_and_clear_price_and_tariff_entities(monkeypatch, tmp_path) -> None:
    entry = SimpleNamespace(
        options={
            "import_profile": "DCQC",
            "export_profile": "DSQC",
            "price_entity": "sensor.price",
            "tariff_entity": "binary_sensor.hdo",
        },
        data={"import_profile": "ICQ2", "export_profile": "ISQ2"},
    )
    hass = HomeAssistant(str(tmp_path))
    monkeypatch.setattr(frame._hass, "hass", hass)
    hass.config_entries = SimpleNamespace(async_get_known_entry=lambda entry_id: entry)
    flow = config_flow.EgdConfigFlow.async_get_options_flow(entry)
    flow.hass = hass
    flow.handler = "fake-entry-id"
    schema = (await flow.async_step_init())["data_schema"]
    suggested = {
        str(key): key.description["suggested_value"]
        for key in schema.schema
        if getattr(key, "description", None)
    }
    assert suggested == {"price_entity": "sensor.price", "tariff_entity": "binary_sensor.hdo"}

    kept = await flow.async_step_init(schema({**suggested}))
    assert kept["data"]["tariff_entity"] == "binary_sensor.hdo"
    cleared = await flow.async_step_init(schema({"price_entity": "sensor.price"}))
    assert "tariff_entity" not in cleared["data"]
    assert cleared["data"]["price_entity"] == "sensor.price"
