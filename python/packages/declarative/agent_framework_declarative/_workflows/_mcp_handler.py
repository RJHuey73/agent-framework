# Copyright (c) Microsoft. All rights reserved.

"""MCP tool handler abstraction for declarative workflows.

Mirrors the .NET ``IMcpToolHandler`` / ``DefaultMcpToolHandler`` pair from
``Microsoft.Agents.AI.Workflows.Declarative.Mcp``. Provides:

- :class:`MCPToolInvocation` — request input data passed from the executor.
- :class:`MCPToolResult` — response data returned to the executor.
- :class:`MCPToolHandler` — :class:`typing.Protocol` callers implement to plug
  in custom transports (e.g. with allowlisting, Foundry connection resolution,
  per-server auth, etc.).
- :class:`DefaultMCPToolHandler` — production-grade default backed by
  :class:`agent_framework.MCPStreamableHTTPTool`.
- :class:`MCPServerURLBlockedError` — raised by :class:`DefaultMCPToolHandler`
  when a ``server_url`` targets a private/loopback/link-local/reserved
  address (see Security note below).

Security note: :class:`DefaultMCPToolHandler` resolves ``server_url`` before
connecting and rejects targets that classify as private, loopback,
link-local, or reserved per :mod:`ipaddress` (this also covers the
``169.254.0.0/16`` link-local range used by cloud metadata endpoints). A
workflow author can explicitly trust a specific internal host via the
``allowed_hosts`` constructor argument; everything else in those ranges stays
blocked. This guard defends against a workflow whose ``serverUrl`` PowerFx
expression is (even partially) bound to untrusted conversation/state input
being redirected to an internal service. It does **not** fully close a
DNS-rebinding window (the resolved address is validated once, before
``connect()``, not pinned for the lifetime of the connection) — a custom
:class:`MCPToolHandler` implementation is still the right place for a
stronger, DNS-rebinding-resistant policy. This split mirrors the .NET design.

Prompt-injection note: MCP tool outputs flow back into agent conversations
(via ``conversationId`` and Tool-role messages emitted by the executor) so
they share the same risk surface as ``HttpRequestAction``. Workflow authors
must trust the MCP server they invoke.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import socket
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, cast, runtime_checkable
from urllib.parse import urlparse

import httpx

if TYPE_CHECKING:
    from agent_framework import Content

__all__ = [
    "ClientProvider",
    "DefaultMCPToolHandler",
    "MCPServerURLBlockedError",
    "MCPToolHandler",
    "MCPToolInvocation",
    "MCPToolResult",
]

logger = logging.getLogger(__name__)

_DEFAULT_CACHE_MAX_SIZE = 32


@dataclass
class MCPToolInvocation:
    """Description of an MCP tool call to be dispatched by a :class:`MCPToolHandler`.

    Mirrors the input parameters of the .NET ``IMcpToolHandler.InvokeToolAsync``
    method. Field semantics:

    - ``server_url``: Absolute URL of the MCP server. Already evaluated from
      the YAML expression.
    - ``server_label``: Optional human-readable label used for diagnostics
      and as the underlying ``MCPStreamableHTTPTool`` name.
    - ``tool_name``: Name of the tool to invoke on the MCP server.
    - ``arguments``: Tool arguments. Already evaluated; values may be any
      JSON-serialisable Python object (str, int, bool, dict, list, None).
    - ``headers``: Outbound HTTP headers (e.g. authentication). Empty values
      are skipped by the executor before construction.
    - ``connection_name``: Optional Foundry connection name forwarded for
      handlers that resolve auth/credentials by connection. The default
      handler does not consume this field.
    """

    server_url: str
    tool_name: str
    server_label: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)  # type: ignore[reportUnknownVariableType]
    headers: dict[str, str] = field(default_factory=dict)  # type: ignore[reportUnknownVariableType]
    connection_name: str | None = None


def _empty_outputs() -> list[Any]:
    """Default factory for ``MCPToolResult.outputs``.

    Typed as ``list[Any]`` here to keep the dataclass field's runtime
    factory simple; the public type on :class:`MCPToolResult` is
    ``list[Content]``.
    """
    return []


@dataclass
class MCPToolResult:
    """Response returned by an :class:`MCPToolHandler`.

    Mirrors the .NET ``McpServerToolResultContent`` shape. ``outputs`` is a
    list of :class:`agent_framework.Content` items as parsed by the MCP
    transport (TextContent / DataContent / UriContent / etc.).

    On error, ``is_error`` is ``True``, ``error_message`` carries a human
    readable description, and ``outputs`` typically contains a single
    ``Content.from_text("Error: ...")`` entry for downstream display.
    """

    outputs: list[Content] = field(default_factory=_empty_outputs)
    is_error: bool = False
    error_message: str | None = None


@runtime_checkable
class MCPToolHandler(Protocol):
    """Protocol for MCP tool handlers used by ``InvokeMcpTool``.

    Mirrors :class:`HttpRequestHandler` — declares ONLY the invocation method.
    Lifecycle methods (``aclose`` / ``__aenter__`` / ``__aexit__``) are NOT
    part of the Protocol; concrete implementations may add them as
    appropriate.

    Implementations must be safe to call concurrently from multiple workflow
    runs. Implementations are responsible for any URL allowlisting, SSRF
    guards, retry policies, auth resolution, and other policies the workflow
    author wants applied.
    """

    async def invoke_tool(self, invocation: MCPToolInvocation) -> MCPToolResult:
        """Dispatch ``invocation`` and return the result.

        Args:
            invocation: Description of the MCP tool call to perform.

        Returns:
            The :class:`MCPToolResult` carrying the parsed outputs (or an
            error flag if the tool raised). Implementations SHOULD return a
            result with ``is_error=True`` rather than raising for transport
            or tool-level failures, so the workflow can store the message in
            ``output.result`` (matching .NET ``AssignErrorAsync`` behaviour).
            They MAY raise on unexpected programming errors — these will be
            propagated unchanged by the executor so they fail loudly.
        """
        ...


ClientProvider = Callable[[MCPToolInvocation], Awaitable["httpx.AsyncClient | None"]]


class MCPServerURLBlockedError(ValueError):
    """Raised when an ``InvokeMcpTool`` ``server_url`` targets a disallowed network address.

    Raised by :meth:`DefaultMCPToolHandler._create_entry` *before* any MCP
    client is constructed or connected, so no request (and no configured
    ``headers``) ever reaches the blocked target. Callers that invoke
    through :meth:`DefaultMCPToolHandler.invoke_tool` never see this
    exception directly — it is caught alongside other connect failures and
    surfaced as ``MCPToolResult(is_error=True, ...)``.
    """


# Mirrors the four classifications the SSRF-protection finding calls out by
# name (``is_private`` / ``is_loopback`` / ``is_link_local`` / ``is_reserved``);
# ``is_unspecified`` (0.0.0.0 / ::) and ``is_multicast`` are included as
# additional defense-in-depth since neither is a legitimate MCP server
# target. Order matters only for the human-readable reason string.
_BLOCKED_IP_PREDICATES: tuple[tuple[str, str], ...] = (
    ("is_loopback", "loopback"),
    ("is_link_local", "link-local"),  # covers 169.254.0.0/16, incl. cloud metadata endpoints
    ("is_unspecified", "unspecified"),  # 0.0.0.0 / :: -- checked before is_private for a precise reason
    ("is_private", "private"),
    ("is_reserved", "reserved"),
    ("is_multicast", "multicast"),
)


def _blocked_ip_reason(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Return a short reason (e.g. ``"link-local"``) if ``ip`` must not be reached, else ``None``.

    Pure/synchronous: takes an already-parsed address, does no I/O. Kept
    separate from URL parsing and DNS resolution so it can be unit-tested
    directly against hardcoded IP strings without needing a live resolver.
    """
    for attr, reason in _BLOCKED_IP_PREDICATES:
        if getattr(ip, attr):
            return reason
    return None


_DNS_RESOLVE_TIMEOUT_SECONDS = 5.0


async def _resolve_host_ips(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Best-effort DNS resolution of ``host`` to its candidate IP addresses.

    Runs the blocking :func:`socket.getaddrinfo` call off the event loop,
    bounded by :data:`_DNS_RESOLVE_TIMEOUT_SECONDS`. Returns an empty list
    (rather than raising) when resolution fails or times out -- a DNS
    failure is not itself evidence of an SSRF attempt, and the real MCP
    connection attempt below will surface its own (already-handled)
    connection error for a genuinely unreachable host. The timeout keeps a
    slow/unreachable resolver from stalling this pre-connect check itself.
    """
    try:
        infos = await asyncio.wait_for(
            asyncio.to_thread(socket.getaddrinfo, host, None),
            timeout=_DNS_RESOLVE_TIMEOUT_SECONDS,
        )
    except (OSError, socket.gaierror, asyncio.TimeoutError):
        # ``asyncio.TimeoutError`` is the builtin ``TimeoutError`` on 3.11+ but a
        # distinct class pre-3.11 (this package supports 3.10+); name it
        # explicitly so the timeout is caught on every supported version.
        return []
    results: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        sockaddr = info[4]
        try:
            results.append(ipaddress.ip_address(sockaddr[0]))
        except ValueError:
            # Not an IP literal we recognise; skip rather than fail the whole lookup.
            continue
    return results


async def _validate_server_url(server_url: str, allowed_hosts: Collection[str] | None) -> None:
    """Reject ``server_url`` if it targets a private/loopback/link-local/reserved address.

    The host component is checked directly as an IP literal first (no
    network call needed); if it is not an IP literal, it is resolved via DNS
    and every returned address is checked. A host listed in ``allowed_hosts``
    (case-insensitive, exact match against the URL's host component) bypasses
    the IP classification entirely -- this is the explicit, per-host opt-in
    for a workflow author who intentionally targets an internal MCP server.

    Args:
        server_url: The evaluated ``InvokeMcpTool`` server URL.
        allowed_hosts: Optional collection of hostnames/IP literals the
            workflow author has explicitly vetted; matching hosts skip the
            private-range check entirely.

    Raises:
        MCPServerURLBlockedError: ``server_url`` has no host component, or
            the host (directly or via DNS) resolves to a disallowed address
            and is not covered by ``allowed_hosts``.
    """
    host = urlparse(server_url).hostname
    if not host:
        raise MCPServerURLBlockedError(f"InvokeMcpTool server_url {server_url!r} has no resolvable host.")

    if allowed_hosts and host.lower() in {allowed.lower() for allowed in allowed_hosts}:
        return

    try:
        candidates = [ipaddress.ip_address(host)]
    except ValueError:
        # Not an IP literal -- it's a hostname; resolve it.
        candidates = await _resolve_host_ips(host)

    for ip in candidates:
        reason = _blocked_ip_reason(ip)
        if reason is not None:
            raise MCPServerURLBlockedError(
                f"InvokeMcpTool server_url {server_url!r} resolves to a {reason} address ({ip}); refusing to "
                "connect. Pass this host in DefaultMCPToolHandler(allowed_hosts=...) if it is an intentional, "
                "trusted target."
            )


@dataclass
class _CacheEntry:
    """Internal record stored in the LRU cache."""

    tool: Any  # MCPStreamableHTTPTool — typed Any to avoid import at module load
    owned_httpx_client: httpx.AsyncClient | None


class DefaultMCPToolHandler:
    """Default :class:`MCPToolHandler` backed by :class:`agent_framework.MCPStreamableHTTPTool`.

    Caches one :class:`agent_framework.MCPStreamableHTTPTool` instance per
    ``(server_url, server_label, connection_name, headers_hash)`` in a
    bounded LRU. The cache prevents re-establishing an MCP session for every
    invocation while ensuring different header sets (auth tokens) cannot
    share a session — matches the .NET design intent while bounding
    cardinality. ``server_label`` and ``connection_name`` participate in
    the key so that callers using ``client_provider`` to dispatch on those
    fields receive a fresh client per logical connection (see below).
    Header *names* are lower-cased inside the hash payload only — the
    headers passed on the wire keep the caller's original casing — so two
    YAML actions that spell ``Authorization`` differently still share a
    cache entry.

    Construction modes:

    1. ``DefaultMCPToolHandler()`` — owns its own ``httpx.AsyncClient``
       instances created lazily per cache entry. Closed by :meth:`aclose`.
    2. ``DefaultMCPToolHandler(client_provider=cb)`` — per-server client
       lookup (parity with .NET ``httpClientProvider`` callback). The
       callback receives the full :class:`MCPToolInvocation` so it can
       dispatch on ``server_url`` / ``connection_name`` / ``server_label``.
       Returning ``None`` falls back to an internally-created client. Caller
       supplied clients are NOT closed by :meth:`aclose`.

    .. warning::

       Before connecting, ``server_url`` is resolved and checked against
       private/loopback/link-local/reserved address ranges (see the module
       Security note) -- this is an SSRF *mitigation*, not a full policy
       engine. It does not validate ``headers``/``connection_name``-based
       auth resolution, does not pin the resolved address against
       DNS-rebinding, and only ever permits an otherwise-blocked host via
       exact-match entries in ``allowed_hosts``. Wrap or replace this handler
       with a custom implementation for a stronger production policy.

    Args:
        client_provider: Optional per-server ``httpx.AsyncClient`` provider.
        cache_max_size: Maximum number of cached MCP clients. When exceeded,
            the least-recently-used entry is evicted and its client closed
            (only owned clients are closed; caller-supplied ones are not).
            Defaults to ``32``.
        allowed_hosts: Optional collection of hostnames/IP literals that are
            explicitly trusted even though they classify as private,
            loopback, link-local, or reserved (exact match against the
            ``server_url`` host component, case-insensitive). ``None``
            (default) allows none of those ranges -- only ordinary,
            publicly-routable hosts connect. This is the only override for
            the private-range check; there is no blanket opt-out.
    """

    LIST_TOOLS_TOOL_NAME: ClassVar[str] = "tools/list"
    """Reserved ``tool_name`` that maps an :class:`MCPToolHandler` invocation
    to the MCP protocol ``tools/list`` discovery operation.

    The constant matches the underlying MCP method name so a single
    string travels unchanged through host code, YAML, and the protocol
    wire. When this handler receives an invocation with this name it
    pages through ``session.list_tools()`` and returns the catalog as a
    single ``TextContent`` containing JSON of shape
    ``{"tools": [{name, description, inputSchema, outputSchema}, ...]}``.
    Workflows can reference this name from an ``InvokeMcpTool`` declarative
    action to introspect a server's tool surface without an extra round-trip
    from host code.
    """

    def __init__(
        self,
        *,
        client_provider: ClientProvider | None = None,
        cache_max_size: int = _DEFAULT_CACHE_MAX_SIZE,
        allowed_hosts: Collection[str] | None = None,
    ) -> None:
        if cache_max_size <= 0:
            raise ValueError(f"cache_max_size must be positive, got {cache_max_size}")
        self._client_provider = client_provider
        self._allowed_hosts = allowed_hosts
        self._cache_max_size = cache_max_size
        self._cache: OrderedDict[tuple[str, str, str, str], _CacheEntry] = OrderedDict()
        # Outer lock guards the cache + in-flight-future map only — never
        # held across network I/O.
        self._cache_lock = asyncio.Lock()
        # Per-key in-flight futures: while one task is connecting, other
        # tasks awaiting the same key will await the same future and share
        # the resulting cache entry.
        self._inflight: dict[tuple[str, str, str, str], asyncio.Future[_CacheEntry]] = {}
        # Set by ``aclose`` to prevent post-close cache insertions and to
        # reject new ``invoke_tool`` calls. Once set, never cleared.
        self._closed = False

    async def invoke_tool(self, invocation: MCPToolInvocation) -> MCPToolResult:
        """Invoke ``invocation.tool_name`` on the cached MCP client for the server.

        The reserved name :attr:`LIST_TOOLS_TOOL_NAME` (``"tools/list"``) is
        intercepted client-side: instead of being forwarded as a tool call,
        it is translated to an MCP ``session.list_tools()`` discovery
        operation (paginated automatically) and returned as a single
        ``TextContent`` containing a JSON tool catalog.
        """
        from agent_framework import Content
        from agent_framework.exceptions import ToolExecutionException

        # Reserved-name args validation runs before connect: rejecting bad
        # input shouldn't require establishing an MCP session.
        if invocation.tool_name == self.LIST_TOOLS_TOOL_NAME and invocation.arguments:
            message = f"The reserved MCP '{self.LIST_TOOLS_TOOL_NAME}' operation does not accept tool arguments."
            return MCPToolResult(
                outputs=[Content.from_text(f"Error: {message}")],
                is_error=True,
                error_message=message,
            )

        try:
            entry = await self._get_or_create_entry(invocation)
        except Exception as exc:
            # Connect / cache lookup failures surface as tool errors so the
            # workflow can store them at output.result without crashing.
            logger.warning(
                "DefaultMCPToolHandler: failed to obtain MCP client for url=%s tool=%s: %s",
                invocation.server_url,
                invocation.tool_name,
                exc,
            )
            message = f"Failed to connect to MCP server: {type(exc).__name__}: {exc}".rstrip(": ")
            return MCPToolResult(
                outputs=[Content.from_text(f"Error: {message}")],
                is_error=True,
                error_message=message,
            )

        try:
            if invocation.tool_name == self.LIST_TOOLS_TOOL_NAME:
                return await self._invoke_list_tools(entry)
            raw = await entry.tool.call_tool(invocation.tool_name, **invocation.arguments)
        except ToolExecutionException as exc:
            logger.info(
                "DefaultMCPToolHandler: tool '%s' on '%s' raised ToolExecutionException",
                invocation.tool_name,
                invocation.server_url,
            )
            message = str(exc) or type(exc).__name__
            return MCPToolResult(
                outputs=[Content.from_text(f"Error: {message}")],
                is_error=True,
                error_message=message,
            )
        except httpx.HTTPError as exc:
            message = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            return MCPToolResult(
                outputs=[Content.from_text(f"Error: {message}")],
                is_error=True,
                error_message=message,
            )
        except Exception as exc:
            # Be defensive about MCP errors that may bubble up without being
            # wrapped in ToolExecutionException by custom parsers.
            try:
                from mcp.shared.exceptions import McpError
            except ImportError:  # pragma: no cover - mcp is a hard dep but stay defensive
                raise
            if isinstance(exc, McpError):
                message = str(exc) or type(exc).__name__
                return MCPToolResult(
                    outputs=[Content.from_text(f"Error: {message}")],
                    is_error=True,
                    error_message=message,
                )
            raise

        # Defensive normalisation: call_tool is typed ``str | list[Content]``.
        # Default parser returns list, but custom parse_tool_results may return str.
        if isinstance(raw, str):
            outputs: list[Content] = [Content.from_text(raw)]
        else:
            outputs = list(raw)
        return MCPToolResult(outputs=outputs)

    @staticmethod
    async def _invoke_list_tools(entry: _CacheEntry) -> MCPToolResult:
        """Handle the reserved :attr:`LIST_TOOLS_TOOL_NAME` invocation.

        Pages through ``session.list_tools()`` (mirroring the pagination loop
        in :meth:`agent_framework.MCPTool.load_tools`) and serialises the
        full catalog as a single ``TextContent`` containing JSON of shape
        ``{"tools": [{name, description, inputSchema, outputSchema}, ...]}``.

        The output shape, property names, and property order are stable so
        downstream PowerFx expressions can rely on the schema. ``indent=2``
        produces human-readable JSON for the conversation log;
        ``allow_nan=False`` guards against producing non-conformant JSON
        ``NaN``/``Infinity`` tokens if a misbehaving server returns such
        values in a schema.
        """
        from agent_framework import Content

        session = getattr(entry.tool, "session", None)
        if session is None:
            message = "MCP session is not connected; cannot list tools."
            return MCPToolResult(
                outputs=[Content.from_text(f"Error: {message}")],
                is_error=True,
                error_message=message,
            )

        # Lazy import keeps ``mcp`` types out of module import time.
        from mcp import types as mcp_types

        collected: list[Any] = []
        params: mcp_types.PaginatedRequestParams | None = None
        while True:
            tool_list = await session.list_tools(params=params)
            collected.extend(tool_list.tools)
            next_cursor = getattr(tool_list, "nextCursor", None)
            if not next_cursor:
                break
            params = mcp_types.PaginatedRequestParams(cursor=next_cursor)

        payload = {
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.inputSchema,
                    "outputSchema": tool.outputSchema,
                }
                for tool in collected
            ],
        }
        return MCPToolResult(outputs=[Content.from_text(json.dumps(payload, indent=2, allow_nan=False))])

    async def aclose(self) -> None:
        """Close all cached MCP clients and the owned httpx clients.

        Caller-supplied :class:`httpx.AsyncClient` instances (returned by the
        ``client_provider`` callback) are NOT closed.

        Idempotent — a second call returns immediately. Drains any in-flight
        ``_create_entry`` tasks before returning so their resources are
        cleaned up; the in-flight tasks see ``self._closed`` in phase 3 of
        :meth:`_get_or_create_entry`, close their own entry, and resolve
        their future with ``RuntimeError("DefaultMCPToolHandler is closed")``.
        """
        async with self._cache_lock:
            if self._closed:
                return
            self._closed = True
            entries = list(self._cache.values())
            self._cache.clear()
            inflight_futures = list(self._inflight.values())

        # Wait for in-flight creations to finish their self-cleanup. Each
        # in-flight task self-closes its entry under the closed-flag branch
        # in phase 3 and resolves its future with ``RuntimeError``; we
        # swallow it here because the failure is expected at shutdown.
        for fut in inflight_futures:
            try:
                await fut
            except BaseException:
                logger.debug("DefaultMCPToolHandler: in-flight future raised during aclose", exc_info=True)
                continue

        for entry in entries:
            await self._close_entry(entry)

    async def __aenter__(self) -> DefaultMCPToolHandler:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_or_create_entry(self, invocation: MCPToolInvocation) -> _CacheEntry:
        """Look up (or create) the cached MCP client for this invocation."""
        key = self._cache_key(
            invocation.server_url,
            invocation.server_label,
            invocation.connection_name,
            invocation.headers,
        )

        # Phase 1: check the cache and either claim creation or wait for an
        # already in-flight creation.
        creating = False
        async with self._cache_lock:
            if self._closed:
                raise RuntimeError("DefaultMCPToolHandler is closed")
            existing = self._cache.get(key)
            if existing is not None:
                self._cache.move_to_end(key)
                return existing
            inflight = self._inflight.get(key)
            if inflight is None:
                inflight = asyncio.get_running_loop().create_future()
                self._inflight[key] = inflight
                creating = True

        if not creating:
            return await inflight

        # Phase 2: we own creation. Build the entry outside the lock.
        try:
            entry = await self._create_entry(invocation)
        except BaseException as exc:
            async with self._cache_lock:
                self._inflight.pop(key, None)
            if not inflight.done():
                inflight.set_exception(exc if isinstance(exc, BaseException) else RuntimeError(str(exc)))
            # Mark the exception retrieved to suppress noisy "Future exception
            # was never retrieved" warnings when there are no other awaiters
            # (other awaiters still see the exception through their ``await``).
            inflight.exception()
            raise

        # Phase 3: insert with LRU eviction; resolve the in-flight future.
        # If ``aclose`` ran while we were connecting, ``_closed`` is now
        # True; don't insert into the cache (it has been drained), close
        # the just-built entry, and surface the closed-handler error to
        # all awaiters of the future.
        evicted: _CacheEntry | None = None
        duplicate: _CacheEntry | None = None
        handler_closed = False
        async with self._cache_lock:
            self._inflight.pop(key, None)
            if self._closed:
                handler_closed = True
            else:
                existing = self._cache.get(key)
                if existing is not None:
                    # Another writer beat us; prefer the existing entry and
                    # discard ours after the lock is released.
                    self._cache.move_to_end(key)
                    duplicate = entry
                    entry = existing
                else:
                    self._cache[key] = entry
                    self._cache.move_to_end(key)
                    if len(self._cache) > self._cache_max_size:
                        _evicted_key, evicted = self._cache.popitem(last=False)
                if not inflight.done():
                    inflight.set_result(entry)

        if handler_closed:
            # Close our orphaned entry; resolve the future with a clear
            # error so the caller (and any other awaiters) surface a
            # consistent "handler is closed" failure rather than receiving
            # an entry we are about to close behind their back.
            await self._close_entry(entry)
            err = RuntimeError("DefaultMCPToolHandler is closed")
            if not inflight.done():
                inflight.set_exception(err)
            inflight.exception()
            raise err
        if duplicate is not None:
            await self._close_entry(duplicate)
        if evicted is not None:
            await self._close_entry(evicted)
        return entry

    async def _create_entry(self, invocation: MCPToolInvocation) -> _CacheEntry:
        """Construct (and connect) a fresh MCP client for ``invocation``.

        Validates ``invocation.server_url`` against the private/loopback/
        link-local/reserved address check *before* doing anything else --
        including before ``client_provider`` runs and before
        ``invocation.headers`` are captured into the ``header_provider``
        closure below -- so a blocked target never has its (potentially
        auth-bearing) headers attached to any outbound request.
        """
        from agent_framework import MCPStreamableHTTPTool

        await _validate_server_url(invocation.server_url, self._allowed_hosts)

        provided_client: httpx.AsyncClient | None = None
        if self._client_provider is not None:
            provided_client = await self._client_provider(invocation)
        # Capture headers for this cache entry so the header_provider closure
        # always returns the same set, regardless of the runtime kwargs.
        captured_headers = dict(invocation.headers)

        def _header_provider(_kwargs: dict[str, Any]) -> dict[str, str]:
            return captured_headers

        tool: Any = MCPStreamableHTTPTool(
            name=invocation.server_label or "McpClient",
            url=invocation.server_url,
            load_prompts=False,
            http_client=provided_client,
            header_provider=_header_provider if captured_headers else None,
        )
        try:
            await tool.connect()
        except BaseException:
            try:
                await tool.close()
            except Exception:  # pragma: no cover - best effort
                logger.debug("DefaultMCPToolHandler: error closing tool after failed connect", exc_info=True)
            raise

        # ``MCPStreamableHTTPTool.get_mcp_client`` lazily creates an
        # ``httpx.AsyncClient`` when no caller client was provided AND a
        # ``header_provider`` was set. We treat any client allocated this
        # way as owned (closed by the handler). When the caller supplies
        # one, we never close it.
        owned_client: httpx.AsyncClient | None = None
        if provided_client is None:
            owned_client = cast("httpx.AsyncClient | None", getattr(tool, "_httpx_client", None))
        return _CacheEntry(tool=tool, owned_httpx_client=owned_client)

    async def _close_entry(self, entry: _CacheEntry) -> None:
        """Close the MCP tool and any owned httpx client."""
        try:
            await entry.tool.close()
        except Exception:  # pragma: no cover - best effort
            logger.debug("DefaultMCPToolHandler: error closing MCP tool", exc_info=True)
        if entry.owned_httpx_client is not None:
            try:
                await entry.owned_httpx_client.aclose()
            except Exception:  # pragma: no cover - best effort
                logger.debug("DefaultMCPToolHandler: error closing owned httpx client", exc_info=True)

    @staticmethod
    def _cache_key(
        server_url: str,
        server_label: str | None,
        connection_name: str | None,
        headers: dict[str, str] | None,
    ) -> tuple[str, str, str, str]:
        """Build an order-independent cache key for the invocation identity.

        The key includes ``server_label`` and ``connection_name`` so that
        callers using ``client_provider`` to dispatch on those fields
        receive a fresh client per logical connection (matches the
        documented dispatch contract).

        Header *names* are lower-cased inside the hash payload only so
        that ``Authorization`` and ``authorization`` map to the same
        cache entry. Header values remain case-sensitive (per RFC 7235).
        """
        if not headers:
            headers_hash = "0"
        else:
            normalized = sorted((k.lower(), v) for k, v in headers.items())
            payload = json.dumps(normalized, ensure_ascii=False)
            headers_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return (server_url, server_label or "", connection_name or "", headers_hash)
