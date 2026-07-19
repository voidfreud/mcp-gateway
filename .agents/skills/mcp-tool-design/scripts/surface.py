#!/usr/bin/env python3
"""Render an explicitly supplied MCP gateway surface without touching a daemon.

Usage:
    uv run python .agents/skills/mcp-tool-design/scripts/surface.py \
        --config ./config.toml [--defaults-dir ./defaults] [--backend NAME] \
        [--client generic|claude-code|codex] [--format text|json] \
        [--names-only] [--strict]

This is deliberately a *file inspector*.  It neither discovers configuration nor
contacts a backend.  In particular, it does not use the gateway loader because
that loader is intentionally allowed to seed a daemon configuration.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
# The inspector imports the project's validation model below.  Do not let that
# import create ``__pycache__`` entries in a caller's checkout.
sys.dont_write_bytecode = True
_CLIENTS = ("generic", "claude-code", "codex")
_CAPTURE_FIELDS: dict[str, str] = {
    "tools": "original",
    "resources": "uri",
    "resource_templates": "uri",
    "prompts": "original",
}
_URL_RE = re.compile(r"\b(?:https?|wss?)://[^\s<>\"')\]]+", re.IGNORECASE)
_ENV_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")
_AUTH_RE = re.compile(
    r"\b(?:bearer|basic)\s+(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)",
    re.IGNORECASE,
)
_QUOTED_ASSIGNMENT_RE = re.compile(
    r"(?P<prefix>[\"'](?:"
    r"access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"client[_-]?secret|api[ _-]?key|authorization|"
    r"oauth(?:[_-]?token)?|password|secret|token"
    r")[\"']\s*(?:=|:)\s*)"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)",
    re.IGNORECASE,
)
_ASSIGNMENT_RE = re.compile(
    r"\b(?:"
    r"access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"client[_-]?secret|api[ _-]?key|authorization|"
    r"oauth(?:[_-]?token)?|password|secret|token"
    r")\s*(?:=|:)\s*(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{8,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b"
)
_PERSONAL_PATH_RE = re.compile(r"(?:/Users|/home)/[^\s<>\"')\]]+|[A-Za-z]:\\[^\s]+")


class InputError(ValueError):
    """A supplied file or option cannot be inspected safely."""


def project_root() -> Path:
    """Find the checkout from this script's path, not the caller's CWD."""
    for candidate in (Path(__file__).resolve(), *Path(__file__).resolve().parents):
        directory = candidate if candidate.is_dir() else candidate.parent
        if (directory / "pyproject.toml").is_file() and (
            directory / "AGENTS.md"
        ).is_file():
            return directory
    raise RuntimeError("project root not found")


def _project_model(raw: dict[str, Any]):
    """Validate an in-memory TOML object without using the daemon loader."""
    root = project_root()
    source_root = root / "src"
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    # Importing the model is safe: no config is loaded, expanded, or written.
    from mcp_gateway.config_loader import GatewayConfig  # noqa: PLC0415

    return GatewayConfig.model_validate(raw)


def load_config(path: Path):
    """Read one explicit TOML file and validate its static shape."""
    if not path.is_file():
        raise InputError("--config must name an existing regular file")
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise InputError("--config is not valid TOML") from exc
    if not isinstance(raw, dict):
        raise InputError("--config must contain a TOML table")
    try:
        return _project_model(raw)
    except Exception as exc:  # Pydantic and project validation have many error types.
        raise InputError("--config does not satisfy the gateway schema") from exc


def _optional_text(item: Mapping[str, Any], field: str, context: str) -> str | None:
    value = item.get(field)
    if value is not None and not isinstance(value, str):
        raise InputError(f"capture {context} has a non-text {field}")
    return value


def _capture_items(capture: Mapping[str, Any], field: str) -> list[dict[str, Any]]:
    """Validate only the public capture fields rendered by this inspector."""
    value = capture.get(field, [])
    if not isinstance(value, list):
        raise InputError(f"capture {field} must be a list")
    identity = _CAPTURE_FIELDS[field]
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise InputError(f"capture {field} item {index} must be an object")
        original = item.get(identity)
        if not isinstance(original, str) or not original:
            raise InputError(f"capture {field} item {index} needs a text {identity}")
        if original in seen:
            raise InputError(f"capture {field} has duplicate identities")
        seen.add(original)
        parsed: dict[str, Any] = {"identity": original}
        for text_field in ("name", "title", "description"):
            parsed[text_field] = _optional_text(
                item, text_field, f"{field} item {index}"
            )
        if field == "tools":
            params = item.get("params", [])
            if not isinstance(params, list):
                raise InputError(f"capture tools item {index} params must be a list")
            parsed_params: list[dict[str, Any]] = []
            param_names: set[str] = set()
            for param_index, param in enumerate(params, start=1):
                if not isinstance(param, dict):
                    raise InputError(
                        f"capture tools item {index} parameter {param_index} must be an object"
                    )
                name = param.get("original")
                if not isinstance(name, str) or not name or name in param_names:
                    raise InputError(
                        f"capture tools item {index} has an invalid parameter identity"
                    )
                param_names.add(name)
                description = _optional_text(
                    param, "description", f"tools item {index} parameter {param_index}"
                )
                required = param.get("required", False)
                if not isinstance(required, bool):
                    raise InputError(
                        f"capture tools item {index} parameter {param_index} has non-boolean required"
                    )
                parsed_params.append(
                    {"identity": name, "description": description, "required": required}
                )
            parsed["parameters"] = parsed_params
        if field == "prompts":
            args = item.get("args", [])
            if not isinstance(args, list):
                raise InputError(f"capture prompts item {index} args must be a list")
            parsed_args: list[dict[str, Any]] = []
            arg_names: set[str] = set()
            for arg_index, argument in enumerate(args, start=1):
                if not isinstance(argument, dict):
                    raise InputError(
                        f"capture prompts item {index} parameter {arg_index} must be an object"
                    )
                name = argument.get("original")
                if not isinstance(name, str) or not name or name in arg_names:
                    raise InputError(
                        f"capture prompts item {index} has an invalid parameter identity"
                    )
                arg_names.add(name)
                description = _optional_text(
                    argument,
                    "description",
                    f"prompts item {index} parameter {arg_index}",
                )
                required = argument.get("required", False)
                if not isinstance(required, bool):
                    raise InputError(
                        f"capture prompts item {index} parameter {arg_index} has non-boolean required"
                    )
                parsed_args.append(
                    {"identity": name, "description": description, "required": required}
                )
            parsed["parameters"] = parsed_args
        result.append(parsed)
    return result


def load_capture(defaults_dir: Path | None, backend: str) -> dict[str, Any] | None:
    """Read a backend capture only from an explicitly supplied directory."""
    if defaults_dir is None:
        return None
    path = defaults_dir / f"{backend}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputError("a selected capture is not valid UTF-8 JSON") from exc
    if not isinstance(data, dict):
        raise InputError("a selected capture must be a JSON object")
    if data.get("backend") != backend:
        raise InputError("a selected capture belongs to a different backend")
    instructions = data.get("instructions")
    if instructions is not None and not isinstance(instructions, str):
        raise InputError("capture instructions must be text or null")
    return {
        "instructions": instructions,
        "tools": _capture_items(data, "tools"),
        "resources": _capture_items(data, "resources"),
        "resource_templates": _capture_items(data, "resource_templates"),
        "prompts": _capture_items(data, "prompts"),
    }


def _redact(text: str) -> str:
    """Remove secrets, references, and personal paths from every rendered string."""
    value = _ENV_RE.sub("<redacted>", text)
    value = _URL_RE.sub("<redacted>", value)
    value = _PERSONAL_PATH_RE.sub("<redacted>", value)
    value = _QUOTED_ASSIGNMENT_RE.sub(r'\g<prefix>"<redacted>"', value)
    value = _AUTH_RE.sub("<redacted>", value)
    value = _ASSIGNMENT_RE.sub("<redacted>", value)
    return _TOKEN_RE.sub("<redacted>", value)


def _text(value: str | None, source: str, names_only: bool) -> dict[str, Any]:
    safe = _redact(value) if value is not None else None
    result: dict[str, Any] = {
        "source": source if safe is not None else "none",
        "utf8_bytes": len(safe.encode("utf-8")) if safe is not None else 0,
    }
    if not names_only and safe is not None:
        result["value"] = safe
    return result


def _effective_text(
    captured: str | None, override: str | None, names_only: bool
) -> dict[str, Any]:
    if override is not None:
        return _text(override, "override", names_only)
    return _text(captured, "captured" if captured is not None else "none", names_only)


def _safe_label(value: str, fallback: str) -> str:
    """Return a stable user-facing label without rendering an opaque URI."""
    safe = _redact(value)
    return fallback if safe == "<redacted>" else safe


def _override_index(items: Iterable[Any], identity: str) -> dict[str, Any]:
    return {getattr(item, identity): item for item in items}


def _effective_parameters(
    captured: list[dict[str, Any]], overrides: Iterable[Any], names_only: bool
) -> tuple[list[dict[str, Any]], set[str]]:
    by_original = _override_index(overrides, "original")
    present = {item["identity"] for item in captured}
    rendered: list[dict[str, Any]] = []
    for item in captured:
        override = by_original.get(item["identity"])
        name = getattr(override, "name", None) if override is not None else None
        enabled = not bool(getattr(override, "hide", False))
        record: dict[str, Any] = {
            "original": _redact(item["identity"]),
            "name": _redact(name or item["identity"]),
            "enabled": enabled,
            "required": item["required"],
            "description": _effective_text(
                item["description"],
                getattr(override, "description", None)
                if override is not None
                else None,
                names_only,
            ),
        }
        rendered.append(record)
    return rendered, present


def _effective_tools(backend: Any, capture: list[dict[str, Any]], names_only: bool):
    overrides = _override_index(backend.tools, "original")
    present = {item["identity"] for item in capture}
    result: list[dict[str, Any]] = []
    dangling: list[dict[str, str]] = []
    for item in capture:
        override = overrides.get(item["identity"])
        parameters, parameter_names = _effective_parameters(
            item["parameters"], getattr(override, "params", []), names_only
        )
        if override is not None:
            for parameter in override.params:
                if parameter.original not in parameter_names:
                    dangling.append(
                        {
                            "kind": "tool-parameter",
                            "original": _redact(
                                f"{item['identity']}.{parameter.original}"
                            ),
                        }
                    )
        record: dict[str, Any] = {
            "original": _redact(item["identity"]),
            "name": _redact(getattr(override, "name", None) or item["identity"]),
            "enabled": backend.enabled and getattr(override, "enabled", True),
        }
        if not names_only:
            record.update(
                {
                    "title": _effective_text(
                        item["title"],
                        getattr(override, "title", None)
                        if override is not None
                        else None,
                        names_only,
                    ),
                    "description": _effective_text(
                        item["description"],
                        getattr(override, "description", None)
                        if override is not None
                        else None,
                        names_only,
                    ),
                    "parameters": parameters,
                }
            )
        result.append(record)
    for override in backend.tools:
        if override.original not in present:
            dangling.append({"kind": "tool", "original": _redact(override.original)})
    return result, dangling


def _effective_resources(
    backend: Any,
    capture: list[dict[str, Any]],
    names_only: bool,
    label_prefix: str,
) -> list[dict[str, Any]]:
    overrides = _override_index(backend.resources, "uri")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(capture, start=1):
        override = overrides.get(item["identity"])
        fallback = f"{label_prefix}-{index}"
        record: dict[str, Any] = {
            "original": _safe_label(item["name"] or item["identity"], fallback),
            "name": _safe_label(
                getattr(override, "name", None) or item["name"] or item["identity"],
                fallback,
            ),
            "enabled": backend.enabled and getattr(override, "enabled", True),
        }
        if not names_only:
            record.update(
                {
                    "title": _effective_text(
                        item["title"],
                        getattr(override, "title", None)
                        if override is not None
                        else None,
                        names_only,
                    ),
                    "description": _effective_text(
                        item["description"],
                        getattr(override, "description", None)
                        if override is not None
                        else None,
                        names_only,
                    ),
                }
            )
        result.append(record)
    return result


def _effective_prompts(backend: Any, capture: list[dict[str, Any]], names_only: bool):
    overrides = _override_index(backend.prompts, "original")
    present = {item["identity"] for item in capture}
    result: list[dict[str, Any]] = []
    dangling: list[dict[str, str]] = []
    for item in capture:
        override = overrides.get(item["identity"])
        parameters, parameter_names = _effective_parameters(
            item["parameters"], getattr(override, "args", []), names_only
        )
        if override is not None:
            for argument in override.args:
                if argument.original not in parameter_names:
                    dangling.append(
                        {
                            "kind": "prompt-parameter",
                            "original": _redact(
                                f"{item['identity']}.{argument.original}"
                            ),
                        }
                    )
        record: dict[str, Any] = {
            "original": _redact(item["identity"]),
            "name": _redact(getattr(override, "name", None) or item["identity"]),
            "enabled": backend.enabled and getattr(override, "enabled", True),
        }
        if not names_only:
            record.update(
                {
                    "title": _effective_text(
                        item["title"],
                        getattr(override, "title", None)
                        if override is not None
                        else None,
                        names_only,
                    ),
                    "description": _effective_text(
                        item["description"],
                        getattr(override, "description", None)
                        if override is not None
                        else None,
                        names_only,
                    ),
                    "parameters": parameters,
                }
            )
        result.append(record)
    for override in backend.prompts:
        if override.original not in present:
            dangling.append({"kind": "prompt", "original": _redact(override.original)})
    return result, dangling


def _resource_dangling(backend: Any, capture: dict[str, list[dict[str, Any]]]):
    known = {
        item["identity"]
        for field in ("resources", "resource_templates")
        for item in capture[field]
    }
    return [
        {"kind": "resource", "original": "<redacted>"}
        for override in backend.resources
        if override.uri not in known
    ]


def render_backend(backend: Any, capture: dict[str, Any] | None, names_only: bool):
    """Create the stable, secret-free public report for one configured backend."""
    if capture is None:
        blank_capture = {field: [] for field in _CAPTURE_FIELDS}
        instructions = None
        capture_status = "missing"
    else:
        blank_capture = capture
        instructions = capture["instructions"]
        capture_status = "present"
    tools, tool_dangling = _effective_tools(backend, blank_capture["tools"], names_only)
    prompts, prompt_dangling = _effective_prompts(
        backend, blank_capture["prompts"], names_only
    )
    dangling = (
        tool_dangling + _resource_dangling(backend, blank_capture) + prompt_dangling
    )
    result: dict[str, Any] = {
        "name": _redact(backend.name),
        "enabled": backend.enabled,
        "capture": {"status": capture_status},
        "instructions": _effective_text(instructions, backend.instructions, names_only),
        "tools": tools,
        "resources": _effective_resources(
            backend, blank_capture["resources"], names_only, "resource"
        ),
        "resource_templates": _effective_resources(
            backend, blank_capture["resource_templates"], names_only, "template"
        ),
        "prompts": prompts,
        "dangling_overrides": dangling,
    }
    return result


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(args.config)
    by_name = {backend.name: backend for backend in config.backends}
    requested = set(args.backend)
    unknown = requested - set(by_name)
    if unknown:
        raise InputError("--backend includes a name that is not in --config")
    if args.defaults_dir is not None and not args.defaults_dir.is_dir():
        raise InputError("--defaults-dir must name an existing directory")
    selected = [
        backend
        for backend in config.backends
        if not requested or backend.name in requested
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "client": args.client,
        "backends": [
            render_backend(
                backend, load_capture(args.defaults_dir, backend.name), args.names_only
            )
            for backend in selected
        ],
    }


def render_text(report: Mapping[str, Any], names_only: bool) -> str:
    """Render a deterministic human view of the JSON contract."""
    lines = [
        f"MCP surface schema {report['schema_version']}",
        f"Client profile: {report['client']}",
    ]
    for backend in report["backends"]:
        lines.append(
            f"Backend: {backend['name']} ({'enabled' if backend['enabled'] else 'disabled'})"
        )
        lines.append(f"  Capture: {backend['capture']['status']}")
        instruction = backend["instructions"]
        lines.append(
            f"  Instructions: {instruction['source']} ({instruction['utf8_bytes']} UTF-8 bytes)"
        )
        for label, key in (
            ("Tools", "tools"),
            ("Resources", "resources"),
            ("Resource templates", "resource_templates"),
            ("Prompts", "prompts"),
        ):
            lines.append(f"  {label}:")
            for item in backend[key]:
                state = "enabled" if item["enabled"] else "disabled"
                lines.append(f"    - {item['name']} ({state})")
                if not names_only and "description" in item:
                    description = item["description"]
                    lines.append(
                        f"      description: {description['source']} "
                        f"({description['utf8_bytes']} UTF-8 bytes)"
                    )
        lines.append("  Dangling overrides:")
        for item in backend["dangling_overrides"]:
            lines.append(f"    - {item['kind']}: {item['original']}")
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, metavar="PATH")
    parser.add_argument("--defaults-dir", type=Path, metavar="PATH")
    parser.add_argument("--backend", action="append", default=[], metavar="NAME")
    parser.add_argument("--client", choices=_CLIENTS, default="generic")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--names-only", action="store_true")
    parser.add_argument("--strict", action="store_true")
    return parser.parse_args(argv)


def _is_incomplete(report: Mapping[str, Any]) -> bool:
    return any(
        backend["capture"]["status"] != "present" or backend["dangling_overrides"]
        for backend in report["backends"]
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = build_report(args)
    except InputError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return 2
    if args.format == "json":
        print(
            json.dumps(
                report, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        )
    else:
        sys.stdout.write(render_text(report, args.names_only))
    return 1 if args.strict and _is_incomplete(report) else 0


if __name__ == "__main__":
    raise SystemExit(main())
