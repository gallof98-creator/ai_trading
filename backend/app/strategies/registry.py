"""Versioned strategy registry. Plugins register with the @register decorator."""
from __future__ import annotations

from pydantic import ValidationError

from .base import Strategy

_REGISTRY: dict[str, dict[str, type[Strategy]]] = {}


class UnknownStrategyError(KeyError):
    pass


def register(cls: type[Strategy]) -> type[Strategy]:
    versions = _REGISTRY.setdefault(cls.name, {})
    if cls.version in versions and versions[cls.version] is not cls:
        raise ValueError(f"Strategy {cls.name} v{cls.version} is already registered; bump the version instead of replacing it")
    versions[cls.version] = cls
    return cls


def _load_builtin() -> None:
    from . import library  # noqa: F401  (import registers the built-ins)


def _version_key(v: str) -> tuple:
    return tuple(int(x) for x in v.split("."))


def get_strategy_class(name: str, version: str | None = None) -> type[Strategy]:
    _load_builtin()
    versions = _REGISTRY.get(name)
    if not versions:
        raise UnknownStrategyError(f"Unknown strategy {name!r}")
    if version is None:
        version = max(versions, key=_version_key)
    if version not in versions:
        raise UnknownStrategyError(f"Strategy {name!r} has no version {version}")
    return versions[version]


def create_strategy(name: str, params: dict | None = None, version: str | None = None) -> Strategy:
    """Instantiate with validated parameters. Raises ValueError on any invalid or unknown parameter."""
    cls = get_strategy_class(name, version)
    try:
        return cls(params)
    except ValidationError as exc:
        raise ValueError(f"Invalid parameters for {name}: " + "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())) from None


def list_strategies() -> list[dict]:
    _load_builtin()
    out = []
    for name in sorted(_REGISTRY):
        versions = sorted(_REGISTRY[name], key=_version_key)
        out.append({**_REGISTRY[name][versions[-1]].describe(), "versions": versions})
    return out
