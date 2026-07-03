#!/usr/bin/env python3
"""Dump the effective broadcast surface of the gateway's enabled backends.

The mechanical first step of the mcp-tool-design pipeline: what each enabled
backend actually broadcasts to Claude Code right now — server instructions,
tool names, descriptions, pinning, params — with UTF-8 byte counts against the
2 KB truncation caps.

Run from anywhere inside the repo:
    uv run .claude/skills/mcp-tool-design/scripts/surface.py            # summary, all enabled backends
    uv run .claude/skills/mcp-tool-design/scripts/surface.py deepwiki   # summary, one backend
    uv run .claude/skills/mcp-tool-design/scripts/surface.py deepwiki --full   # full text for grading
    uv run .claude/skills/mcp-tool-design/scripts/surface.py --turn0    # turn-0 view for the cold-eval:
                                                                        # instructions + full callables, no descriptions

Reads the LIVE config (config.toml) plus the captured defaults under
~/.local/state/mcp-gateway/defaults/. Read-only — never writes.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

import config_loader as cl  # noqa: E402
from admin import load_defaults  # noqa: E402

CAP = 2048  # Claude Code truncates descriptions and instructions at 2 KB


def nbytes(s: str | None) -> int:
    return len((s or "").encode("utf-8"))


def budget(s: str | None) -> str:
    n = nbytes(s)
    flag = "  ** OVER 2KB — TRUNCATED BY CLAUDE CODE **" if n > CAP else ""
    return f"{n}B/{CAP}{flag}"


def turn0(cfg, target: str | None) -> int:
    """The cold agent's actual turn-0 view: each enabled server's instructions
    plus its bare deferred callables (mcp__gateway-<backend>__<name>) —
    descriptions and params withheld. Feed this to the fresh-agent cold-eval
    (differentiation.md step 7)."""
    shown = 0
    for b in cfg.backends:
        if target and b.name != target:
            continue
        if not b.enabled:
            continue
        d = load_defaults(b.name)
        if d is None:
            continue
        shown += 1
        instr = b.instructions if b.instructions is not None else d.get("instructions")
        print(f"## gateway-{b.name}")
        print(instr or "(no instructions)")
        print()
        ovs = {t.original: t for t in b.tools}
        for dt in d.get("tools", []):
            ov = ovs.get(dt["original"])
            if ov is not None and not ov.enabled:
                continue
            name = ov.name if (ov and ov.name) else dt["original"]
            print(f"mcp__gateway-{b.name}__{name}")
        print()
    if target and shown == 0:
        print(f"no enabled backend named {target!r}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    full = "--full" in sys.argv
    target = args[0] if args else None

    cfg = cl.load(REPO_ROOT / "config.toml")
    if "--turn0" in sys.argv:
        return turn0(cfg, target)
    shown = 0
    for b in cfg.backends:
        if target and b.name != target:
            continue
        if not b.enabled:
            print(f"backend {b.name}: DISABLED — out of scope for tuning\n")
            continue
        shown += 1
        d = load_defaults(b.name)
        if d is None:
            print(f"backend {b.name}: no captured defaults (never introspected?)\n")
            continue
        instr = b.instructions if b.instructions is not None else d.get("instructions")
        src = "override" if b.instructions is not None else "captured"
        pin = " pinned:ALL-TOOLS" if b.always_load else ""
        print(f"backend {b.name} ({b.transport}){pin}")
        print(f"  instructions [{src}]: {budget(instr)}")
        if full:
            print("  ---8<--- instructions")
            print(instr or "(none)")
            print("  --->8---")
        ovs = {t.original: t for t in b.tools}
        for dt in d.get("tools", []):
            orig = dt["original"]
            ov = ovs.get(orig)
            if ov is not None and not ov.enabled:
                print(f"  - {orig}: DISABLED (not broadcast)")
                continue
            name = ov.name if (ov and ov.name) else orig
            desc = ov.description if (ov and ov.description) else dt.get("description")
            pinned = b.always_load or (ov.always_load if ov else False)
            tags = []
            if pinned:
                tags.append("pinned")
            if name != orig:
                tags.append(f"renamed from {orig}")
            tag = f" [{', '.join(tags)}]" if tags else ""
            print(f"  - {name}{tag}: desc {budget(desc)}")
            if full:
                print(f"      desc: {desc or '(none)'}")
            povs = {p.original: p for p in (ov.params if ov else [])}
            for dp in dt.get("params", []):
                po = dp["original"]
                pov = povs.get(po)
                if pov and pov.hide:
                    print(f"      param {po}: HIDDEN")
                    continue
                pname = pov.name if (pov and pov.name) else po
                pdesc = (
                    pov.description
                    if (pov and pov.description)
                    else dp.get("description")
                )
                if full:
                    ptag = f" (renamed from {po})" if pname != po else ""
                    print(f"      param {pname}{ptag}: {pdesc or '(no description)'}")
        print()
    if target and shown == 0:
        print(f"no enabled backend named {target!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
