from __future__ import annotations

import httpx
import pytest

from issuebot.plugins.sources.issuebear.discovery import WELL_KNOWN, DiscoveryError, discover


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def test_discover_returns_document_on_200():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"api_url": "a", "mcp_url": "m"})

    result = discover("https://issuebear.example/", transport=_transport(handler))

    assert result == {"api_url": "a", "mcp_url": "m"}
    assert seen["path"] == WELL_KNOWN


def test_discover_strips_trailing_slash_before_well_known():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"api_url": "a", "mcp_url": "m"})

    discover("https://issuebear.example//", transport=_transport(handler))

    assert seen["path"] == WELL_KNOWN


def test_discover_raises_on_404():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    with pytest.raises(DiscoveryError):
        discover("https://issuebear.example", transport=_transport(handler))


def test_discover_raises_when_keys_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"api_url": "a"})

    with pytest.raises(DiscoveryError):
        discover("https://issuebear.example", transport=_transport(handler))


def test_discover_raises_when_body_not_a_dict():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["a", "m"])

    with pytest.raises(DiscoveryError):
        discover("https://issuebear.example", transport=_transport(handler))
