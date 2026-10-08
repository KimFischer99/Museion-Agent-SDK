"""First-run setup and readiness for the three-file local release."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import shlex
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import Job, JobSpec, PASError, ErrorCode
from .config import PasConfig
from .daemon import DaemonLock
from .decision_parse import strict_json
from .service import _probe_socket

_ENV_KEYS = frozenset({
    "PAS_EXECUTOR", "PAS_MODEL_BASE_URL", "PAS_MODEL_API_KEY", "PAS_MODEL_NAME",
    "PAS_HERMES_URL", "PAS_HERMES_TOKEN", "PAS_PI_COMMAND", "PAS_PI_ENTRY", "PAS_PI_CWD",
    "PAS_NOTIFY_URL", "PAS_NOTIFY_CHANNEL", "PAS_NOTIFY_HOST", "PAS_GWS_COMMAND",
})
_REQUIRED = {
    "model": ("PAS_MODEL_BASE_URL", "PAS_MODEL_API_KEY", "PAS_MODEL_NAME"),
    "hermes": ("PAS_HERMES_URL", "PAS_HERMES_TOKEN"),
    "pi": ("PAS_PI_COMMAND", "PAS_PI_ENTRY"),
}


def _error(message: str) -> PASError:
    return PASError(ErrorCode.INVALID_CONFIG, message, scope="launcher")


def _defaults(root: Path, state_dir: str | None) -> dict:
    previous = root / "state"
    state = Path(state_dir).expanduser() if state_dir else (
        previous if previous.exists() else Path.home() / ".local/state/museion"
    )
    return {"state_dir": str(state.resolve()), "timezone": "UTC",
            "environment": {key: os.environ[key] for key in _ENV_KEYS if key in os.environ}}


def load_settings(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise _error("settings.json 必须是普通本地文件且权限为 0600；请修正文件权限后重试。")
    try:
        if path.stat().st_size > 65_536:
            raise ValueError
        settings = strict_json(path.read_text())
    except (ValueError, UnicodeError):
        raise _error("settings.json 不是有效的配置对象；可运行 setup --state-dir 原状态目录 重新配置。") from None
    if not isinstance(settings, dict) or set(settings) != {"state_dir", "timezone", "environment"}:
        raise _error("settings.json 需要 state_dir、timezone 和 environment 三项。")
    environment = settings["environment"]
    if not isinstance(environment, dict) or set(environment) - _ENV_KEYS or any(
        not isinstance(value, str) or len(value) > 4096 for value in environment.values()
    ):
        raise _error("settings.json 的 environment 含不支持的配置项或类型。")
    if not isinstance(settings["state_dir"], str) or not settings["state_dir"].strip():
        raise _error("settings.json 的 state_dir 不能为空。")
    try:
        ZoneInfo(settings["timezone"])
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise _error("时区无效；请使用 UTC 或 America/Los_Angeles 等时区名称。") from None
    return settings


def save_settings(path: Path, settings: dict) -> None:
    if path.is_symlink():
        raise _error("settings.json 不允许使用符号链接。")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            os.chmod(temporary, 0o600)
            json.dump(settings, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _ask(label: str, default: str = "", *, secret: bool = False, required: bool = False) -> str:
    if secret and not sys.stdin.isatty():
        if default:
            return default
        raise _error("凭据输入需要交互式终端；也可先设置对应 PAS_* 环境变量再运行 setup。")
    prompt = label + (" [已配置，回车保留]" if secret and default else f" [{default}]" if default else "") + "："
    value = (getpass.getpass(prompt) if secret else input(prompt)).strip() or default
    if required and not value:
        raise _error(f"{label}不能为空，请重新运行 setup。")
    return value


def collect_settings(previous: dict) -> tuple[dict, list[str], bool, bool]:
    old = previous["environment"]
    print("选择运行方式：1 仅提醒（无需模型）  2 模型 API  3 Pi  4 Hermes")
    default_kind = {"pi": "3", "hermes": "4"}.get(old.get("PAS_EXECUTOR"), "2" if any(old.get(key) for key in _REQUIRED["model"]) else "1")
    choice = _ask("运行方式", default_kind)
    if choice not in {"1", "2", "3", "4"}:
        raise _error("运行方式请选择 1、2、3 或 4。")
    kind = {"1": "model", "2": "model", "3": "pi", "4": "hermes"}[choice]
    environment = {"PAS_EXECUTOR": kind}
    if choice == "2":
        environment.update({
            "PAS_MODEL_BASE_URL": _ask("模型 API 地址", old.get("PAS_MODEL_BASE_URL", ""), required=True),
            "PAS_MODEL_API_KEY": _ask("API key", old.get("PAS_MODEL_API_KEY", ""), secret=True, required=True),
            "PAS_MODEL_NAME": _ask("模型名称", old.get("PAS_MODEL_NAME", ""), required=True),
        })
    elif choice == "3":
        try:
            old_command = shlex.split(old.get("PAS_PI_COMMAND", ""))
        except ValueError:
            print("旧 Node 命令格式无效，请重新填写可执行文件。", file=sys.stderr)
            old_command = []
        node = _ask("Node 可执行文件", old_command[0] if old_command else "node", required=True)
        environment["PAS_PI_COMMAND"] = shlex.join([node, "--experimental-strip-types",
            str(Path(__file__).parent / "pi_worker/pi_worker.ts")])
        environment["PAS_PI_ENTRY"] = _ask("已安装 Pi 的 dist/index.js 路径", old.get("PAS_PI_ENTRY", ""), required=True)
        if old.get("PAS_PI_CWD"):
            environment["PAS_PI_CWD"] = old["PAS_PI_CWD"]
    elif choice == "4":
        environment["PAS_HERMES_URL"] = _ask("Hermes API 地址", old.get("PAS_HERMES_URL", ""), required=True)
        environment["PAS_HERMES_TOKEN"] = _ask("Hermes token", old.get("PAS_HERMES_TOKEN", ""), secret=True, required=True)
    tz = _ask("时区", previous["timezone"])
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise _error("时区无效，请使用 UTC 或 America/Los_Angeles 等时区名称。") from None
    notify = _ask("通知方式：1 本地收件箱，2 自己的 HTTPS webhook", "2" if old.get("PAS_NOTIFY_URL") else "1")
    if notify == "2":
        url = _ask("自己的 webhook URL", old.get("PAS_NOTIFY_URL", ""), secret=True, required=True)
        try:
            parts = urlsplit(url)
            _ = parts.port
        except ValueError:
            raise _error("Webhook URL 无效。") from None
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise _error("Webhook 需要不含用户名/密码的 HTTPS URL。")
        environment.update(PAS_NOTIFY_URL=url, PAS_NOTIFY_HOST=parts.hostname, PAS_NOTIFY_CHANNEL="push:owner")
    elif notify != "1":
        raise _error("通知方式请选择 1 或 2。")
    if old.get("PAS_GWS_COMMAND"):
        environment["PAS_GWS_COMMAND"] = old["PAS_GWS_COMMAND"]
    topics, enable = [], False
    if choice != "1":
        text = _ask("公开新闻主题（逗号分隔，最多 8 项；主题将发送给新闻搜索服务）")
        topics = list(dict.fromkeys(t.strip().casefold() for t in text.replace("，", ",").split(",") if t.strip()))
        if len(topics) > 8 or any(len(topic) > 128 for topic in topics):
            raise _error("公开主题最多 8 项，每项最多 128 字符。")
        enable = _ask("启用/保持后台研究，允许将已保存记忆交给所选模型分析并向自己发送通知？y/N", "N").casefold() in {"y", "yes", "是"}
    demo = choice == "1" and _ask("创建一条 5 秒后的体验提醒？y/N", "N").casefold() in {"y", "yes", "是"}
    return {"state_dir": previous["state_dir"], "timezone": tz, "environment": environment}, topics, enable, demo


def _apply_environment(settings: dict) -> None:
    for key in _ENV_KEYS:
        os.environ.pop(key, None)
    os.environ.update(settings["environment"])


def readiness(agent, settings: dict) -> dict:
    environment = settings["environment"]
    kind = environment.get("PAS_EXECUTOR", "model").strip().casefold() or "model"
    configured = all(environment.get(key, "").strip() for key in _REQUIRED.get(kind, ()))
    problems = []
    if kind not in _REQUIRED:
        problems.append("运行方式无效，请运行 setup。")
    if configured and kind in {"model", "hermes"}:
        key = "PAS_MODEL_BASE_URL" if kind == "model" else "PAS_HERMES_URL"
        try:
            address = urlsplit(environment[key])
            _ = address.port
            valid = address.scheme in {"http", "https"} and address.hostname and not address.username and not address.password
        except ValueError:
            valid = False
        if not valid:
            problems.append("模型/宿主 API 地址需要有效的 HTTP(S) URL，凭据应使用独立配置项。")
    if kind == "pi":
        command = shlex.split(environment.get("PAS_PI_COMMAND", ""))
        if not command or not shutil.which(command[0]):
            problems.append("找不到 Node 可执行文件。")
        if not Path(environment.get("PAS_PI_ENTRY", "")).expanduser().is_file():
            problems.append("找不到已安装的 Pi entry 文件。")
    limit = 104 if sys.platform == "darwin" else 108
    if len(os.fsencode(agent.state_dir / "pas.sock")) >= limit:
        problems.append("状态目录路径过长，Unix socket 无法启动；请用 --state-dir 指定短路径。")
    prefs = agent.proactive_preferences()
    topics = agent.proactive.public_topics()
    ready, _grants = agent.proactive._has_research_prerequisites()
    job = agent.jobs_get("proactive-research")
    if not configured:
        state, detail = "reminder_only", "提醒模式可用；主动研究需要模型配置。"
    elif not prefs["enabled"]:
        state, detail = "disabled", "主动通知已关闭；确定时间提醒仍可运行。"
    elif not topics:
        state, detail = "waiting_interests", "没有可研究的公开兴趣，或兴趣被主题规则过滤。"
    elif not ready:
        state, detail = "waiting_authorization", "公开研究尚未获完整授权；可用 say 启用主动通知。"
    elif job is None:
        state, detail = "waiting_job", "研究任务未创建；可用 say 启用主动通知。"
    elif job.stopped_at_ms is not None:
        state, detail = "stopped", "研究任务已停止跟踪；需要明确创建新的跟踪任务。"
    elif not job.enabled:
        state, detail = "paused", "研究任务已暂停。"
    else:
        state, detail = "ready", "公开研究已就绪，将按计划检查；无变化时保持静默。"
    if problems:
        state, detail = "blocked", "启动前检查未通过，请修正 problems 中列出的配置。"
    lock = DaemonLock(agent.state_dir)
    locked = lock.path.exists() and lock._owner_alive()
    daemon = "running" if locked and _probe_socket(str(agent.state_dir / "pas.sock")) else "starting_or_locked" if locked else "stopped"
    activity = agent.activity_list(limit=5)
    latest = activity[0] if activity else None
    recent = "尚无执行记录。"
    if latest:
        reason = str(latest.get("reason") or "")
        if latest.get("state") in {"failed", "failed_terminal", "unknown"} or reason.startswith(("l0_source_error", "l0_context_stale", "l0_source_unauthorized")):
            recent = "最近一次检查或投递失败，请查看 activity 中的原因。"
            if state == "ready":
                state, detail = "attention", "最近检查失败，需要处理 activity 中的原因。"
        elif latest.get("state") == "suppressed":
            recent = "最近一次检查保持静默；原因：" + reason
        else:
            recent = "最近一次状态：" + str(latest.get("state"))
    counts = agent.store.outbox_state_counts()
    if counts.get("unknown", 0):
        recent = "存在投递结果未确认的通知，请查看 outbox 状态。"
        if state == "ready":
            state, detail = "attention", "通知投递结果未确认，需要查看 outbox 状态。"
    return {"can_start": not problems, "proactive_ready": state == "ready",
            "state": state, "detail": detail, "problems": problems,
            "backend": {"kind": kind, "configured": configured, "connection": "not_checked"},
            "notification": "webhook" if environment.get("PAS_NOTIFY_URL") else "local_inbox",
            "daemon": daemon, "state_dir": str(agent.state_dir),
            "public_topics": list(topics), "next_due_ms": job.next_due_ms if job else None,
            "recent_status": recent, "activity": activity, "outbox": counts}


def _check_topic_capacity(agent, topics: list[str]) -> None:
    old = {item["topic"] for item in agent.proactive.interests() if item["public"]}
    if len(old | set(topics)) > 8:
        raise _error("现有与新增公开兴趣合计超过 8 项，请先整理兴趣后重试。")


async def _configure(agent, topics: list[str], enable: bool, demo: bool) -> None:
    _check_topic_capacity(agent, topics)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    for topic in topics:
        await agent.remember_user_context(agent.proactive._interest_entry(topic, public=True, now=now))
    if enable:
        agent.proactive.enable(actor="launcher")
    else:
        agent.proactive.disable()
    job = agent.jobs_get("proactive-research")
    if job is not None and job.stopped_at_ms is None:
        policy = {**job.delivery_policy, "notification_profile": agent.policy.config.notification_profile}
        if policy != job.delivery_policy:
            agent.jobs_upsert(JobSpec(job_id=job.job_id, revision=job.revision+1, mode=job.mode,
                schedule=job.schedule, task=job.task, enabled=job.enabled, owner=job.owner,
                grant_refs=job.grant_refs, delivery_policy=policy, misfire_policy=job.misfire_policy,
                deadline=datetime.fromtimestamp(job.deadline_ms/1000, timezone.utc).isoformat() if job.deadline_ms else None,
                reminder=job.reminder, obligation=job.obligation))
    if demo:
        grant = agent.grant("notify.self", evidence="consent:launcher-demo")
        old_job = agent.jobs_get("first-demo")
        agent.jobs_upsert(Job(id="first-demo", revision=old_job.revision + 1 if old_job else 1,
            mode="reminder", schedule={"kind": "runonce", "at": (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()},
            reminder={"body": "体验提醒已送达。你现在可以创建自己的提醒。", "timezone": agent.timezone},
            grant_refs=(grant.grant_id,)))


def main(factory, root: Path, argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Museion：首次配置后直接运行。默认检查不会发起付费模型请求。")
    parser.add_argument("action", nargs="?", default="run", choices=("run", "setup", "check", "status", "inbox", "say"))
    parser.add_argument("text", nargs="?")
    parser.add_argument("--state-dir", help="明确指定持久状态目录")
    parser.add_argument("--inbox-id", help="say 知道了 时对应的通知 id")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    path = root / "settings.json"
    try:
        try:
            settings = load_settings(path) if path.exists() else _defaults(root, args.state_dir)
        except PASError:
            if args.action != "setup" or not args.state_dir:
                raise
            print("现有配置未通过检查；将重新配置，使用明确指定的状态目录。", file=sys.stderr)
            settings = _defaults(root, args.state_dir)
        if args.state_dir:
            settings["state_dir"] = str(Path(args.state_dir).expanduser().resolve())
        setup = args.action == "setup" or args.action == "run" and not path.exists()
        topics, enable, demo = [], False, False
        if setup:
            if args.action == "run" and not sys.stdin.isatty():
                raise _error("首次启动需要交互配置。请在终端运行 python3 app.py，或先运行 setup。")
            settings, topics, enable, demo = collect_settings(settings)
        _apply_environment(settings)

        async def run():
            state_dir = Path(settings["state_dir"]).expanduser()
            state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
            if state_dir.stat().st_mode & 0o077:
                raise _error("状态目录需限制为 0700；请修正该目录权限，或用 --state-dir 选择新的专用目录。")
            agent = factory(PasConfig(state_dir=settings["state_dir"], profile="personal", timezone=settings["timezone"], locale="zh-CN"))
            try:
                if setup:
                    initial = readiness(agent, settings)
                    if initial["daemon"] != "stopped":
                        raise _error("已有运行实例；请先在原终端 Ctrl-C 停止，再运行 setup 修改配置。")
                    if not initial["can_start"]:
                        raise _error("；".join(initial["problems"]))
                    _check_topic_capacity(agent, topics)
                    save_settings(path, settings)
                    await _configure(agent, topics, enable, demo)
                if args.action == "say":
                    if not args.text:
                        raise _error("say 需要用户本人输入的文本。")
                    result = await agent.proactive.handle_input(args.text, actor="launcher", inbox_id=args.inbox_id)
                elif args.action == "inbox":
                    result = {"notifications": agent.inbox_list(limit=20)}
                else:
                    result = readiness(agent, settings)
                print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 2), flush=True)
                if args.action == "run":
                    if not result["can_start"]:
                        return 2
                    print("保持此终端运行；Ctrl-C 停止。在另一终端运行 python3 app.py inbox 查看通知。", flush=True)
                    print("模型和外部来源连接尚未验证；真实执行结果可用 status 查看。", flush=True)
                    await agent.serve()
                return 0 if args.action != "check" or result["can_start"] and result["state"] in {"ready", "reminder_only", "disabled", "paused", "stopped"} else 2
            finally:
                await agent.close()

        return asyncio.run(run())
    except KeyboardInterrupt:
        return 0
    except EOFError:
        print("配置输入中断；请重新运行 setup。", file=sys.stderr)
        return 2
    except PASError as exc:
        print(f"配置/运行检查失败：[{exc.code.value}] {exc.safe_message}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"本地文件或环境不可用：{type(exc).__name__}；请检查目录权限和安装环境。", file=sys.stderr)
        return 2
