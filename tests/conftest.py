import sys
import types
import importlib.abc
import importlib.machinery
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# homeassistant and paho aren't installed in the test shell. Fabricate any submodule on import;
# every attribute resolves to a permissive dummy class (subclassable AND any attr access returns a
# dummy), so import-time class bodies like `class Em(NumberEntity, EmEntity)` and `NumberMode.AUTO` work.
_FAKE_ROOTS = ("homeassistant", "paho")


class _AnyMeta(type):
    def __getattr__(cls, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _AnyMeta(name, (), {})

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
        obj = _AnyMeta(name, (), {})
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
