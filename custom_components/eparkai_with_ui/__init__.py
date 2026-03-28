import logging
import random
import re
from datetime import timedelta, datetime

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticMetaData,
    StatisticData,
    StatisticMeanType,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics, statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_ID, CONF_NAME, CONF_USERNAME, CONF_PASSWORD, CONF_CLIENT_ID,
    UnitOfEnergy, EVENT_HOMEASSISTANT_STARTED,
)
from homeassistant.core import HomeAssistant, Event, ServiceCall
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    CONF_POWER_PLANTS,
    CONF_OBJECT_ADDRESS,
    CONF_GENERATION_PERCENTAGE,
    CONF_STATISTICS_ID_SUFFIX,
)
from .eparkai_client import EParkaiClient

_LOGGER = logging.getLogger(__name__)

SERVICE_IMPORT_GENERATION = "import_generation"

POWER_PLANT_SCHEMA = vol.Schema({
    vol.Required(CONF_NAME): cv.string,
    vol.Required(CONF_ID): cv.string,
    vol.Optional(CONF_OBJECT_ADDRESS, default=None): vol.Any(None, cv.string),
    vol.Optional(CONF_STATISTICS_ID_SUFFIX, default=""): cv.string,
    vol.Optional(CONF_GENERATION_PERCENTAGE, default=100): vol.All(int, vol.Range(min=1, max=100))
})

CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Required(CONF_CLIENT_ID): cv.string,
        vol.Required(CONF_POWER_PLANTS): cv.ensure_list(POWER_PLANT_SCHEMA),
    })
}, extra=vol.ALLOW_EXTRA)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up from YAML configuration."""
    hass.data.setdefault(DOMAIN, {})

    if DOMAIN not in config:
        return True

    # Import YAML config into a config entry
    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "import"},
            data=config[DOMAIN],
        )
    )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up eParkai from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    client = EParkaiClient(
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        client_id=entry.data[CONF_CLIENT_ID],
    )

    async def async_import_generation(now: datetime) -> None:
        if hass.is_stopping:
            _LOGGER.debug("HA is stopping, skipping generation import")
            return

        power_plants = entry.options.get(CONF_POWER_PLANTS, [])
        if not power_plants:
            _LOGGER.warning("No power plants configured for eParkai")
            return

        try:
            _LOGGER.info("Logging in to eParkai.lt (%s)", now)
            await hass.async_add_executor_job(client.login)
        except Exception as e:
            _LOGGER.error(f"eParkai login error: {e}")
            return

        for power_plant in power_plants:
            _LOGGER.info("Update requested [%s]", power_plant[CONF_NAME])
            try:
                await hass.async_add_executor_job(
                    client.fetch_generation_data,
                    power_plant[CONF_ID],
                    power_plant.get(CONF_OBJECT_ADDRESS) or None,
                    now
                )
            except Exception as e:
                _LOGGER.error(f"eParkai fetch generation data error [{power_plant[CONF_NAME]}]: {e}")
                continue

            _LOGGER.info("Importing statistics [%s] with id [%s]", power_plant[CONF_NAME], power_plant[CONF_ID])
            await async_insert_statistics(
                hass,
                power_plant,
                client.get_generation_data(power_plant[CONF_ID])
            )
            _LOGGER.info(f"Import completed for {power_plant[CONF_NAME]}")

    async def async_first_start(event: Event) -> None:
        await async_import_generation(datetime.now())

    async def async_handle_import_service(call: ServiceCall) -> None:
        _LOGGER.info("Manual import triggered via service call")
        await async_import_generation(datetime.now())

    # Register service for manual import
    hass.services.async_register(DOMAIN, SERVICE_IMPORT_GENERATION, async_handle_import_service)

    # Schedule automatic imports
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, async_first_start)
    cancel_interval = async_track_time_interval(
        hass, async_import_generation, timedelta(hours=6, minutes=random.randint(0, 59))
    )

    # Listen for options updates (power plant changes)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    hass.data[DOMAIN][entry.entry_id] = {
        "client": client,
        "cancel_interval": cancel_interval,
    }

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    _LOGGER.info("eParkai configuration updated, reloading")
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    data = hass.data[DOMAIN].pop(entry.entry_id, None)
    if data and "cancel_interval" in data:
        data["cancel_interval"]()

    # Remove service if no more entries
    if not hass.data[DOMAIN]:
        hass.services.async_remove(DOMAIN, SERVICE_IMPORT_GENERATION)

    return True


async def async_insert_statistics(
    hass: HomeAssistant, power_plant: dict, generation_data: dict
) -> None:
    id_suffix = power_plant.get(CONF_STATISTICS_ID_SUFFIX, "")
    raw_id = f"{DOMAIN}:energy_generation_{power_plant[CONF_ID]}_{id_suffix}".strip("_").lower()
    _LOGGER.debug(f"Statistic ID BEFORE cleanup for {power_plant[CONF_NAME]} is {raw_id}")
    statistic_id = re.sub(r"[^a-z0-9_:]", "_", raw_id)
    _LOGGER.debug(f"Statistic ID for {power_plant[CONF_NAME]} is {statistic_id}")

    if not generation_data:
        _LOGGER.error(f"Received empty generation data for {statistic_id}")
        return None

    _LOGGER.debug(f"Received generation data for {statistic_id}: {generation_data}")

    metadata = StatisticMetaData(
        has_sum=True,
        mean_type=StatisticMeanType.NONE,
        name=power_plant[CONF_NAME],
        source=DOMAIN,
        statistic_id=statistic_id,
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        unit_class="energy",
    )

    _LOGGER.debug(f"Preparing long-term statistics for {statistic_id}")
    statistics = await _async_get_statistics(hass, metadata, power_plant, generation_data)
    _LOGGER.debug(f"Generated statistics for {statistic_id}: {statistics}")
    async_add_external_statistics(hass, metadata, statistics)
    return None


async def _async_get_statistics(
    hass: HomeAssistant, metadata: StatisticMetaData, power_plant: dict, generation_data: dict
) -> list[StatisticData]:
    statistic_id = metadata["statistic_id"]
    statistics: list[StatisticData] = []
    generation_percentage = power_plant.get(CONF_GENERATION_PERCENTAGE, 100)
    sum_ = None
    tz = dt_util.get_time_zone("Europe/Vilnius")

    # IMPORTANT: sort by timestamp to keep the cumulative sum correct.
    for ts in sorted(generation_data):
        generated_kwh = generation_data[ts]
        dt_object = datetime.fromtimestamp(ts, tz=tz)

        if generation_percentage != 100:
            generated_percentage_kwh = generated_kwh * (generation_percentage / 100)
            _LOGGER.debug(
                "Applying generation percentage of %s%% for %s: %s kWh -> %s kWh",
                generation_percentage,
                statistic_id,
                generated_kwh,
                generated_percentage_kwh,
            )
            generated_kwh = generated_percentage_kwh

        if sum_ is None:
            sum_ = await get_yesterday_sum(hass, metadata, dt_object)

        sum_ += generated_kwh

        statistics.append(
            StatisticData(
                start=dt_object,
                state=generated_kwh,
                sum=sum_,
            )
        )
    return statistics


async def get_yesterday_sum(hass: HomeAssistant, metadata: StatisticMetaData, date: datetime) -> float:
    statistic_id = metadata["statistic_id"]
    start = date - timedelta(days=1)
    end = date - timedelta(minutes=1)
    _LOGGER.debug(f"Looking history sum for {statistic_id} for {date} between {start} and {end}")
    stat = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, end, {statistic_id}, "day", None, {"sum"}
    )
    if statistic_id not in stat:
        _LOGGER.debug(f"No history sum found")
        return 0.0
    sum_ = stat[statistic_id][0]["sum"]
    _LOGGER.debug(f"History sum for {statistic_id} = {sum_}")
    return sum_