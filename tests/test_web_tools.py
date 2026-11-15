"""SSRF protection and URL validation tests for the web tools.

URL validation happens before any network request, so these tests exercise the gate
directly with IP literals and a monkeypatched resolver -- never real DNS or HTTP.
"""

from __future__ import annotations

import pytest

from agent_code.tools import web as web_module
from agent_code.tools.web import _validate_url, web_fetch


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://172.16.0.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://[fe80::1]/",
        "http://[fc00::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:169.254.169.254]/",
    ],
)
def test_validate_url_blocks_private_and_loopback_literals(url: str) -> None:
    with pytest.raises(ValueError):
        _validate_url(url)


def test_validate_url_blocks_blocked_hostnames() -> None:
    for url in (
        "http://metadata.google.internal/",
        "http://foo.local/",
        "http://foo.internal/",
        "http://localhost/",
    ):
        with pytest.raises(ValueError):
            _validate_url(url)


def test_validate_url_allows_public_ip_literal() -> None:
    _validate_url("http://8.8.8.8/")


def test_validate_url_blocks_hostname_resolving_to_private_ip(monkeypatch) -> None:
    monkeypatch.setattr(web_module, "_resolve_host", lambda host: ["10.0.0.5"])
    with pytest.raises(ValueError):
        _validate_url("http://evil.example.com/")


def test_validate_url_allows_hostname_resolving_to_public_ip(monkeypatch) -> None:
    monkeypatch.setattr(web_module, "_resolve_host", lambda host: ["93.184.216.34"])
    _validate_url("http://example.com/")


def test_web_fetch_returns_clear_error_for_private_url(tmp_path) -> None:
    from agent_code.tools import ToolContext

    out = web_fetch({"url": "http://127.0.0.1/"}, ToolContext(cwd=tmp_path))
    assert out.startswith("error:")
    assert "127.0.0.1" in out
