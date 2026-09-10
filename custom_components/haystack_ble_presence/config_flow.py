"""Config flow for the Haystack BLE presence integration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_DIRS,
    CONF_OFFLINE_TIMEOUT,
    DEFAULT_KEY_DIR,
    DEFAULT_OFFLINE_TIMEOUT,
    DOMAIN,
    MIN_OFFLINE_TIMEOUT,
)


def default_dirs(hass: HomeAssistant) -> list[str]:
    """Return the default key file directory inside the HA config dir."""
    return [hass.config.path(DEFAULT_KEY_DIR)]


def _parse_dirs(raw: str) -> list[str]:
    """Parse a multi-line directory list into clean absolute paths."""
    dirs: list[str] = []
    for line in raw.splitlines():
        candidate = line.strip().strip('"').strip("'").rstrip("/\\").strip()
        if candidate and candidate not in dirs:
            dirs.append(candidate)
    return dirs


def _dirs_to_text(dirs: list[str]) -> str:
    return "\n".join(dirs)


def _number_selector(minimum: int, maximum: int, step: int) -> selector.Selector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum, max=maximum, step=step, mode="box", unit_of_measurement="s"
        )
    )


def _flow_schema(current: Mapping[str, Any], hass: HomeAssistant) -> vol.Schema:
    """Schema for the add/options steps: directories + offline timeout."""
    dirs = current.get(CONF_DIRS, []) or default_dirs(hass)
    timeout = int(current.get(CONF_OFFLINE_TIMEOUT, DEFAULT_OFFLINE_TIMEOUT))
    return vol.Schema(
        {
            vol.Optional(CONF_DIRS, default=_dirs_to_text(dirs)): selector.TextSelector(
                selector.TextSelectorConfig(multiline=True)
            ),
            vol.Optional(CONF_OFFLINE_TIMEOUT, default=timeout): _number_selector(
                MIN_OFFLINE_TIMEOUT, 86400, 5
            ),
        }
    )


class HaystackBlePresenceConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for the integration."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            dirs = _parse_dirs(user_input[CONF_DIRS])
            if not dirs:
                errors[CONF_DIRS] = "empty_dirs"
            else:
                return self.async_create_entry(
                    title="Haystack BLE presence",
                    data={
                        CONF_DIRS: dirs,
                        CONF_OFFLINE_TIMEOUT: user_input[CONF_OFFLINE_TIMEOUT],
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_flow_schema({}, self.hass),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return HaystackBlePresenceOptionsFlow()


class HaystackBlePresenceOptionsFlow(OptionsFlow):
    """Handle an options flow for the integration."""

    def _current_entry(self) -> ConfigEntry:
        """Return the config entry being edited.

        Newer HA provides it as a read-only ``config_entry`` property on the
        flow; older versions expose it through ``self.handler`` (entry id).
        """
        entry = getattr(self, "config_entry", None)
        if entry is None:
            entry = self.hass.config_entries.async_get_entry(self.handler)
        if entry is None:
            raise RuntimeError("Config entry not found for options flow")
        return entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        config_entry = self._current_entry()
        merged = dict(config_entry.options)
        merged.setdefault(CONF_DIRS, config_entry.data.get(CONF_DIRS, []))
        merged.setdefault(
            CONF_OFFLINE_TIMEOUT,
            config_entry.data.get(CONF_OFFLINE_TIMEOUT, DEFAULT_OFFLINE_TIMEOUT),
        )

        if user_input is not None:
            dirs = _parse_dirs(user_input[CONF_DIRS])
            if not dirs:
                return self.async_show_form(
                    step_id="init",
                    data_schema=_flow_schema(merged, self.hass),
                    errors={CONF_DIRS: "empty_dirs"},
                )
            return self.async_create_entry(
                title="",
                data={
                    CONF_DIRS: dirs,
                    CONF_OFFLINE_TIMEOUT: user_input[CONF_OFFLINE_TIMEOUT],
                },
            )

        return self.async_show_form(
            step_id="init", data_schema=_flow_schema(merged, self.hass)
        )
