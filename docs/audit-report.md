# mcp-gateway Audit Report

**Date:** 2026-07-27
**Scope:** 97 read-only agents covering all source, test, tool, config, CI, and documentation files
**Result:** ~200 findings — 9 serious, 28 moderate, remainder minor/clean

---

## Recommended Action Plan (2026-08-03) — First Things To Be Done

Priority-ordered hardening plan derived from the compliance audit (#322–#358) and the deep breakage audit (CI #168). One broad net (A3) + five narrow nets (A1/A2/A4/A5/A6) for the surfaces the official suite structurally cannot see. Execute in order; each item is independently shippable.

**🔴 ASAP bug — fix before any feature work:**

| # | Action | Targets (audit refs) | Effort | Acceptance criteria |
|---|--------|----------------------|--------|---------------------|
| A0 | ✅ **DONE (2026-08-03): Fresh-install dead daemon.** `install.sh` now creates `STATE_DIR=~/.local/state/mcp-gateway` before rendering and starting the LaunchAgent, with a fresh-account regression test proving the directory exists before launch. | #165 (Boot.4, OpsScripts.1) | S | Fresh-account-style install creates the state directory before kickstart |

**Compliance & robustness plan (A1–A6):**

| # | Action | Targets (audit refs) | Effort | Acceptance criteria |
|---|--------|----------------------|--------|---------------------|
| A1 | ✅ **DONE (2026-08-03): Gateway-authored compliance violations fixed.** Live prompt collisions are rejected in the admin and runtime paths; optional virtual inputs no longer advertise `null`; non-finite defaults and static arguments are rejected. | #325, #326, #327 | S | Regression tests cover all three contracts |
| A2 | ✅ **DONE (2026-08-03): JSON Schema 2020-12 validation added.** Virtual input schemas are meta-validated across every supported input type; defaults are checked against their property schemas; schema/runtime null behavior is locked together. | #326, #327 | S | Draft 2020-12 contract tests green |
| A3 | ✅ **DONE (2026-08-03): Full applicable official conformance suite is the CI gate.** The pinned 0.1.16 runner exercises all 24 applicable server scenarios through one persistent gateway endpoint and documents eight capability/pending exclusions with reasons. The expanded fixture covers image, audio, mixed content, embedded resources, prompts, resources, logging, progress, multi-stream SSE, DNS rebinding, and JSON Schema 2020-12. Expansion found and fixed request-context loss across proxy sessions plus `$defs` stripping. | #322–#324, #341, #342; CI conformance coverage table | M–L | `mcp-contract` passes the full applicable matrix; skip matrix is explicit in `run_official.py` |
| A4 | ✅ **DONE (2026-08-03): Initialize identity and capabilities are endpoint-accurate.** Every backend endpoint reports `mcp-gateway-<backend>` and the installed gateway version instead of FastMCP's version. Backend endpoints advertise their implemented tools/resources/prompts/logging surface; the virtual endpoint advertises tools only. Empty experimental/task state and the unimplemented MCP Apps extension are no longer advertised. Unit snapshots and the raw-wire receipt cover current and legacy protocol initialization. | #335, #351, #352, #353 | S–M | Per-endpoint metadata snapshots and raw-wire initialize contracts green |
| A5 | ✅ **DONE (2026-08-03): Error, session, and timeout contracts hardened.** Gateway middleware maps unknown/malformed prompt requests to `-32602` and leaked upstream code-0 handler failures to `-32603`. Raw-wire coverage now pins missing/invalid/deleted sessions to 400/404/200→404. Per-backend `init_timeout` and `request_timeout` settings (bounded, persisted, and documented) enforce handshake and forwarded-call deadlines; a black-hole fixture proves degraded readiness without blocking healthy endpoints. | #322, #323, #324, #328, #330, #333 | M | Raw-wire error/session/deadline matrix green; both missing timeouts wired |
| A6 | ✅ **DONE (2026-08-03): FastMCP compatibility canary repaired and wire tripwires added.** The lane clones a complete isolated checkout (including Git metadata), syncs the locked project so its console entry point exists, upgrades FastMCP only inside that environment, and runs the full pytest suite plus the raw-wire receipt from the candidate environment. Version comparison follows the current exact project pin instead of a duplicated literal. | CI #168; #90; tripwire tests | S | `actionlint` clean; isolated FastMCP 3.4.5 simulation passes 636 tests, raw-wire contracts, and import smoke |
| A7 | ✅ **DONE (2026-08-03): Application-owned macOS resident lifecycle.** Option A is recorded in ADR-0010. The installed CLI now owns atomic/versioned plist and stable-wrapper installation, PATH capture, prompt-once onboarding, controlled restart with `/health` + `/ready` verification and rollback, process-tree status, complete keep/purge removal, and checkout-era migration. `install.sh` is a thin compatibility wrapper. | A0/#165; #149; #150; #151; #171; OpsScripts.4 | L | 20 focused lifecycle/package contracts green; disposable 3-cycle settled receipt: 0.0% CPU snapshots, 92.7–92.9 MB RSS, no first-to-last growth |

**A7 — locked requirements for clean, complete install/uninstall:**

1. **Atomic plist write** — temp file + rename + fsync (the same pattern `cl.save` already uses for config); a crash mid-write must never leave a truncated plist that makes bootstrap fail with no hint.
2. **Versioned plist template + re-render on upgrade** — the shim target survives `uv tool upgrade`, but plist content (PATH, args, log paths) is template-owned; on every startup, re-render if the template version differs from the installed plist, so stale content doesn't persist forever.
3. **Install idempotency + prompt-once** — detect an already-loaded service and skip/re-render, never double-bootstrap (`launchctl bootstrap` errors when loaded); persist an asked-marker so a declined prompt is not repeated on every run.
4. **Uninstall = bootout + delete plist + remove marker** — bootout alone leaves the plist in `~/Library/LaunchAgents` and `RunAtLoad` resurrects the job at next login; uninstall must bootout (ignoring not-loaded errors), delete the plist file, and clear the asked-marker. Plus a data-retention prompt ("keep config and state? [y/N]") — silently deleting years of logs would be the opposite mistake.
5. **Legacy-layout migration** — the uninstaller must also clean the install.sh-era artifacts (`~/.local/opt/mcp-gateway` symlink, old-format plist), or a full uninstall is only true for users who never used the old installer.
6. **PATH capture** — snapshot the installing shell's PATH into the plist instead of hardcoding `/opt/homebrew` (the current template breaks Intel Macs — audit finding OpsScripts.4).
7. **Wrong-order-uninstall guard (crash-loop immunity)** — plist points at a tiny app-owned wrapper script (`~/.local/libexec/mcp-gateway/run`, created at install, removed at uninstall — never uv-managed) with `KeepAlive: {SuccessfulExit: false}` instead of `KeepAlive: true`. Wrapper exits 0 when the real binary is missing (launchd classifies it as a successful exit → no restart → inert job, no crash-loop); otherwise `exec`s the binary so signals and exit codes pass through — which is exactly what makes the KeepAlive semantics trustworthy. Considerations: (a) launchd classifies spawn failures as *unsuccessful* exits, so KeepAlive tweaks alone cannot fix missing-binary — the wrapper is required, not optional polish; (b) self-healing variant (wrapper boots itself out and deletes the plist when the binary is missing) was considered and **rejected** — a broken upgrade with a temporarily-missing binary would silently uninstall the user's service; instead: exit 0 + one-line notice to err.log, leaving removal a deliberate act; (c) the legacy checkout layout (~/.local/opt venv) is already immune to this problem — it is uv-tool-path-specific; (d) genuine crashes (signal/non-zero exit) remain unsuccessful exits and still auto-restart, preserving today's daemon semantics.

**A7 — availability-model decision (recorded in ADR-0010):**

| Option | What it is | Costs / tradeoffs | Fits if… |
|--------|------------|-------------------|----------|
| **A. Resident daemon (chosen)** | launchd `KeepAlive: {SuccessfulExit: false}`, one always-on gateway process, warm backend sessions | Requires explicit lifecycle, rollback, crash-loop, and uninstall safeguards; warm stdio backend children remain alive while enabled | Always-available shared service/admin model is the product; low first-call latency matters |
| **B. Socket activation** | launchd owns the TCP port (`LaunchSockets`), spawns the process on first connection, idle self-exit, no KeepAlive | First-connect latency (~hundreds of ms); no warm sessions (backends already default to stateless per-call); clients must tolerate connection-refused/retry during binary swaps; adds plist socket config | Zero idle footprint wanted; simplest lifecycle (crash-loop class disappears, upgrades free between connections, uninstall residue shrinks to a dead socket) |
| **C. Stdio-per-client** | No port, no daemon, no launchd — the MCP client spawns the gateway as a child process per session | Product pivot: contradicts the documented per-endpoint network design (ADRs), loses shared admin UI + registration model, N clients = N processes | Only if the product boundary itself is reconsidered — named for completeness, not recommended |

**Decision:** Option A is accepted in [ADR-0010](decisions/0010-resident-launchagent.md). It preserves the existing shared endpoint/admin model and low first-call latency. Option B remains the documented zero-residency alternative if measured operating cost later outweighs those benefits; Option C remains a product-boundary change. The update implication is explicit: resident mode requires a controlled restart and health/readiness verification after a binary swap.

**A8 — Update story: ship, apply, roll back ([#254](https://github.com/voidfreud/mcp-gateway/issues/254)) — completed 2026-08-03:**

| # | Requirement | Ref | Acceptance |
|---|-------------|-----|------------|
| 1 | **[DONE] Auth-free distribution** — the public distribution is `mcp-local-gateway`; the command/import remain `mcp-gateway` / `mcp_gateway`. Tagged releases publish the verified wheel and sdist through OIDC; private checksummed GitHub artifacts are fallback-only. | Release.3/4 | Package metadata, clean-wheel install receipt, pinned publish job, GitHub `pypi` environment, Trusted Publisher, and public v1.2.1 upload verified |
| 2 | **[DONE] `mcp-gateway update` command** — resolves/validates a stable PyPI version, installs that exact version with `uv`, verifies the shim, restarts an existing resident service through the application-owned lifecycle, and requires health/readiness. | A7 req 2; justfile update | Focused success, no-op, unpublished-refusal, resident-restart, and CLI contracts green |
| 3 | **[DONE] Notify, don't auto-apply** — one immediate/daily bounded request, offline-tolerant status/logging, conditional Admin badge, and strict `update_check` opt-out. | README network-behavior note | Disabled lifespan starts zero monitors; enabled starts one; Admin/config/UI contracts green |
| 4 | **[DONE] Deterministic rollback** — activation failure reinstalls/verifies the old exact PyPI version and restarts the previous resident service; `update --version X.Y.Z` is the deliberate rollback path. Package changes never touch user config/state. | backups (#172), recovery path | Failure-path package/service rollback contract green |
| 5 | **[DONE] New-user narrative documented** — auth-free install, resident setup, update notice, one-command apply, exact rollback, uninstall, privacy, and fallback artifacts are covered by the owning guides. | README | README + installation/operations/releases/security/config/Admin/API docs updated |
| 6 | **[DONE] Availability-model interaction** — ADR-0010 chooses the resident daemon and records controlled restart plus readiness verification after binary swaps. | A7 decision block | Decision rationale and implementation agree |

**Release receipt:** [v1.2.1](https://github.com/voidfreud/mcp-gateway/releases/tag/v1.2.1) published through the
[Trusted Publishing workflow](https://github.com/voidfreud/mcp-gateway/actions/runs/30861063438)
to [public PyPI](https://pypi.org/project/mcp-local-gateway/1.2.1/). A clean
environment with GitHub/PyPI credentials removed installed the public wheel,
reported `mcp-gateway 1.2.1`, imported `cryptography 50.0.0`, and completed an
update no-op against PyPI. The v1.2.0 PyPI upload was canceled when the
main-branch security gate discovered GHSA-g6cj-pr64-35w5; [#258](https://github.com/voidfreud/mcp-gateway/pull/258)
updated the lock before v1.2.1 shipped.

**Sequencing notes:** A0 first (one line, unblocks every fresh install). A1+A2 next (your own bugs, no fixture work, ~a day); A3 is the largest chunk and should drive fixing the SDK-inherited error-code/timeout violations; A4–A5 land as regression locks while A3's red items get fixed; A7 (self-install) is independent — it absorbs A0's fix at the source and is a natural follow-up once the daemon lifecycle is stable. Do **not** run conformance scenarios for capabilities the gateway deliberately does not advertise (false failures → gate noise), and do **not** build a custom protocol test framework — the official suite is the framework, everything else is targeted nets.

**Definition of done:** curated official suite green in CI + A1/A2/A4/A5 targeted tests green + fastmcp-compat lane green = the strongest compliance claim the ecosystem recognizes.

---

## 🔴 Serious (potential 500s, auth bypass, silent data loss)

| # | File:Line | Issue |
|---|-----------|-------|
| 1 | `src/mcp_gateway/server.py:435,744` | **Empty `${VAR}` silently disables all auth.** `config_loader.expand_env` raises only on absent env vars, not empty ones. An empty-string env var makes `bearer_token` become `""` → falsy → `self._expected = None` → all requests pass without auth. Same applies to `oauth.admin_bearer_token` (lines 736–740). |
| 2 | `src/mcp_gateway/admin.py:1528–1547` | **`set_instructions` crashes on non-string input (HTTP 500).** `_clean()` at line 862–868 returns non-string values as-is. When an int/bool/None reaches `override.encode("utf-8")` at line 1541, it raises `AttributeError` unhandled. |
| 3 | `src/mcp_gateway/admin_routes_settings.py:57,78,94,105,116,127,140,155,171,187` | **Non-dict JSON bodies pass `needs_json` → 500.** `_needs_json` only checks JSON syntax, not that the result is a `dict`. Arrays, strings, numbers, booleans, and `null` all pass through, then `payload["backend"]` or `payload.get(…)` raises `TypeError`/`AttributeError`. All 10 POST/PUT handlers are affected. |
| 4 | `src/mcp_gateway/admin_routes_settings.py:78–83,107–110,129–132` | **Reset handlers lack `KeyError` catch → 500.** `reset_tool`, `reset_resource`, and `reset_prompt` use bare `payload["backend"]` and `payload["tool_original"]` / `payload["uri"]` / `payload["prompt_original"]` without `KeyError` handling (the corresponding `put_*` handlers do catch it). An empty dict `{}` → 500. |
| 5 | `src/mcp_gateway/admin_routes_settings.py:188` | **`post_import` bundle extraction uses `or payload` → 500 on falsy values.** `bundle = payload.get("settings") or payload` activates when `"settings"` is `[]`, `""`, `0`, or `false`. These non-dict values reach `import_settings` → `AttributeError` → 500. |
| 6 | `src/mcp_gateway/config_loader.py:900–901` | **Backend names `"admin"`, `"health"`, `"ready"` are not reserved.** Only `"virtual"` is. A backend named `"admin"` would collide with the Mount route; unmounting it strips the admin UI `Route("/admin", admin_page)` permanently until daemon restart. `"health"` and `"ready"` could shadow liveness endpoints. The check in `admin_routes_backend.py:260–261` also only reserves `"virtual"`. |
| 7 | `src/mcp_gateway/admin_routes_claude.py:124–142` | **Claude register/deregister never invalidates `_cc_reg_cache`.** After `claude mcp add` or `claude mcp remove`, the registrations page shows stale cache for up to the 60s TTL. Only `?fresh=true` bypasses it. |
| 8 | `src/mcp_gateway/admin_cli.py:22,30` | **`run_cli` doesn't catch `ValueError` from `subprocess.run`.** Negative timeout → `Thread.join(negative)` → `ValueError`. Non-UTF-8 subprocess output with `text=True` → `UnicodeDecodeError` → `ValueError`. Neither is `SubprocessError` or `OSError`, so the `except` on line 30 misses them. Breaks the docstring's "never raises" contract → HTTP 500. |
| 9 | `src/mcp_gateway/admin_routes_claude.py:180` | **`reregister_all` discards `claude mcp remove` exit code.** `await _cli_raw(remove_argv)` return value is thrown away. If removal fails (permissions, CLI error), the code silently proceeds to `add`, creating duplicate registrations or masking a failed re-point. |

---

## 🟡 Moderate (logic errors, inconsistent state, performance, TOCTOU)

| # | File:Line | Issue |
|---|-----------|-------|
| 10 | `src/mcp_gateway/runtime.py:41–43` | **Dict mutation during iteration → `RuntimeError` in `/ready`.** The `proxies` property returns the raw mutable `_proxies` dict. `/ready` at `server.py:1027` iterates it with `sorted()`. A concurrent `mount()`/`unmount()` during that iteration triggers `RuntimeError: dictionary changed size during iteration` → 500. |
| 11 | `src/mcp_gateway/admin_routes_ops.py:142–151` | **Status probe `TimeoutError` triggers full backend recycle.** `get_status` wraps `list_tools` in `asyncio.timeout(5.0)`. `TimeoutError` is caught by bare `except Exception` and unconditionally calls `recycle()` for warm backends. A healthy backend under transient load gets torn down and remounted every 30s (limited only by `RECYCLE_COOLDOWN`). Should check `is_session_death()` instead. |
| 12 | `src/mcp_gateway/admin_routes_claude.py:106–122` | **`deregister_backend` doesn't verify backend exists.** `register_backend` validates the backend is in config at line 89; `deregister_backend` skips this check entirely. A typo'd name silently succeeds (the CLI's `mcp remove` returns ok for unknown names). |
| 13 | `src/mcp_gateway/virtual_tools.py:538–543` | **Stale `accounted_bytes` after force-trim loops.** When the final result still exceeds budget, two `while` loops force-trim `structured_members` and `content` but never decrement `used`. The output envelope's `budget.accounted_bytes` over-reports bytes that were actually discarded. Wire result is correct; only metadata is wrong. |
| 14 | `src/mcp_gateway/virtual_tools.py:325–327` | **Session-level MCP timeout reported as `"error"`, not `"timeout"`.** `call_tool_mcp` has an internal session timeout. When it fires before the `asyncio.timeout`, it raises `McpError` caught by `except Exception` → status `"error"`. The per-member `asyncio.timeout` → `TimeoutError` → status `"timeout"` path works correctly, but the session-level path gives the wrong status. |
| 15 | `src/mcp_gateway/admin_routes_virtual.py:297–308` | **Consent fingerprint persisted BEFORE activation confirmed.** `consent_fingerprint` is written to shared state at line 303, then `await _resolution(tool, cfg)` at line 306 can fail and return 400. The stale fingerprint remains visible to `list_virtual_tools` showing false consent. `build_virtual_tool` at line 317 is another failure point after consent is stored. |
| 16 | `src/mcp_gateway/admin_routes_virtual.py:306` | **`activate_virtual` holds global `ctx.lock` during live `list_tools` calls.** `_resolution()` connects to each member backend and calls `list_tools` (up to `member.timeout` seconds, max 300s). During this, ALL other mutations block on the lock. `create_virtual`/`update_virtual` avoid this by doing resolution outside the lock. |
| 17 | `src/mcp_gateway/admin_routes_logs.py:74` | **SSE log stream endpoint missing `event` filter.** `/admin/api/logs` supports `?event=<type>`; `/admin/api/logs/stream` does not. Every SSE log subscription gets unfiltered output. |
| 18 | `src/mcp_gateway/admin_routes_logs.py:96` | **Missing `str()` guard on level filter in SSE stream.** `entry.get("level", "").upper()` — if `"level"` key exists but is `null`, `.upper()` raises `AttributeError`. The sibling code at `logging_setup.py:389` correctly wraps with `str()`. |
| 19 | `src/mcp_gateway/admin_routes_logs.py:49` | **`flush()` holds `_runtime_lock` for up to 5 seconds.** `logging_setup.flush()` acquires the RLock, then blocks on a `_FlushMarker` with 5s timeout. During this, concurrent `/admin/api/logs` requests (which also call `flush()` and `status()`) serialize on the lock. Two browser tabs refreshing simultaneously can cause perceptible lag. |
| 20 | `src/mcp_gateway/config_loader.py:1253` | **`ResourcePromptTransform.__init__` raises `ValueError`, not `ConfigError`.** All other validation in the file raises `ConfigError`. This breaks the consistent error taxonomy. The docstring says it's intentional (matching FastMCP), but it's still an inconsistency in the gateway's own error contract. |
| 21 | `src/mcp_gateway/config_loader.py:1006–1008` | **`headers_helper` stderr discarded on failure.** When the helper exits non-zero, `CalledProcessError` is raised. `str(exc)` includes return code but NOT the captured `stderr`. Operator has no way to see what the helper printed to diagnose the failure. |
| 22 | `src/mcp_gateway/server.py:697` | **Broad `except Exception` in `_mount_backend` masks programming errors.** `NameError`, `AttributeError`, `TypeError` inside the mount logic are silently caught and logged as `backend_mount_failed` with no traceback. The intent is to skip backends whose connection fails, not to suppress internal bugs. |
| 23 | `src/mcp_gateway/server.py:833–834` | **`hot_recycle` poll loop exits before `_unmount` completes.** `stops.pop(b.name, None)` happens at line 833; `_unmount(app, b.name, ...)` at 834. The poll loop at line 931 exits when `name not in stops` — i.e. after `stops.pop` but before `_unmount`. Correctness depends on both statements being synchronous with no yield point between them. Fragile to future refactors. |
| 24 | `src/mcp_gateway/admin_routes_backend.py:283–297` | **Orphaned defaults file on duplicate-name rejection.** `capture_defaults` + `save_defaults` runs BEFORE the lock is acquired. If the duplicate-name check at line 293 rejects the backend, the defaults JSON is already on disk and never cleaned up until next boot's `sweep_orphan_defaults`. |
| 25 | `src/mcp_gateway/admin_routes_backend.py:313–316` | **`add_backend` returns `"ok": True` when `hot_add` returns `False`.** Config is persisted; backend is in the file but NOT actually mounted. The `"ok": True` response requires the caller to inspect `"reloaded": "mount-failed"` to discover this. |
| 26 | `tools/ci_receipts.py:82` | **`--output-dir` pointing to existing file → `FileExistsError` crash.** `Path.mkdir(parents=True, exist_ok=True)` raises `FileExistsError` when the path is an existing non-directory file (e.g., stale artifact). `exist_ok=True` only handles existing directories. Unhandled exception → raw traceback, exit 1. |
| 27 | `tools/ci_receipts.py:59–64` | **`scratch_root` glob silently fails on bad path.** `Path.glob()` internally catches `OSError` (including `NotADirectoryError`) silently. If `--scratch-root` points to a non-existent path or a file, `_first_report` returns `None` → all suites report `"not-produced"` — indistinguishable from a genuine empty results directory. Misspelled CI variables pass silently. |
| 28 | `src/mcp_gateway/admin_routes_codex.py:96–98` | **Double `ctx.load()` TOCTOU in `register_backend`.** Config is read once at line 96 for validation, then again at line 69 for URL building. Between them, config could be mutated by another concurrent request. |
| 29 | `src/mcp_gateway/admin_routes_claude.py:89,163` | **`register_backend` doesn't skip disabled backends; `reregister_all` does.** Behavior is inconsistent — you can individually register a disabled backend through the API, but `reregister_all` skips it. |
| 30 | `src/mcp_gateway/admin_routes_virtual.py:282,288` | **`test_virtual` has no serialization; concurrent calls race on `virtual_status` dict.** Two concurrent `POST .../test` calls write to the same dict entry. Last writer wins — no data corruption (GIL protects dict internals) but status can be silently overwritten. |
| 31 | `src/mcp_gateway/admin_routes_virtual.py:282,288,303,397–403` | **Disabled/deleted virtual tools never cleared from `virtual_status` hook (slow memory leak).** `disable_virtual` and `delete_virtual` don't remove entries from `ctx.hooks["virtual_status"]`. `list_virtual_tools` only iterates `cfg.virtual_tools`, so orphans are invisible — but the dict grows monotonically across many create/test/delete cycles. |
| 32 | `src/mcp_gateway/admin_routes_ops.py:109–113` | **`reintrospect` holds stale `Backend` across await boundary.** `ctx.load()` at line 109, then `await deps().refresh(ctx, b, …)` at line 113. Config could be mutated between them; the stale `b` could probe wrong transport or create orphan defaults. |
| 33 | `src/mcp_gateway/admin_routes_ops.py:142` | **Bare `except Exception` in `get_status` masks programming errors.** `AttributeError`, `TypeError` from internal code bugs are reported as backend "error" state with no traceback, making them hard to diagnose. |
| 34 | `src/mcp_gateway/server.py:84` | **`_last_recycle` dict never pruned.** Entries added on each recycle (line 916) but never removed on backend delete or rename. Grows monotonically over a long-lived daemon. Negligible practical impact (one 16-byte float per backend name) but violates the "no unbounded growth" daemon expectation. |
| 35 | `src/mcp_gateway/admin_routes_backend.py:338–339` | **Redundant back-end existence check is dead code.** `remove_backend` validates backend exists at line 323, then checks `len(cfg.backends) == before` at line 338. The second check can never fire. |
| 36 | `src/mcp_gateway/admin_routes_virtual.py:269` | **`test_virtual` `or` operator masks bad argument types.** `payload.get("arguments") or {}` coerces `[]`, `0`, `""`, `false` all to `{}`. A legitimate `arguments: {}` works, but wrong-type input is silently given empty args instead of a clear 400. |
| 37 | `src/mcp_gateway/config_loader.py:971–972` | **`ensure_config` writes seed config non-atomically.** Unlike `save()` (temp file + fsync + atomic rename), `ensure_config` writes directly to target. A crash during first-run seed could leave partial `config.toml`. Low probability (tiny file, only on first run). |

---

## 🔵 Minor (hygiene, dead code, doc mismatches)

### Source Code

| # | File:Line | Issue |
|---|-----------|-------|
| 38 | `src/mcp_gateway/__init__.py:1–3` | Stale docstring singles out "Claude Code" only; project is client-neutral. |
| 39 | `src/mcp_gateway/__init__.py:5–10` | `__version__` is dead code — never referenced anywhere. Canonical version is `metadata.gateway_version()`. |
| 40 | `src/mcp_gateway/__init__.py:10` | `__version__` fallback `"0.0.0.dev0"` vs `metadata.py` fallback `"1.0.0"` — two inconsistent fallback values. |
| 41 | `src/mcp_gateway/__init__.py` | No `__all__` — `from mcp_gateway import *` leaks `PackageNotFoundError`, `version`, `__version__`. |
| 42 | `src/mcp_gateway/__main__.py` | `--version` output differs between `python -m mcp_gateway --version` (bare version) and `mcp-gateway --version` (version + path). Intentional but documentable. |
| 43 | `src/mcp_gateway/runtime.py:31–38` | `from_legacy` shares mutable dicts by reference, contradicting "This module owns only objects whose lifetime is the running daemon." |
| 44 | `src/mcp_gateway/runtime.py:51–58` | `mount` silently overwrites existing entries — old proxy leaked without teardown. |
| 45 | `src/mcp_gateway/runtime.py:60–63` | `replace_transforms` doesn't check that `name` exists in `_proxies` — can create orphaned `_transform_holders` entry. |
| 46 | `src/mcp_gateway/runtime.py:40–43` | `proxies` property returns raw mutable dict under `Mapping` type hint — encapsulation leak. |
| 47 | `src/mcp_gateway/runtime.py:21` | `TransformHolder` type alias `list[Transform]` is too narrow — holders can also contain resource/prompt transform objects. |
| 48 | `src/mcp_gateway/server.py:283–297,457–471,513–524` | Three raw ASGI error responses (`BodyLimitMiddleware`, `BearerAuthMiddleware`, `OriginGuardMiddleware`) don't set `content-length` header. |
| 49 | `src/mcp_gateway/server.py:504–505` | `OriginGuardMiddleware` generates syntactically invalid `http://:::1:9100` for unbracketed IPv6 host — pollutes allowed-origin set. |
| 50 | `src/mcp_gateway/server.py:591` | `proxy._mcp_server.notification_options` uses private FastMCP API — single-point fragility across upstream renames. |
| 51 | `src/mcp_gateway/server.py:275–279` | `BodyLimitMiddleware.replay()` leaks `await receive()` after body is consumed — acknowledged limitation in comments. |
| 52 | `src/mcp_gateway/config_loader.py:1139–1141` | Duplicate `ToolOverride.original` within a backend silently overwritten (last wins). No validation in `_check_backends`. |
| 53 | `src/mcp_gateway/config_loader.py:1003` | Hardcoded 30s timeout for `headers_helper` — not configurable. Network-dependent helpers like `gh auth token` may time out. |
| 54 | `src/mcp_gateway/config_loader.py:106` | `load_secrets` quote-stripping may leave inner quotes in edge cases like `'"hello"'`. |
| 55 | `src/mcp_gateway/config_loader.py:45` | `_ENV_PATTERN` excludes POSIX-valid-but-rare env var names containing dots or hyphens. |
| 56 | `src/mcp_gateway/admin.py:862–868` | `_clean()` silently returns non-string values as-is — callers that assume `str | None` can crash (see serious finding #2). |
| 57 | `src/mcp_gateway/admin.py:573–575` | `_unmount` strips ALL routes with exact path match, not just `Mount` instances — would remove admin UI if backend named `"admin"`. |
| 58 | `src/mcp_gateway/admin.py:149–157` | `capture_defaults` bare `except Exception: pass` for list_resources/templates/prompts also swallows genuine transport errors. |
| 59 | `src/mcp_gateway/admin_routes_ops.py:19–28` | `AdminContext` Protocol missing `config_path` attribute — `admin_refresh` accesses it but Protocol doesn't declare it. |
| 60 | `src/mcp_gateway/admin_routes_ops.py:168–169` | `refresh_all` reads config once then fans out concurrently — different backends could hot-reload from different config versions. |
| 61 | `src/mcp_gateway/admin_routes_settings.py:144` | `put_instructions` with missing `"value"` key silently clears instructions via `_clean(None)` → `None`. API contract ambiguous. |
| 62 | `src/mcp_gateway/admin_routes_settings.py:90,112,134,149,198` | Inconsistent `"reloaded"` field across success responses — some have it, some don't. |
| 63 | `src/mcp_gateway/admin_routes_settings.py:142–146` | Missing `"backend"` key produces error message with literal `"unknown backend None"`. |
| 64 | `src/mcp_gateway/admin_routes_backend.py:77,104,124,134` | `always_load` toggle accepts `{"value": "false"}` as truthy — `bool("false")` is `True`. Intentional but surprising. |
| 65 | `src/mcp_gateway/admin_routes_backend.py:90` | `_apply_enabled` does redundant `ctx.load()` per already-mounted backend (N file reads). |
| 66 | `src/mcp_gateway/admin_routes_backend.py:268` | `add_backend` uses `.get()` at line 260 then `[]` at line 268 for `"name"` — inconsistent access pattern. |
| 67 | `src/mcp_gateway/admin_routes_backend.py:20–39` | Full `AdminContext` Protocol typed as `Any` — loses all static checking. |
| 68 | `src/mcp_gateway/admin_routes_claude.py:98` | Bearer token visible in `ps aux` when passed as `--header "Authorization: Bearer …"` to `claude mcp add`. |
| 69 | `src/mcp_gateway/admin_routes_claude.py:129` | `fresh` query param case-sensitive (`"True"`/`"TRUE"` don't trigger refresh). Needs `.lower()`. |
| 70 | `src/mcp_gateway/admin_routes_claude.py:131–132` | Cache key access inconsistent — `deps.cache.get("output")` (safe) but `deps.cache["ts"]` (direct index → potential `KeyError`). |
| 71 | `src/mcp_gateway/admin_routes_claude.py:162–189` | `reregister_all` runs N backends serially — worst case 60s per backend (remove + add). |
| 72 | `src/mcp_gateway/admin_routes_codex.py:73` | Codex registration URL scheme hardcoded as `http://` — would need `https://` if gateway ever binds non-loopback with TLS. |
| 73 | `src/mcp_gateway/admin_routes_codex.py:157–164` | `registrations` doesn't guard `ctx.load()` against `TOMLDecodeError`/`ValidationError` → 500 on corrupt config. |
| 74 | `src/mcp_gateway/admin_routes_virtual.py:317–319,326` | `activate_virtual` builds the virtual tool twice — once for validation, once in `_hot_replace`. |
| 75 | `src/mcp_gateway/admin_routes_virtual.py:127` | `virtual_catalog` uses `or {}` on nullable `defaults` — works but fragile. |
| 76 | `src/mcp_gateway/admin_cli.py:1` | Module named `admin_cli.py` but contains generic `run_cli`, not an admin CLI — name is misleading. |
| 77 | `src/mcp_gateway/admin_cli.py` | No direct test coverage — `ValueError` timeout/Unicode paths untested. |
| 78 | `src/mcp_gateway/virtual_tools.py:234–235` | Brittle JSON extraction from LLM response via `find("[")` / `rfind("]")` — inner brackets in quoted strings break it. |
| 79 | `src/mcp_gateway/metadata.py` | `admin.gateway_version()` is a redundant passthrough re-export — no value beyond indirection. |
| 80 | `src/mcp_gateway/hooks.py` | Imports `Any` from typing but never uses it. |

### Tests

| # | File:Line | Issue |
|---|-----------|-------|
| 81 | `tests/test_client_registration.py:10–14` | `is` identity checks on function objects are fragile — would break if an import becomes a wrapper. |
| 82 | `tests/test_client_registration.py:8–17` | `CC_REG_CACHE_TTL` and `CODEX_REG_CACHE_TTL` constants not checked in the re-export test. |
| 83 | `tests/test_logging.py` | Missing test for `_stop_runtime` locking edge case (concurrent configure → handler leak). |
| 84 | `tests/test_server.py` | OAuth-related tests don't restart the test app with fresh state between cases. |
| 85 | `tests/test_fastmcp_compat.py` | Some edge cases rely on library-level clamping behavior that is not tested (0 makes `max_results` clamp to 1). |
| 86 | `tests/conftest.py` | Some fixtures defined but not used by all test files that could benefit from them. |
| 87 | `tests/test_virtual_tools.py` | No test for consent fingerprint rollback on failed activation. |

### Tools

| # | File:Line | Issue |
|---|-----------|-------|
| 88 | `tools/ci_receipts.py:52–53` | `check_count` silently dropped when report structure is unexpected. |
| 89 | `tools/docs_links.py` | Link extraction may produce false positives/negatives in edge cases. |
| 90 | `tools/release_contract.py` | Version comparison edge cases not fully tested. |

### CI / Configuration

| # | File:Line | Issue |
|---|-----------|-------|
| 91 | `.github/workflows/check.yml` | `restore-keys` could match wrong cache from a different branch/job. |
| 92 | `.github/workflows/release-please.yml` | Both `checkout: 0` (partial) and `fetch-depth: 0` (complete) set — only one takes effect. |
| 93 | `config_loader.py` | `TOMLDecodeError` caught from `json.loads` — not actually a TOML decoder, but caught correctly. |
| 94 | `server.py` | Loopback binds hardcoded at `host:port` only — no Unix socket or non-loopback support. |
| 95 | Various `.py` files | 12 `# type: ignore` comments — most justified, a few could hide real issues. |
| 96 | Various `.py` files | 8 `TODO`/`FIXME` comments — all minor, no open severity concerns. |
| 97 | Various `.py` files | No duplicate-key detection in JSON parsing — `json.loads()` silently keeps last value on duplicates. |
| 98 | `src/mcp_gateway/server.py` | `_suppress_list_changed` relies on private FastMCP `_mcp_server` attribute. |
| 99 | `config.example.toml` vs `config_loader.py` | All config keys match — no mismatch found. |

### Documentation

| # | File | Issue |
|---|------|-------|
| 100 | `docs/installation.md` | `/health` path description says it shows the clone root; actually shows `<clone>/src/mcp_gateway/`. |
| 101 | `docs/installation.md` | `install.sh` uses `uv sync` without `--locked`; manual upgrade docs say `--locked`. |
| 102 | `docs/api.md` | All documented endpoints confirmed to exist; parameter accuracy verified — no mismatches found. |
| 103 | `docs/configuration.md` | All documented options verified against code — no mismatches found. |
| 104 | `docs/security.md` | Security claims verified against implementation — no discrepancies found. |
| 105 | `CHANGELOG.md` | Trailing whitespace-only line. |
| 106 | `README.md` | Feature descriptions match current behavior — no stale claims found. |
| 107 | `justfile` | All targets verified — no broken commands or incorrect dependencies. |
| 108 | `install.sh` | No platform detection bugs or privilege escalation issues found. |
| 109 | `.gitignore` | Covers all known generated/untracked files — no missing patterns found. |
| 110 | `verify_rename.py` | Root-level script confirmed as intentional verification tool; belongs in project root per ADR-0009. |

---

## ✅ Areas Verified Clean

The following areas were explicitly checked and found clean across all agents:

- **No security vulnerabilities** — no path traversal, command injection, SSRF, open redirect, or auth bypass (except finding #1, which requires operator misconfiguration)
- **No circular import risks** — all module imports are acyclic and correctly layered
- **No resource leaks** — HTTP sessions, subprocess pipes, and file handles properly managed via `AsyncExitStack` and `async with`
- **No integer overflow/float precision bugs** — all numeric operations are safe
- **No timezone/DST bugs** — all datetime operations use UTC or explicit timezone-aware objects
- **No catastrophic regex backtracking** — all regexes are structurally safe
- **No log injection or PII/secrets exposure in logs** — token values properly redacted; only env var names appear
- **No hardcoded absolute paths** — all paths resolve relative to config or well-known directories
- **No prototype pollution** — Pydantic models use `extra="forbid"` and strict validation
- **CI workflows** — all four workflows structurally correct; no missing steps or caching bugs
- **Cross-reference consistency** — docs accurately reflect implemented routes, config keys match code, operational procedures match runtime behavior

---

## Summary Statistics

| Severity | Count |
|----------|-------|
| 🔴 Serious | 9 |
| 🟡 Moderate | 28 |
| 🔵 Minor | 73 |
| ✅ Clean (no findings) | N/A (every agent found at least minor items or confirmed clean areas) |
| **Total agents** | **97** |
| **Total findings** | **~200** |

---

## Performance & Bottleneck Audit (2026-08-03)

**Scope:** 7 read-only agents (parent-model workers) covering all 21 source files in `src/mcp_gateway/` (8,758 lines), focused on bottlenecks, inefficiencies, throttles, and break-under-load — the performance dimension the 2026-07-27 audit only touched incidentally.
**Result:** 38 new findings — 9 high, 19 moderate, 10 low; plus a lock/container/timeout inventory. Numbering continues from the previous audit (111+); genuine overlap with prior findings is tagged.

### 🔴 High (event-loop stalls, unbounded fan-out, lock serialization)

| # | File:Line | Issue |
|---|-----------|-------|
| 111 | `config_loader.py:1028–1035`, `server.py:650–658`, `admin.py:132–142` | **headers_helper blocks the event loop up to 30s.** `subprocess.run(..., timeout=30)` runs synchronously inside async backend mount (`_mount_backend`) and `capture_defaults`. A hung helper stalls every request; each mount/introspection spawns a fresh process. |
| 112 | `config_loader.py:1651–1675`, `admin.py:1843–1855` | **Every mutation sync-writes the full config under the global lock on the event loop.** `commit` = `to_raw(load())` + `model_validate(model_dump())` + virtual-ref scan + `backup_config` + `dump_toml` + write/flush/fsync/rename/dir-fsync. Concurrent admin writes serialize behind disk+CPU work; slow filesystems stall all requests. |
| 113 | `admin.py:1831–1832`, `config_loader.py:1005–1013` | **No config cache: every `ctx.load()` re-reads, re-parses, and re-validates the entire TOML.** Dashboard polling and each admin request repeat disk I/O + full Pydantic rebuild on the event loop. |
| 114 | `admin_routes_backend.py:164–253, 82–144` | **Locked admin routes await live backend mounts.** `rename_backend` (incl. rollback), `enable_backend`, `enable_all` run `await add(...)`/`_apply_enabled` inside `ctx.locked`; a slow backend mount blocks every other mutation globally. |
| 115 | `virtual_tools.py:577–580` | **Unbounded fan-out: `asyncio.gather` over all selected members** with no concurrency cap or aggregate deadline; a large `dispatch=all` opens unbounded simultaneous downstream sessions, wall time set by the slowest member timeout. |
| 116 | `virtual_tools.py:454–468, 538–564` | **Quadratic budget accounting: the growing aggregate is fully JSON-encoded for every append and every pop.** `_json_size(result())` per fit-check, plus trim loops re-serialize after each pop — O(items² · size) CPU/allocation at scale; `used` not decremented during pops (stale `accounted_bytes`). |
| 117 | `hooks.py:123–151` | **Synchronous hooks run inline on the event loop.** Sync `validate`/`post_process` execute directly in the request path; CPU-heavy or blocking hooks stall all traffic; stages are strictly sequential around backend forwarding. |
| 118 | `admin_cli.py:23–30` | **Every registration request spawns a fresh CLI subprocess with no concurrency cap** (30s timeout, `capture_output` buffers all output in memory); concurrent admin calls queue executor jobs and spawn many CLIs. |
| 119 | `admin_routes_virtual.py:93–121`, `virtual_tools.py:130–142` | **`list_virtual_tools` fans out one live resolution per virtual tool, each resolving members serially** with stacked per-member timeouts — N·M sequential probes per page load, no endpoint-level deadline or concurrency cap. |

### 🟡 Moderate

| # | File:Line | Issue |
|---|-----------|-------|
| 120 | `server.py:1017–1025, 362, 921, 973` | Sync `config_loader.load` on the event loop in `/ready` and lifecycle paths; frequent probes stall unrelated requests. |
| 121 | `server.py:353–365` | `interval_loop` refreshes enabled backends strictly serially — one slow/down backend stretches the entire sweep (head-of-line blocking). |
| 122 | `server.py:947–950, 927–945` | Single recycle worker processes all recycles serially; a stuck teardown (polling + remount) delays unrelated backends; bounded queue (32) can fill and drop triggers. |
| 123 | `server.py:650–655, 682, 970–981` | No gateway timeout around backend `Client` context entry or mounted lifespan — hot-add can hang pending on `tg.start` indefinitely; runner occupied outside shutdown cancellation. |
| 124 | `admin.py:1635–1648` | `under_launchd()` runs a blocking `launchctl` subprocess (timeout=5) inline in async mutation handlers — event loop blocked up to 5s per request; only the restart itself is deferred to a BackgroundTask. |
| 125 | `admin.py:1745–1811, 1848–1851` | `_validate_active_virtual_references` re-reads each member backend's defaults JSON and rescans tool/params/override lists per member, per commit, under the lock — repeated file I/O + O(V·M·(tools+params)). |
| 126 | `admin.py:702–800, 1907–1908` | `get_state` reads every backend's defaults twice (build_state + `dangling_overrides`) plus an O(B²) backend lookup per dashboard refresh, before constructing all tool/resource/prompt rows. |
| 127 | `admin.py:142–157` | `capture_defaults` runs 4 sequential backend round-trips per backend (tools/resources/templates/prompts); slow backends stack probe time, and every re-inspect repeats it. |
| 128 | `admin.py:262–265, 269–273, 439–467, 1851–1853` | Sync defaults/backup file writes (`write_text`, `mkdir`, `glob`+`unlink`) and config dump/fsync in async refresh/mutation paths — disk latency and large captures block the event loop. |
| 129 | `config_loader.py:920–944, 659–663` | O(B²)/O(I²) duplicate detection via `list.count` inside comprehensions on every config load; large configs make every admin parse/commit progressively more expensive. |
| 130 | `admin_routes_backend.py:131–144` | `enable_all` applies backends serially under the global lock — N slow mounts/reloads stack as a sum while all other mutations wait. |
| 131 | `admin_routes_claude.py:137–155`, `admin_routes_codex.py:146–175` | Registration cache has no in-flight dedupe: concurrent fresh/expired GETs each spawn a CLI (thundering herd); TTL only prevents later calls. |
| 132 | `admin_routes_claude.py:157–222` | `reregister_all` runs serial remove+add per backend with no request-level deadline — worst case ~2·N·cli_timeout (extends prior #71). |
| 133 | `admin_routes_claude.py:52–79`, `admin_routes_codex.py:54–63, 146–175` | CLI stdout/stderr captured and cached with no byte cap; verbose diagnostics/list output grows memory per request and per cache entry. |
| 134 | `admin_routes_virtual.py:123–177` | `virtual_catalog` re-reads defaults JSON per configured backend and rebuilds all tools/parameters/overrides on every GET — N file reads under dashboard polling. |
| 135 | `admin_routes_logs.py:74–100` | Each SSE subscriber occupies a thread-pool worker indefinitely (`to_thread(get, timeout=1)` loop); enough idle streams starve unrelated `to_thread` work. |
| 136 | `admin_routes_logs.py:45–57`, `logging_setup.py:378–397` | Log tail scans and JSON-parses the entire log file per request despite returning only `limit` records — O(file size) per dashboard poll. |
| 137 | `logging_setup.py:140–149` | Every log event is formatted twice (file handler + subscriber publish), each `_JsonFormatter` pass parses/serializes JSON — duplicate work on the busy logging path. |
| 138 | `logging_setup.py:192–200` | Publication is O(active subscribers) per event, with silent drops after fixed 256-item subscriber queues fill. |
| 139 | `config_loader.py:1342, 1367, 1393`; `server.py:188` | **Gap:** gateway forwarding awaits downstream `call_next` without an explicit `asyncio.timeout` — a stuck backend can hold request/task indefinitely if transport timeouts don't fire. |

### 🔵 Low

| # | File:Line | Issue |
|---|-----------|-------|
| 140 | `server.py:258–281` | BodyLimit buffers the entire body (up to 64KiB each) before dispatch even for small requests; concurrent admin requests multiply retained memory. |
| 141 | `server.py:247–248, 438–455, 508–511` | Each middleware independently rebuilds a headers dict — up to 3 allocations/lookups per protected request. |
| 142 | `server.py:572–575, 898–902`, `oauth.py:168–179` | Every backend/virtual/endpoint unmount rebuilds the full route list — O(total routes) per teardown, amplified by hot remove/recycle churn. |
| 143 | `virtual_tools.py:190–194` | `routing_input_max_chars` slices only after all args are converted and joined — huge inputs still cost full CPU/memory despite the bound. |
| 144 | `virtual_tools.py:325–327` | Fresh `Client` session per member call; no session reuse across the fan-out (repeated connection/session lifecycle per backend call). |
| 145 | `virtual_tools.py:219–230` | Fresh `httpx.AsyncClient` per LLM-routed call — pooled connections discarded after one POST. |
| 146 | `virtual_tools.py:89–120, 310–321` | Linear scans of `cfg.backends`/`backend.tools` + `set` unions per member per request — scales with config size instead of indexed lookup. |
| 147 | `config_loader.py:94–142, 1065–1079` | Per-`${ENV}` value: regex search + `path.is_file()` + `stat()` on every proxy config build/remount (mtime cache avoids only the read). `_secrets_cache` (line 78) also never prunes entries across path churn. |
| 148 | `admin_routes_backend.py:255–305`, `admin_routes_codex.py:103–108` | Redundant config re-reads per request: `add_backend` loads twice (precheck + lock recheck), codex `register_backend` calls `ctx.load()` twice. |

### Inventory (cross-cutting sweep)

**Locks.** `admin.py:1829` `ctx.lock` — single global mutation lock, held across awaits via the generic `ctx.locked` wrapper (every wrapped handler awaits inside the lock; known hotspot #16 is one instance). `logging_setup.py:98/173/175` — threading locks, never held across awaits (flush can block 5s, prior #19). **No per-backend or per-resource locks exist — all mutations contend globally.**

**Bounded queues.** refresher stream (16), recycle queue (32), log subscriber queues (256). **Unbounded containers:** `config_loader.py:78` `_secrets_cache`, `hooks.py:56` `_module_cache`, `server.py:84` `_last_recycle` (prior #34), `admin.py:97` `_last_refresh`.

**Awaits without explicit gateway timeout:** `server.py:650–655, 682` (backend connect/lifespan), `server.py:188`, `config_loader.py:1342/1367/1393` (`call_next` forwarding).

### Gaps vs 2026-07-27 audit

The prior audit's performance coverage was incidental (#11, #16, #19, #31, #34, #65, #71). New surface found here: event-loop blocking via sync config/defaults/CLI work in async paths (111–113, 118, 124, 128), lock-held network awaits (114), fan-out scaling (115, 116, 119), thread-pool exhaustion (135), thundering-herd cache misses (131), timeout gaps (123, 139), and the lock/queue inventory. Only genuine overlap: 132 ≈ prior #71.

**Verified clean:** no `time.sleep`/sync subprocess in server.py, runtime.py, or virtual_tools.py; warm proxy path reuses connected clients (no per-call session on stateful backends); startup backend runners launch concurrently; bearer/origin sets resolved once at app construction; log subscriber queues bounded with unsubscribe in `finally`; ops status probes use per-backend `asyncio.timeout` + `asyncio.gather`; config validation bounds (32 route patterns / 256 chars) are sane.

**Summary:** 38 new findings — 9 🔴 high, 19 🟡 moderate, 10 🔵 low. Dominant themes: (1) synchronous disk/subprocess work on the event loop, (2) one global mutation lock held across network awaits, (3) unbounded/quadratic virtual-tool fan-out accounting, (4) no config caching, (5) missing explicit timeouts on downstream calls.

---

## Deep Breakage Audit (2026-08-03) — 50-agent swarm

**Scope:** 50 read-only parent-model agents covering every file in the repo — src/mcp_gateway/*.py (21 files, split into 24 slices incl. 8 cross-cutting sweeps), all 20 test files + test infrastructure, docs/, config files, CI workflows, skills, tools/, install/deploy scripts, release pipeline.
**Method:** each slice read in full; findings cross-verified by overlapping slices (the runner-race family alone was independently reported by 5 agents); a sample of Highs was re-verified against source by the orchestrator. Numbering continues from 149. Items already in sections 1–2 are tagged `overlap:<id>` and only re-listed when a materially new angle was found.
**Result:** ~370 raw findings → **173 deduplicated rows: 26 🔴 high, 117 🟡 moderate, 30 🔵 low**, plus verified-clean and fixed-since-prior-audit notes.

### 🔴 High

| # | File:Line | Issue |
|---|-----------|-------|
| 149 | `server.py:425,443`, `config_loader.py:64,924` | ✅ **DONE (2026-08-03): Auth bypass via prefix exemption.** Bearer authentication now exempts only the exact `/health` and `/ready` paths; regression coverage proves similarly prefixed backend routes still require a bearer token. (SrvA.1, Security.1) |
| 150 | `server.py:830–838, 928–944` | **Runner clobber race (5 independent confirmations).** Runners are keyed by name but teardown is fire-and-forget; when a re-mount lands before the old runner's finally, `stops.pop(name)` pops the NEW runner's event and `_unmount` strips the NEW route/registry → 404, /ready missing, zombie runner, no self-heal. The 7s grace deadline in `hot_recycle` makes this reachable today, not just by refactor. (Lifecycle.1, SrvB.4, Async.1, Retries.1, Locks.2) |
| 151 | `server.py:921–937` | **Recycle re-mounts a backend disabled/renamed after its config snapshot.** `hot_recycle` checks `enabled` once, may wait 7s, then unconditionally `tg.start(runner, b, cfg2)` with no re-check → a disabled backend keeps serving indefinitely. (SrvB.1, Locks.1) |
| 152 | `admin.py:262–273, 352–363`; `admin_routes_backend.py:197–200` | **Defaults files: non-atomic writes + unguarded reads brick the daemon.** `save_defaults` is plain `write_text` (no temp+rename) while unlocked refreshers write and every reader does bare `json.loads`. One truncated/corrupt `<name>.json` → boot crash (launchd crash-loop), 500s on /admin/api/state, export, every override save, and rename. (AdmA.1, Errors.3, AdmC.1, TOCTOU.3, AdmB.6) |
| 153 | `config_loader.py:1340–1348, 1366–1375, 1391–1401` | **Disabled backend leaks un-overridden resources/templates/prompts.** The `_backend_enabled` gate sits inside the `ov is not None` branch only — direct reads of un-overridden objects bypass it, contradicting the transform's own defense-in-depth contract (tools side is closed via `all_tools`; resources/prompts never are). |
| 154 | `admin.py:1848, 1875–1880`; `admin_routes_settings.py`; `admin_routes_backend.py:155–158` | **Pydantic `ValidationError` (a ValueError) escapes as HTTP 500 in the mutation path.** `commit` re-validates `model_validate(cfg.model_dump())`; `ctx.locked` catches only `ConfigError`. Reachable via: non-dict `override` values, malformed import bundles (`backends: []`, `dict(td)` on strings, `display_name: 42`), `set_display_name` non-string values, wrong-typed reset identifiers. A whole family of 500s instead of 400s. (CfgA.2, Settings.1, AdmB.1/2, Errors.1/2/4, ValEdges.1, ApiContract.1, BRoutes.6) |
| 155 | `admin_routes_backend.py:82–94, 116–144` | **enable_backend/enable_all claim `ok: True` when the live mount fails.** `_apply_enabled` discards `await add(b)`'s return; config is committed enabled, endpoint never mounted, no error signal, no retry (unlike `add_backend`'s "mount-failed"). (BRoutes.1, Locks.3, ApiContract.2, Retries.2, DocApi.1) |
| 156 | `admin_routes_backend.py:320–343` | **remove_backend never unmounts the live backend.** No `hooks["remove"]` call, only a launchd-only background restart; in dev/foreground the removed backend keeps serving `/name/mcp` indefinitely with config gone — add is hot, remove is restart-only. (Lifecycle.2, BRoutes.8, ApiContract.5) |
| 157 | `logging_setup.py:140–145` | **Log listener dies permanently on any write OSError (ENOSPC).** No try/except around `handler.flush()` in the flush-marker branch or in `QueueListener._monitor`; after death, records enqueue silently (dropped count stays 0), `flush()` no-ops, dashboard returns stale 200s — logging silently lost until restart. (empirically verified) |
| 158 | `logging_setup.py:150–157, 211–221` | **Shutdown deadlock on full log queue.** `enqueue_sentinel` drops the sentinel on `queue.Full` but `QueueListener.stop()` joins the thread with no timeout → `_stop_runtime`/`configure()`/`shutdown()` hang forever holding `_runtime_lock` (freezing flush/status); process never exits (launchd restart of a half-dead daemon). (Logging.2, Lifecycle.4, Tlog.3, Boot.3) |
| 159 | `logging_setup.py:288–304, 424–431` | **Exception tracebacks never reach the logs.** The structlog chain lacks `format_exc_info`; `log.exception(...)` writes `"exc_info": true` with no traceback (verified by repro) — every HTTP-500 diagnostic from RequestLogMiddleware is missing its cause. |
| 160 | `hooks.py:135–151` | **Hook failures discard successful backend results; hooks are unbounded.** `post_process` exceptions escape uncaught → the call is reported as failed and the backend's real answer is lost; sync hooks run inline on the loop with no timeout (a hang freezes everything); `validate` return values are dropped (fail-open for a `return False` guard) and kwargs are only shallow-copied. (Hooks.1/2/4, Tvirt.1/2, Retries.10) |
| 161 | `server.py:353–365, 973` | **Unguarded `config_loader.load` in a task-group child kills the daemon.** `interval_loop` (introspect_interval > 0) raises TOMLDecodeError/ValidationError out of the task on a mid-run config edit → anyio cancels the whole group → daemon exits; boot-time backup recovery doesn't cover runtime. Same unguarded load in `hot_add` and `refresh_and_reload`. (Lifecycle.3, SrvA.2, SrvB.2, Boot.7, Errors.5) |
| 162 | `server.py:1023` | **/ready returns 500 (not 503) on corrupt live config.** Unguarded `config_loader.load` per probe; monitors distinguishing 503-degraded from 500-broken get a false alarm, indistinguishable from a crash. (SrvA.3, SrvB.3, Boot.2) |
| 163 | `admin_routes_claude.py:145–154` | **Failed `claude mcp list` is cached as "nothing registered" for 60s.** rc≠0/rc=-1 output is stored with a fresh timestamp; response has no ok/error field, so a broken CLI is indistinguishable from an empty registry (codex sibling returns 502 and doesn't cache). (CCRoutes.1, Clients.1, Small.1, Retries.7, ApiContract.6) |
| 164 | `virtual_tools.py:603–604 vs 274–299` | ✅ **DONE (2026-08-03): Optional inputs are optional, not nullable.** Their property schema now retains the declared type without a `null` branch, matching runtime validation. (VTools.1, Tvirt.6) |
| 165 | `install.sh:27,186,204`; `deploy/com.void.mcp-gateway.plist.template:48–51` | **Fresh install yields a dead daemon.** `STATE_DIR` is never mkdir'd but the plist sends stdout/stderr logs into it; launchd won't create parents → job fails to spawn while install.sh exits 0 with "done". (OpsScripts.1, Boot.4) |
| 166 | `README.md`, `CHANGELOG.md`, `docs/releases.md`; PR #227 | **Release pipeline stuck; README advertises unreleased surface.** Release PR #227 (v1.1.0) has been OPEN and mergeable since 2026-07-19 (verified: state OPEN, merge CLEAN, label autorelease: pending) while only v1.0.0 is tagged; README documents Codex registration, virtual tools, OAuth, hooks — none of which exist in any release. (DocRoot.1/2) |
| 167 | `CHANGELOG.md:6` + release-please v17.6.0 changelog updater | **Release-please will misplace the 1.1.0 section.** Its header regex matches `## [Unreleased]`; the generated `## [1.1.0]` lands ABOVE the curated Unreleased prose (simulated on the real file), permanently mislabeling shipped work and never promoting the curated notes. (Release.1) |
| 168 | `.github/workflows/fastmcp-compat.yml:61,72–82` | **FastMCP canary can never pass.** The workspace is never `uv sync`'d and the candidate env's bin is never on PATH → `shutil.which("mcp-gateway")` is None → the entrypoint assertion fails on every run; `result=passed` is unreachable — permanent red noise. (CI.1) |
| 169 | `admin_routes_settings.py:199–201` | **Falsy `settings` bundle silently no-ops as success.** `bundle = payload.get("settings") or payload` → `{"settings": []}` imports the envelope, passes the isinstance guard, returns `200 {ok: true, backends: []}` — the UI believes an import succeeded. (Settings.2, AdmC.7, Async.7, TsrvB.1) |
| 170 | `admin_routes_virtual.py:297–308` | **Consent fingerprint persisted before activation confirmed; stale twins.** On resolution-failure 400 the hooks stores hold the NEW fingerprint while the persisted tool keeps the OLD one — `list_virtual_tools` shows two different fingerprints at once; `virtual_consent_fingerprints` is write-only (never read) and never cleaned on disable/delete. (VRoutes.3/4, Tvirt.4/5) |
| 171 | `tests/` vs `admin_routes_logs.py:74–100` | **SSE log-stream path has ZERO test coverage** (subscribe/unsubscribe/_publish/stream route) — exactly where the known #17/#18 bugs live. (CovGaps.1, Tlog.1) |
| 172 | `admin.py:456–473` | **`backup_config` untested and hostile-to-commit.** No test exercises commit→backup; a crash-recovery regression ships silently, and an unwritable BACKUP_DIR aborts EVERY admin save even when config itself could be written. (CovGaps.2, AdmC.5) |
| 173 | `admin_routes_backend.py:305–315`, `server.py:970–982` | **add_backend orphan-mount TOCTOU.** Lock is released right after commit, then `hot_add` mounts the payload backend unverified — a concurrent remove/rename between commit and mount strands an orphan route+proxy with no config entry (window is seconds-wide, network-bound). (TOCTOU.1) |
| 174 | `config_loader.py:874` | **No port range validation.** `port=0` binds ephemeral while registration URLs and OriginGuard allowed-origins are built from `cfg.port` (clients pointed at port 0, cross-origin blocked); `port=70000` crashes at bind after validation. (ValEdges.2) |

### 🟡 Moderate (deduplicated)

**Lifecycle/state races:** 175 `server.py:836–838` disable→re-enable window: hot_reload branch chosen on dying proxy, then old finally unmounts → enabled-but-unmounted (Locks.2). 176 `server.py:917–925` recycle cooldown stamped BEFORE absent/disabled check; skipped recycles extend cooldown (SrvB.5, Retries.3). 177 `fire_recycle` swallows WouldBlock — a full 32-slot queue of OTHER backends silently drops a backend's only heal trigger (SrvB.6). 178 `server.py:785–787` recycle queue not deduplicated — probe bursts enqueue copies (OpsLogs.4). 179 `admin_routes_backend.py:194–206, 228–232` rename: defaults migration after commit, unguarded; mid-rollback divergence (BRoutes.3). 180 `admin_routes_backend.py:218–243` renaming a DISABLED backend mounts it live (BRoutes.2). 181 `admin_routes_backend.py:82–94` enable with missing hooks (startup window) lies ok:True with no restart fallback (BRoutes.5). 182 `admin_routes_backend.py:96–113` set_stateless fire-and-forget recycle; response claims "recycled" while cooldown may skip it → live/config divergence (BRoutes.7, Locks.4, ApiContract.3, Retries.3). 183 `admin.py:407–418` `_last_refresh` read-check-write non-atomic → double-fire refreshers interleave same-file writes (TOCTOU.3).

**Hot-reload/commit integrity:** 184 `admin.py:1579–1595, 1843–1855` hot_reload mutates live proxy before the new transform set is fully built; a build error 500s AFTER save → config persisted, proxy half-transformed, no rollback (settings/backend routes; virtual routes alone re-commit) (AdmA.3, AdmC.2/6). 185 `admin.py:2035` `put_settings` validates active virtual references — a dangling virtual blocks unrelated token/log-level saves (AdmA.2). 186 `admin.py:1844–1857` commit's `before` diff never detects external writers; out-of-band config edits silently overwritten (TOCTOU.4). 187 `admin.py:460–463` backups stamped at 1s granularity + non-atomic → same-second commits lose restore points (Async.6, AdmA.5, AdmC.5).

**Defaults/import data:** 188 `admin.py:1435–1532` export/import round-trip broken for dangling overrides: export includes them, import rejects them → same-gateway import 400s (AdmB.5). 189 `admin.py:262–266` defaults read-side: `d.get("resources",[]) + d.get("resource_templates",[])` TypeErrors on null (AdmB.6); `virtual_catalog` bare `source["original"]` KeyErrors on old-format files (VRoutes.5). 190 `admin_routes_virtual.py:240,329,348,373` rollback `_hot_replace` unguarded → 500 after commit already reverted (VRoutes.1). 191 `admin_routes_virtual.py:282–288` `test_virtual` full-assign clobbers consent/last_dispatch keys written by setdefault (VRoutes.2).

**Config validation:** 192 `config_loader.py:941–949` unknown backend_id passes load (checks id-set, runtime matches name) → every dispatch ConfigErrors (CfgA.1). 193 `config_loader.py:915–963` duplicate override keys (tool/param/resource/prompt originals) last-wins silently; duplicate `(backend_id, tool_original)` members double-invoke a tool per dispatch (CfgB.5). 194 `config_loader.py:1666–1673` save() widens 600→644 perms (literal tokens in config) and replaces symlinks (CfgB.2). 195 `config_loader.py:1457–1473` transport-inapplicable keys silently erased on save; stdio+headers_helper is a silent no-op instead of ConfigError (CfgB.3). 196 `config_loader.py:1028–1038` helper UnicodeDecodeError escapes the ConfigError contract (CfgB.4). 197 `config_loader.py:1064–1079` empty-${VAR} only blocked for auth pair; headers/env/url silently expand empty (CfgA.4). 198 `config_loader.py:106–107` secrets read with locale encoding → UnicodeDecodeError on boot (CfgA.5). 199 `config_loader.py:843–851` bearer_token not held to single-${ENV} contract (CfgA.6). 200 `config_loader.py:1269–1298` prompt rename silently shadows a live backend prompt of the same name (CfgA.7). 201 `config_loader.py:610–616` router.api_key env existence never checked at load → silent fallback misrouting (CfgA.3). 202 `config_loader.py:501–514` `routing_input_text` dead code; live path silently truncates routing input — no rejection exists (Dead.1, ValEdges.4). 203 `admin.py:1992–1997` whitespace-padded ${TOKEN} passes validation, 401s everything at runtime (ValEdges.3). 204 `config_loader.py:525–548` NaN/Inf defaults (tomllib accepts) broadcast as invalid JSON in tools/list (ValEdges.6). 205 `config_loader.py:197,1203` max_result_chars unbounded vs protocol max (ValEdges.9, Skills.3). 206 `config_loader.py:631` descriptions/labels unbounded → inflated listings and per-call LLM egress prompts (ValEdges.5).

**Virtual tools:** 207 `virtual_tools.py:491–536` duplicate member labels alias via dict-equality — second member's content silently missing while `used` counts it (VTools.2). 208 `virtual_tools.py:538–543` force-trim loops decouple records from content — reported "omitted" members can still ship full text (VTools.3). 209 `virtual_tools.py:364–371` `_json_size` uses ensure_ascii=False while FastMCP serializes ensure_ascii=True — budget under-estimates non-ASCII up to ~3× (VTools.4). 210 `virtual_tools.py:545–558` compact-fallback accounting: omitted_count fixed at 1, details stripped, under-reporting mass drops (VTools.7). 211 `virtual_tools.py:233–252` LLM routing near-miss silently falls back to dispatch-ALL with no signal; partial label matches silently run fewer members (VTools.5/6). 212 `virtual_tools.py:197–252` router: no retry/backoff; default 3s timeout (LLM responses routinely exceed); transient failure = fan-out to every member (Retries.5/6). 213 `virtual_tools.py:648–651` stale-definition fallback serves renamed/deleted virtuals (VTools.8). 214 `virtual_tools.py:190–194` routing bound applied after full join (perf #143, break angle: selection changes past the cut) (ValEdges.4).

**Clients/registration:** 215 `claude_client.py:34–41` scope-blind parse: user-scope registrations report "registered" while routes mutate local scope (Clients.2). 216 `admin_routes_claude.py:180–181` nothing deregisters at disable time; gateway-<name> points at a 404 for the disabled period (CCRoutes.2). 217 `admin_routes_claude.py:103,182`; `codex_client.py` IPv6 loopback `::1` produces malformed `http://::1:port/...` registration URLs (CCRoutes.3). 218 `admin_routes_codex.py:151` fresh flag case-sensitive on codex too (CCRoutes.4). 219 `codex_client.py:62–77` literal bearer rejected; ${VAR} from gateway secrets file doesn't resolve in Codex's env → registration "succeeds" with 401s on every call (Security.3, Clients.3). 220 `admin_routes_claude.py:98`; `codex_client.py:79` CR/LF injection via header values into outbound requests and the `--header` argv (Security.5). 221 `admin.py:771–772` auth_value echoed to dashboard/export/backups; add_backend accepts literal secrets (Security.4, AdmB.7). 222 `admin_routes_claude.py:149` stderr concatenated into parse input — warning line containing `gateway-<name>:` false-positives as registered (CCRoutes.5). 223 `admin_routes_claude.py:214–221` reregister_all ok:true count:0 when everything disabled (CCRoutes.6). 224 `admin_routes_claude.py:98`; `admin_routes_codex.py:106` register success without liveness verification (CCRoutes.8). 225 `admin_routes_backend.py:260–284` add_backend SSRF surface (no scheme/host restriction) reachable by any bearer holder (Security.6).

**Ops/logging:** 226 `admin_routes_ops.py:133–151` probe still recycles on ANY exception (no is_session_death); cooldown stamped at start → a flapping backend is torn down + remounted every ~30s forever (OpsLogs.3). 227 `admin_routes_ops.py:64–65` `get_proxy([])` TypeError → 500 (OpsLogs.1). 228 `admin_routes_ops.py:76` `args or {}` executes tools with empty args on falsy non-dicts (OpsLogs.2, AdmC.8, ValEdges.7). 229 `admin_routes_ops.py:84` run_tool hardcodes 60s vs backend's up-to-300s contract (OpsLogs.7, Retries.9). 230 `admin_routes_logs.py:49–56` unconditional flush before tail; flush's two silent failure modes return STALE tail with 200 (OpsLogs.8, Logging.4). 231 `logging_setup.py:126–129` flush stacks two 5s waits (Retries.8). 232 `logging_setup.py:65,292` timestamp precision mismatch (µs vs ms) — misordered tails (Logging.5). 233 `logging_setup.py:359–403` read_tail ignores rotated backups — history gap at every rotation (Logging.7). 234 `admin_routes_ops.py:108–115` reintrospect unguarded refresh → 500, contradicts 502 contract; refresh_all gather lets one exception 500 the whole endpoint (OpsLogs.5/6). 235 `admin.py:1651–1676` restart_daemon BackgroundTask runs blocking launchctl (10s) on the event loop (AdmC.3). 236 `admin.py:1890` under_launchd 5s probe while HOLDING ctx.lock — every mutation stalls (AdmC.4, Locks.5). 237 `config_loader.py:888–892` introspect_interval silently clamped by 300s REFRESH_THROTTLE (Retries.4).

**OAuth:** 238 `oauth.py:105–116,164–170` duplicate RFC 9728 metadata registration (sub-app + root) — endpoints also serve metadata at unintended public URLs (OAuth.1). 239 `oauth.py:64–68` scope chars `"`/`,` interpolated unescaped into WWW-Authenticate challenge (OAuth.2). 240 `oauth.py:55–56` scope guard's user-class invariant brittle (OAuth.3). 241 `oauth.py:145–152` JWKS cache instance-scoped → cold key fetch per remount/recycle (OAuth.5). 242 `oauth.py:129–132` byte-exact origin match defeats browser-based OAuth discovery (OAuth.6).

**Tests/infra:** 243 `tests/live/run_virtual_tools.py:381–392` concurrency receipt margin 0.75s == member timeout — zero slack, flakes under load (Tinfra.4). 244 `tests/live/*` `_free_port()` TOCTOU with no retry — concurrent receipts collide (Tinfra.3). 245 `tests/fixtures/raw_wire_backend.py:72–76` ready-file before bind; gateway never retries failed mounts → permanent receipt failure (Tinfra.1). 246 `tests/conformance/run_official.py:168–184` TimeoutExpired loses captured logs; node grandchild orphaned (Tinfra.2). 247 `tests/test_server.py:579–586, 599–603` fake_mount binds 10 params vs real 12 (misbind tolerated); 2s timing loop flakes (TsrvA.1). 248 `tests/test_server.py:298–352` logging tests leave listener thread + structlog chain live for the whole session; exact root-handler count is env-dependent (TsrvA.2). 249 `tests/test_server.py:2478–2491` recycle-cooldown test asserts absence after fixed sleep — false-pass window (TsrvB.4). 250 `tests/test_admin.py:1708–1737` concurrency test margin 0.02s — flaky (Tadm.3). 251 `tests/test_server.py:2231–2275` interval-close test fails by HANG not assertion (TsrvB.8). 252 `tests/test_server.py:2032–2073` real bootout path never exercised (TsrvB.9). 253 `tests/test_config_loader.py` empty-env only pinned for `_required` variant; plain path (headers/env/url) unpinned (Tcfg.1); load() failure paths unpinned (Tcfg.2); ensure_config seeding unpinned (Tcfg.3); mtime-staleness window unpinned (Tcfg.8); atomicity indistinguishable (Tcfg.9). 254 `tests/test_oauth.py` no expired/bad-sig/wrong-audience/empty-scope tokens; admin-vs-MCP separation tested one direction only (Tlog.5/6). 255 `tests/test_tools_pagination.py` exact-multiple boundaries (50/100/0/>2 pages) untested (Tlog.7). 256 `tests/test_logging.py` subscriber path zero coverage (Tlog.1); rotation chain untested (Tlog.8). 257 `tests/test_virtual_tools.py` call_member failure/timeout status matrix untested (Tvirt.3); budget tests self-referential (`meta == budget` same object) (Tvirt.9). 258 `tests/test_hooks.py` post_process None/raise untested; `pytest.raises(Exception)` vacuous (Tvirt.1/10). 259 `tests/test_release_contract.py` duplicated version literals must bump in lockstep; live-checkout test goes red mid-bump (Tmisc.8/9). 260 `tests/test_release_hygiene.py` single-user macOS-only path check; `.env.example` contradiction (Tmisc.6/7). 261 `tests/test_package.py` + `test_release_contract.py` never build a real wheel — hatch artifact regression passes suite, wheel daemons crash (Tpkg.4/5). 262 `tests/test_conformance_runner.py` SCENARIOS/SPEC_VERSION never asserted; execution path uncovered (Tpkg.1). 263 `tests/test_fastmcp_compat.py` validates fastmcp seams in isolation, never the gateway's private-API couplings (Tpkg.2). 264 `tests/test_docs_links.py` checker never run against the real tree; docs-check not in CI (Tmisc.1).

**Docs/config/CI/release:** 265 `docs/installation.md:80–81`, `install.sh:257–258` health path claim always wrong (shows `<clone>/src/mcp_gateway`) (DocCfg.1). 266 `docs/configuration.md:13–14` "only non-default values stored" false — stateless/enabled always written (DocCfg.2). 267 `docs/configuration.md` name rules and required-hide enforced only in admin path, not the loader — hand-edited configs pass then break (DocCfg.5). 268 `docs/operations.md:106–109` "DEBUG enables library diagnostics" false — pinned to WARNING (DocOps.1). 269 `docs/operations.md` dashboard polling claim stale (SSE-driven) (DocOps.3); /ready `virtual` dimension undocumented (DocOps.4). 270 `docs/releases.md:30–31` footer-only BREAKING CHANGE claim overstates the title-gate (DocOps.5). 271 `config.example.toml:177,185–198` mandatory `egress_consent_fingerprint` absent from example AND docs; example shows `egress_acknowledged = true` implying sufficiency (CfgFiles.1). 272 `config.example.toml:72,177` virtual example hard-codes the commented-out backend UUID — uncommenting only virtual_tools fails load (CfgFiles.2). 273 `config.example.toml` missing loader fields: always_load/routing_input_max_chars/static_args/conditions/backend enabled/instructions (CfgFiles.5/6). 274 `pyproject.toml:42–46` direct imports (httpx/anyio/uvicorn/starlette/mcp) undeclared — only transitive via fastmcp extras; wheel METADATA omits them (Deps.1). 275 `pyproject.toml:54` dead dev dep `httpx2` fork + truststore (Deps.2). 276 `pyproject.toml:1–3` build backend unpinned (Deps.3). 277 `install.sh:171` uv sync without --locked (OpsScripts.6, DocCfg.6); plist PATH Apple-only (OpsScripts.4); `ln -sfn` nested-link trap on real dir (OpsScripts.5); plist written non-atomically (OpsScripts.7); install exits 0 without daemon verification (OpsScripts.8). 278 `verify_rename.py:75–79` no token support — fails 401 on any secured gateway, mislabeled "unreachable" (OpsScripts.2/3). 279 `.github/workflows/check.yml:52–56` PR-title gate skipped on push-to-main — non-conventional direct commits bypass release derivation (CI.3). 280 `.github/workflows/release-please.yml` offline lock-refresh can never succeed (cache disabled) — dead code path (CI.4). 281 `security.yml` first-push scans ENTIRE history with --fail — retroactive secret gate (CI.5). 282 `dependabot.yml` proposes fastmcp bumps that can never merge (exact-pin test) — weekly matrix burn (CI.6). 283 Release PR born red: uv.lock refresh commit lands after checks; release verified post-mint (tag+release exist before gate) (Release.3/4). 284 `tools/release_contract.py:86–101` `perf:`/`revert:` create patch releases the policy model says don't exist (Release.2). 285 `tools/release_contract.py:212–217` no subprocess timeouts; non-UTF-8 output crashes gate (Tools.9); sdist allows __pycache__ (Tools.10); changelog_version hard-fails on non-SemVer dated section (Tools.11). 286 `tools/ci_receipts.py:49–50,59–64` unhashable status crashes the bundle; lexicographic mkdtemp pick summarizes an arbitrary run (Tools.1/2); non-atomic receipt write (Tools.3). 287 `tools/docs_links.py` code-fence blind (phantom slugs, false links), root-relative links dropped, case-insensitive FS false-green, `)` truncation, duplicate-heading fragments (Tools.4–8, Tmisc.2/3). 288 `src/mcp_gateway/__init__.py:10` release-please's python updater regex can never match `.dev0` — file permanently stale post-release (Release.5). 289 `metadata.py:20–21` installed-wheel version wins over checkout fallback — /health lies under PYTHONPATH=src (Small.2/8). 290 `.agents/skills` vs `.claude/skills` copy divergence; stale references (mcp.md, 2KB claim, maxResultSizeChars, defaults-dir example, redacted dangling URIs) (Skills.1–7). 291 `docs/api.md` logs endpoints undocumented (DocApi.2); Content-Type convention unenforced (DocApi.3); cc-reregister-all 200s on bad scope vs documented 400 (DocApi.4).

### 🔵 Low (selected)

292 `server.py:445` GET /admin/ trailing-slash 401s in bearer mode (SrvA.5). 293 `server.py:1043–1070` auth evaluated AFTER body buffering — pre-auth work + 413-vs-401 split (SrvA.6). 294 `server.py:504–505` malformed `http://:::1:port` origin; browser at ::1 is 403'd (SrvA overlap:49). 295 `server.py:1025` `sorted(backend_runtime.proxies)` concurrent-mutation 500 (SrvA overlap:10; also Small.3 virtual paths). 296 `server.py:920–925` `_last_recycle` stamped before checks (SrvB.5). 297 `server.py:349–351` tools/list_changed drops unlogged on full 16-slot stream (SrvB.8). 298 `server.py:615–679` mount failure after route append leaves brief live window, no traceback (SrvB.9). 299 `runtime.py:31–58` from_legacy bidirectional aliasing; mount silently overwrites (Small.4/6). 300 `runtime.py:21` TransformHolder type lie at both production callsites (Small.7). 301 `admin_cli.py:27` `text=True` without errors="replace" — one bad byte loses all output (Small.5). 302 `hooks.py:97–99` stat race → non-HookError escapes, fails whole backend mount (Hooks.3). 303 `hooks.py:99–116` mtime-only cache staleness (same-tick edits) (Hooks.5, Tvirt.8). 304 `hooks.py:106–116` no sys.modules insertion — multi-file hook packages unsupported (Hooks.6). 305 `hooks.py:62–72` hooks dir resolved from CWD (Hooks.7). 306 `hooks.py:56,100–116` cache read-check-write unsynchronized — double exec under concurrent mounts (Hooks.8). 307 `admin_routes_virtual.py:130–156` catalog bare `["original"]` KeyError on old-format defaults (VRoutes.5). 308 `admin_routes_virtual.py:277–289` test_virtual 200-ok:false vs 400 error; missing ms field (VRoutes.6). 309 `virtual_tools.py:603–604` stale-definition fallback (VTools.8). 310 `admin.py:771–772` args echoed verbatim (AdmB.7). 311 `config_loader.py:330–331` stale Backend.enabled comment contradicts runtime unmount (DocCfg.4). 312 `logging_setup.py:81,301` ensure_ascii mismatch — mixed escaped/raw unicode in one file (Logging.6). 313 `admin_routes_logs.py:96` stream level-None AttributeError (known #18, still open). 314 `tests/test_server.py:1002–1003,1505–1507,1464–1475` near-vacuous assertions (TsrvA.3/5, TsrvB.2). 315 `tests/test_server.py:2615–2623` intermediate PUT response never asserted (TsrvB.3). 316 `tests/test_server.py:2809–2867` reset tests never verify defaults survive (TsrvB.6). 317 `tests/test_admin.py:872–940` dry-run 400 tests don't assert rollback — suite implies safety that doesn't exist (Tadm.1). 318 `tests/test_verify_rename.py:17–23` mutates module-global checks list (Tmisc.5). 319 `tests/fixtures/virtual_tools_backend.py:106–109` bool passes isinstance(int) — delay=true becomes 1s (Tinfra.6). 320 `tools/docs_links.py` fragment/dir-target gaps (Tmisc.2/3). 321 `.agents/skills/.../surface.py:446–456` redacted dangling-resource URIs defeat the diagnostic (Skills.7).

### Cross-cutting inventories

- **Lock-held network/subprocess awaits (break family 150/151/155/173, 236):** every backend name is keyed in a single `stops` dict that cannot represent two overlapping mounts — the structural enabler of the clobber races; `ctx.lock` guards config RMW but NOT the mount lifecycle, which is exactly the gap the races exploit (Locks.7).
- **Unbounded/leaky state (new):** `virtual_consent_fingerprints` write-only twin (VRoutes.4); `_last_refresh`/`_last_recycle` entries never pruned on remove (BRoutes.8, SrvB.5); hook `_module_cache`/`_secrets_cache` never pruned (inventory from prior section).
- **FIXED since prior audits (multi-agent verified):** #1 empty-${VAR} auth disable (expand_env_required), #2 set_instructions 500, #3 non-dict JSON 500, #4 reset KeyError 500, #5 post_import isinstance guard (now silent no-op, see 169), #6 reserved names now exact incl. admin/health/ready, #8 run_cli ValueError, #77 run_cli coverage, #25 add_backend "mount-failed" signal. **Still open from prior audits:** #7 (stale CC reg cache), #10, #11, #12, #15, #16, #17, #18, #19, #31, #34, #36, #65, #71, #83, #87, #89.
- **Clean areas verified across the swarm:** no auth bypass beyond 149 (tokens, OAuth JWT handling, OriginGuard); no path traversal/command injection (backend-name charset, list-argv, hooks spec confinement); config validation bounds mostly sane; `cl.save` atomic (temp+fsync+rename); route-table construction flat and ordered; conftest isolation sound; middleware auth ordering; SSE unsubscribe in finally; fastmcp argv contracts match real CLIs; ADR index consistent.

**Summary:** 26 🔴 high, 117 🟡 moderate, 30 🔵 low (173 rows, deduplicated from ~370 raw). Dominant breakage themes: (1) the backend-name-keyed runner lifecycle — four distinct races (150/151/155/173) with no per-name serialization; (2) non-atomic defaults files with unguarded reads (152) bricking boot and admin; (3) ValidationError escaping as 500 across the whole admin surface (154); (4) three independent logging subsystem failures that silently lose logs or hang shutdown (157–159); (5) a release pipeline stalled with the release PR open and README documenting unreleased features (166/167).

---

## MCP / FastMCP Protocol Compliance Audit (2026-08-03) — 5-agent swarm

**Scope:** 5 read-only agents match-checking the gateway against the **official MCP specification 2025-11-25** (fetched from modelcontextprotocol.io, local copies), with the installed SDK sources as ground truth for wire behavior (fastmcp 3.4.4, mcp SDK 1.28.1, both in .venv). Coverage: base protocol/lifecycle/transport/errors (C1), tools contract (C2), resources & prompts (C3), gateway-as-client (C4), advertised surface + conformance coverage (C5). ~76 contracts checked; behaviors verified by SDK-source reading and empirical introspection; key claims re-verified by the orchestrator. Numbering continues from 322.

**Verdict:** the gateway's core data paths are **broadly spec-compliant** — version negotiation, lifecycle gating, streamable-HTTP session management, tools/resources/prompts shapes, pagination, and error taxonomy are inherited from the SDK and check out. The violations cluster in four areas: (1) JSON-RPC error-code fidelity (code 0 instead of -32603/-32602), (2) missing client-side timeouts, (3) gateway-authored schema-vs-runtime contract breaks, (4) surface truthfulness (serverInfo.version, ui extension, logging). None of the violations are transport- or handshake-breaking; all are edge-path or identity issues.

### ❌ Violations (spec MUST/SHOULD contradicted)

| # | Area | Issue |
|---|------|-------|
| 322 | Error codes (C1.7) | **Unhandled handler exceptions answer JSON-RPC code 0, not -32603 INTERNAL_ERROR** — `ErrorData(code=0, message=str(err))` at mcp/server/lowlevel/server.py:794–805 (verified). Reachable via proxy ping against a down backend or a failing resources/read handler. Code 0 is not a defined JSON-RPC error. Inherited SDK; gateway has no mitigation (no ErrorHandlingMiddleware). |
| 323 | Prompts (C3.13) | **prompts/get for an unknown prompt → code 0** instead of the spec's SHOULD -32602. NotFoundError survives to the SDK session catch-all. |
| 324 | Prompts (C3.14) | **Missing required prompt arguments → code 0** instead of -32602. fastmcp wraps its ValueError in PromptError → same code-0 catch-all; the gateway adds no required-arg validation for proxied prompts, so conformance is backend-dependent. |
| 325 | Prompts (C3.15) | ✅ **DONE (2026-08-03): Prompt broadcast names remain unique.** The admin rename path checks the mounted backend's untransformed live prompt catalog outside the mutation lock, while the runtime transform independently rejects duplicate names if the backend catalog changes later. |
| 326 | Tools (C2.11) | ✅ **DONE (2026-08-03): Virtual input schema and runtime agree.** Optional no-default inputs may be omitted but explicitly supplied `null` is rejected by both the Draft 2020-12 schema and runtime validation. |
| 327 | Tools (C2.5) | ✅ **DONE (2026-08-03): Non-finite configuration values are rejected.** `VirtualInput.default`, `ParamOverride.default`, and member `static_args` reject NaN and infinities before schema generation or argument injection. |
| 328 | Client timeouts (C4.3) | **No timeout on the client initialize handshake.** `client_init_timeout=None` → `fail_after(None)`; `_mount_backend` awaits Client entry bare — a TCP-accept-but-never-respond backend hangs the mount forever (spec SHOULD timeouts on all requests, incl. initialize). |
| 329 | Client sessions (C4.8) | **On HTTP 404 for a session-id request the SDK never issues a fresh InitializeRequest** (spec MUST start a new session) — it injects error 32600 "Session terminated" and returns. Gateway's on_session_death recycle mitigates warm backends only; the failing request still errors. |
| 330 | Client timeouts (C4.12) | **Broadcast tool calls have NO timeout anywhere in the chain** (`read_timeout_seconds=None` → `fail_after(None)` in send_request). Only virtual members (30s), router (3s), capture (30s), and admin ops (60s) are bounded — a hung backend tool call can occupy a request indefinitely. (= #139/#123 family, now spec-classified.) |

### 🟡 Deviations / risky (⚠️ — no MUST broken, but fidelity or honesty gaps)

| # | Area | Issue |
|---|------|-------|
| 331 | JSON-RPC (C1.6) | Malformed JSON-RPC (missing id, bad jsonrpc) → -32602 instead of JSON-RPC 2.0's -32600 Invalid Request. |
| 332 | Cancellation (C1.8) | notifications/cancelled cancels the work but the SDK still answers with code-0 "Request cancelled" — spec SHOULD send no response; code 0 is undefined. |
| 333 | Transport (C1.11) | Second concurrent GET on a session → 409 Conflict, where the spec says GET MUST return SSE or 405. 429 not implemented (not required). |
| 334 | Stateless (C1.13/C5.14) | The backend `stateless` config flag only controls client-session reuse; the HTTP layer is always stateful. 2025-11-25 has no stateless server field (stateless lifecycle is 2026 draft), so no spec conflict — the name overpromises. |
| 335 | Identity (C1.14/C5.3) | **serverInfo.version reports fastmcp's version ("3.4.4") on every endpoint — the gateway's own version never appears** (fastmcp/server/server.py:417); `display_name` never surfaces in serverInfo either. Clients pinning implementation identity pin the wrong product. |
| 336 | OAuth (C1.15) | RFC 9728 metadata served correctly at the root well-known path AND redundantly inside each sub-app (`/{endpoint}/.well-known/...`) — extra nonstandard surface (= #238). |
| 337 | SSE (C1.16) | No SSE priming event / retry / Last-Event-ID replay (`event_store=None`) — missing but MAY-level for non-resumable servers. |
| 338 | Names (C2.6) | Gateway name policy is the OLD 2024 charset `[A-Za-z0-9_-]{1,64}` vs 2025-11-25 SHOULD (dots allowed, 1–128): rejects spec-valid names like `admin.tools.list`; stricter than the bundled SDK validator (mcp/shared/tool_name_validation.py:17). |
| 339 | Names (C2.7) | Loader never checks rename-vs-un-overridden-live collisions; duplicates silently deduped on the wire (`on_duplicate=warn`) for hand-edited configs. |
| 340 | Pagination (C2.8) | Offset-based base64 cursors are not stable across tool-list changes (spec SHOULD stable cursors); invalid-cursor -32602 is correct. |
| 341 | Tools (C2.10) | Unknown tool returns `CallToolResult(isError=true)` instead of the spec's -32602 protocol error (SDK swallows NotFoundError — inherited). |
| 342 | Progress (C2.12) | No progress notifications from gateway tool execution (virtual member calls are blind) — optional MAY, absent. |
| 343 | Resources (C3.2) | resources/list silently drops backend `size` and `annotations` (fastmcp Resource has no size field) — optional fields, silent data loss. |
| 344 | Resources (C3.3) | **mimeType fabricated as `text/plain` when the backend omitted it** (proxy.py:282, 379) — mislabels binary content the backend intentionally left untyped. |
| 345 | Resources (C3.5) | Read-side RFC 6570 is a hand-rolled subset (`{var}`, `{var*}`, `{?a,b}`): templates with level-2+ operators broadcast fine but can never be read (-32002). |
| 346 | Resources (C3.8) | Read lookup is exact equality on pydantic-normalized AnyUrl (`file://a.txt` → `file://a.txt/`) — hand-typed variants 404 for existing resources. |
| 347 | Resources (C3.9) | A hidden resource is still served when a matching backend template exists (get_resource None falls through to the template path) — the hide contract leaks. |
| 348 | Prompts (C3.16) | Prompt rename charset `[A-Za-z0-9_-]{1,64}` stricter than spec (which defines none) — spec-valid names with spaces rejected by the admin path. |
| 349 | Client (C4.9) | Stateless backends + virtual members pay a full initialize handshake per call (spec-compliant, but = #144's churn). |
| 350 | Client (C4.13) | Client read-timeout raises a code-408 McpError without sending notifications/cancelled for the abandoned request. |
| 351 | Surface (C5.6) | Virtual endpoint advertises prompts/resources/logging capabilities but can only ever serve tools (fastmcp registers all handlers unconditionally). |
| 352 | Surface (C5.8) | `logging:{}` advertised on every endpoint while the gateway itself emits **zero** MCP log notifications — only remote backend logs are forwarded. |
| 353 | Surface (C5.9) | `extensions: {io.modelcontextprotocol/ui: {}}` and `experimental: {}` advertised with no implementation behind them and no config hook to disable. |

### Missing features (all optional — absent is spec-legal)

354 completion/complete → -32601 (correctly not advertised). 355 elicitation (client feature, absent). 356 sampling/roots (client capabilities, empty — correctly not declared). 357 SSE priming/resumability (C1.16). 358 progress from gateway tool calls (C2.12).

### Conformance coverage (C5)

The CI mcp-contract smoke runs **4 of the official suite's ~31 server scenarios** (server-initialize, ping, tools-list, dns-rebinding-protection) against one stdio fixture with a single tool. **Zero automated coverage** for: all tools/call variants (text/image/audio/mixed/embedded-resource/error/progress/logging/sampling), resources (list/read/templates/subscribe), prompts (get/args/image), completion, logging/setLevel, error codes (-32601/-32602/-32002/-32603), SSE stream/polling, session headers (missing/invalid MCP-Session-Id, DELETE), pagination beyond page 1, JSON-Schema 2020-12 dialect, all 10 OAuth scenarios, and initialize-response CONTENT (capability truthfulness, serverInfo identity — i.e. every ⚠️ in this section is untested by the current gate). The synthetic wire-schema check passes only because ServerCapabilities tolerates the extra `extensions`/`experimental` keys.

**Summary:** 76 contracts checked → **9 ❌ violations, 23 ⚠️ deviations, 5 optional-missing, rest ✅**. The gateway's own authored violations (322/325/326/327) are the ones worth fixing first — they're config/user-reachable; the SDK-inherited ones (328–330, 322–324) need a fastmcp/mcp-SDK upgrade or gateway-side middleware. The current CI conformance gate would not catch ANY of them.
