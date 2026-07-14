"""Per-tool behavior hooks: user-authored validate / post-process Python (#16).

A tool override may name two hooks, each a ``module:function`` string pointing
into the gateway's hooks directory (:func:`hooks_dir`):

- ``validate = "mymod:check"`` — ``check(args: dict) -> None`` runs BEFORE the
  call is forwarded to the backend. Raise ``ValueError("why")`` to reject the
  call; the message is returned to the caller as the tool error. ``args`` is
  the EXPOSED argument dict — post-rename names, hidden params absent — i.e.
  exactly what the caller sent (plus schema defaults), so a rejection message
  can reference the same names the caller used.
- ``post_process = "mymod:trim"`` — ``trim(result) -> result`` runs AFTER the
  backend answered. ``result`` is a FastMCP ``ToolResult``; return it (mutated
  or copied) or any plain value (the gateway converts it to content).

Both hooks may be sync or async. Config strings are NEVER evaluated as code:
the module part must be a bare identifier resolved to ``<hooks_dir>/<module>.py``
(no path separators, so no traversal), imported with ``importlib``.

This is arbitrary code execution in the daemon BY DESIGN — the hooks dir is
local-admin-owned, the same trust level as a stdio backend ``command`` (see
docs/security.md). Error policy: a hook that fails to LOAD (missing file,
missing function, import error, bad spec) never takes down the mount and never
fails open — the tool stays broadcast, but every call to it errors with the
load failure until the hook is fixed (fail closed, loudly, per tool). The
admin state surfaces the same error per tool.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.tools.tool_transform import (
    ToolTransformConfig,
    TransformedTool,
    _set_visibility_metadata,
    forward,
)

DEFAULT_HOOKS_DIR = "~/.config/mcp-gateway/hooks"

# module:function — module is a bare identifier (an optional .py suffix is
# tolerated), so the spec can never name a path outside the hooks dir.
_SPEC_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(?:\.py)?:([A-Za-z_][A-Za-z0-9_]*)$")

# (resolved module path) -> (mtime, module). Mtime-keyed like the secrets
# cache (#105): an edited hook file is picked up on the next transform build
# without a daemon restart; an unchanged one imports once.
_module_cache: dict[str, tuple[float, Any]] = {}


class HookError(RuntimeError):
    """A hook spec that cannot be resolved to a callable (bad spec, missing
    module/function, import failure). Never raised during a tool CALL — load
    failures are converted to a per-call fail-closed error instead."""


def hooks_dir() -> Path:
    """Where hook modules live, mirroring the config precedence:
    ``MCP_GATEWAY_HOOKS`` env var > a repo-local ``./hooks/`` directory (dev
    checkout) > ``~/.config/mcp-gateway/hooks/``."""
    env = os.environ.get("MCP_GATEWAY_HOOKS")
    if env:
        return Path(env).expanduser()
    if Path("hooks").is_dir():
        return Path("hooks")
    return Path(DEFAULT_HOOKS_DIR).expanduser()


def valid_spec(spec: str) -> bool:
    """True iff *spec* is a well-formed ``module:function`` hook reference."""
    return bool(_SPEC_RE.match(spec))


def load_hook(spec: str) -> Callable:
    """Resolve a ``module:function`` spec to a callable from the hooks dir.

    Raises :class:`HookError` on a malformed spec, a missing module file, an
    import-time exception in the module, or a missing/non-callable attribute.
    The config string is never evaluated — only imported from the hooks dir.
    """
    m = _SPEC_RE.match(spec or "")
    if not m:
        raise HookError(
            f"invalid hook spec {spec!r}: use 'module:function' "
            f"(a function in <hooks_dir>/module.py; hooks dir: {hooks_dir()})"
        )
    module_name, func_name = m.group(1), m.group(2)
    path = (hooks_dir() / f"{module_name}.py").resolve()
    if not path.is_file():
        raise HookError(f"hook module not found: {path} (from spec {spec!r})")
    mtime = path.stat().st_mtime
    cached = _module_cache.get(str(path))
    if cached is not None and cached[0] == mtime:
        module = cached[1]
    else:
        py_spec = importlib.util.spec_from_file_location(
            f"mcp_gateway_hooks.{module_name}", path
        )
        if py_spec is None or py_spec.loader is None:  # pragma: no cover — defensive
            raise HookError(f"cannot build an import spec for {path}")
        module = importlib.util.module_from_spec(py_spec)
        try:
            py_spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 — any import-time crash is a load error
            raise HookError(
                f"hook module {path} failed to import: {type(exc).__name__}: {exc}"
            ) from exc
        _module_cache[str(path)] = (mtime, module)
    fn = getattr(module, func_name, None)
    if not callable(fn):
        raise HookError(f"hook {spec!r}: {path} has no function {func_name!r}")
    return fn


async def _maybe_await(value):
    """Support sync and async hooks with one call site."""
    if inspect.isawaitable(value):
        return await value
    return value


def make_hook_fn(validate: Callable | None, post_process: Callable | None) -> Callable:
    """Build the FastMCP ``transform_fn`` that runs the hooks around forward().

    The returned coroutine takes ``**kwargs`` (so FastMCP keeps the transformed
    schema untouched — see ``TransformedTool.from_tool``) under EXPOSED names;
    ``forward(**kwargs)`` reverse-maps renames and injects hidden defaults
    before the backend sees the call.
    """

    async def _hooked(**kwargs):
        if validate is not None:
            try:
                await _maybe_await(validate(dict(kwargs)))
            except ValueError as exc:
                # The contract: ValueError message -> the caller's tool error.
                # ToolError text is never masked by FastMCP; a bare ValueError
                # could be, depending on mask_error_details.
                raise ToolError(str(exc) or "call rejected by validate hook") from exc
        result = await forward(**kwargs)
        if post_process is not None:
            result = await _maybe_await(post_process(result))
        return result

    return _hooked


def make_failing_hook_fn(tool: str, error: str) -> Callable:
    """The fail-CLOSED stand-in for a hook that did not load: the mount stays
    up and the tool stays broadcast, but every call errors with the load
    failure. Failing open (silently skipping a broken validate hook) would
    drop a guard the operator deliberately configured."""

    async def _broken(**kwargs):  # noqa: ARG001 — signature must accept the call
        raise ToolError(f"tool {tool!r}: behavior hook failed to load — {error}")

    return _broken


class HookedToolTransformConfig(ToolTransformConfig):
    """A ``ToolTransformConfig`` that also installs a gateway-built
    ``transform_fn`` (#16). FastMCP's config model has no transform_fn field —
    only ``TransformedTool.from_tool`` accepts one — so this subclass overrides
    ``apply`` to pass it through while keeping every other semantic of the
    parent (the parent's ``apply`` body is mirrored; a FastMCP change here is
    tripwired by tests)."""

    transform_fn: Callable | None = None

    def apply(self, tool) -> TransformedTool:
        tool_changes: dict[str, Any] = self.model_dump(
            exclude_unset=True, exclude={"arguments", "enabled", "transform_fn"}
        )
        transformed = TransformedTool.from_tool(
            tool=tool,
            **tool_changes,
            transform_fn=self.transform_fn,
            transform_args={k: v.to_arg_transform() for k, v in self.arguments.items()},
            # Keep the broadcast identical with or without hooks: from_tool
            # would otherwise re-infer the output schema from the transform_fn.
            output_schema=tool.output_schema,
        )
        if "enabled" in self.model_fields_set:
            _set_visibility_metadata(transformed, enabled=self.enabled)
        return transformed
