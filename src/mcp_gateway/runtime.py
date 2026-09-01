"""Typed ownership of the gateway's live per-backend proxy state.

Captured defaults are deliberately separate snapshot data: runners rebuild them
on boot, hot-add, and recycle.  This module owns only objects whose lifetime is
the running daemon and whose proxy/transform entries must move together.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from fastmcp.server.providers.proxy import FastMCPProxy
from fastmcp.server.transforms import Transform

type BackendName = str
type CapturedTools = dict[BackendName, list[str]]
type CapturedMeta = dict[BackendName, dict[str, dict[str, Any]]]
type CapturedInstructions = dict[BackendName, str | None]
type TransformHolder = list[Transform]


@dataclass
class BackendRuntime:
    """Live mounted backends and the gateway transforms attached to each one."""

    _proxies: dict[BackendName, FastMCPProxy] = field(default_factory=dict)
    _transform_holders: dict[BackendName, TransformHolder] = field(default_factory=dict)
    _status: dict[BackendName, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_legacy(
        cls,
        proxies: dict[BackendName, FastMCPProxy],
        transform_holders: dict[BackendName, TransformHolder],
    ) -> BackendRuntime:
        """Adapt old shared-dict callers without copying their live state."""
        return cls(proxies, transform_holders)

    @property
    def proxies(self) -> Mapping[BackendName, FastMCPProxy]:
        """A read-only view for consumers that only need proxy lookup."""
        return self._proxies

    @property
    def status(self) -> Mapping[BackendName, dict[str, Any]]:
        """Each runner's connection state: connecting, up, down, reconnecting."""
        return self._status

    def set_status(self, name: BackendName, state: str, **detail: Any) -> None:
        self._status[name] = {"state": state, **detail}

    def clear_status(self, name: BackendName) -> None:
        self._status.pop(name, None)

    def get_proxy(self, name: BackendName) -> FastMCPProxy | None:
        return self._proxies.get(name)

    def get_transforms(self, name: BackendName) -> TransformHolder:
        return self._transform_holders.get(name, [])

    def mount(
        self,
        name: BackendName,
        proxy: FastMCPProxy,
        transforms: TransformHolder,
    ) -> None:
        self._proxies[name] = proxy
        self._transform_holders[name] = transforms

    def replace_transforms(
        self, name: BackendName, transforms: TransformHolder
    ) -> None:
        self._transform_holders[name] = transforms

    def unmount(self, name: BackendName) -> None:
        self._proxies.pop(name, None)
        self._transform_holders.pop(name, None)
