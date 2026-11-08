"""Every brand ZenMaster speaks to; the coordinator builds one of each, the config flow asks them about a find before the entry exists."""

from .anker.driver import AnkerDriver
from .deye.driver import DeyeDriver
from .marstek.driver import MarstekDriver
from .sessy.driver import SessyDriver
from .zendure.driver import ZendureDriver

DRIVERS = (ZendureDriver, MarstekDriver, AnkerDriver, SessyDriver, DeyeDriver)
