"""Google Workspace skill adapter: restricted ``hatch_gws_cli`` grammar
(SPEC §11.4; P6 邮件/日历最小兼容工具).

原则（逐条对应 SPEC §11.4 表）：

- **argv 列表解析**：命令必须是已解析的 argv 列表，不接受任意 shell 拼接；
  未识别的子命令返回 ``unsupported_command``，不"尽力猜测"后调用更高
  权限 API。
- **只读子集**：首版只实现并测试确定需要的读命令（gmail
  status/+triage/+read，calendar status/+agenda）。所有写操作（+send、
  +draft、事件创建……）一律 unsupported——写入走 PAS 的 actions/outbox
  审批链，不从这里旁路。
- **不伪造授权**：status 返回 not_connected 时如实携带 provider 给出的
  connect_url（可能为 None）；provider 没给 URL 就是 unavailable，绝不
  编造链接或凭据（不得模拟已授权）。后续命令遇 auth 错误如实上抛
  reauth_required。
- **locale 覆盖**：gmail skill 内置"输出必须是英文"等宿主风格假设；
  本适配器只产出类型化数据，摘要语言由 PAS 按用户 locale 决定——
  兼容层显式覆盖宿主约束。
- 路径不落真实机器：不创建 /opt/hatch、不覆写 home；sidecar 的
  path_strategy 恒为 isolated_virtual_mount。
"""

from __future__ import annotations

import json
import math
import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from .contracts import ErrorCode, PASError

__all__ = [
    "GwsAdapter",
    "GwsResult",
    "GwsConnector",
    "SubprocessGwsConnector",
    "GwsStatus",
]

_TRUTHY_FLAGS = ("--headers", "--json")
_VALUE_FLAGS = ("--query", "--max", "--id", "--days", "--format", "--account", "--calendar", "--for-command")
_JSON_ALIASES = {"triage": "+triage", "read": "+read", "agenda": "+agenda"}


class GwsConnector:
    """Provider transport behind the grammar. Deployments bind a real
    connector (daemon/CLI subprocess with a typed interface); tests bind a
    scripted one. The adapter alone never talks to the network."""

    def call(self, service: str, args: list[str]) -> dict[str, Any]:
        raise NotImplementedError


class SubprocessGwsConnector(GwsConnector):
    """Run a configured read-only-compatible ``hatch_gws_cli`` command."""

    def __init__(
        self,
        command: tuple[str, ...] | list[str],
        *,
        timeout_s: float = 10,
        max_output_bytes: int = 1_048_576,
    ) -> None:
        if not isinstance(command, (tuple, list)) or not command or not all(
            isinstance(part, str) and part for part in command
        ):
            raise PASError(ErrorCode.INVALID_CONFIG, "command must be a non-empty argv", scope="gws")
        if (
            not isinstance(timeout_s, (int, float))
            or isinstance(timeout_s, bool)
            or not math.isfinite(timeout_s)
            or not 0 < timeout_s <= 120
        ):
            raise PASError(ErrorCode.INVALID_CONFIG, "timeout_s must be in (0, 120]", scope="gws")
        if (
            not isinstance(max_output_bytes, int)
            or isinstance(max_output_bytes, bool)
            or not 1 <= max_output_bytes <= 1_048_576
        ):
            raise PASError(ErrorCode.INVALID_CONFIG, "max_output_bytes must be 1..1MiB", scope="gws")
        self.command = tuple(command)
        self.timeout_s = float(timeout_s)
        self.max_output_bytes = max_output_bytes

    def call(self, service: str, args: list[str]) -> dict[str, Any]:
        if service not in ("gmail", "calendar"):
            raise PASError(ErrorCode.UNSUPPORTED_CAPABILITY, "unsupported GWS service", scope="gws")
        if not isinstance(args, list) or not all(isinstance(arg, str) and arg for arg in args):
            raise PASError(ErrorCode.INVALID_CONFIG, "args must be a list of non-empty strings", scope="gws")

        try:
            proc = subprocess.Popen(
                [*self.command, service, *args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=(os.name == "posix"),
            )
        except (OSError, ValueError) as exc:
            raise PASError(ErrorCode.PROVIDER_UNAVAILABLE, "could not start GWS CLI", scope="gws") from exc

        assert proc.stdout is not None
        deadline = time.monotonic() + self.timeout_s
        output = bytearray()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise PASError(ErrorCode.PROVIDER_UNAVAILABLE, "GWS CLI timed out", scope="gws")
                    chunk = os.read(proc.stdout.fileno(), min(65536, self.max_output_bytes + 1 - len(output)))
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > self.max_output_bytes:
                        raise PASError(ErrorCode.PROVIDER_UNAVAILABLE, "GWS CLI output exceeded limit", scope="gws")
            try:
                return_code = proc.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as exc:
                raise PASError(ErrorCode.PROVIDER_UNAVAILABLE, "GWS CLI timed out", scope="gws") from exc
            if return_code != 0:
                raise PASError(ErrorCode.PROVIDER_UNAVAILABLE, f"GWS CLI exited with status {return_code}", scope="gws")
        except PASError:
            self._terminate(proc)
            raise
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            self._terminate(proc)
            raise PASError(ErrorCode.PROVIDER_UNAVAILABLE, "GWS CLI I/O failed", scope="gws") from exc
        finally:
            try:
                proc.stdout.close()
            except OSError:
                pass
            if proc.poll() is None:
                self._terminate(proc)

        try:
            data = json.loads(output.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PASError(ErrorCode.PROVIDER_UNAVAILABLE, "GWS CLI returned invalid JSON", scope="gws") from exc
        if not isinstance(data, dict):
            raise PASError(ErrorCode.PROVIDER_UNAVAILABLE, "GWS CLI JSON must be an object", scope="gws")
        return data

    @staticmethod
    def _terminate(proc: subprocess.Popen[bytes]) -> None:
        if os.name == "posix":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                if proc.poll() is None:
                    proc.kill()
        else:
            if proc.poll() is None:
                proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


@dataclass(frozen=True)
class GwsResult:
    """One executed (or refused) grammar command."""

    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None  # unsupported_command | not_connected | reauth_required | provider_error
    safe_error: str | None = None

    @property
    def connect_url(self) -> str | None:
        """The provider-provided connect URL, verbatim; never synthesized."""
        url = self.data.get("connect_url") if isinstance(self.data, dict) else None
        return url if isinstance(url, str) and url else None


@dataclass(frozen=True)
class GwsStatus:
    service: str
    connected: bool
    connect_url: str | None  # None = provider did not offer one → unavailable
    accounts: tuple[dict[str, str], ...] = ()


class GwsAdapter:
    """Executes the audited read-only subset of the legacy grammar."""

    def __init__(self, *, connector: GwsConnector, default_account: str | None = None) -> None:
        if not isinstance(connector, GwsConnector):
            raise PASError(ErrorCode.INVALID_CONFIG, "connector must be a GwsConnector", scope="gws")
        self._connector = connector
        self._default_account = default_account

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #

    def execute(self, argv: list[str]) -> GwsResult:
        if not isinstance(argv, list) or not argv or not all(
            isinstance(part, str) and part for part in argv
        ):
            return GwsResult(False, error_code="unsupported_command",
                             safe_error="argv must be a non-empty list of strings")
        if argv[0] != "hatch_gws_cli":
            return GwsResult(False, error_code="unsupported_command",
                             safe_error="commands must start with hatch_gws_cli")
        rest = argv[1:]
        if not rest:
            return GwsResult(False, error_code="unsupported_command", safe_error="no service given")
        service, service_args = rest[0], rest[1:]
        try:
            if service == "gmail":
                return self._gmail(service_args)
            if service == "calendar":
                return self._calendar(service_args)
        except _FlagError:
            return GwsResult(False, error_code="unsupported_command", safe_error="malformed flags")
        return GwsResult(False, error_code="unsupported_command",
                         safe_error=f"service {service!r} is not in the audited subset")

    # ------------------------------------------------------------------ #
    # Grammar
    # ------------------------------------------------------------------ #

    def _gmail(self, args: list[str]) -> GwsResult:
        if not args:
            return GwsResult(False, error_code="unsupported_command", safe_error="no gmail command")
        head = args[0]
        if head not in ("status", "+triage", "+read"):
            return GwsResult(False, error_code="unsupported_command",
                             safe_error=f"gmail command {head!r} is not in the audited read subset "
                                        "(writes go through PAS approvals, never this adapter)")
        flags = _parse_flags(args[1:])
        if head == "status":
            status = self._status("gmail", flags)
            return _status_result(status)
        if head in ("+triage", "+read"):
            return self._gmail_read(head, flags)
        return GwsResult(False, error_code="unsupported_command", safe_error="unreachable")

    def _calendar(self, args: list[str]) -> GwsResult:
        if not args:
            return GwsResult(False, error_code="unsupported_command", safe_error="no calendar command")
        head = args[0]
        if head not in ("status", "+agenda"):
            return GwsResult(False, error_code="unsupported_command",
                             safe_error=f"calendar command {head!r} is not in the audited read subset "
                                        "(event writes go through PAS approvals)")
        flags = _parse_flags(args[1:])
        if head == "status":
            return _status_result(self._status("calendar", flags))
        if head == "+agenda":
            return self._calendar_agenda(flags)
        return GwsResult(False, error_code="unsupported_command",
                         safe_error=f"calendar command {head!r} is not in the audited read subset "
                                    "(event writes go through PAS approvals)")

    def _gmail_read(self, head: str, flags: dict[str, Any]) -> GwsResult:
        if head == "+triage":
            query = flags.get("query")
            if not isinstance(query, str) or not query:
                return GwsResult(False, error_code="unsupported_command",
                                 safe_error="+triage requires --query")
            try:
                max_items = int(flags.get("max", 20))
            except (TypeError, ValueError):
                return GwsResult(False, error_code="unsupported_command", safe_error="--max must be an integer")
            if not 1 <= max_items <= 50:
                return GwsResult(False, error_code="unsupported_command",
                                 safe_error="--max must be 1..50 (cost bound)")
            provider_args = ["+triage", "--query", query, "--max", str(max_items), "--format", "json"]
        else:  # +read
            message_id = flags.get("id")
            if not isinstance(message_id, str) or not message_id or len(message_id) > 256:
                return GwsResult(False, error_code="unsupported_command", safe_error="+read requires --id")
            provider_args = ["+read", "--id", message_id, "--headers", "--format", "json"]
        account = flags.get("account", self._default_account)
        if isinstance(account, str) and account:
            provider_args.extend(["--account", account])
        return self._call_provider("gmail", provider_args)

    def _calendar_agenda(self, flags: dict[str, Any]) -> GwsResult:
        provider_args = ["+agenda"]
        if "today" in flags:
            provider_args.append("--today")
        elif "week" in flags:
            provider_args.append("--week")
        elif "days" in flags:
            try:
                days = int(flags["days"])
            except (TypeError, ValueError):
                return GwsResult(False, error_code="unsupported_command", safe_error="--days must be an integer")
            if not 1 <= days <= 30:
                return GwsResult(False, error_code="unsupported_command", safe_error="--days must be 1..30")
            provider_args.extend(["--days", str(days)])
        else:
            provider_args.append("--today")
        if flags.get("calendar"):
            provider_args.extend(["--calendar", str(flags["calendar"])])
        provider_args.extend(["--format", "json"])
        account = flags.get("account", self._default_account)
        if isinstance(account, str) and account:
            provider_args.extend(["--account", account])
        return self._call_provider("calendar", provider_args)

    # ------------------------------------------------------------------ #
    # Provider plumbing
    # ------------------------------------------------------------------ #

    def _status(self, service: str, flags: dict[str, Any]) -> GwsStatus:
        args = ["status"]
        if flags.get("for_command"):
            args.extend(["--for-command", str(flags["for_command"])])
        result = self._call_provider(service, args)
        if not result.ok:
            return GwsStatus(service=service, connected=False, connect_url=None)
        data = result.data
        connected = data.get("connected") is True
        url = data.get("connect_url")
        accounts = tuple(
            {"account_id": str(a.get("account_id", "")), "display_name": str(a.get("display_name", ""))}
            for a in data.get("accounts", [])
            if isinstance(a, dict)
        ) if isinstance(data.get("accounts"), list) else ()
        return GwsStatus(
            service=service,
            connected=connected,
            connect_url=url if isinstance(url, str) and url else None,
            accounts=accounts,
        )

    def _call_provider(self, service: str, args: list[str]) -> GwsResult:
        try:
            data = self._connector.call(service, args)
        except PASError as exc:
            return GwsResult(False, error_code=_provider_error_code(exc), safe_error=str(exc))
        except Exception as exc:  # noqa: BLE001 — provider internals never leak
            return GwsResult(False, error_code="provider_error",
                             safe_error=f"connector failure: {exc.__class__.__name__}")
        if not isinstance(data, dict):
            return GwsResult(False, error_code="provider_error", safe_error="connector returned a non-object")
        auth_error = data.get("auth_error")
        if auth_error:
            return GwsResult(False, error_code="reauth_required",
                             safe_error="provider reported an auth error; rerun status",
                             data={"for_command": str(auth_error)[:128]})
        return GwsResult(True, data=data)


def _provider_error_code(exc: PASError) -> str:
    if exc.code in (ErrorCode.AUTH_REQUIRED, ErrorCode.PERMISSION_DENIED):
        return "reauth_required"
    if exc.code == ErrorCode.PROVIDER_UNAVAILABLE:
        return "provider_error"
    return "provider_error"


def _status_result(status: GwsStatus) -> GwsResult:
    data: dict[str, Any] = {
        "service": status.service,
        "connected": status.connected,
        "accounts": [dict(a) for a in status.accounts],
    }
    if not status.connected and status.connect_url:
        # 连接是用户的动作（skill 原文同义）：只转发 provider 给的 URL。
        data["connect_url"] = status.connect_url
    if not status.connected and not status.connect_url:
        data["unavailable"] = True  # provider 没给 URL：如实 unavailable，不编造
    return GwsResult(True, data=data)


def _parse_flags(argv: list[str]) -> dict[str, Any]:
    flags: dict[str, Any] = {}
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in _TRUTHY_FLAGS:
            flags[token[2:].replace("-", "_")] = True
            i += 1
        elif token in _VALUE_FLAGS:
            if i + 1 >= len(argv):
                raise _FlagError(token)
            flags[token[2:].replace("-", "_")] = argv[i + 1]
            i += 2
        elif token.startswith("--"):
            flags[token[2:].replace("-", "_")] = True
            i += 1
        else:
            raise _FlagError(token)
    return flags


class _FlagError(ValueError):
    pass


def parse_gws_argv(argv: list[str]) -> dict[str, Any]:
    """Public flag parser; malformed flags become typed unsupported results
    at the adapter boundary."""
    try:
        return _parse_flags(argv)
    except _FlagError:
        return {}
