"""Resolve an Issuebear base URL to its API + MCP URLs via the public
.well-known endpoint, so the user enters one URL instead of two.

Lives inside this plugin, not core: every line of it is one board server's own
protocol — its well-known path, its document's two required keys — and the
"one URL instead of two" convenience only exists because *this* source needs
two. A neutral "discover a source's endpoints" abstraction over a single
implementation would be the leak dressed up as an interface.
"""

from __future__ import annotations

import httpx

WELL_KNOWN = "/.well-known/issuebot.json"


class DiscoveryError(Exception):
    """The base URL did not serve a usable issuebot discovery document."""


def discover(base_url: str, *, transport: httpx.BaseTransport | None = None) -> dict[str, str]:
    """Return {api_url, mcp_url, name?} for an Issuebear base URL, or raise
    DiscoveryError (caller falls back to asking for the URLs directly)."""
    url = base_url.rstrip("/") + WELL_KNOWN
    try:
        with httpx.Client(transport=transport, timeout=10.0, follow_redirects=True) as client:
            resp = client.get(url)
    except httpx.HTTPError as exc:
        raise DiscoveryError(f"could not reach {url}: {exc}") from exc
    if resp.status_code != 200:
        raise DiscoveryError(f"{url} returned {resp.status_code} (server may be too old)")
    data = resp.json()
    if not isinstance(data, dict) or "api_url" not in data or "mcp_url" not in data:
        raise DiscoveryError(f"{url} did not return api_url + mcp_url")
    return data
