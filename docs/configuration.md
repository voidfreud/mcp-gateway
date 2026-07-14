# Configuration reference

The gateway is configured by a single file, `config.toml`. The admin UI reads and
writes this file for you, so you rarely need to edit it by hand — but you can, and
this page is the complete reference.

A few things to know first:

- **The admin UI owns this file.** Every UI save regenerates it. Comments you add
  by hand are **not** preserved on the next UI save. The annotated template
  `config.example.toml` in the repo keeps a commented copy of the schema for
  reference.
- **Only non-default values are stored.** The UI writes an override only when it
  actually differs from the backend's original, so the file stays small.
- **Where it lives** depends on how you installed — see
  [installation.md](installation.md#where-the-config-lives). It is auto-created
  from a working default on first run.
- **Secrets never go in this file.** Use `${ENV_VAR}` references; see
  [Secrets](#secrets) below.

## File shape

```toml
host = "127.0.0.1"
port = 9100
log_file = "~/.local/state/mcp-gateway/gateway.log"
# introspect_interval = 0
# bearer_token = "${MCP_GATEWAY_TOKEN}"

[[backends]]
name = "exa"
transport = "http"
url = "https://your-exa-endpoint/mcp"
auth_header = "Authorization"
auth_value = "Bearer ${EXA_TOKEN}"
stateless = true

  [[backends.tools]]
  original = "web_search_exa"
  name = "web_search"
  description = "What Claude should read for this tool."
  enabled = true

    [[backends.tools.params]]
    original = "query"
    description = "What Claude should read for this parameter."

    [[backends.tools.params]]
    original = "internal_flag"
    hide = true
```

`[[backends]]` repeats once per backend; `[[backends.tools]]` repeats per tool
you override; `[[backends.tools.params]]` repeats per parameter you override. You
only add tool and param blocks for the tools and params you actually change.
Backends that broadcast **resources** or **prompts** can have those rewritten
too — `[[backends.resources]]` and `[[backends.prompts]]` blocks, same
diff-vs-default model (see below).

## Gateway settings (top level)

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `host` | string | `"127.0.0.1"` | The address to bind. Keep it loopback (`127.0.0.1`). Never bind `0.0.0.0` — that would expose the gateway off-machine. |
| `port` | integer | `9100` | The port the gateway listens on. |
| `log_file` | string | `"~/.local/state/mcp-gateway/gateway.log"` | Where the structured log is written. Rotates automatically (5 MB × 5 files). |
| `introspect_interval` | integer (seconds) | `0` (off) | How often to re-scan every backend's tool list on a timer. `0` means off, which is the recommended default — the gateway already refreshes on reconnect, on a backend's own change notification, and on admin page load. Set an interval only for a long-lived remote backend that silently swaps its tools. |
| `bearer_token` | string or unset | unset | Optional access token. When set (as a `${ENV}` reference), every backend endpoint **and** the admin API require `Authorization: Bearer <token>`. See [security.md](security.md#the-optional-bearer-token). |
| `backends` | list | required | One `[[backends]]` block per backend. At least one is required. |

## Backend settings (`[[backends]]`)

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `name` | string | required | The backend's identity. Drives the `/<name>/mcp` endpoint, the config key, the captured-defaults file, and the `gateway-<name>` Claude Code registration. Letters, digits, `_`, `-`, up to 64 characters. Changing it is a rename (restart + re-register). |
| `display_name` | string or unset | unset | Cosmetic label shown in the admin UI only. Does not affect routing, the endpoint, or registration. Empty falls back to `name`. |
| `transport` | `"http"` \| `"streamable-http"` \| `"sse"` \| `"stdio"` | required | How to reach the backend. `http` and `streamable-http` are the same modern remote transport; `sse` is the legacy remote transport; `stdio` runs a local command. |
| `url` | string or unset | unset | The backend's URL. **Required for `http`/`streamable-http`/`sse`.** May contain `${ENV}` references. |
| `auth_header` | string or unset | unset | Name of an auth header to send (for example `Authorization`). Set **both** `auth_header` and `auth_value` or neither. |
| `auth_value` | string or unset | unset | Value for that header (for example `Bearer ${EXA_TOKEN}`). Use `${ENV}` — never a raw secret. |
| `headers` | table of string→string | empty | Extra static headers to send. Values may use `${ENV}`. Merged with `auth_header`/`auth_value` (the pair wins on a name clash). |
| `auth` | `"oauth"` or unset | unset | Set to `"oauth"` for an OAuth-protected remote MCP. The gateway runs the browser consent flow on first connect and caches the tokens. |
| `headers_helper` | string or list of strings, or unset | unset | A command that prints a JSON object of headers, for tokens computed at connect time. A **list** is run as arguments with no shell (safe). A **string** is run through the shell (for `$()`/pipes) and carries full shell privilege. Runs **once** when the backend connects, not per call and not on a timer — good for a token valid for the daemon's uptime (e.g. `gh auth token`), not one that must rotate mid-session. |
| `command` | string or unset | unset | The program to run for a `stdio` backend. **Required for `stdio`.** |
| `args` | list of strings | empty | Arguments for the `stdio` `command`. |
| `env` | table of string→string | empty | Environment variables for the `stdio` process. Values may use `${ENV}`. |
| `stateless` | boolean | `false` | Session strategy. `false` (**warm**, the default and what the UI's import uses) keeps one persistent connection — much faster, and the gateway automatically reconnects it if it dies (at most one repair per 30s). `true` opens a fresh session per request — a fallback for backends whose sessions misbehave when held. Toggleable live per backend in the admin UI. |
| `always_load` | boolean | `false` | Pin **all** of this backend's tools to load upfront (eager), instead of Claude Code's default deferred loading. |
| `enabled` | boolean | `true` | Whether the backend is broadcast at all. `false` disables every tool, drops its server instructions, and unmounts the endpoint. Toggles live in the admin UI without a restart. |
| `instructions` | string or unset | unset | Overrides the backend's server-level instructions (the always-loaded blurb Claude reads at connect). Unset inherits the backend's captured original. Set it even when the backend sends none, to add your own. Capped at Claude Code's ~2KB budget. |
| `tools` | list | empty | One `[[backends.tools]]` block per tool you override. |
| `resources` | list | empty | One `[[backends.resources]]` block per resource or resource template you override. |
| `prompts` | list | empty | One `[[backends.prompts]]` block per prompt you override. |

## Tool overrides (`[[backends.tools]]`)

Each block rewrites one of the backend's tools. Any field you omit keeps the
backend's original.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `original` | string | required | The backend's own (real) name for the tool. This is the key that ties the override to the tool; it is never changed. |
| `name` | string or unset | unset | The broadcast name Claude sees. Must be a valid identifier and unique within the backend. Omit to keep the original. |
| `title` | string or unset | unset | A human-readable display title. |
| `description` | string or unset | unset | The description Claude reads to decide when and how to call the tool. |
| `enabled` | boolean | `true` | `false` drops the tool from the listing entirely. |
| `always_load` | boolean | `false` | Pin this one tool to load upfront (eager). |
| `validate` | string or unset | unset | A behavior hook, `module:function` (see [Behavior hooks](#behavior-hooks-validate--post_process)). Runs before every call; raise `ValueError("why")` to reject it. |
| `post_process` | string or unset | unset | A behavior hook, `module:function`. Runs on every result before the caller sees it. |
| `params` | list | empty | One `[[backends.tools.params]]` block per parameter you override. |

## Behavior hooks (`validate` / `post_process`)

Hooks are small, hand-authored Python functions that run inside the gateway
around a tool call — input validation before the backend sees the call, and
result post-processing (truncate, strip noise, reformat) before the caller
sees the answer. They are the escape hatch for the cases text rewriting can't
reach, without forking the backend.

> **This is arbitrary code execution in the daemon, by design.** Hooks run
> with the gateway's full privileges — the same trust level as a stdio
> backend `command`. Read the [security guide](security.md#behavior-hooks-run-your-code)
> before using them.

**Where hooks live.** A dedicated hooks directory, resolved in this order
(mirroring the config precedence): the `MCP_GATEWAY_HOOKS` env var, a
repo-local `./hooks/` directory (dev checkout), else
`~/.config/mcp-gateway/hooks/`. A hook spec `myhooks:check_query` means "the
function `check_query` in `<hooks_dir>/myhooks.py`". The module part must be a
bare identifier — no paths, so a spec can never reach outside the hooks dir —
and the string is imported, never evaluated.

**The contract** (sync or async, both supported):

```python
# ~/.config/mcp-gateway/hooks/myhooks.py

def check_query(args: dict) -> None:
    """validate: runs BEFORE the call is forwarded. Raise ValueError to
    reject; the message is returned to the caller as the tool error."""
    if len(args.get("query", "")) > 500:
        raise ValueError("query too long (max 500 chars)")

def trim_output(result):
    """post_process: runs AFTER the backend answered. `result` is a FastMCP
    ToolResult; return it (mutated or copied), or any plain value."""
    for block in result.content:
        if getattr(block, "text", None):
            block.text = block.text[:4000]
    return result
```

**Argument names.** `validate` receives the **exposed** arguments — the names
after your renames, with hidden parameters absent — i.e. exactly what the
caller sent (plus schema defaults). That way a rejection message can reference
the same names the caller used. The gateway still reverse-maps renames and
injects hidden defaults before the backend sees the call.

**Error handling.** A hook that fails to *load* (missing file or function,
import error, malformed spec) never takes the backend's mount down and never
fails open: the tool stays broadcast, but every call to it returns a clear
error naming the load failure until the hook is fixed. The structured log gets
a `hook_load_error` line and the admin state shows the same error per tool
(`hook_error`). A malformed spec *string* is rejected at config load. Hook
files are re-checked on every transform rebuild (mtime-cached), so fixing the
file heals the tool without a restart.

Hooks are hand-authored in `config.toml` — the admin UI displays them
read-only and preserves them across saves. They are **not** part of the
settings export/import bundle (a hook spec references code on *this*
machine); note that a replace-mode import or a per-tool reset clears the
whole override entry, hooks included.

## Parameter overrides (`[[backends.tools.params]]`)

Each block rewrites (or hides) one parameter of a tool.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `original` | string | required | The backend's own (real) name for the parameter. The key that ties the override to the param; never changed. |
| `name` | string or unset | unset | The parameter name Claude sees. Omit to keep the original. |
| `description` | string or unset | unset | What Claude reads about the parameter. |
| `hide` | boolean | `false` | Remove the parameter from the schema Claude sees. A **required** parameter can only be hidden if you also set `default` (below); otherwise the save is rejected. |
| `default` | string, number, or boolean, or unset | unset | A fixed value the gateway injects into every call to the backend. Scalars only. Setting it makes hiding a required parameter safe: Claude never sees the parameter, and the backend always receives this value. An optional parameter may take a `default` without being hidden. |

## Resource overrides (`[[backends.resources]]`)

Each block rewrites the display text of one resource **or resource template**.
The `uri` is the identity Claude reads by — it is never rewritten.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `uri` | string | required | The resource's URI (or a template's `uriTemplate`). The key that ties the override to the resource; never changed. |
| `name` | string or unset | unset | The display name Claude sees. Free-form text (resources have no identifier charset). |
| `title` | string or unset | unset | A human-readable display title. |
| `description` | string or unset | unset | The description Claude reads. |
| `enabled` | boolean | `true` | `false` drops the resource from the listing **and** blocks reads through the gateway. |

## Prompt overrides (`[[backends.prompts]]`)

Each block rewrites one of the backend's prompts. Renames are real: Claude sees
the new name and a `prompts/get` for it is forwarded to the backend under its
original name.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `original` | string | required | The backend's own (real) name for the prompt. Never changed. |
| `name` | string or unset | unset | The broadcast name Claude sees. Must be a valid identifier and unique within the backend's prompts. |
| `title` | string or unset | unset | A human-readable display title. |
| `description` | string or unset | unset | The description Claude reads. |
| `enabled` | boolean | `true` | `false` drops the prompt from the listing and blocks `prompts/get`. |
| `args` | list | empty | One `[[backends.prompts.args]]` block per argument whose **description** you override. Argument *names* are not renameable — the call forwards the arguments to the backend verbatim. Each block: `original` (the argument name) + `description`. |

## Secrets

Secrets are never written into `config.toml`. Instead you write a reference —
`${ENV_VAR}` — and the gateway resolves it at startup. If a referenced variable
is missing, the gateway fails loudly at startup rather than sending an empty
credential.

Values are supplied one of two ways:

1. **The environment.** Any variable already in the daemon's environment.
2. **The gateway secrets file** at `~/.config/mcp-gateway/secrets.env` — one
   `KEY=VALUE` per line (blank lines, `#` comments, and a leading `export ` are
   tolerated; surrounding quotes are stripped). This is convenient because the
   login service otherwise runs with a minimal environment. Override the path with
   the `MCP_GATEWAY_SECRETS` environment variable.

The environment wins over the secrets file on a conflict. A freshly-edited
secrets file is picked up without a restart (the gateway notices the file
changed).

Put **only** the tokens the gateway's own backends need in the secrets file, and
never point it at a global key store: values from this file are deliberately kept
out of the process environment, so a local `stdio` backend's subprocess cannot
read another backend's secrets — but every backend's auth references resolve from
the same file.

Because the config holds only references and public endpoints, it is safe to
commit or share. See [security.md](security.md#secrets-handling) for more.

## Related

- [admin-guide.md](admin-guide.md) — editing these values in the UI.
- [operations.md](operations.md) — where backups and captured defaults live.
- [security.md](security.md) — the bearer token and secrets in depth.
