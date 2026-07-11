from custom_components.energymaster.config import Config
from custom_components.energymaster.const import Phase
from custom_components.energymaster.solver.consumers.appliance import Appliance


def _payload():
    return {
        "consumers": [
            {"type": "Appliance", "kind": "dishwasher", "id": "dw1", "switch_entity": "switch.dw", "power_entity": "sensor.dw_w", "armed": True, "power_rating": 2, "cycle_duration_slots": "6", "phase": 2},
            {"type": "Appliance", "kind": "dryer", "id": "dr1", "switch_entity": "switch.dryer", "armed": False, "power_rating": 3.5, "cycle_duration_slots": 8, "phase": 1},
            {"type": "Appliance", "kind": "washingmachine", "id": "wm1", "switch_entity": "switch.wm", "phase": 3},
        ]
    }


def _load(payload, into=None):
    cfg = Config("unused.json")
    cfg.payload = payload
    consumers = [] if into is None else into
    cfg.readlist(consumers, payload["consumers"])
    return consumers


def test_readlist_creates_typed_consumers():
    consumers = _load(_payload())
    assert [type(c) for c in consumers] == [Appliance, Appliance, Appliance]
    assert [c.id for c in consumers] == ["dw1", "dr1", "wm1"]
    assert [c.kind for c in consumers] == ["dishwasher", "dryer", "washingmachine"]


def test_readlist_casts_fields():
    dw, dr, wm = _load(_payload())
    assert dw.armed is True and dr.armed is False
    assert dw.power_rating == 2.0 and isinstance(dw.power_rating, float)
    assert dr.power_rating == 3.5 and isinstance(dr.power_rating, float)
    assert dw.cycle_duration_slots == 6 and isinstance(dw.cycle_duration_slots, int)   # "6" -> int
    assert dw.switch_entity == "switch.dw" and dw.power_entity == "sensor.dw_w"
    assert dw.phase is Phase.l2 and dr.phase is Phase.l1 and wm.phase is Phase.l3   # "L2" -> Phase enum


def test_readlist_is_idempotent_and_updates_in_place():
    consumers = _load(_payload())
    ids = [id(c) for c in consumers]
    payload = _payload()
    payload["consumers"][0]["armed"] = False   # flip the dishwasher
    _load(payload, into=consumers)
    assert len(consumers) == 3                  # matched by id, no duplicates
    assert [id(c) for c in consumers] == ids    # same objects, updated in place
    assert consumers[0].armed is False


def test_readlist_skips_unknown_type():
    payload = _payload()
    payload["consumers"].append({"type": "Toaster", "id": "t1"})
    consumers = _load(payload)
    assert len(consumers) == 3
    assert all(type(c).__name__ != "Toaster" for c in consumers)
