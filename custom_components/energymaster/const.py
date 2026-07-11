"""Constants for the EnergyMaster integration."""

from enum import IntEnum, StrEnum

DOMAIN = "energymaster"

PANEL_TITLE = "EnergyMaster"
PANEL_ICON = "mdi:home-lightning-bolt"
STATIC_URL = f"/{DOMAIN}_static"

CONF_APPTOKEN = "apptoken"
CONF_P1DEVICE = "p1device"
CONF_P1METER = "p1meter"
CONF_MQTTSERVER = "mqttserver"
CONF_MQTTPORT = "mqttport"
CONF_MQTTUSER = "mqttuser"
CONF_MQTTPSW = "mqttpsw"
CONF_WIFISSID = "wifissid"
CONF_WIFIPSW = "wifipsw"


class Phase(IntEnum):
    all = 0
    l1 = 1
    l2 = 2
    l3 = 3

    def __str__(self) -> str:
        return self.name.upper()


class SchemaAction(StrEnum):
    add = "add"
    edit = "edit"
    delete = "delete"
