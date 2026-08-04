"""Shared plumbing for the scriptable mcp-gateway control CLI.

Owns the Admin API HTTP client, the per-invocation context, the JSON
input helper, the terminal-safe human-text renderer, and the
destructive-operation guard. Domain command modules import ONLY the names
here (plus argparse) so the HTTP/auth/JSON surface has exactly one
implementation. Stdlib only — no new dependencies.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO, cast


class CLIError(RuntimeError):
    """A user-facing CLI failure: printed as ``error: <message>`` on stderr.

    ``response`` carries the server's parsed JSON body when the failure came
    from an HTTP error response, and is ``None`` for transport errors or when
    the error body was not JSON. Handlers may inspect it for structured
    error payloads (e.g. Virtual Tool validation results) without
    re-parsing, and it is never printed.
    """

    def __init__(self, message: str, *, response: object = None) -> None:
        super().__init__(message)
        self.response = response


_CONTROL_REPLACEMENTS = {
    0x00: "\\0",
    0x07: "\\a",
    0x08: "\\b",
    0x09: "\\t",
    0x0A: "\\n",
    0x0B: "\\v",
    0x0C: "\\f",
    0x0D: "\\r",
    0x1B: "\\x1b",
}


def safe_human_text(value: object) -> str:
    """Render *value* as terminal-safe text for human output.

    Every C0/C1 control character (including ESC, CR, embedded LF, BEL, and
    the C1 CSI/OSC/ST terminators) and DEL becomes a visible ``\\xNN``-style
    escape, so attacker-controlled backend metadata, log content, or error
    text cannot forge terminal sequences or extra output lines. Ordinary
    Unicode passes through unchanged. JSON output must NOT use this —
    ``json.dumps`` already escapes control characters.
    """
    text = str(value)
    if not any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in text):
        return text
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if code < 0x20 or 0x7F <= code <= 0x9F:
            out.append(_CONTROL_REPLACEMENTS.get(code, f"\\x{code:02x}"))
        else:
            out.append(ch)
    return "".join(out)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse every HTTP redirect: Authorization must never be forwarded."""

    def redirect_request(  # noqa: PLR0913, PLR0917 - fixed urllib hook signature
        self,
        req,
        fp,
        code,
        msg,
        headers,
        newurl,  # noqa: ANN001
    ):
        raise CLIError(
            f"gateway refused redirect ({code}) for "
            f"{req.get_method()} {req.full_url} -> {newurl}"
        )


def _verified_loopback(host: str) -> bool:
    """True when *host* is provably loopback: ``localhost`` or a loopback IP.

    Hostnames that merely RESOLVE to loopback do not qualify — DNS is not
    consulted, so a name the attacker controls can never pass.
    """
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class AdminClient:
    """Authenticated JSON client for the Admin API.

    One instance per invocation, shared by every command handler through
    :attr:`CLIContext.client`. Token secrets stay in memory only — they are
    never logged, printed, or accepted from argv. The base URL is validated
    and canonicalized at construction (CLI-CLEARTEXT-AUTH-002): only
    http/https origins without userinfo or a path are accepted, a bearer
    token is refused over plain http to any host that is not verified
    loopback, and redirects are refused entirely so Authorization can never
    be forwarded to another origin.
    """

    def __init__(self, base_url: str, token: str | None, timeout: float = 30.0) -> None:
        self.base_url, self._scheme, self._host = self._validate_url(base_url)
        if (
            token is not None
            and self._scheme == "http"
            and not _verified_loopback(self._host)
        ):
            raise CLIError(
                f"refusing to send a bearer token over http to {self._host!r}: "
                "use an https URL or a verified loopback host "
                "(localhost, 127/8, ::1)"
            )
        self.token = token
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirectHandler)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _validate_url(base_url: str) -> tuple[str, str, str]:
        """Parse and canonicalize the base URL, rejecting unsafe shapes."""
        try:
            parts = urllib.parse.urlsplit(base_url)
        except ValueError as exc:
            raise CLIError(f"invalid gateway URL {base_url!r}: {exc}") from None
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https"):
            raise CLIError(
                f"unsupported gateway URL scheme {parts.scheme!r} (use http or https)"
            )
        if parts.username is not None or parts.password is not None:
            raise CLIError("gateway URL must not contain userinfo (user:pass@)")
        host = parts.hostname
        if not host:
            raise CLIError(f"gateway URL is missing a host: {base_url!r}")
        if parts.path not in ("", "/"):
            raise CLIError("gateway URL must be an origin without a path")
        if parts.query or parts.fragment:
            raise CLIError("gateway URL must not contain a query or fragment")
        canonical = f"{scheme}://{parts.netloc}"
        return canonical, scheme, host

    def _url(self, path: str, params: Mapping[str, object] | None) -> str:
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: str(v) for k, v in params.items()}, doseq=True
            )
        return url

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _error_from_http(self, exc: urllib.error.HTTPError) -> CLIError:
        body: object = None
        detail = ""
        try:
            raw = exc.read()
        except OSError:
            raw = b""
        if raw:
            try:
                body = json.loads(raw)
            except ValueError:
                body = None
            if isinstance(body, dict) and body.get("error"):
                detail = f": {body['error']}"
        return CLIError(
            f"gateway {exc.code} {exc.reason} for {exc.url}{detail}",
            response=body,
        )

    # -- public API --------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: object | None = None,
        params: Mapping[str, object] | None = None,
    ) -> object:
        """Perform one authenticated JSON API call.

        Returns the parsed JSON body (``None`` for an empty body). Raises
        :class:`CLIError` on transport failures, redirects, and any HTTP
        error status; ``CLIError.response`` then carries the parsed error
        body when the server returned JSON.
        """
        data = None
        headers = self._headers()
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self._url(path, params), data=data, headers=headers, method=method
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise self._error_from_http(exc) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CLIError(
                f"could not reach the gateway at {self.base_url}: {exc}"
            ) from None
        if not body:
            return None
        try:
            return json.loads(body)
        except ValueError as exc:
            raise CLIError(
                f"gateway returned invalid JSON from {method} {path}: {exc}"
            ) from None

    def stream(
        self,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
    ) -> Iterator[str]:
        """Iterate raw text lines of a streaming endpoint (``logs follow``).

        The response is consumed lazily. Connection, redirect, and HTTP
        errors raise :class:`CLIError` when the stream starts; the socket
        timeout applies per read, so keepalive-padded streams stay alive.
        """
        request = urllib.request.Request(
            self._url(path, params), headers=self._headers(), method="GET"
        )
        try:
            response = self._opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            raise self._error_from_http(exc) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CLIError(
                f"could not reach the gateway at {self.base_url}: {exc}"
            ) from None
        try:
            for raw in response:
                yield raw.decode("utf-8", errors="replace").rstrip("\r\n")
        finally:
            response.close()


@dataclass
class CLIContext:
    """Everything a command handler needs for one invocation."""

    client: AdminClient
    stdin: TextIO
    stdout: TextIO
    stderr: TextIO
    json_output: bool

    def emit(
        self,
        payload: object,
        human: str | Iterable[str] | None = None,
    ) -> None:
        """Write the NON-streaming result: exactly one JSON value (``--json``)
        or concise human text.

        ``human`` is a string or an iterable of lines and is ignored in JSON
        mode. With ``json_output=False`` and ``human=None`` nothing is
        written — handlers that print raw output themselves bypass ``emit``
        entirely (e.g. ``logs follow --json`` streams one JSON value per
        line, NDJSON, until interrupted). Every human line is rendered
        through :func:`safe_human_text` so terminal control sequences cannot
        be injected; JSON output is untouched (``json.dumps`` already
        escapes control characters).
        """
        if self.json_output:
            self.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
            return
        if human is None:
            return
        lines = [human] if isinstance(human, str) else list(human)
        for line in lines:
            text = safe_human_text(line)
            self.stdout.write(text if text.endswith("\n") else text + "\n")


def expect_object(value: object, context: str) -> dict[str, Any]:
    """Require *value* to be a JSON object, returning it as a typed dict.

    Admin API payloads arrive as ``object``; handlers narrow them through
    this helper so field access type-checks. Raises :class:`CLIError` with
    *context* naming the response when the value is not a dict.
    """
    if not isinstance(value, dict):
        raise CLIError(
            f"{context}: expected a JSON object (got {type(value).__name__})"
        )
    return cast(dict[str, Any], value)


def reject_unknown_fields(
    value: Mapping[str, object],
    allowed: frozenset[str],
    context: str,
) -> None:
    """Fail closed when a JSON input object contains unsupported fields."""
    unknown = sorted(key for key in value if key not in allowed)
    if unknown:
        noun = "field" if len(unknown) == 1 else "fields"
        raise CLIError(f"{context} contains unknown {noun}: {', '.join(unknown)}")


def read_json_source(source: str, *, stdin: TextIO) -> object:
    """Load a JSON document from *source*: a file path, or ``-`` for stdin.

    Raises :class:`CLIError` when the source cannot be read or does not
    contain a single valid JSON document.
    """
    if source == "-":
        text = stdin.read()
        origin = "<stdin>"
    else:
        path = Path(source).expanduser()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CLIError(f"could not read {path}: {exc}") from None
        origin = str(path)
    try:
        return json.loads(text)
    except ValueError as exc:
        raise CLIError(f"{origin} is not valid JSON: {exc}") from None


def require_yes(args: argparse.Namespace, action: str) -> None:
    """Guard a destructive operation: requires ``args.yes`` (the ``--yes`` flag).

    This CLI is scriptable by design, so it never prompts; a missing ``--yes``
    is a hard error even on a TTY.
    """
    if not getattr(args, "yes", False):
        raise CLIError(f"{action} requires --yes (refusing to prompt)")
