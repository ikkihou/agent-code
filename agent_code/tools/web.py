"""Web tools: web_search, web_fetch."""

from __future__ import annotations

import ipaddress
import re
import socket
import threading
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import html2text
import httpx

from ..bash_runner import run_sync
from ..bg_manager import start_background
from ..fs_safety import truncate_output
from .core import Tool, ToolContext

WEB_USER_AGENT = "agent-code/0.1 (+https://example.com/agent-code)"
WEB_FETCH_MAX_BYTES = 10 * 1024 * 1024
WEB_FETCH_MAX_CHARS = 20_000
WEB_URL_MAX_LENGTH = 2000
WEB_FETCH_TIMEOUT_S = 30.0
WEB_SEARCH_TIMEOUT_S = 15.0
DNS_TIMEOUT_S = 3.0

# Hostnames we never resolve/fetch, regardless of what DNS says. ``localhost`` has no
# dot so the dot check below already rejects it, but keep it here for clarity.
_BLOCKED_HOSTNAMES = frozenset({"localhost", "metadata.google.internal"})

# Private / loopback / link-local / unique-local ranges. 169.254.0.0/16 includes the
# cloud metadata endpoint 169.254.169.254.
_BLOCKED_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


def _is_blocked_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # Normalize IPv4-mapped IPv6 (::ffff:127.0.0.1) back to IPv4 first, otherwise every
    # mapped form of the private ranges slips past the IPv4 network checks.
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address in network for network in _BLOCKED_NETWORKS)


def _resolve_host(host: str) -> list[str]:
    """Resolve a hostname to its address list, bounded by a short timeout.

    ``socket.getaddrinfo`` is a blocking libc call that ignores socket timeouts, so the
    lookup runs on a daemon thread and the caller waits at most ``DNS_TIMEOUT_S``.
    """
    addresses: list[str] = []
    errors: list[BaseException] = []

    def _lookup() -> None:
        try:
            infos = socket.getaddrinfo(host, None)
        except Exception as exc:  # surface any lookup failure as a clear ValueError
            errors.append(exc)
            return
        addresses.extend(str(info[4][0]) for info in infos)

    thread = threading.Thread(target=_lookup, daemon=True)
    thread.start()
    thread.join(timeout=DNS_TIMEOUT_S)
    if thread.is_alive():
        raise ValueError(f"dns resolution timed out for host: {host}")
    if errors:
        raise ValueError(f"could not resolve host: {host}")
    return addresses


def _validate_url(url: str) -> None:
    # URL validation is web_fetch's first gate: every failure happens before httpx
    # actually sends a request.
    if len(url) > WEB_URL_MAX_LENGTH:
        raise ValueError(f"url too long: {len(url)} > {WEB_URL_MAX_LENGTH}")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme: {parsed.scheme or '(none)'}")
    if parsed.username or parsed.password:
        raise ValueError("url with credentials is not allowed")
    if not parsed.hostname or "." not in parsed.hostname:
        raise ValueError(f"invalid hostname: {parsed.hostname}")

    # SSRF guard: never reach loopback/private/link-local/unique-local addresses.
    host = parsed.hostname.rstrip(".").lower()
    if host in _BLOCKED_HOSTNAMES or host.endswith(".local") or host.endswith(".internal"):
        raise ValueError(f"blocked host: {host}")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _is_blocked_address(literal):
            raise ValueError(f"blocked private address: {host}")
        return

    for address in _resolve_host(host):
        if _is_blocked_address(ipaddress.ip_address(address)):
            raise ValueError(f"url resolves to a private address: {address}")


def _html_to_markdown(html: str) -> str:
    converter = html2text.HTML2Text()
    converter.body_width = 0  # Disable hard wrapping, keep longer paragraphs in context.
    converter.ignore_images = True
    converter.ignore_emphasis = False
    return converter.handle(html).strip()


def web_fetch(args: dict[str, Any], ctx: ToolContext) -> str:
    url = args.get("url", "")
    if not url:
        return "error: missing required argument 'url'"
    try:
        _validate_url(url)
    except ValueError as exc:
        return f"error: {exc}"

    headers = {"User-Agent": WEB_USER_AGENT, "Accept": "text/html,text/*;q=0.9,*/*;q=0.5"}
    try:
        with httpx.Client(timeout=WEB_FETCH_TIMEOUT_S, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        return f"error: {exc}"

    if len(resp.content) > WEB_FETCH_MAX_BYTES:
        return f"error: response too large: {len(resp.content)} > {WEB_FETCH_MAX_BYTES}"

    content_type = resp.headers.get("content-type", "").lower()
    if "text/html" in content_type or "application/xhtml" in content_type:
        body = _html_to_markdown(resp.text)
    elif content_type.startswith("text/") or "json" in content_type or "xml" in content_type:
        body = resp.text
    else:
        return f"error: unsupported content-type: {content_type or '(none)'}"

    return truncate_output(body, max_chars=WEB_FETCH_MAX_CHARS)


def _unwrap_ddg_url(href: str) -> str:
    # DuckDuckGo's HTML endpoint returns hrefs like /l/?uddg=ENCODED_URL&rut=...
    # Extract the real target URL so the model sees the final landing address.
    if "/l/" not in href:
        return href
    if href.startswith("//"):
        parsed = urlparse(f"https:{href}")
    elif href.startswith("/"):
        parsed = urlparse(f"https://duckduckgo.com{href}")
    else:
        parsed = urlparse(href)
    params = parse_qs(parsed.query)
    if "uddg" in params:
        return unquote(params["uddg"][0])
    return href


def _duckduckgo_search(query: str, max_results: int) -> list[dict[str, str]]:
    # DuckDuckGo has no official API. The HTML endpoint is the teaching-grade
    # fallback; switch to an API-key provider such as Tavily/Serper/Brave for
    # reliability.
    headers = {"User-Agent": WEB_USER_AGENT}
    with httpx.Client(timeout=WEB_SEARCH_TIMEOUT_S, follow_redirects=True) as client:
        resp = client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers=headers,
        )
        resp.raise_for_status()

    pattern = re.compile(
        r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        re.DOTALL,
    )
    results: list[dict[str, str]] = []
    for href, title_html in pattern.findall(resp.text):
        title = re.sub(r"<[^>]+>", "", title_html).strip()
        url = _unwrap_ddg_url(href)
        if not title or not url:
            continue
        results.append({"title": title, "url": url})
        if len(results) >= max_results:
            break
    return results


def web_search(args: dict[str, Any], ctx: ToolContext) -> str:
    query = args.get("query", "")
    if not query:
        return "error: missing required argument 'query'"
    max_results = max(1, min(int(args.get("max_results", 5)), 10))
    try:
        results = _duckduckgo_search(query, max_results=max_results)
    except httpx.HTTPError as exc:
        return f"error: {exc}"
    if not results:
        return "(no results)"
    lines = [f"- {r['title']}\n  {r['url']}" for r in results]
    return truncate_output("\n".join(lines))


def tools() -> list[Tool]:
    return [
        Tool(
            name="web_fetch",
            description="Fetch a URL and return its content as markdown (or raw text).",
            run=web_fetch,
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Absolute http(s) URL."},
                },
                "required": ["url"],
            },
        ),
        Tool(
            name="web_search",
            description="Search the web (DuckDuckGo) and return top results.",
            run=web_search,
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                    "max_results": {
                        "type": "integer",
                        "description": "How many results to return (1-10).",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        ),
    ]
