"""Support for klyqa vacuum cleaners."""
from __future__ import annotations

from collections.abc import Callable, Coroutine
from functools import partial
import json
from homeassistant.helpers.entity_registry import EntityRegistry, RegistryEntry
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr

from homeassistant.components.vacuum import (
    STATE_CLEANING,
    STATE_DOCKED,
    STATE_ERROR,
    STATE_IDLE,
    STATE_PAUSED,
    STATE_RETURNING,
    StateVacuumEntity,
    VacuumEntityFeature,
    ENTITY_ID_FORMAT,
)
from enum import Enum

from typing import Any

# from homeassistant.util import dt as slugify
from homeassistant.util import slugify

from homeassistant.core import HomeAssistant, Event

from homeassistant.const import Platform
from homeassistant.helpers.entity_component import EntityComponent

import traceback
from collections.abc import Callable
import asyncio

from homeassistant.helpers.area_registry import SAVE_DELAY

from homeassistant.const import (
    EVENT_HOMEASSISTANT_STOP,
    Platform,
)

import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity import DeviceInfo, Entity, generate_entity_id
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
import homeassistant.util.color as color_util
from homeassistant.config_entries import ConfigEntry


from klyqa_ctl import klyqa_ctl as api
from . import datacoordinator as coord
from .datacoordinator import HAKlyqaAccount

from .const import (
    CONF_POLLING,
    DOMAIN,
    LOGGER,
    CONF_SYNC_ROOMS,
    EVENT_KLYQA_NEW_VC,
)

from datetime import timedelta
import functools as ft

from homeassistant.helpers.area_registry import AreaEntry, AreaRegistry
import homeassistant.helpers.area_registry as area_registry

TIMEOUT_SEND = 11
# PARALLEL_UPDATES = 0
SCAN_INTERVAL = timedelta(seconds=205)

SUPPORT_KLYQA = (
    VacuumEntityFeature.BATTERY
    | VacuumEntityFeature.FAN_SPEED
    | VacuumEntityFeature.PAUSE
    | VacuumEntityFeature.RETURN_HOME
    | VacuumEntityFeature.START
    | VacuumEntityFeature.STATE
    | VacuumEntityFeature.STATUS
    | VacuumEntityFeature.STOP
    | VacuumEntityFeature.LOCATE
    | VacuumEntityFeature.TURN_ON
    | VacuumEntityFeature.TURN_OFF
    # | VacuumEntityFeature.SEND_COMMAND
    # | VacuumEntityFeature.CLEAN_SPOT
    # | VacuumEntityFeature.MAP
)


async def async_setup(hass: HomeAssistant, yaml_config: ConfigType) -> bool:
    """Expose vacuum control via state machine and services."""
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Async_setup_entry."""
    klyqa: HAKlyqaAccount | None = None

    klyqa = hass.data[DOMAIN].entries[entry.entry_id]
    if klyqa:
        await async_setup_klyqa(
            hass, ConfigType(entry.data), async_add_entities, entry=entry, klyqa=klyqa
        )


async def async_setup_klyqa(
    hass: HomeAssistant,
    config: ConfigType,
    add_entities: AddEntitiesCallback,
    klyqa: HAKlyqaAccount,
    discovery_info: DiscoveryInfoType | None = None,
    entry: ConfigEntry | None = None,
) -> None:
    """Set up the Klyqa Vacuum."""

    async def on_hass_stop(event: Event) -> None:
        """Stop push updates when hass stops."""
        await klyqa.search_and_send_loop_task_stop()
        await hass.async_add_executor_job(klyqa.shutdown)

    listener = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, on_hass_stop)

    if entry:
        entry.async_on_unload(listener)

    entity_registry = er.async_get(hass)

    async def add_new_entity(event: Event) -> None:

        device_settings = event.data

        u_id = api.format_uid(device_settings["localDeviceId"])

        entity_id = ENTITY_ID_FORMAT.format(u_id)

        device_state = klyqa.devices[u_id] if u_id in klyqa.devices else api.KlyqaVC()

        new_entity = entity_registry.async_get(entity_id)

        registered_entity_id = entity_registry.async_get_entity_id(
            Platform.VACUUM, DOMAIN, u_id
        )

        if registered_entity_id and registered_entity_id != entity_id:
            entity_registry.async_remove(str(registered_entity_id))

        registered_entity_id = entity_registry.async_get_entity_id(
            Platform.VACUUM, DOMAIN, u_id
        )

        LOGGER.info(f"Add entity {entity_id} ({device_settings.get('name')}).")
        new_entity = KlyqaVC(
            device_settings,
            device_state,
            klyqa,
            entity_id,
            should_poll=klyqa.polling,
            config_entry=entry,
            hass=hass,
        )
        await new_entity.async_update_settings()
        new_entity._update_state(device_state.status)
        if new_entity:
            add_entities([new_entity], True)

    hass.data[DOMAIN].remove_listeners.append(
        hass.bus.async_listen(EVENT_KLYQA_NEW_VC, add_new_entity)
    )

    await klyqa.update_account()
    return


class KlyqaVC(StateVacuumEntity):
    """Representation of the Klyqa vacuum cleaner."""

    _attr_supported_features = SUPPORT_KLYQA

    _klyqa_api: HAKlyqaAccount
    _klyqa_device: api.KlyqaVC
    settings: dict[Any, Any] = {}
    """synchronise rooms to HA"""

    config_entry: ConfigEntry | None = None
    entity_registry: EntityRegistry | None = None
    """entity added finished"""
    _added_klyqa: bool = False
    u_id: str
    send_event_cb: asyncio.Event | None = None
    hass: HomeAssistant

    def __init__(
        self,
        settings: Any,
        device: api.KlyqaVC,
        klyqa_api: Any,
        entity_id: Any,
        hass: HomeAssistant,
        should_poll: Any = True,
        config_entry: Any = None,
    ) -> None:
        """Initialize a Klyqa vacuum cleaner."""
        self.hass = hass
        # self.entity_registry = er.async_get(self.hass)

        self._klyqa_api = klyqa_api

        self.u_id = api.format_uid(settings["localDeviceId"])
        self._attr_unique_id: str = api.format_uid(self.u_id)
        self._klyqa_device = device
        self.entity_id = entity_id

        self._attr_should_poll = should_poll

        self.config_entry = config_entry
        self.send_event_cb: asyncio.Event = asyncio.Event()

        self.device_config: api.Device_config = {}
        self.settings = {}

        self._attr_fan_speed_list = [member.name for member in api.VC_SUCTION_STRENGTHS]
        self._state = None
        self._attr_battery_level = 0

    async def async_stop(self, **kwargs: Any) -> None:
        """Stop the vacuum cleaner, do not return to base."""
        args = ["set", "--cleaning", "off"]

        await self.send_to_devices(args)

    async def async_start(self) -> None:
        """Start or resume the cleaning task.

        This method must be run in the event loop.
        """
        args = ["set", "--cleaning", "on"]

        await self.send_to_devices(args)

    # clean_spot or async_clean_spot
    # Perform a spot clean-up.

    # set_fan_speed or async_set_fan_speed
    # Set the fan speed.
    # async def async_set_fan_speed(self, fan_speed: str, **kwargs: Any) -> None:
    #     """Set fan speed.

    #     This method must be run in the event loop.
    #     """
    #     await self.hass.async_add_executor_job(
    #         partial(self.set_fan_speed, fan_speed, **kwargs)
    #     )

    async def async_send_command(
        self,
        command: str,
        params: dict[str, Any] | list[Any] | None = None,
        **kwargs: Any,
    ) -> None:
        pass

    async def async_update_settings(self) -> None:
        """Set device specific settings from the klyqa settings cloud."""
        devices_settings = self._klyqa_api.acc_settings["devices"]

        device_result = [
            x
            for x in devices_settings
            if api.format_uid(str(x["localDeviceId"])) == self.u_id
        ]
        if len(device_result) < 1:
            return

        self.settings = device_result[0]

        self._attr_name = self.settings["name"]
        self._attr_unique_id = api.format_uid(self.settings["localDeviceId"])
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._attr_unique_id)},
            name=self.name,
            manufacturer="QConnex GmbH",
            model=self.settings["productId"],
            sw_version=self.settings["firmwareVersion"],
            hw_version=self.settings["hardwareRevision"],
        )

        if (
            self.device_config
            and "productId" in self.device_config
            and self.device_config["productId"] in api.PRODUCT_URLS
        ):
            self._attr_device_info["configuration_url"] = api.PRODUCT_URLS[
                self.device_config["productId"]
            ]

        entity_registry = er.async_get(self.hass)
        entity_id: str | None = entity_registry.async_get_entity_id(
            Platform.VACUUM, DOMAIN, str(self.unique_id)
        )
        entity_registry_entry: RegistryEntry | None = None
        if entity_id:
            entity_registry_entry = entity_registry.async_get(str(entity_id))

        device_registry = dr.async_get(self.hass)

        device = device_registry.async_get_device(
            identifiers={(DOMAIN, self._attr_unique_id)}
        )

        if self.config_entry:

            device_registry.async_get_or_create(
                **{
                    "config_entry_id": self.config_entry.entry_id,
                    **self._attr_device_info,
                }
            )

        if entity_registry_entry:
            self._attr_device_info["suggested_area"] = entity_registry_entry.area_id

    @property
    def entity_registry_enabled_default(self) -> bool:
        """Return if the entity should be enabled when first added to the entity registry."""
        return True

    async def async_turn_on(self, **kwargs):
        """Turn the vacuum on and start cleaning."""
        args = ["--power", "on"]

        await self.send_to_devices(args)

    async def async_turn_off(self, **kwargs):
        """Turn the vacuum off stopping the cleaning and returning home."""

        args = ["set", "--workingmode", "CHARGE_GO"]

        if self._attr_transition_time:
            args.extend(["--transitionTime", str(self._attr_transition_time)])

        LOGGER.info(
            f"Send to device {self.entity_id}%s: %s",
            f" ({self.name})" if self.name else "",
            " ".join(args),
        )
        await self.send_to_devices(args)

    async def async_update_klyqa(self) -> None:
        """Fetch settings from klyqa cloud account."""

        await self._klyqa_api.request_account_settings_eco()
        if self._added_klyqa:
            await self._klyqa_api.process_account_settings()
        await self.async_update_settings()

    async def async_update(self) -> None:
        """Fetch new state data for this device. Called by HA."""

        name = f" ({self.name})" if self.name else ""
        LOGGER.info("Update device %s%s", self.entity_id, name)

        try:
            await self.async_update_klyqa()

        except (Exception,) as exception:  # pylint: disable=bare-except,broad-except
            LOGGER.error(str(exception))
            LOGGER.error("%s", traceback.format_exc())
            LOGGER.exception(exception)

        # if self._added_klyqa:
        await self.send_to_devices(["--request"])

        self._update_state(self._klyqa_api.devices[self.u_id].status)

    # async def async_clean_spot(self, **kwargs: Any) -> None:
    #     """Perform a spot clean-up.

    #     This method must be run in the event loop.
    #     """
    #     await self.send_to_devices(["get", "--workstatus", "CLEANING_SPOT"])

    async def async_locate(self, **kwargs: Any) -> None:
        """Locate the vacuum cleaner."""
        # await self._try_command("Unable to locate the botvac: %s", self._device.find)

        await self.send_to_devices(["set", "--beeping", "on"])

    async def async_set_fan_speed(self, fan_speed: str, **kwargs: Any) -> None:
        """Set fan speed.

        This method must be run in the event loop.
        """

        await self.send_to_devices(["set", "--suction", fan_speed])

    async def async_pause(self) -> None:
        """Pause the cleaning task.

        This method must be run in the event loop.
        """
        await self.send_to_devices(["set", "--workingmode", "STANDBY"])

    async def async_return_to_base(self, **kwargs: Any) -> None:
        """Set the vacuum cleaner to return to the dock.

        This method must be run in the event loop.
        """
        await self.send_to_devices(["set", "--workingmode", "CHARGE_GO"])

    async def send_to_devices(
        self,
        args: list[Any],
        callback: Callable[[Any, str], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        """Send_to_devices."""

        send_event_cb: asyncio.Event = asyncio.Event()

        async def send_answer_cb(msg: api.Message, uid: str) -> None:
            nonlocal callback, send_event_cb
            if callback is not None:
                await callback(msg, uid)
            try:
                LOGGER.debug("Send_answer_cb %s", str(uid))
                # ttl ended
                if uid != self.u_id:
                    return
                self._update_state(self._klyqa_api.devices[self.u_id].status)
                if self._added_klyqa:
                    self.schedule_update_ha_state()  # force_refresh=True)
                # self.async_schedule_update_ha_state(force_refresh=True)
            except:  # noqa: E722 pylint: disable=bare-except
                LOGGER.error(traceback.format_exc())
            finally:
                send_event_cb.set()

        parser = api.get_description_parser()
        # args.extend(["--local", "--device_unitids", f"{self.u_id}"])
        args = ["--local", "--device_unitids", f"{self.u_id}"] + args

        api.add_config_args(parser=parser)
        api.add_command_args(parser=parser)

        args_parsed = parser.parse_args(args=args)

        LOGGER.info("Send start!")
        new_task = asyncio.create_task(
            self._klyqa_api._send_to_devices(
                args_parsed,
                args,
                async_answer_callback=send_answer_cb,
                timeout_ms=TIMEOUT_SEND * 1000,
            )
        )
        LOGGER.info("Send started!")
        await send_event_cb.wait()

        LOGGER.info("Send started wait ended!")
        try:
            await asyncio.wait([new_task], timeout=0.001)
        except asyncio.TimeoutError:
            LOGGER.error("Timeout send")
        pass

    async def async_added_to_hass(self) -> None:
        """Added to hass."""
        await super().async_added_to_hass()
        self._added_klyqa = True
        try:
            await self.async_update_settings()
        except Exception:  # pylint: disable=bare-except,broad-except
            LOGGER.error(traceback.format_exc())

    def _update_state(self, state_complete: api.KlyqaVCResponseStatus) -> None:
        """Process state request response from the device to the entity state."""
        # self._attr_state = STATE_OK if state_complete else STATE_UNAVAILABLE
        self._attr_assumed_state = True
        # if not self._attr_state:
        #     LOGGER.info(
        #         "device " + str(self.entity_id) + "%s unavailable.",
        #         " (" + self.name + ")" if self.name else "",
        #     )

        if not state_complete or not isinstance(
            state_complete, api.KlyqaVCResponseStatus
        ):
            return

        LOGGER.debug(
            "Update vc state %s%s",
            str(self.entity_id),
            " (" + self.name + ")" if self.name else "",
        )

        if state_complete.type == "error":
            LOGGER.error(state_complete.type)
            return

        state_type = state_complete.type
        if not state_type or state_type != "status":
            return

        self._klyqa_device.status = state_complete

        self._attr_battery_level = (
            int(state_complete.battery) if state_complete.battery else 0
        )

        # VC_WORKSTATUS = [ "SLEEP","STANDBY","CLEANING","CLEANING_AUTO","CLEANING_RANDOM","CLEANING_SROOM","CLEANING_EDGE","CLEANING_SPOT","CLEANING_COMP",
        # "DOCKING","CHARGING","CHARGING_DC","CHARGING_COMP","ERROR" ]
        status = {
            api.VC_WORKSTATUS.SLEEP: None,
            api.VC_WORKSTATUS.STANDBY: None,
            api.VC_WORKSTATUS.CLEANING: STATE_CLEANING,
            api.VC_WORKSTATUS.CLEANING_AUTO: STATE_CLEANING,
            api.VC_WORKSTATUS.CLEANING_RANDOM: STATE_CLEANING,
            api.VC_WORKSTATUS.CLEANING_SROOM: STATE_CLEANING,
            api.VC_WORKSTATUS.CLEANING_EDGE: STATE_CLEANING,
            api.VC_WORKSTATUS.CLEANING_SPOT: STATE_CLEANING,
            api.VC_WORKSTATUS.CLEANING_COMP: STATE_CLEANING,
            api.VC_WORKSTATUS.DOCKING: STATE_RETURNING,
            api.VC_WORKSTATUS.CHARGING: STATE_DOCKED,
            api.VC_WORKSTATUS.CHARGING_DC: STATE_DOCKED,
            api.VC_WORKSTATUS.CHARGING_COMP: STATE_DOCKED,
            api.VC_WORKSTATUS.ERROR: STATE_ERROR,
        }
        self._state = (
            status[state_complete.workstatus]
            if state_complete.workstatus in status
            else None
        )
        self._attr_fan_speed = state_complete.suction

        self._attr_is_on = state_complete.power == "on"

        self._attr_assumed_state = False
        self.state_complete = state_complete

    @property
    def state(self) -> str | None:
        """Return the state of the vacuum cleaner."""
        return self._state
