from types import SimpleNamespace

import pytest

from app.services import network_security


def _dns(*addresses: str):
    return [
        (2, 1, 6, "", (address, 443))
        for address in addresses
    ]


def test_public_outbound_url_is_allowed(monkeypatch):
    monkeypatch.setattr(
        network_security,
        "get_settings",
        lambda: SimpleNamespace(outbound_private_host_allowlist=""),
    )
    monkeypatch.setattr(network_security.socket, "getaddrinfo", lambda *args, **kwargs: _dns("8.8.8.8"))

    assert network_security.validate_outbound_url("https://api.example.com/v1") == (
        "https://api.example.com/v1"
    )


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1"],
)
def test_private_or_embedded_address_is_blocked(address, monkeypatch):
    monkeypatch.setattr(
        network_security,
        "get_settings",
        lambda: SimpleNamespace(outbound_private_host_allowlist=""),
    )
    monkeypatch.setattr(network_security.socket, "getaddrinfo", lambda *args, **kwargs: _dns(address))

    with pytest.raises(network_security.OutboundTargetError, match="已阻止"):
        network_security.validate_outbound_url("https://api.example.com/v1")


def test_mixed_public_and_private_dns_answers_are_blocked(monkeypatch):
    monkeypatch.setattr(
        network_security,
        "get_settings",
        lambda: SimpleNamespace(outbound_private_host_allowlist=""),
    )
    monkeypatch.setattr(
        network_security.socket,
        "getaddrinfo",
        lambda *args, **kwargs: _dns("8.8.8.8", "10.0.0.8"),
    )

    with pytest.raises(network_security.OutboundTargetError, match="已阻止"):
        network_security.validate_outbound_url("https://api.example.com/v1")


def test_explicit_private_hostname_allowlist_is_allowed(monkeypatch):
    monkeypatch.setattr(
        network_security,
        "get_settings",
        lambda: SimpleNamespace(outbound_private_host_allowlist="mail.internal.example"),
    )
    monkeypatch.setattr(network_security.socket, "getaddrinfo", lambda *args, **kwargs: _dns("10.0.0.8"))

    assert network_security.validate_outbound_host("mail.internal.example", 993)


def test_credentials_in_url_are_rejected(monkeypatch):
    monkeypatch.setattr(
        network_security,
        "get_settings",
        lambda: SimpleNamespace(outbound_private_host_allowlist=""),
    )
    with pytest.raises(network_security.OutboundTargetError, match="用户名或密码"):
        network_security.validate_outbound_url("https://user:secret@example.com/v1")
