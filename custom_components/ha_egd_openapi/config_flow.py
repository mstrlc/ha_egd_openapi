"""Config flow for EG.D OpenAPI."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.helpers import aiohttp_client, selector

from .api import EgdApiClient, EgdApiError, EgdAuthError
from .const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_EAN,
    CONF_ENABLE_DIAGNOSTICS,
    CONF_EXPORT_PROFILE,
    CONF_IMPORT_PROFILE,
    CONF_PRICE_ENTITY,
    CONF_REVALIDATE_DAYS,
    CONF_TARIFF_ENTITY,
    CONF_UPDATE_HOUR,
    CONF_UPDATE_MINUTE,
    DEFAULT_ENABLE_DIAGNOSTICS,
    DEFAULT_EXPORT_PROFILE,
    DEFAULT_IMPORT_PROFILE,
    DEFAULT_NAME,
    DEFAULT_REVALIDATE_DAYS,
    DEFAULT_UPDATE_HOUR,
    DEFAULT_UPDATE_MINUTE,
    DOMAIN,
)

# Profile codes, metering types and units are language-independent labels.
IMPORT_OPTIONS = [
    {"value": "ICQ2", "label": "ICQ2 (A/B, kWh)"},
    {"value": "ICC1", "label": "ICC1 (A/B, kW)"},
    {"value": "DCQC", "label": "DCQC (C1, kWh)"},
]
EXPORT_OPTIONS = [
    {"value": "ISQ2", "label": "ISQ2 (A/B, kWh)"},
    {"value": "ISC1", "label": "ISC1 (A/B, kW)"},
    {"value": "DSQC", "label": "DSQC (C1, kWh)"},
]


_ENTITY_FIELD_DOMAINS = {
    CONF_PRICE_ENTITY: ["sensor", "input_number", "number"],
    CONF_TARIFF_ENTITY: ["binary_sensor", "input_boolean"],
}


def _entity_fields(defaults: dict[str, Any]) -> dict[Any, Any]:
    """Build the optional price and tariff entity fields; they can be cleared again."""
    return {
        vol.Optional(
            key,
            description={"suggested_value": defaults[key]} if defaults.get(key) else None,
        ): selector.EntitySelector(selector.EntitySelectorConfig(domain=domains))
        for key, domains in _ENTITY_FIELD_DOMAINS.items()
    }


def _build_user_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, DEFAULT_NAME)): str,
            vol.Required(CONF_EAN, default=defaults.get(CONF_EAN, "")): str,
            vol.Required(CONF_CLIENT_ID, default=defaults.get(CONF_CLIENT_ID, "")): str,
            vol.Required(
                CONF_CLIENT_SECRET, default=defaults.get(CONF_CLIENT_SECRET, "")
            ): str,
            vol.Required(
                CONF_IMPORT_PROFILE,
                default=defaults.get(CONF_IMPORT_PROFILE, DEFAULT_IMPORT_PROFILE),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=IMPORT_OPTIONS,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_EXPORT_PROFILE,
                default=defaults.get(CONF_EXPORT_PROFILE, DEFAULT_EXPORT_PROFILE),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=EXPORT_OPTIONS,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_UPDATE_HOUR,
                default=int(defaults.get(CONF_UPDATE_HOUR, DEFAULT_UPDATE_HOUR)),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=23,
                    mode=selector.NumberSelectorMode.BOX,
                    step=1,
                )
            ),
            vol.Required(
                CONF_UPDATE_MINUTE,
                default=int(defaults.get(CONF_UPDATE_MINUTE, DEFAULT_UPDATE_MINUTE)),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=59,
                    mode=selector.NumberSelectorMode.BOX,
                    step=1,
                )
            ),
            vol.Required(
                CONF_REVALIDATE_DAYS,
                default=int(defaults.get(CONF_REVALIDATE_DAYS, DEFAULT_REVALIDATE_DAYS)),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=365,
                    mode=selector.NumberSelectorMode.BOX,
                    step=1,
                )
            ),
            **_entity_fields(defaults),
            vol.Required(
                CONF_ENABLE_DIAGNOSTICS,
                default=bool(
                    defaults.get(CONF_ENABLE_DIAGNOSTICS, DEFAULT_ENABLE_DIAGNOSTICS)
                ),
            ): bool,
        }
    )


def _build_options_schema(config_entry: config_entries.ConfigEntry) -> vol.Schema:
    return _build_user_schema(
        {
            CONF_NAME: config_entry.title,
            CONF_EAN: config_entry.data.get(CONF_EAN, ""),
            CONF_CLIENT_ID: config_entry.data.get(CONF_CLIENT_ID, ""),
            CONF_CLIENT_SECRET: config_entry.data.get(CONF_CLIENT_SECRET, ""),
            CONF_IMPORT_PROFILE: config_entry.options.get(
                CONF_IMPORT_PROFILE,
                config_entry.data.get(CONF_IMPORT_PROFILE, DEFAULT_IMPORT_PROFILE),
            ),
            CONF_EXPORT_PROFILE: config_entry.options.get(
                CONF_EXPORT_PROFILE,
                config_entry.data.get(CONF_EXPORT_PROFILE, DEFAULT_EXPORT_PROFILE),
            ),
            CONF_UPDATE_HOUR: config_entry.options.get(
                CONF_UPDATE_HOUR,
                config_entry.data.get(CONF_UPDATE_HOUR, DEFAULT_UPDATE_HOUR),
            ),
            CONF_UPDATE_MINUTE: config_entry.options.get(
                CONF_UPDATE_MINUTE,
                config_entry.data.get(CONF_UPDATE_MINUTE, DEFAULT_UPDATE_MINUTE),
            ),
            CONF_REVALIDATE_DAYS: config_entry.options.get(
                CONF_REVALIDATE_DAYS,
                config_entry.data.get(CONF_REVALIDATE_DAYS, DEFAULT_REVALIDATE_DAYS),
            ),
            CONF_PRICE_ENTITY: (config_entry.options or config_entry.data).get(
                CONF_PRICE_ENTITY
            ),
            CONF_TARIFF_ENTITY: (config_entry.options or config_entry.data).get(
                CONF_TARIFF_ENTITY
            ),
            CONF_ENABLE_DIAGNOSTICS: config_entry.options.get(
                CONF_ENABLE_DIAGNOSTICS,
                config_entry.data.get(
                    CONF_ENABLE_DIAGNOSTICS, DEFAULT_ENABLE_DIAGNOSTICS
                ),
            ),
        }
    )


def _entity_data(user_input: dict[str, Any]) -> dict[str, str]:
    """Return the price and tariff entities to store, omitting cleared fields."""
    return {key: user_input[key] for key in _ENTITY_FIELD_DOMAINS if user_input.get(key)}


async def _validate_input(hass, data: dict[str, Any]) -> None:
    session = aiohttp_client.async_get_clientsession(hass)
    client = EgdApiClient(session, data[CONF_CLIENT_ID], data[CONF_CLIENT_SECRET])
    await client.async_get_token()


class EgdConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for EG.D OpenAPI."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                await _validate_input(self.hass, user_input)
            except EgdAuthError:
                errors["base"] = "invalid_auth"
            except EgdApiError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(user_input[CONF_EAN])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data={
                        CONF_NAME: user_input[CONF_NAME],
                        CONF_EAN: user_input[CONF_EAN],
                        CONF_CLIENT_ID: user_input[CONF_CLIENT_ID],
                        CONF_CLIENT_SECRET: user_input[CONF_CLIENT_SECRET],
                        CONF_IMPORT_PROFILE: user_input[CONF_IMPORT_PROFILE],
                        CONF_EXPORT_PROFILE: user_input[CONF_EXPORT_PROFILE],
                        CONF_UPDATE_HOUR: int(user_input[CONF_UPDATE_HOUR]),
                        CONF_UPDATE_MINUTE: int(user_input[CONF_UPDATE_MINUTE]),
                        CONF_REVALIDATE_DAYS: int(user_input[CONF_REVALIDATE_DAYS]),
                        CONF_ENABLE_DIAGNOSTICS: bool(user_input[CONF_ENABLE_DIAGNOSTICS]),
                        **_entity_data(user_input),
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_build_user_schema(user_input),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return EgdOptionsFlowHandler(config_entry)


class EgdOptionsFlowHandler(config_entries.OptionsFlowWithConfigEntry):
    """Handle EG.D options."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    CONF_IMPORT_PROFILE: user_input[CONF_IMPORT_PROFILE],
                    CONF_EXPORT_PROFILE: user_input[CONF_EXPORT_PROFILE],
                    CONF_UPDATE_HOUR: int(user_input[CONF_UPDATE_HOUR]),
                    CONF_UPDATE_MINUTE: int(user_input[CONF_UPDATE_MINUTE]),
                    CONF_REVALIDATE_DAYS: int(user_input[CONF_REVALIDATE_DAYS]),
                    CONF_ENABLE_DIAGNOSTICS: bool(user_input[CONF_ENABLE_DIAGNOSTICS]),
                    **_entity_data(user_input),
                },
            )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_IMPORT_PROFILE,
                        default=self.config_entry.options.get(
                            CONF_IMPORT_PROFILE,
                            self.config_entry.data.get(
                                CONF_IMPORT_PROFILE, DEFAULT_IMPORT_PROFILE
                            ),
                        ),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=IMPORT_OPTIONS,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(
                        CONF_EXPORT_PROFILE,
                        default=self.config_entry.options.get(
                            CONF_EXPORT_PROFILE,
                            self.config_entry.data.get(
                                CONF_EXPORT_PROFILE, DEFAULT_EXPORT_PROFILE
                            ),
                        ),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=EXPORT_OPTIONS,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(
                        CONF_UPDATE_HOUR,
                        default=int(
                            self.config_entry.options.get(
                                CONF_UPDATE_HOUR,
                                self.config_entry.data.get(
                                    CONF_UPDATE_HOUR, DEFAULT_UPDATE_HOUR
                                ),
                            )
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=23,
                            mode=selector.NumberSelectorMode.BOX,
                            step=1,
                        )
                    ),
                    vol.Required(
                        CONF_UPDATE_MINUTE,
                        default=int(
                            self.config_entry.options.get(
                                CONF_UPDATE_MINUTE,
                                self.config_entry.data.get(
                                    CONF_UPDATE_MINUTE, DEFAULT_UPDATE_MINUTE
                                ),
                            )
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=59,
                            mode=selector.NumberSelectorMode.BOX,
                            step=1,
                        )
                    ),
                    vol.Required(
                        CONF_REVALIDATE_DAYS,
                        default=int(
                            self.config_entry.options.get(
                                CONF_REVALIDATE_DAYS,
                                self.config_entry.data.get(
                                    CONF_REVALIDATE_DAYS, DEFAULT_REVALIDATE_DAYS
                                ),
                            )
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1,
                            max=365,
                            mode=selector.NumberSelectorMode.BOX,
                            step=1,
                        )
                    ),
                    **_entity_fields(self.config_entry.options or self.config_entry.data),
                    vol.Required(
                        CONF_ENABLE_DIAGNOSTICS,
                        default=bool(
                            self.config_entry.options.get(
                                CONF_ENABLE_DIAGNOSTICS,
                                self.config_entry.data.get(
                                    CONF_ENABLE_DIAGNOSTICS,
                                    DEFAULT_ENABLE_DIAGNOSTICS,
                                ),
                            )
                        ),
                    ): bool,
                }
            ),
        )
