"""Platform for sensor integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date, datetime, timedelta
import logging
from operator import itemgetter
import random

from homeassistant.components import persistent_notification
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_RATE_REMINDER_DAYS,
    DEFAULT_LITER_COST,
    DOMAIN,
    MAX_RATE_REMINDER_DAYS,
    MIN_RATE_REMINDER_DAYS,
    NEXT_LITER_COST_KEY,
    NEXT_LITER_COST_START_DATE_KEY,
    RATE_REMINDER_DAYS_KEY,
    TEST_MODE_KEY,
)
from .entity import ThamesWaterEntity
from .thameswaterclient import ThamesWater

_LOGGER = logging.getLogger(__name__)
UPDATE_HOURS = [15, 23]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> bool:
    """Set up the Thames Water sensor platform."""
    sensor = ThamesWaterSensor(
        hass,
        entry,
    )

    async_add_entities([sensor], update_before_add=False)

    if entry.data.get("fetch_hours"):
        try:
            update_hours = [
                int(h.strip()) for h in entry.data["fetch_hours"].split(",")
            ]
        except (ValueError, AttributeError):
            _LOGGER.warning("Invalid fetch_hours configuration, using defaults")
            update_hours = UPDATE_HOURS
    else:
        update_hours = UPDATE_HOURS

    # Schedule the sensor to update every day at UPDATE_HOURS.
    rand_minute = random.randint(0, 10)
    unsubscribe = async_track_time_change(
        hass,
        sensor.async_update_callback,
        hour=update_hours,
        minute=rand_minute,
        second=0,
    )
    entry.async_on_unload(unsubscribe)

    # Run an initial refresh in the background so setup can complete quickly.
    initial_update_task = hass.async_create_task(sensor.async_update_callback(None))
    entry.async_on_unload(initial_update_task.cancel)
    return True


def _generate_statistics_from_readings(
    readings: list[dict],
    cumulative_start: float = 0.0,
    cost_for_dt: Callable[[datetime], float] | None = None,
) -> list[StatisticData]:
    """Convert a list of (datetime, reading) entries into StatisticData entries."""
    sorted_readings = sorted(readings, key=lambda x: x["dt"])
    cumulative = cumulative_start
    stats: list[StatisticData] = []
    for elem in sorted_readings:
        # Normalize the start timestamp to the hour
        hour_ts = elem["dt"].replace(minute=0, second=0, microsecond=0)
        if cost_for_dt is None:
            value = elem["state"]
        else:
            value = elem["state"] * cost_for_dt(elem["dt"])
        cumulative += value
        stats.append(
            StatisticData(
                start=dt_util.as_utc(hour_ts),
                state=value,
                sum=cumulative,
            )
        )
    return stats


class ThamesWaterSensor(ThamesWaterEntity, SensorEntity):
    """Thames Water Sensor class."""

    _attr_state_class = SensorStateClass.TOTAL
    _attr_device_class = SensorDeviceClass.WATER
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
    _attr_name = "Thames Water Sensor"

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        self._hass = hass
        self._config_entry = config_entry
        self._state: float | None = None

        username = config_entry.data.get("username")
        password = config_entry.data.get("password")
        account_number = config_entry.data.get("account_number")
        meter_id = config_entry.data.get("meter_id")

        # Validate required fields and log errors
        if not username:
            _LOGGER.error(
                "Username not found in config entry data. Available keys: %s",
                list(config_entry.data.keys()),
            )
            raise ConfigEntryNotReady(
                "Username not configured. Please remove and re-add the integration."
            )

        if not password:
            _LOGGER.error(
                "Password not found in config entry data. Available keys: %s",
                list(config_entry.data.keys()),
            )
            raise ConfigEntryNotReady(
                "Password not configured. Please remove and re-add the integration."
            )

        if not account_number:
            _LOGGER.error(
                "Account number not found in config entry data. Available keys: %s",
                list(config_entry.data.keys()),
            )
            raise ConfigEntryNotReady(
                "Account number not configured. Please remove and re-add the integration."
            )

        if not meter_id:
            _LOGGER.error(
                "Meter ID not found in config entry data. Available keys: %s",
                list(config_entry.data.keys()),
            )
            raise ConfigEntryNotReady(
                "Meter ID not configured. Please remove and re-add the integration."
            )

        self._username: str = username
        self._password: str = password
        self._account_number: int = account_number
        self._meter_id: int = meter_id
        self._attr_unique_id = f"water_usage_{self._meter_id}"
        self._attr_should_poll = False

    def _get_current_liter_cost(self) -> float:
        """Read current liter cost from options first, then config entry data."""
        raw_value = self._config_entry.options.get(
            "liter_cost",
            self._config_entry.data.get("liter_cost", DEFAULT_LITER_COST),
        )
        try:
            return float(raw_value)
        except (TypeError, ValueError):
            _LOGGER.warning("Invalid liter_cost value: %s, using default", raw_value)
            return DEFAULT_LITER_COST

    def _get_next_liter_cost(self) -> float | None:
        """Read next liter cost if configured."""
        raw_value = self._config_entry.options.get(
            NEXT_LITER_COST_KEY,
            self._config_entry.data.get(NEXT_LITER_COST_KEY),
        )
        if raw_value in (None, ""):
            return None
        try:
            return float(raw_value)
        except (TypeError, ValueError):
            _LOGGER.warning("Invalid %s value: %s", NEXT_LITER_COST_KEY, raw_value)
            return None

    def _get_next_liter_cost_start_date(self) -> date | None:
        """Read and parse next liter cost start date (YYYY-MM-DD) if configured."""
        raw_value = self._config_entry.options.get(
            NEXT_LITER_COST_START_DATE_KEY,
            self._config_entry.data.get(NEXT_LITER_COST_START_DATE_KEY),
        )
        if raw_value in (None, ""):
            return None
        try:
            return datetime.strptime(str(raw_value), "%Y-%m-%d").date()
        except ValueError:
            _LOGGER.warning(
                "Invalid %s value '%s'. Expected format YYYY-MM-DD.",
                NEXT_LITER_COST_START_DATE_KEY,
                raw_value,
            )
            return None

    def _get_rate_reminder_days(self) -> int | None:
        """Read reminder days; blank disables reminders."""
        raw_value = self._config_entry.options.get(
            RATE_REMINDER_DAYS_KEY,
            self._config_entry.data.get(
                RATE_REMINDER_DAYS_KEY, DEFAULT_RATE_REMINDER_DAYS
            ),
        )
        if raw_value in (None, ""):
            return None
        try:
            reminder_days = int(raw_value)
        except (TypeError, ValueError):
            _LOGGER.warning("Invalid %s value: %s", RATE_REMINDER_DAYS_KEY, raw_value)
            return DEFAULT_RATE_REMINDER_DAYS

        if (
            reminder_days < MIN_RATE_REMINDER_DAYS
            or reminder_days > MAX_RATE_REMINDER_DAYS
        ):
            _LOGGER.warning(
                "%s value %s is out of range, using default %s",
                RATE_REMINDER_DAYS_KEY,
                reminder_days,
                DEFAULT_RATE_REMINDER_DAYS,
            )
            return DEFAULT_RATE_REMINDER_DAYS
        return reminder_days

    def _is_test_mode(self) -> bool:
        """Return whether synthetic test mode data generation is enabled."""
        raw_value = self._config_entry.options.get(
            TEST_MODE_KEY,
            self._config_entry.data.get(TEST_MODE_KEY, False),
        )
        return bool(raw_value)

    def _build_test_readings(
        self,
        start_date: date,
        end_date: date,
    ) -> tuple[list[dict], float]:
        """Generate deterministic hourly readings for local testing."""
        readings: list[dict] = []
        latest_usage = 0.0
        current = start_date

        while current <= end_date:
            daily_total = 0.0
            # Deterministic but non-flat pattern: 10, 11, 12, 13 repeating each hour.
            for hour in range(24):
                usage = float(10 + (hour % 4))
                daily_total += usage
                readings.append(
                    {
                        "dt": datetime(current.year, current.month, current.day, hour, 0),
                        "state": usage,
                    }
                )
            latest_usage = daily_total
            current = current + timedelta(days=1)

        _LOGGER.warning(
            "TEST MODE ENABLED: Generated %d synthetic hourly readings from %s to %s",
            len(readings),
            start_date,
            end_date,
        )
        return readings, latest_usage

    async def _promote_next_rate_if_due(self) -> None:
        """Promote next rate to current and clear next fields once start date has passed."""
        next_liter_cost = self._get_next_liter_cost()
        next_start_date = self._get_next_liter_cost_start_date()
        if next_liter_cost is None or next_start_date is None:
            return

        today = dt_util.now().date()
        if today < next_start_date:
            return

        new_options = dict(self._config_entry.options)
        new_options["liter_cost"] = next_liter_cost
        new_options[NEXT_LITER_COST_KEY] = ""
        new_options[NEXT_LITER_COST_START_DATE_KEY] = ""
        self.hass.config_entries.async_update_entry(
            self._config_entry, options=new_options
        )
        _LOGGER.info(
            "Promoted %s to liter_cost for start date %s and cleared next rate fields",
            next_liter_cost,
            next_start_date,
        )

    async def _update_rate_change_notification(self) -> None:
        """Show or dismiss annual reminder before April 1 when next cost is blank."""
        notification_id = f"{DOMAIN}_{self._config_entry.entry_id}_next_rate_reminder"
        reminder_days = self._get_rate_reminder_days()
        next_liter_cost = self._get_next_liter_cost()
        if reminder_days is None or next_liter_cost is not None:
            persistent_notification.async_dismiss(self._hass, notification_id)
            return

        today = dt_util.now().date()
        april_first = date(today.year, 4, 1)
        target_april_first = (
            april_first if today < april_first else date(today.year + 1, 4, 1)
        )
        reminder_start = target_april_first - timedelta(days=reminder_days)

        if reminder_start <= today < target_april_first:
            translations = await async_get_translations(
                self._hass,
                self._hass.config.language,
                "config",
                [DOMAIN],
            )
            title = translations[
                f"component.{DOMAIN}.config.error.tariff_reminder_title"
            ]
            message_template = translations[
                f"component.{DOMAIN}.config.error.tariff_reminder_message"
            ]
            message = message_template.format(
                april_first=target_april_first.isoformat(),
                reminder_days=reminder_days,
            )
            persistent_notification.async_create(
                self._hass,
                message,
                title=title,
                notification_id=notification_id,
            )
        else:
            persistent_notification.async_dismiss(self._hass, notification_id)

    @property
    def state(self) -> float | None:
        """Return the sensor state (latest hourly consumption in Liters)."""
        return self._state

    async def async_update_callback(self, ts) -> None:
        """Update the sensor state."""
        try:
            await self.async_update()
            self.async_write_ha_state()
        except asyncio.CancelledError:
            _LOGGER.debug("Thames Water sensor update callback was cancelled")
            raise
        except Exception as err:
            _LOGGER.error("Unexpected error in Thames Water update callback: %s", err)

    async def async_update(self):
        """Fetch data, build hourly statistics, and inject external statistics."""
        consumption_stat_id = f"{DOMAIN}:thameswater_consumption"
        cost_stat_id = f"{DOMAIN}:thameswater_cost"

        last_stats = None
        last_cost_stats = None

        try:
            async with asyncio.timeout(30):
                last_stats = await get_instance(self.hass).async_add_executor_job(
                    get_last_statistics,
                    self.hass,
                    1,
                    consumption_stat_id,
                    True,
                    {"sum"},
                )
            async with asyncio.timeout(30):
                last_cost_stats = await get_instance(self.hass).async_add_executor_job(
                    get_last_statistics, self.hass, 1, cost_stat_id, True, {"sum"}
                )

            # If a previous value exists, use its "sum" as the starting cumulative.
            if len(last_stats.get(consumption_stat_id, [])) > 0:
                last_stats = last_stats[consumption_stat_id]
                last_stats = sorted(last_stats, key=itemgetter("start"), reverse=False)[
                    0
                ]
            # If a previous value exists, use its "sum" as the starting cumulative.
            if len(last_cost_stats.get(cost_stat_id, [])) > 0:
                last_cost_stats = last_cost_stats[cost_stat_id]
                last_cost_stats = sorted(
                    last_cost_stats, key=itemgetter("start"), reverse=False
                )[0]

        except TimeoutError:
            _LOGGER.warning(
                "Timeout while fetching last statistics for Thames Water integration"
            )
            last_stats = None
            last_cost_stats = None
        except (Exception) as err:
            _LOGGER.error("Error fetching last statistics: %s", err)
            last_stats = None
            last_cost_stats = None

        test_mode_enabled = self._is_test_mode()
        if test_mode_enabled:
            # In test mode, use the most recent days to make tariff boundary testing quick.
            end_dt = datetime.now()
            start_dt = end_dt - timedelta(days=4)
        else:
            # Live mode uses Thames Water delayed data.
            end_dt = datetime.now() - timedelta(days=3)
            if (
                last_stats is not None
                and last_stats.get("sum") is not None
                and last_stats.get("start") is not None
            ):
                start_dt = dt_util.as_utc(
                    datetime.fromtimestamp(last_stats.get("start"))
                )
            else:
                start_dt = end_dt - timedelta(days=30)

        current_date = start_dt.date()
        end_date = end_dt.date()

        # readings holds all hourly data for the entire period.
        readings: list[dict] = []
        latest_usage = 0.0

        if test_mode_enabled:
            readings, latest_usage = self._build_test_readings(
                start_date=current_date,
                end_date=end_date,
            )
        else:
            try:
                _LOGGER.debug("Creating Thames Water Client")
                async with asyncio.timeout(120):
                    tw_client = await self._hass.async_add_executor_job(
                        ThamesWater,
                        self._username,
                        self._password,
                        self._account_number,
                    )
            except TimeoutError:
                _LOGGER.error("Timeout creating Thames Water client")
                return
            except asyncio.CancelledError:
                _LOGGER.warning("Thames Water client creation was cancelled")
                raise
            except Exception as err:
                _LOGGER.error("Error creating Thames Water client: %s", err)
                return

            while current_date <= end_date:
                year = current_date.year
                month = current_date.month
                day = current_date.day
                current_date = current_date + timedelta(days=1)

                d = datetime(year, month, day)
                _LOGGER.debug("Fetching data for %s/%s/%s", day, month, year)

                try:
                    async with asyncio.timeout(30):
                        data = await self._hass.async_add_executor_job(
                            tw_client.get_meter_usage,
                            self._meter_id,
                            d,
                            d,
                        )
                except TimeoutError:
                    _LOGGER.warning(
                        "Timeout fetching data for %s/%s/%s", day, month, year
                    )
                    break
                except Exception as err:
                    _LOGGER.warning(
                        "Could not get data for %s/%s/%s: %s", day, month, year, err
                    )
                    break

                if (
                    data is None
                    or data.Lines is None
                    or data.IsDataAvailable is False
                    or data.IsError
                ):
                    break

                # Process the returned data; expect a "Lines" list.
                lines = data.Lines

                if len(lines) < 24:
                    _LOGGER.warning(
                        "Stopping at %s/%s/%s - only %d/24 hours available, Thames Water data not yet complete",
                        day,
                        month,
                        year,
                        len(lines),
                    )
                    break

                latest_usage = 0
                for line in lines:
                    time_str = line.Label
                    usage = line.Usage
                    latest_usage += usage
                    try:
                        hour, minute = map(int, time_str.split(":"))
                    except (ValueError, AttributeError) as err:
                        _LOGGER.error("Error parsing time %s: %s", time_str, err)
                        continue

                    naive_datetime = datetime(year, month, day, hour, minute)
                    readings.append(
                        {
                            "dt": naive_datetime,
                            "state": usage,  # Usage in Liters per hour
                        }
                    )

        _LOGGER.info("Fetched %d historical entries", len(readings))

        current_liter_cost = self._get_current_liter_cost()
        next_liter_cost = self._get_next_liter_cost()
        next_start_date = self._get_next_liter_cost_start_date()
        _LOGGER.debug(
            "Using current liter cost: %s, next liter cost: %s, next start date: %s",
            current_liter_cost,
            next_liter_cost,
            next_start_date,
        )
        await self._update_rate_change_notification()

        if (
            last_stats is not None
            and last_stats.get("sum") is not None
            and last_stats.get("start") is not None
        ):
            initial_cumulative = last_stats.get("sum")
            # Discard all readings before last_stats["start"].
            start_ts = dt_util.as_utc(datetime.fromtimestamp(last_stats.get("start")))

            try:
                # Attempt to restore state if None.
                if self._state is None and len(readings) > 0:
                    last_recorded_date = (
                        start_ts.date() - timedelta(days=1)
                        if start_ts.hour == 0
                        else start_ts.date()
                    )
                    daily_total = sum(
                        r["state"]
                        for r in readings
                        if r["dt"].date() == last_recorded_date
                    )
                    if daily_total > 0:
                        self._state = daily_total
                        _LOGGER.debug(
                            "Restored state from last recorded day %s: %s L",
                            last_recorded_date,
                            self._state,
                        )
            except Exception as err:
                _LOGGER.error("Failed to restore state from last recorded day: %s", err)

            readings = [r for r in readings if dt_util.as_utc(r["dt"]) > start_ts]
        else:
            initial_cumulative = 0.0

        if last_cost_stats is not None and last_cost_stats.get("sum") is not None:
            initial_cost_cumulative = last_cost_stats.get("sum")
        else:
            initial_cost_cumulative = 0.0

        if len(readings) == 0:
            _LOGGER.warning("No new readings available")
            await self._promote_next_rate_if_due()
            return

        def cost_for_dt(dt_value: datetime) -> float:
            if (
                next_liter_cost is not None
                and next_start_date is not None
                and dt_value.date() >= next_start_date
            ):
                return next_liter_cost
            return current_liter_cost

        # Generate new StatisticData entries using the previous cumulative sum.
        stats = _generate_statistics_from_readings(
            readings, cumulative_start=initial_cumulative
        )
        cost_stats = _generate_statistics_from_readings(
            readings,
            cumulative_start=initial_cost_cumulative,
            cost_for_dt=cost_for_dt,
        )
        if latest_usage > 0:
            self._state = latest_usage

        # Build per-hour statistics from each reading.
        metadata_consumption = StatisticMetaData(
            has_mean=False,
            has_sum=True,
            name="Thames Water Consumption",
            source=DOMAIN,
            statistic_id=consumption_stat_id,
            unit_of_measurement=UnitOfVolume.LITERS,
            mean_type=StatisticMeanType.NONE,
            unit_class="volume",
        )
        metadata_cost = StatisticMetaData(
            has_mean=False,
            has_sum=True,
            name="Thames Water Cost",
            source=DOMAIN,
            statistic_id=cost_stat_id,
            unit_of_measurement="GBP",
            mean_type=StatisticMeanType.NONE,
            unit_class=None,
        )
        try:
            async_add_external_statistics(self._hass, metadata_consumption, stats)
            async_add_external_statistics(self._hass, metadata_cost, cost_stats)
            await self._promote_next_rate_if_due()
        except Exception as err:
            _LOGGER.error("Error writing statistics to database: %s", err)
            raise
