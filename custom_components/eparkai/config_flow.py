"""Config flow for eParkai.lt Solar Energy."""
import logging
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD, CONF_CLIENT_ID, CONF_ID, CONF_NAME
from homeassistant.core import callback

from .const import (
    DOMAIN,
    CONF_POWER_PLANTS,
    CONF_OBJECT_ADDRESS,
    CONF_GENERATION_PERCENTAGE,
    CONF_STATISTICS_ID_SUFFIX,
)
from .eparkai_client import EParkaiClient

_LOGGER = logging.getLogger(__name__)


class EParkaiConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for eParkai."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial credentials step."""
        errors = {}

        if user_input is not None:
            client = EParkaiClient(
                username=user_input[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
                client_id=user_input[CONF_CLIENT_ID],
            )
            try:
                await self.hass.async_add_executor_job(client.login)
                form_id = client.form_parser.get("form_id")
                if form_id != "product_generation_form":
                    errors["base"] = "invalid_auth"
                else:
                    await self.async_set_unique_id(user_input[CONF_CLIENT_ID])
                    self._abort_if_unique_id_configured()

                    return self.async_create_entry(
                        title=f"eParkai ({user_input[CONF_CLIENT_ID]})",
                        data={
                            CONF_USERNAME: user_input[CONF_USERNAME],
                            CONF_PASSWORD: user_input[CONF_PASSWORD],
                            CONF_CLIENT_ID: user_input[CONF_CLIENT_ID],
                        },
                        options={
                            CONF_POWER_PLANTS: [],
                        },
                    )
            except Exception:
                _LOGGER.exception("Error connecting to eParkai.lt")
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Required(CONF_CLIENT_ID): str,
                }
            ),
            errors=errors,
        )

    async def async_step_import(self, import_data):
        """Handle import from YAML."""
        await self.async_set_unique_id(import_data[CONF_CLIENT_ID])
        self._abort_if_unique_id_configured()

        power_plants = []
        for plant in import_data.get(CONF_POWER_PLANTS, []):
            power_plants.append({
                CONF_NAME: plant[CONF_NAME],
                CONF_ID: str(plant[CONF_ID]),
                CONF_OBJECT_ADDRESS: plant.get(CONF_OBJECT_ADDRESS) or "",
                CONF_STATISTICS_ID_SUFFIX: plant.get(CONF_STATISTICS_ID_SUFFIX, ""),
                CONF_GENERATION_PERCENTAGE: plant.get(CONF_GENERATION_PERCENTAGE, 100),
            })

        return self.async_create_entry(
            title=f"eParkai ({import_data[CONF_CLIENT_ID]})",
            data={
                CONF_USERNAME: import_data[CONF_USERNAME],
                CONF_PASSWORD: import_data[CONF_PASSWORD],
                CONF_CLIENT_ID: import_data[CONF_CLIENT_ID],
            },
            options={
                CONF_POWER_PLANTS: power_plants,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return EParkaiOptionsFlowHandler(config_entry)


class EParkaiOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for eParkai - manage power plants."""

    def __init__(self, config_entry):
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Show menu: list plants, add plant."""
        plants = self._config_entry.options.get(CONF_POWER_PLANTS, [])

        menu_options = ["add_plant"]
        if plants:
            menu_options.insert(0, "list_plants")

        return self.async_show_menu(
            step_id="init",
            menu_options=menu_options,
        )

    async def async_step_list_plants(self, user_input=None):
        """List existing power plants and allow removal."""
        plants = self._config_entry.options.get(CONF_POWER_PLANTS, [])

        if user_input is not None:
            selected_index = user_input.get("plant_index")
            if selected_index is not None:
                self._remove_index = selected_index
                return await self.async_step_remove_plant()

        plant_choices = {
            str(i): f"{p[CONF_NAME]} (ID: {p[CONF_ID]})"
            for i, p in enumerate(plants)
        }

        if not plant_choices:
            return await self.async_step_add_plant()

        return self.async_show_form(
            step_id="list_plants",
            data_schema=vol.Schema(
                {
                    vol.Required("plant_index"): vol.In(plant_choices),
                }
            ),
            description_placeholders={
                "plant_count": str(len(plants)),
            },
        )

    async def async_step_remove_plant(self, user_input=None):
        """Confirm and remove a power plant."""
        plants = list(self._config_entry.options.get(CONF_POWER_PLANTS, []))
        index = int(self._remove_index)
        plant = plants[index]

        if user_input is not None:
            if user_input.get("confirm"):
                plants.pop(index)
                return self.async_create_entry(
                    data={CONF_POWER_PLANTS: plants},
                )
            return await self.async_step_init()

        return self.async_show_form(
            step_id="remove_plant",
            data_schema=vol.Schema(
                {
                    vol.Required("confirm", default=False): bool,
                }
            ),
            description_placeholders={
                "name": plant[CONF_NAME],
                "id": plant[CONF_ID],
            },
        )

    async def async_step_add_plant(self, user_input=None):
        """Add a new power plant."""
        if user_input is not None:
            plants = list(self._config_entry.options.get(CONF_POWER_PLANTS, []))
            plants.append({
                CONF_NAME: user_input[CONF_NAME],
                CONF_ID: str(user_input[CONF_ID]),
                CONF_OBJECT_ADDRESS: user_input.get(CONF_OBJECT_ADDRESS, ""),
                CONF_STATISTICS_ID_SUFFIX: user_input.get(CONF_STATISTICS_ID_SUFFIX, ""),
                CONF_GENERATION_PERCENTAGE: user_input.get(CONF_GENERATION_PERCENTAGE, 100),
            })
            return self.async_create_entry(
                data={CONF_POWER_PLANTS: plants},
            )

        return self.async_show_form(
            step_id="add_plant",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME): str,
                    vol.Required(CONF_ID): str,
                    vol.Optional(CONF_OBJECT_ADDRESS, default=""): str,
                    vol.Optional(CONF_STATISTICS_ID_SUFFIX, default=""): str,
                    vol.Optional(CONF_GENERATION_PERCENTAGE, default=100): vol.All(
                        int, vol.Range(min=1, max=100)
                    ),
                }
            ),
        )