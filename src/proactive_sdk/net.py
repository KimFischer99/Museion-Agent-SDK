"""Minimal egress broker (P4/P6 网络层缺口的第一块; SPEC §9 工具执行).

Covers the 公开资料跟踪 connector's real fetch path:

- HTTPS only; explicit host allowlist (exact or subdomain match);
- DNS resolved locally and pinned: the TLS connection dials one resolved
  public IP with SNI set to the host, so a rebinding answer mid-flight
  cannot move the connection to another address;
- private/loopback/link-local/reserved addresses refused (no SSRF into
  the host network); deployment-supplied allowlist entries that resolve
  only to private space fail closed;
- redirects are refused (a redirect is a decision, not a fetch);
- bounded response size, bounded content types, no credentials attached.

残余风险（如实记录）：解析与连接之间的窗口由"连接后证书校验 + SNI =
allowlisted host"收窄，但不做 TOCTOU 双解析比对；跨主机重定向目标不在
本模块能力内（直接拒绝）。
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urlsplit

from .contracts import ErrorCode, PASError

__all__ = ["EgressBroker", "EgressBlocked"]

_MAX_REDIRECTS = 0  # always refused
_DEFAULT_MAX_BYTES = 1024 * 1024
_ALLOWED_TYPES_PREFIX = ("text/", "application/json", "application/atom+xml", "application/xml")


class EgressBlocked(PASError):
    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.PERMISSION_DENIED, message, scope="net")


@dataclass(frozen=True)
class _Target:
    host: str
    port: int
    path: str
    query: str


class _PinnedConnection(http.client.HTTPSConnection):
    """HTTPS connection dialing a pre-resolved public IP with SNI pinned
    to the allowlisted host."""

    def __init__(self, host: str, ip: str, port: int, *, timeout: float,
                 ssl_context: ssl.SSLContext | None = None) -> None:
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = ip
        self._host = host
        self._ssl_context = ssl_context

    def connect(self) -> None:  # type: ignore[override]
        ctx = self._ssl_context or ssl.create_default_context()
        self.sock = ctx.wrap_socket(
            socket.create_connection((self._pinned_ip, self.port), timeout=self.timeout),
            server_hostname=self._host,
        )
        self.sock.settimeout(self.timeout)


class EgressBroker:
    def __init__(
        self,
        *,
        allowed_hosts: tuple[str, ...] | list[str],
        allowed_ports: tuple[int, ...] = (443,),
        max_bytes: int = _DEFAULT_MAX_BYTES,
        timeout_s: float = 20.0,
        allow_loopback: bool = False,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        if not allowed_hosts:
            raise PASError(
                ErrorCode.INVALID_CONFIG, "an explicit non-empty host allowlist is mandatory", scope="net"
            )
        hosts = tuple(h.strip().lower().rstrip(".") for h in allowed_hosts)
        if any(not h for h in hosts):
            raise PASError(ErrorCode.INVALID_CONFIG, "allowlist entries must be non-empty", scope="net")
        if not allowed_ports or any(not isinstance(p, int) or not 1 <= p <= 65535 for p in allowed_ports):
            raise PASError(ErrorCode.INVALID_CONFIG, "allowed_ports must be a tuple of 1..65535", scope="net")
        self.allowed_hosts = hosts
        self.allowed_ports = tuple(allowed_ports)
        self.max_bytes = max_bytes
        self.timeout_s = timeout_s
        # Loopback stays off in production; the flag exists for loopback
        # contract tests and must never be enabled implicitly.
        self._allow_loopback = bool(allow_loopback)
        # Custom trust anchor (private CA / contract-test fixture). Never
        # used to disable verification.
        self._ssl_context = ssl_context

    # ------------------------------------------------------------------ #

    def fetch_text(self, url: str) -> str:
        target = self._check_url(url)
        ips = self._resolve(target.host)
        status, headers, body = self._fetch(target, ips)
        if status in (301, 302, 303, 307, 308):
            raise EgressBlocked("redirect refused: a redirect is a decision, not a fetch")
        if status != 200:
            raise EgressBlocked(f"non-200 response: {status}")
        content_type = ""
        for key, value in headers:
            if key.lower() == "content-type":
                content_type = value.lower()
                break
        if content_type and not content_type.startswith(_ALLOWED_TYPES_PREFIX):
            raise EgressBlocked(f"content type not allowed: {content_type.split(';')[0]}")
        text = body.decode("utf-8", errors="replace")
        return text

    # ------------------------------------------------------------------ #

    def _check_url(self, url: str) -> _Target:
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise EgressBlocked("only https URLs are allowed")
        if not parts.hostname or parts.username or parts.password:
            raise EgressBlocked("URL must carry a bare hostname")
        host = parts.hostname.lower().rstrip(".")
        if not any(host == h or host.endswith("." + h) for h in self.allowed_hosts):
            raise EgressBlocked(f"host {host!r} is not on the allowlist")
        port = parts.port if parts.port is not None else 443
        if port not in self.allowed_ports:
            raise EgressBlocked(f"port {port} is not in the deployment's allowed ports")
        return _Target(host=host, port=port, path=parts.path or "/", query=parts.query)

    def _allowed_ip(self, ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        if self._allow_loopback and ip.is_loopback:
            return True
        return not (
            ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified
        )

    def _resolve(self, host: str) -> list[str]:
        try:
            infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise EgressBlocked(f"DNS resolution failed: {exc.__class__.__name__}") from exc
        candidates: list[str] = []
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if self._allowed_ip(ip):
                candidates.append(str(ip))
        if not candidates:
            raise EgressBlocked(
                "host resolved only to private/reserved addresses; refusing (SSRF guard)"
            )
        return candidates

    def _fetch(self, target: _Target, ips: list[str]) -> tuple[int, list[tuple[str, str]], bytes]:
        last_error: PASError = EgressBlocked("no resolved address was reachable")
        for ip in ips:
            conn = _PinnedConnection(
                target.host, ip, target.port, timeout=self.timeout_s, ssl_context=self._ssl_context
            )
            try:
                return self._single_fetch(conn, target)
            except EgressBlocked as exc:
                last_error = exc
                if "refused" in str(exc) or "timed out" in str(exc):
                    continue  # next resolved address (v6/v4 fallback)
                raise
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
        raise last_error

    def _single_fetch(
        self, conn: _PinnedConnection, target: _Target
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        try:
            path = target.path + (f"?{target.query}" if target.query else "")
            conn.request(
                "GET",
                path,
                headers={
                    "Host": target.host,
                    "Accept": "text/*, application/json",
                    "User-Agent": "pas-egress-broker/1.0",
                    "Connection": "close",
                },
            )
            response = conn.getresponse()
            status = response.status
            headers = response.getheaders()
            raw = response.read(self.max_bytes + 1)
            if len(raw) > self.max_bytes:
                raise EgressBlocked(f"response exceeds {self.max_bytes} bytes")
            return status, headers, raw
        except socket.timeout as exc:
            raise EgressBlocked("fetch timed out") from exc
        except ssl.SSLError as exc:
            raise EgressBlocked(f"TLS failure: {exc.__class__.__name__}") from exc
        except OSError as exc:
            raise EgressBlocked(f"connection failure: {exc.__class__.__name__}") from exc
