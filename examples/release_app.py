"""Minimal release wiring for ``pas serve --app app:build_agent``."""

from __future__ import annotations

import json
import os
import shlex
import hashlib
import subprocess
import sys
import venv
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def _bootstrap() -> None:
    if sys.version_info < (3, 11) or os.name != "posix":
        raise SystemExit("需要 macOS/Linux 和 Python 3.11+。")
    root = Path(__file__).resolve().parent
    wheels = list(root.glob("proactive_sdk-*.whl"))
    if len(wheels) != 1:
        raise SystemExit("运行目录需要恰好一个 proactive_sdk wheel，请使用完整产品包。")
    environment = root / ".venv"
    python = environment / "bin/python"
    marker = environment / ".pas-wheel-sha256"
    digest = hashlib.sha256(wheels[0].read_bytes()).hexdigest()
    try:
        if not python.is_file():
            print("首次启动：准备本地 Python 环境……", file=sys.stderr, flush=True)
            venv.EnvBuilder(with_pip=True).create(environment)
        if not marker.is_file() or marker.read_text().strip() != digest:
            print("安装当前运行包……", file=sys.stderr, flush=True)
            result = subprocess.run(
                [str(python), "-m", "pip", "install", "--no-index", "--no-deps",
                 "--disable-pip-version-check", "--force-reinstall", str(wheels[0])],
                capture_output=True, text=True,
            )
            if result.returncode:
                raise SystemExit("本地 wheel 安装失败：\n" + result.stderr)
            marker.write_text(digest + "\n")
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"无法准备本地环境（{type(exc).__name__}）；请确认目录可写且 Python 提供 venv/ensurepip。") from None
    if Path(sys.prefix).resolve() != environment.resolve():
        os.execv(str(python), [str(python), "-I", "-B", str(root / "app.py"), *sys.argv[1:]])


if __name__ == "__main__":
    _bootstrap()

from proactive_sdk import (
    ChannelSink,
    ErrorCode,
    HermesHostDriver,
    HostBridge,
    OpenAICompatibleModel,
    PASError,
    PiHostDriver,
    ProactiveAgent,
    WebhookNotificationSink,
)
from proactive_sdk.connectors import GmailMailSource
from proactive_sdk.gws import GwsAdapter, SubprocessGwsConnector
from proactive_sdk.hermes import HermesRunsExecutor
from proactive_sdk.net import EgressBroker
from proactive_sdk.policy import PolicyConfig
from proactive_sdk.pi_worker import PiWorkerConfig, PiWorkerExecutor
from proactive_sdk.proactive import ProactiveController
from proactive_sdk.research import NewsSearchSource

_MODEL_ENV = ("PAS_MODEL_BASE_URL", "PAS_MODEL_API_KEY", "PAS_MODEL_NAME")


class _MissingModel:
    async def generate(self, _request: Any) -> Any:
        raise PASError(
            ErrorCode.INVALID_CONFIG,
            "model is not configured; set PAS_MODEL_BASE_URL, PAS_MODEL_API_KEY, and PAS_MODEL_NAME",
            scope="release_app",
        )


def build_agent(config=None) -> ProactiveAgent:
    """Build the release agent from trusted process configuration."""
    state_dir = Path(
        config.state_dir if config is not None else Path(__file__).resolve().parent / "state"
    ).expanduser()
    values = {name: os.environ.get(name, "") for name in _MODEL_ENV}
    present = {name: bool(value.strip()) for name, value in values.items()}
    executor_kind = os.environ.get("PAS_EXECUTOR", "model").strip().casefold() or "model"
    channel, notification_profile = _notification_channel()
    gws_command = _json_argv("PAS_GWS_COMMAND")
    model = None
    executor = None
    if executor_kind == "model":
        if any(present.values()) and not all(present.values()):
            missing = ", ".join(name for name in _MODEL_ENV if not present[name])
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                f"model configuration is incomplete; missing {missing}",
                scope="release_app",
            )
        if all(present.values()):
            base_url, api_key, model_name = (values[name] for name in _MODEL_ENV)
            model = OpenAICompatibleModel(
                base_url=base_url,
                api_key_provider=lambda: api_key,
                model=model_name,
            )
        else:
            model = _MissingModel()
    elif executor_kind == "hermes":
        base_url = os.environ.get("PAS_HERMES_URL", "").strip()
        token = os.environ.get("PAS_HERMES_TOKEN", "")
        if not base_url or not token:
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                "PAS_EXECUTOR=hermes requires PAS_HERMES_URL and PAS_HERMES_TOKEN",
                scope="release_app",
            )
        executor = HostBridge(
            HermesHostDriver(HermesRunsExecutor(base_url, token)),
            capabilities=lambda: agent.store.active_capabilities(now_ms=agent.store.clock.wall_now_ms()),
        )
    elif executor_kind == "pi":
        command_text = os.environ.get("PAS_PI_COMMAND", "").strip()
        pi_entry = os.environ.get("PAS_PI_ENTRY", "").strip()
        if not command_text or not pi_entry:
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                "PAS_EXECUTOR=pi requires PAS_PI_COMMAND and PAS_PI_ENTRY",
                scope="release_app",
            )
        try:
            command = tuple(shlex.split(command_text))
        except ValueError:
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                "PAS_PI_COMMAND must be a quoted argv command",
                scope="release_app",
            ) from None
        if not command:
            raise PASError(ErrorCode.INVALID_CONFIG, "PAS_PI_COMMAND is empty", scope="release_app")
        state_dir.mkdir(parents=True, exist_ok=True)
        cwd = os.environ.get("PAS_PI_CWD", str(state_dir)).strip() or str(state_dir)
        pi_executor = PiWorkerExecutor(PiWorkerConfig(
            command=command,
            pi_entry=pi_entry,
            allowed_tools=(),
        ))
        executor = HostBridge(
            PiHostDriver(pi_executor, cwd=cwd),
            capabilities=lambda: agent.store.active_capabilities(now_ms=agent.store.clock.wall_now_ms()),
        )
    else:
        raise PASError(
            ErrorCode.INVALID_CONFIG,
            "PAS_EXECUTOR must be model, pi, or hermes",
            scope="release_app",
        )

    policy_config = None
    if notification_profile:
        source_policy = config.policy if config is not None else None
        policy_config = PolicyConfig(
            notification_profile=notification_profile,
            max_per_day=source_policy.max_unsolicited_notifications_per_day if source_policy else None,
            cadence=source_policy.cadence if source_policy else "balanced",
            cadence_min_gap_seconds=source_policy.cadence_min_gap_seconds if source_policy else None,
            cadence_max_per_day=source_policy.cadence_max_per_day if source_policy else None,
        )

    agent = ProactiveAgent(
        state_dir=state_dir,
        executor=executor,
        model=model,
        timezone=config.timezone if config is not None else "UTC",
        profile=config.profile if config is not None else "personal",
        locale=config.locale if config is not None else "zh-CN",
        config=config,
        sinks=(channel,) if channel is not None else (),
        policy_config=policy_config,
    )
    controller = ProactiveController(agent)
    agent.proactive = controller
    agent.registry.register(
        source_id="news-search",
        account_ref="account:public",
        source=NewsSearchSource(
            topics=controller.public_topics,
            broker=EgressBroker(allowed_hosts=("www.bing.com",)),
        ),
        required_capability="public.read",
    )
    if gws_command is not None:
        mail = GmailMailSource(
            adapter=GwsAdapter(connector=SubprocessGwsConnector(gws_command)),
            account_ref="account:primary",
        )
        agent.registry.register(
            source_id=mail.source_id,
            account_ref=mail.account_ref,
            source=mail,
            required_capability="gmail.read",
        )
    return agent


def _json_argv(name: str) -> tuple[str, ...] | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        raise PASError(
            ErrorCode.INVALID_CONFIG,
            f"{name} must be a JSON argv array",
            scope="release_app",
        ) from None
    if not isinstance(value, list) or not value or any(not isinstance(part, str) or not part for part in value):
        raise PASError(
            ErrorCode.INVALID_CONFIG,
            f"{name} must be a non-empty JSON argv array",
            scope="release_app",
        )
    return tuple(value)


def _notification_channel() -> tuple[ChannelSink | None, str | None]:
    names = ("PAS_NOTIFY_URL", "PAS_NOTIFY_CHANNEL", "PAS_NOTIFY_HOST")
    values = {name: os.environ.get(name, "").strip() for name in names}
    present = {name: bool(value) for name, value in values.items()}
    if not any(present.values()):
        return None, None
    if not all(present.values()):
        missing = ", ".join(name for name in names if not present[name])
        raise PASError(
            ErrorCode.INVALID_CONFIG,
            f"webhook configuration is incomplete; missing {missing}",
            scope="release_app",
        )
    host = values["PAS_NOTIFY_HOST"].casefold().rstrip(".")
    try:
        parsed = urlsplit(values["PAS_NOTIFY_URL"])
        _ = parsed.port
    except ValueError:
        raise PASError(
            ErrorCode.INVALID_CONFIG,
            "PAS_NOTIFY_URL must be an HTTPS URL whose hostname matches PAS_NOTIFY_HOST",
            scope="release_app",
        ) from None
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.hostname.casefold().rstrip(".") != host
        or any(char.isspace() for char in host)
    ):
        raise PASError(
            ErrorCode.INVALID_CONFIG,
            "PAS_NOTIFY_URL must be an HTTPS URL whose hostname matches PAS_NOTIFY_HOST",
            scope="release_app",
        )
    return (
        ChannelSink(
            channel_ref=values["PAS_NOTIFY_CHANNEL"],
            kind="webhook",
            endpoint={"url": values["PAS_NOTIFY_URL"]},
            push_summary_only=True,
            sink=WebhookNotificationSink(),
        ),
        values["PAS_NOTIFY_CHANNEL"],
    )


if __name__ == "__main__":
    from proactive_sdk.launcher import main

    raise SystemExit(main(build_agent, Path(__file__).resolve().parent))
