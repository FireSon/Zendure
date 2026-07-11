from custom_components.energymaster.solver.consumers.appliance import Appliance


class _Hass:
    # records the switch service calls on_power triggers; async_call is duck-typed (sync here)
    def __init__(self) -> None:
        self.turned_off: list[tuple[str, str]] = []

    def async_create_task(self, _result) -> None:
        return None

    @property
    def services(self) -> "_Hass":
        return self

    def async_call(self, domain: str, service: str, data: dict, blocking: bool = False) -> None:
        self.turned_off.append((service, data.get("entity_id")))


def _dw() -> tuple[Appliance, _Hass]:
    h = _Hass()
    d = Appliance()
    d.hass = h
    d.switch_entity = "switch.dw"
    return d, h


def test_witnessed_start_arms_and_cuts() -> None:
    d, h = _dw()
    d.on_power(0.0, now=1000.0)        # idle -> seen_below
    d.on_power(200.0, now=1001.0)      # rise within the window -> arm + cut
    assert d.armed is True
    assert h.turned_off == [("turn_off", "switch.dw")]


def test_cold_start_already_running_is_not_interrupted() -> None:
    d, h = _dw()
    d.on_power(200.0, now=1000.0)      # first observation already above -> start not witnessed
    assert d.armed is False
    assert h.turned_off == []


def test_not_interrupted_after_two_minutes() -> None:
    d, h = _dw()
    d.on_power(0.0, now=1000.0)
    d.on_power(200.0, now=1001.0)      # witnessed start -> arms
    d.armed = False
    h.turned_off.clear()
    d.on_power(200.0, now=1001.0 + 130)   # 130s after start -> past the 2-min window
    assert d.armed is False
    assert h.turned_off == []


def test_busy_is_not_interrupted() -> None:
    d, h = _dw()
    d.busy = True
    d.on_power(0.0, now=1000.0)
    d.on_power(200.0, now=1001.0)
    assert d.armed is False
    assert h.turned_off == []


def test_power_off_resets_and_restart_rearms() -> None:
    d, h = _dw()
    d.on_power(0.0, now=1000.0)
    d.on_power(200.0, now=1001.0)      # start -> arm
    d.armed = False
    h.turned_off.clear()
    d.on_power(0.0, now=1100.0)        # power off -> reset
    d.on_power(200.0, now=1101.0)      # restart witnessed -> arm again
    assert d.armed is True
    assert h.turned_off == [("turn_off", "switch.dw")]
