import asyncio
from types import SimpleNamespace

from custom_components.zenmaster.solver.consumers.appliance import Appliance, _QUIET_S


class _Done:
    # the service call is recorded synchronously; awaiting it is a no-op
    def __await__(self):
        return iter(())


class _Hass:
    # records the switch service calls on_power triggers; async_call is duck-typed (sync here)
    def __init__(self, switch: str = "off") -> None:
        self.turned_off: list[tuple[str, str]] = []
        self.states = SimpleNamespace(get=lambda eid: SimpleNamespace(state=switch))

    def async_create_task(self, _result) -> None:
        return None

    @property
    def services(self) -> "_Hass":
        return self

    def async_call(self, domain: str, service: str, data: dict, blocking: bool = False) -> "_Done":
        self.turned_off.append((service, data.get("entity_id")))
        return _Done()  # awaitable for entry_prerun, ignorable for the create_task path in on_power


def _dw() -> tuple[Appliance, _Hass]:
    h = _Hass()
    d = Appliance()
    d.hass = h
    d.switch_entity = "switch.dw"
    return d, h


def test_witnessed_start_arms_and_cuts() -> None:
    d, h = _dw()
    d.on_power(0.0, now=1000.0)                    # idle
    d.on_power(200.0, now=1000.0 + _QUIET_S)       # rise after a real rest -> arm + cut
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
    d.on_power(200.0, now=1000.0 + _QUIET_S)       # start -> arm
    d.armed = False
    h.turned_off.clear()
    d.on_power(0.0, now=9000.0)                    # power off
    d.on_power(200.0, now=9000.0 + _QUIET_S)       # rested again -> arm again
    assert d.armed is True
    assert h.turned_off == [("turn_off", "switch.dw")]


def test_a_pause_inside_a_cycle_never_cuts() -> None:
    # measured on the washing machine 24-08: 20 minutes at 3 W halfway, then the heating element again
    d, h = _dw()
    t = 1000.0
    d.on_power(0.0, now=t)
    d.on_power(1900.0, now=t + _QUIET_S)           # the real start, arms and cuts
    d.armed, d.busy = False, True
    h.turned_off.clear()
    t += _QUIET_S
    d.on_power(3.0, now=t + 60)                    # the machine goes quiet mid-programme
    d.busy = False                                 # the done-rule already ended the cycle for the books
    d.on_power(11.0, now=t + 60 + 20 * 60)         # 20 minutes later it draws again
    assert d.armed is False, "een pauze in het programma mag nooit als start tellen"
    assert h.turned_off == []


def test_hands_off_never_touches_the_switch() -> None:
    d, h = _dw()
    d.manage = False
    d.on_power(0.0, now=1000.0)
    d.on_power(200.0, now=1000.0 + _QUIET_S)
    assert d.armed is False
    assert h.turned_off == []


def test_the_button_hands_the_appliance_back() -> None:
    d, h = _dw()
    d.armed = True
    asyncio.run(d.command(h, "arm_off"))
    assert d.manage is False and d.armed is False
    d.on_power(0.0, now=1000.0)
    d.on_power(200.0, now=1000.0 + _QUIET_S)
    assert h.turned_off == []  # arming is off, so the switch is left alone
    asyncio.run(d.command(h, "arm_on"))
    assert d.manage is True
    d.on_power(0.0, now=9000.0)
    d.on_power(200.0, now=9000.0 + _QUIET_S)
    assert d.armed is True and h.turned_off == [("turn_off", "switch.dw")]


def test_the_rest_is_learned_from_history(monkeypatch) -> None:
    # four cycles, one with a 30-minute silence halfway: the rest becomes twice that silence
    import homeassistant.components.recorder as rec
    import homeassistant.components.recorder.statistics as recstats
    from custom_components.zenmaster.solver.consumers import appliance as mod

    quarters = []
    for cycle in range(4):
        quarters += [1500.0, 1500.0]
        if cycle == 1:
            quarters += [0.0, 0.0, 1500.0, 1500.0]  # the pause inside this cycle: 2 x 15 min
        quarters += [0.0] * 8                       # two hours between cycles
    rows = [{"mean": q} for q in quarters for _ in range(3)]   # 15-min values back to 5-min rows

    class _Rec:
        async def async_add_executor_job(self, fn, *args):
            return {"sensor.dw_power": rows}

    monkeypatch.setattr(rec, "get_instance", lambda hass: _Rec())
    monkeypatch.setattr(recstats, "statistics_during_period", lambda *a, **k: None)

    saved = {}
    d, h = _dw()
    d.power_entity = "sensor.dw_power"

    async def save_params(hass, params):
        saved.update(params)

    d.save_params = save_params
    h.states = type("S", (), {"get": staticmethod(lambda eid: None)})()
    lp = type("Lp", (), {"hass": h})()
    asyncio.run(d.calibrate(lp, None))

    assert d._quiet_s == 2 * 30 * 60, "twee keer de langste stilte binnen een cyclus"
    assert saved["quiet_s"] == d._quiet_s, "de geleerde rust moet mee de store in"
    assert d._quiet_s <= mod._QUIET_MAX_S and d._quiet_s >= mod._QUIET_S


def test_the_learned_rest_comes_back_after_a_restart() -> None:
    d, _ = _dw()

    async def load_params(hass):
        return {"profile": [1.0, 1.0], "quiet_s": 3600.0}

    d.load_params = load_params
    asyncio.run(d.restore(None))
    assert d._quiet_s == 3600.0 and d.cycle_duration_slots == 2


def test_the_status_is_published_to_its_own_sensor() -> None:
    # every status change writes straight to the consumer's state sensor, so the panel follows live
    from custom_components.zenmaster.solver.solver import EntryStatus

    d, _ = _dw()
    seen: list[tuple[str, str]] = []
    d.entities = {}  # as EntityDevice.__init__ leaves it
    d.entity_update = lambda key, value: seen.append((key, value))
    d.status = EntryStatus.running
    d.status = EntryStatus.none
    assert seen == [("state", "running"), ("state", "none")]


def test_hands_off_shows_as_its_own_status() -> None:
    from custom_components.zenmaster.solver.solver import EntryStatus

    d, h = _dw()
    asyncio.run(d.command(h, "arm_off"))
    assert d.status is EntryStatus.unmanaged
    asyncio.run(d.command(h, "arm_on"))
    assert d.status is EntryStatus.none


def test_the_row_buttons_come_from_the_appliance() -> None:
    # the override is the manage switch on the device, so the row keeps only the moment-to-moment actions
    ids = [a.id for a in Appliance.ACTIONS]
    assert ids == ["start", "stop", "skip"]
    when = {a.id: a.when for a in Appliance.ACTIONS}
    assert "running" in when["stop"] and "running" not in when["start"]


def _latest(armed: str, slots: int = 6) -> int:
    # run one prerun at the moment of arming and report the deadline place_block was given, in quarters
    from datetime import datetime
    from types import SimpleNamespace

    import numpy as np

    seen = {}
    d, _ = _dw()
    d.data = SimpleNamespace(data=np.zeros(204))  # entry_config normally makes this
    d._profile = [0.5] * slots
    d.armed, d._last_cal = True, datetime.now()
    now = datetime.fromisoformat(f"2026-08-30T{armed}+02:00")
    d.armed_at = now.timestamp()
    lp = SimpleNamespace(
        hass=None,
        price_buy=SimpleNamespace(data=[0.2] * 250),
        globals={},
        place_block=lambda phase, profile, latest: seen.setdefault("latest", latest),
    )
    asyncio.run(d.entry_prerun(lp, now))
    return seen["latest"]


def test_armed_in_the_evening_is_due_at_eight() -> None:
    assert _latest("19:00") == 52   # 13 hours to 08:00


def test_armed_at_night_is_due_the_same_morning() -> None:
    assert _latest("02:00") == 24   # 6 hours to 08:00


def test_armed_in_the_morning_gets_four_hours() -> None:
    assert _latest("10:00") == 16   # four hours, ends 14:00
    assert _latest("13:14") == 15   # four hours would end 17:14, so the 17:00 cap trims it


def test_the_afternoon_deadline_never_passes_five() -> None:
    assert _latest("15:00") == 8   # four hours would be 19:00, but 17:00 wins


def test_from_five_the_evening_rule_takes_over() -> None:
    assert _latest("17:00") == 60  # 15 hours to 08:00, so it no longer has to squeeze in before dinner


def test_a_deadline_that_cannot_be_met_starts_now() -> None:
    assert _latest("16:45", slots=6) == 6  # one quarter left to 17:00, so the cycle is placed straight away


def test_the_arming_survives_a_restart() -> None:
    # the switch is off while armed: lose the flag on a restart and the appliance waits forever
    from custom_components.zenmaster.config import Config

    d, _ = _dw()
    d.id, d.armed, d.armed_at = "vaatwasser", True, 1788100000.0
    payload = {}
    Config().serialize(d, "vaatwasser", payload)
    assert payload["vaatwasser"]["armed"] is True and payload["vaatwasser"]["armed_at"] == 1788100000.0

    back, _ = _dw()
    back.id = "vaatwasser"
    Config().deserialize_poly([back], {"id": "vaatwasser", **payload["vaatwasser"]})
    assert back.armed is True and back.armed_at == 1788100000.0

    fields = {f.id: f for f in Appliance.CONFIG}
    assert fields["armed"].hidden and fields["armed_at"].hidden, "bewaard, maar niet in het formulier"


def test_a_lost_switch_command_is_sent_again() -> None:
    # the switch was unavailable at the planned moment: every next round asks again until power is seen
    from datetime import datetime
    from types import SimpleNamespace

    import numpy as np

    d, h = _dw()
    d.data = SimpleNamespace(data=np.zeros(204))
    d._profile = [0.5] * 4          # place_block puts this at slot 12, so the plan wants it on now
    now = datetime.now().astimezone()
    d.armed, d._last_cal = True, now
    lp = SimpleNamespace(hass=h, price_buy=SimpleNamespace(data=[0.2] * 250), globals={}, place_block=lambda *a: 0)  # the plan puts the block at 'now'

    asyncio.run(d.entry_prerun(lp, now))
    asyncio.run(d.entry_prerun(lp, now))
    assert h.turned_off == [("turn_on", "switch.dw"), ("turn_on", "switch.dw")], "een verloren commando moet opnieuw"
    assert d.busy is False, "bezig pas als er stroom loopt"

    d.on_power(400.0, now=1000.0)   # the appliance really starts
    assert d.busy is True and d.status.name == "running"
    h.turned_off.clear()
    asyncio.run(d.entry_prerun(lp, now))
    assert h.turned_off == [], "eenmaal draaiend hoeft er niets meer gestuurd te worden"


def test_the_switch_being_on_voids_the_arming() -> None:
    # armed means we cut the power; a stale flag from the store must never switch an appliance on again
    from datetime import datetime

    import numpy as np

    d, h = _dw()
    h.states = SimpleNamespace(get=lambda eid: SimpleNamespace(state="on"))
    d.data = SimpleNamespace(data=np.zeros(204))
    now = datetime.now().astimezone()
    d.armed, d.armed_at, d._last_cal, d._profile = True, now.timestamp(), now, [0.5] * 4
    lp = SimpleNamespace(hass=h, price_buy=SimpleNamespace(data=[0.2] * 250), globals={}, place_block=lambda *a: 0)

    asyncio.run(d.entry_prerun(lp, now))
    assert d.armed is False and d.status.name == "none"
    assert h.turned_off == [], "met de schakelaar aan hoort er niets geschakeld te worden"


def test_the_status_waits_for_real_power() -> None:
    from datetime import datetime

    import numpy as np

    d, h = _dw()
    d.data = SimpleNamespace(data=np.zeros(204))
    now = datetime.now().astimezone()
    d.armed, d.armed_at, d._last_cal, d._profile = True, now.timestamp(), now, [0.5] * 4
    lp = SimpleNamespace(hass=h, price_buy=SimpleNamespace(data=[0.2] * 250), globals={}, place_block=lambda *a: 0)

    asyncio.run(d.entry_prerun(lp, now))
    assert d.status.name == "planned", "aangezet maar nog geen stroom: gepland, niet draaiend"
    d.on_power(400.0, now=1000.0)
    assert d.status.name == "running" and d.busy is True


def test_an_unavailable_switch_is_left_alone() -> None:
    # right after a restart the Zendure switch is unavailable: that says nothing, so neither switch nor void
    from datetime import datetime

    import numpy as np

    d, h = _dw()
    h.states = SimpleNamespace(get=lambda eid: SimpleNamespace(state="unavailable"))
    d.data = SimpleNamespace(data=np.zeros(204))
    now = datetime.now().astimezone()
    d.armed, d.armed_at, d._last_cal, d._profile = True, now.timestamp(), now, [0.5] * 4
    lp = SimpleNamespace(hass=h, price_buy=SimpleNamespace(data=[0.2] * 250), globals={}, place_block=lambda *a: 0)

    asyncio.run(d.entry_prerun(lp, now))
    assert d.armed is True, "onbekende stand mag de arming niet wissen"
    assert h.turned_off == [], "en er wordt niets geschakeld zolang we de stand niet zien"

    h.states = SimpleNamespace(get=lambda eid: None)  # the entity does not exist yet right after a restart
    asyncio.run(d.entry_prerun(lp, now))
    assert h.turned_off == [], "een ontbrekende entiteit is geen bewijs dat de schakelaar uit staat"


def test_an_appliance_found_idle_can_be_armed_right_away() -> None:
    # after a restart the rest counter starts at zero; an appliance seen idle has clearly been resting
    d, h = _dw()
    d.on_power(0.0, now=1000.0)      # first reading after the restart: idle
    d.on_power(200.0, now=1060.0)    # one minute later it starts
    assert d.armed is True and h.turned_off == [("turn_off", "switch.dw")]


def test_power_while_unmanaged_keeps_its_own_status() -> None:
    # hands off: the appliance is out of the plan, so power drawn or not changes nothing, it stays "not managed"
    d, h = _dw()
    d.manage = False
    d.on_power(0.0, now=1000.0)
    d.on_power(400.0, now=1060.0)
    assert d.status.name == "unmanaged" and h.turned_off == [], "niet beheerd: geen armen, geen andere status"
    d.on_power(0.0, now=1200.0)
    assert d.status.name == "unmanaged"
