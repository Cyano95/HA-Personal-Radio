"""Config flow — einmalige Einrichtung über Einstellungen → Integrationen."""
from __future__ import annotations
from homeassistant import config_entries
from homeassistant.core import HomeAssistant

DOMAIN = "personal_radio"


class PersonalRadioConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Einrichtungsdialog für Personal Radio."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            # Bereits eingerichtet?
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="Personal Radio", data={})

        return self.async_show_form(
            step_id="user",
            description_placeholders={},
        )
