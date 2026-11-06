import sys
import types
from types import MappingProxyType
from datetime import UTC, datetime

import pytest
import importlib.abc
import importlib.machinery
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# homeassistant isn't installed in the test shell. Fabricate any submodule on import;
# every attribute resolves to a permissive dummy class (subclassable AND any attr access returns a
# dummy), so import-time class bodies like `class Em(SensorEntity, HaEntity)` and `NumberMode.AUTO` work.
_FAKE_ROOTS = ("homeassistant",)


def _instance_getattr(self, name):
    if name.startswith("__"):
        raise AttributeError(name)
    if name.startswith("_attr_"):
        return None  # real HA base classes define `_attr_*` defaults as None
    return _AnyMeta(name, (), _dummy_ns())  # chainable: dummy().method().attr keeps working


def _dummy_ns() -> dict:
    # the class body of every dummy; __init_subclass__ swallows class keywords like `class Flow(ConfigFlow, domain=...)`
    return {"__getattr__": _instance_getattr, "__init_subclass__": classmethod(lambda cls, **kwargs: None)}


class _AnyMeta(type):
    def __getattr__(cls, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _AnyMeta(name, (), _dummy_ns())

    def __call__(cls, *args, **kwargs):
        # dummies accept any constructor args (Store, DeviceInfo, ...); kwargs become attrs
        try:
            return super().__call__(*args, **kwargs)
        except TypeError:
            obj = cls.__new__(cls)
            for k, v in kwargs.items():
                setattr(obj, k, v)
            return obj


class _FakeModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        obj = _AnyMeta(name, (), _dummy_ns())
        setattr(self, name, obj)
        return obj


class _Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path, target=None):
        if fullname.split(".")[0] in _FAKE_ROOTS:
            return importlib.machinery.ModuleSpec(fullname, self)
        return None

    def create_module(self, spec):
        module = _FakeModule(spec.name)
        module.__path__ = []
        return module

    def exec_module(self, module):
        pass


sys.meta_path.insert(0, _Finder())

# the dummies are chainable objects, but this one is iterated over: EntityDevice walks the registry to rebuild known entities
import homeassistant.helpers.entity_registry as _er  # noqa: E402

_er.async_entries_for_device = lambda *args, **kwargs: []

# selector() returns an HA Selector object; voluptuous only accepts a callable validator, so the stub passes the value through
import homeassistant.helpers.selector as _selector  # noqa: E402

_selector.selector = lambda config: (lambda value: value)

# the registry entry a device creates is read back (config_entries_subentries, id): a plain object, not a chainable dummy
import homeassistant.helpers.device_registry as _dr  # noqa: E402

_dr.async_get = lambda hass: types.SimpleNamespace(async_get_or_create=lambda **kwargs: types.SimpleNamespace(id="dev", config_entries_subentries={}), async_update_device=lambda *args, **kwargs: None, async_get_device=lambda **kwargs: None)


# a real subentry carries an id of its own; the coordinator creates one for the manager and then keys on it
import homeassistant.config_entries as _ce  # noqa: E402

class _Subentry:
    _next = 0

    def __init__(self, data=None, subentry_type='', title='', unique_id=None, subentry_id=None):
        _Subentry._next += 1
        self.subentry_id = subentry_id or f'sub{_Subentry._next:03d}'
        self.subentry_type, self.title, self.unique_id = subentry_type, title, unique_id
        self.data = MappingProxyType(dict(data or {}))


_ce.ConfigSubentry = _Subentry


# dt_util is fabricated like the rest, but the code formats a filename with now() and subtracts a timedelta from it
import homeassistant.util.dt as _dt  # noqa: E402

_dt.now = lambda: datetime.now(UTC)
_dt.utcnow = lambda: datetime.now(UTC)
_dt.as_local = lambda value: value


def fake_hass() -> types.SimpleNamespace:
    # the hass stub the tests hand their devices: a loop that runs callbacks inline, and the config_entries the registry lookup reads
    loop = types.SimpleNamespace(call_soon_threadsafe=lambda cb, *a: cb(*a), is_running=lambda: False)
    return types.SimpleNamespace(loop=loop, config_entries=types.SimpleNamespace(async_entries=lambda domain: []))


@pytest.fixture
def hass(tmp_path) -> types.SimpleNamespace:
    # the same stub as a fixture, so a test written against Home Assistant's own hass fixture runs here unchanged
    async def run_job(func, *args):
        return func(*args)

    stub = fake_hass()
    stub.config = types.SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts)))
    stub.async_add_executor_job = run_job
    return stub


@pytest.fixture
def entry() -> types.SimpleNamespace:
    # only the entry_id is read; the real entry carries its data in the store
    return types.SimpleNamespace(entry_id="entry")
