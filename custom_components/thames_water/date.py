"""Date platform for the Thames Water integration."""

from __future__ import annotations

from datetime import date, datetime

from homeassistant.components.date import DateEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import NEXT_LITER_COST_START_DATE_KEY
from .entity import ThamesWaterEntity


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up date entities for Thames Water."""
    if NEXT_LITER_COST_START_DATE_KEY in entry.options:
        next_date_raw = entry.options[NEXT_LITER_COST_START_DATE_KEY]
    else:
        next_date_raw = entry.data.get(NEXT_LITER_COST_START_DATE_KEY)

    async_add_entities(
        [ThamesWaterNextLiterCostStartDate(entry, initial_value=next_date_raw)]
    )


class ThamesWaterNextLiterCostStartDate(ThamesWaterEntity, DateEntity):
    """Date entity for the next planned liter cost start date."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "Next Liter Cost Start Date"
    _attr_icon = "mdi:calendar-start"

    def __init__(
        self,
        config_entry: ConfigEntry,
        initial_value: str | date | None = None,
    ) -> None:
        """Initialize the next rate start date entity."""
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_next_liter_cost_start_date"
        self._value = self._parse_date(initial_value)

    @staticmethod
    def _parse_date(value: str | date | None) -> date | None:
        """Parse date values in YYYY-MM-DD format."""
        if value in (None, ""):
            return None
        if isinstance(value, date):
            return value
        try:
            return datetime.strptime(str(value), "%Y-%m-%d").date()
        except ValueError:
            return None

    @property
    def native_value(self) -> date | None:
        """Return the configured next liter cost start date."""
        return self._value

    async def async_set_value(self, value: date) -> None:
        """Set the next liter cost start date."""
        self._value = value
        new_options = dict(self._config_entry.options)
        new_options[NEXT_LITER_COST_START_DATE_KEY] = value.isoformat()
        self.hass.config_entries.async_update_entry(
            self._config_entry, options=new_options
        )
        self.async_write_ha_state()
