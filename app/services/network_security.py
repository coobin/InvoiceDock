from __future__ import annotations

import socket
from dataclasses import dataclass
from ipaddress import (
    IPv4Address,
    IPv4Network,
    IPv6Address,
    IPv6Network,
    ip_address,
    ip_network,
)
from urllib.parse import urlsplit

from app.config import get_settings


class OutboundTargetError(ValueError):
    """Raised when an outbound target could reach a protected network."""


@dataclass(frozen=True)
class _Allowlist:
    hosts: frozenset[str]
    networks: tuple[IPv4Network | IPv6Network, ...]


def _normalise_host(host: str) -> str:
    value = host.strip().rstrip(".").lower()
    if not value or len(value) > 253 or "\x00" in value:
        raise OutboundTargetError("外连地址缺少有效域名")
    try:
        return value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise OutboundTargetError("外连域名格式不正确") from exc


def _allowlist() -> _Allowlist:
    raw = str(getattr(get_settings(), "outbound_private_host_allowlist", "") or "")
    hosts: set[str] = set()
    networks: list[IPv4Network | IPv6Network] = []
    for entry in raw.split(","):
        value = entry.strip()
        if not value:
            continue
        try:
            networks.append(ip_network(value, strict=False))
            continue
        except ValueError:
            pass
        try:
            hosts.add(_normalise_host(value))
        except OutboundTargetError:
            # An invalid item must never broaden access or prevent startup.
            continue
    return _Allowlist(frozenset(hosts), tuple(networks))


def _embedded_addresses(address: IPv4Address | IPv6Address) -> list[IPv4Address | IPv6Address]:
    addresses: list[IPv4Address | IPv6Address] = [address]
    if isinstance(address, IPv6Address):
        if address.ipv4_mapped is not None:
            addresses.append(address.ipv4_mapped)
        if address.sixtofour is not None:
            addresses.append(address.sixtofour)
        if address.teredo is not None:
            addresses.extend(address.teredo)
    return addresses


def _network_allowed(
    address: IPv4Address | IPv6Address,
    allowlist: _Allowlist,
) -> bool:
    for candidate in _embedded_addresses(address):
        if not candidate.is_global and not any(candidate in network for network in allowlist.networks):
            return False
    return True


def _resolve(host: str, port: int) -> tuple[IPv4Address | IPv6Address, ...]:
    try:
        literal = ip_address(host.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None:
        return (literal,)
    try:
        results = socket.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except (socket.gaierror, UnicodeError) as exc:
        raise OutboundTargetError("外连域名无法解析") from exc
    addresses: list[IPv4Address | IPv6Address] = []
    for result in results:
        try:
            address = ip_address(str(result[4][0]).split("%", 1)[0])
        except ValueError:
            continue
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise OutboundTargetError("外连域名没有可用地址")
    return tuple(addresses)


def validate_outbound_host(host: str, port: int) -> tuple[IPv4Address | IPv6Address, ...]:
    """Resolve an outbound host and reject local/private/reserved destinations.

    Private services that are intentionally required can be listed explicitly
    in ``OUTBOUND_PRIVATE_HOST_ALLOWLIST`` as exact hostnames, IP addresses or
    CIDR networks.  Every DNS answer is checked to prevent mixed public/private
    records from bypassing the rule.
    """
    try:
        checked_port = int(port)
    except (TypeError, ValueError) as exc:
        raise OutboundTargetError("外连端口格式不正确") from exc
    if not 1 <= checked_port <= 65535:
        raise OutboundTargetError("外连端口超出有效范围")
    checked_host = _normalise_host(host)
    if checked_host == "localhost" or checked_host.endswith(".localhost") or checked_host.endswith(".local"):
        allowlist = _allowlist()
        if checked_host not in allowlist.hosts:
            raise OutboundTargetError("外连地址指向本机或内网，已阻止")
    else:
        allowlist = _allowlist()
    addresses = _resolve(checked_host, checked_port)
    if checked_host not in allowlist.hosts and not all(
        _network_allowed(address, allowlist) for address in addresses
    ):
        raise OutboundTargetError("外连地址指向本机、内网或保留网络，已阻止")
    return addresses


def validate_outbound_url(
    url: str,
    *,
    allowed_schemes: frozenset[str] = frozenset({"http", "https"}),
) -> str:
    value = str(url or "").strip()
    if not value or len(value) > 2048:
        raise OutboundTargetError("外连地址为空或过长")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise OutboundTargetError("外连地址格式不正确") from exc
    scheme = parsed.scheme.lower()
    if scheme not in allowed_schemes:
        raise OutboundTargetError("外连地址协议不受支持")
    if not parsed.hostname:
        raise OutboundTargetError("外连地址缺少服务器域名")
    if parsed.username is not None or parsed.password is not None:
        raise OutboundTargetError("外连地址不能包含用户名或密码")
    validate_outbound_host(parsed.hostname, port or (443 if scheme == "https" else 80))
    return value
