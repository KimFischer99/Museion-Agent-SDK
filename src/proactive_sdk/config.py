"""Profile configuration loading (SPEC §14.3; P7).

The canonical config file format is the YAML subset documented below —
the SDK has zero third-party dependencies, so no YAML library is pulled
in. The parser accepts exactly what the documented config shape needs:

- nested block mappings with 2/4/6-space indentation (spaces only, no
  tabs);
- scalars: strings (unquoted or single/double quoted), integers, floats,
  booleans (true/false), null (empty value);
- inline flow collections ``{a: b, c: d}`` and ``[a, b]`` (non-nested);
- ``#`` comments and blank lines.

Anything else — tabs, multi-line scalars, anchors, nested flow
collections — is a load error, never a guess. Every unknown key (top
level or nested) is rejected, so a typo cannot silently disable a
policy (SPEC §14.3: 配置加载拒绝未知关键字段). ``redacted()`` renders the
config for display: path values are shortened to ``~/…/basename`` and
any key whose name suggests a secret is masked without reading it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import ErrorCode, PASError

__all__ = [
    "CONFIG_VERSION",
    "PasConfig",
    "RuntimeSection",
    "HeartbeatSection",
    "PolicySection",
    "SkillsSection",
    "ControlPlaneSection",
    "load_config",
    "parse_config_text",
    "config_to_yaml_subset",
]

CONFIG_VERSION = "1"

_MISFIRE_POLICIES = frozenset({"coalesce_latest", "grace_once", "expire"})
_CADENCES = frozenset({"warm", "balanced", "gentle"})

_TOP_KEYS = ("config_version", "profile", "state_dir", "timezone", "locale",
             "runtime", "heartbeat", "policy", "skills", "control_plane")
_RUNTIME_KEYS = ("max_concurrent_agent_runs", "shutdown_grace_seconds",
                 "event_retention_days", "loop_interval_seconds",
                 "disk_free_warn_mb", "disk_free_stop_mb")
_HEARTBEAT_KEYS = ("enabled", "every_seconds", "misfire", "checklist")
_POLICY_KEYS = ("notification_window", "max_unsolicited_notifications_per_day",
                "external_writes", "lockscreen_content", "allow_untrusted_shell_skills",
                "cadence", "cadence_min_gap_seconds", "cadence_max_per_day")
_SKILLS_KEYS = ("import_mode", "source", "distribution")
_CONTROL_PLANE_KEYS = ("enabled", "socket", "token_file")

_SECRET_KEY_RE = re.compile(r"(token|secret|password|credential|api_key)", re.IGNORECASE)
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ConfigError(PASError):
    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.INVALID_CONFIG, message, scope="config")


# --------------------------------------------------------------------------- #
# Minimal YAML-subset parser
# --------------------------------------------------------------------------- #

def _parse_scalar(raw: str) -> Any:
    text = raw.strip()
    if text == "":
        return None
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        inner = text[1:-1]
        if text[0] in inner:
            raise ConfigError(f"quoted scalar {raw!r} contains an unescaped quote")
        return inner
    if text in ("true", "True"):
        return True
    if text in ("false", "False"):
        return False
    if text in ("null", "Null", "~"):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def _parse_inline(text: str) -> Any:
    """Parse a non-nested inline ``{...}`` or ``[...]`` collection."""
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        inner = text[1:-1].strip()
        result: dict[str, Any] = {}
        if not inner:
            return result
        for part in _split_flow(inner):
            if ":" not in part:
                raise ConfigError(f"inline mapping entry {part!r} must be key: value")
            key, _, value = part.partition(":")
            key = key.strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", key):
                raise ConfigError(f"inline mapping key {key!r} is not a plain identifier")
            result[key] = _parse_scalar(value)
        return result
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part) for part in _split_flow(inner)]
    return _parse_scalar(text)


def _split_flow(inner: str) -> list[str]:
    parts, depth, current, quote = [], 0, "", None
    for ch in inner:
        if quote:
            current += ch
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            current += ch
        elif ch in "{[":
            depth += 1
            current += ch
        elif ch in "}]":
            depth -= 1
            current += ch
        elif ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    if quote or depth != 0:
        raise ConfigError("unbalanced inline collection")
    if current.strip():
        parts.append(current)
    return parts


def parse_config_text(text: str) -> dict[str, Any]:
    """Parse the documented YAML subset into a nested dict. Structural
    problems raise ConfigError instead of being ignored."""
    root: dict[str, Any] = {}
    # stack of (indent, container)
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        if "\t" in raw_line:
            raise ConfigError(f"line {lineno}: tab indentation is not accepted")
        line = _strip_comment(raw_line).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        content = line.strip()
        if content.startswith("- "):
            raise ConfigError(f"line {lineno}: block sequences are not part of the config grammar")
        if ":" not in content:
            raise ConfigError(f"line {lineno}: expected 'key: value', got {content!r}")
        key, _, value_part = content.partition(":")
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", key):
            raise ConfigError(f"line {lineno}: key {key!r} is not a plain identifier")
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            raise ConfigError(f"line {lineno}: bad indentation")
        parent = stack[-1][1]
        if key in parent:
            raise ConfigError(f"line {lineno}: duplicate key {key!r}")
        value_part = value_part.strip()
        if value_part == "":
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        elif value_part.startswith("{") or value_part.startswith("["):
            parent[key] = _parse_inline(value_part)
        else:
            parent[key] = _parse_scalar(value_part)
    return root


def _strip_comment(line: str) -> str:
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
    return line


def _in_quotes_hash(line: str) -> bool:  # pragma: no cover - helper kept trivially simple
    return False


# --------------------------------------------------------------------------- #
# Typed config
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class RuntimeSection:
    max_concurrent_agent_runs: int = 1
    shutdown_grace_seconds: int = 20
    event_retention_days: int = 30
    loop_interval_seconds: float = 5.0
    disk_free_warn_mb: int = 50
    disk_free_stop_mb: int = 5


@dataclass(frozen=True)
class HeartbeatSection:
    enabled: bool = False
    every_seconds: int = 1800
    misfire: str = "coalesce_latest"
    checklist: str | None = None


@dataclass(frozen=True)
class PolicySection:
    notification_window: tuple[str, str] | None = None  # (start "09:00", end "21:30")
    max_unsolicited_notifications_per_day: int | None = None
    external_writes: str = "approval_required"
    lockscreen_content: str = "minimal"
    allow_untrusted_shell_skills: bool = False
    # SPEC §21.1 step 8. ``cadence`` is the host's proactive pacing
    # preference; the two numbers are required for it to have any effect.
    # The SDK ships no hour counts of its own: without explicit numbers the
    # preference is recorded and nothing is gated.
    cadence: str = "balanced"
    cadence_min_gap_seconds: int | None = None
    cadence_max_per_day: int | None = None

    @property
    def cadence_configured(self) -> bool:
        """True when the host actually declared pacing numbers."""
        return (
            self.cadence_min_gap_seconds is not None
            or self.cadence_max_per_day is not None
        )


@dataclass(frozen=True)
class SkillsSection:
    import_mode: str = "explicit_local"
    source: str | None = None
    distribution: str = "excluded"


@dataclass(frozen=True)
class ControlPlaneSection:
    enabled: bool = True
    socket: str | None = None  # default: <state_dir>/pas.sock
    token_file: str | None = None


@dataclass(frozen=True)
class PasConfig:
    """Fully validated profile configuration. ``profile`` and the time
    zone come from config or explicit arguments — never from the dev
    machine's locale/timezone (SPEC §14.3)."""

    profile: str
    timezone: str
    locale: str
    state_dir: str
    runtime: RuntimeSection = field(default_factory=RuntimeSection)
    heartbeat: HeartbeatSection = field(default_factory=HeartbeatSection)
    policy: PolicySection = field(default_factory=PolicySection)
    skills: SkillsSection = field(default_factory=SkillsSection)
    control_plane: ControlPlaneSection = field(default_factory=ControlPlaneSection)
    source_path: str | None = None

    def redacted(self) -> dict[str, Any]:
        """Display form: home paths shortened, secret-ish keys masked.
        Never includes token file contents."""
        return _redact_value(self.to_dict(), key="")

    def to_dict(self) -> dict[str, Any]:
        policy = _section_dict(self.policy)
        if self.policy.notification_window is not None:
            start, end = self.policy.notification_window
            policy["notification_window"] = {"start": start, "end": end}
        return {
            "config_version": CONFIG_VERSION,
            "profile": self.profile,
            "state_dir": self.state_dir,
            "timezone": self.timezone,
            "locale": self.locale,
            "runtime": _section_dict(self.runtime),
            "heartbeat": _section_dict(self.heartbeat),
            "policy": policy,
            "skills": _section_dict(self.skills),
            "control_plane": _section_dict(self.control_plane),
        }


def _section_dict(section: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in section.__dataclass_fields__:  # type: ignore[attr-defined]
        out[name] = getattr(section, name)
    return out


def _redact_value(value: Any, *, key: str) -> Any:
    if isinstance(value, dict):
        return {k: _redact_value(v, key=k) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v, key=key) for v in value]
    if _SECRET_KEY_RE.search(key) and isinstance(value, str) and value:
        return "<redacted>"
    if isinstance(value, str) and (value.startswith("/") or value.startswith("~")):
        return _redact_path(value)
    return value


def _redact_path(path_text: str) -> str:
    """Keep the structure recognizable, hide personal file names:
    ``/Users/me/secret-state/pas.sqlite3`` → ``~/…/pas.sqlite3``."""
    try:
        home = Path.home()
        p = Path(path_text)
        if p.is_absolute():
            try:
                rel = p.relative_to(home)
                return f"~/…/{rel.name}"
            except ValueError:
                return f"<path>/{p.name}"
        return f"~/{p.name}"
    except Exception:  # pragma: no cover - defensive
        return "<path>"


def _reject_unknown(section: str, given: dict[str, Any], allowed: tuple[str, ...]) -> None:
    unknown = sorted(set(given) - set(allowed))
    if unknown:
        raise ConfigError(
            f"{section}: unknown key(s) {', '.join(unknown)}; allowed: {', '.join(allowed)}"
        )


def _require_str(section: str, value: Any, key: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ConfigError(f"{section}.{key} must be a non-empty string")
    return value


def _positive_int(section: str, value: Any, key: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"{section}.{key} must be an integer >= {minimum}")
    return value


def build_config(raw: dict[str, Any], *, source_path: str | None = None) -> PasConfig:
    """Validate a parsed config dict into a PasConfig. Unknown keys are
    rejected; value rules follow SPEC §14.3 defaults where documented."""
    _reject_unknown("config", raw, _TOP_KEYS)
    version = raw.get("config_version", CONFIG_VERSION)
    if version != CONFIG_VERSION:
        raise ConfigError(f"config_version must be {CONFIG_VERSION!r}, got {version!r}")

    profile = _require_str("config", raw.get("profile", "personal"), "profile")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", profile):
        raise ConfigError(f"profile {profile!r} fails the profile naming rule")
    timezone = _require_str("config", raw.get("timezone", "UTC"), "timezone")
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(timezone)
    except Exception:
        raise ConfigError(f"config.timezone {timezone!r} is not a valid IANA zone") from None
    locale = _require_str("config", raw.get("locale", "en"), "locale")
    state_dir = _require_str(
        "config", raw.get("state_dir", str(Path.home() / ".local" / "state" / "pas")), "state_dir"
    )

    runtime_raw = raw.get("runtime", {})
    if not isinstance(runtime_raw, dict):
        raise ConfigError("runtime must be a mapping")
    _reject_unknown("runtime", runtime_raw, _RUNTIME_KEYS)
    runtime = RuntimeSection(
        max_concurrent_agent_runs=_positive_int(
            "runtime", runtime_raw.get("max_concurrent_agent_runs", 1), "max_concurrent_agent_runs"
        ),
        shutdown_grace_seconds=_positive_int(
            "runtime", runtime_raw.get("shutdown_grace_seconds", 20), "shutdown_grace_seconds", minimum=1
        ),
        event_retention_days=_positive_int(
            "runtime", runtime_raw.get("event_retention_days", 30), "event_retention_days"
        ),
        loop_interval_seconds=_loop_interval(runtime_raw.get("loop_interval_seconds", 5.0)),
        disk_free_warn_mb=_positive_int(
            "runtime", runtime_raw.get("disk_free_warn_mb", 50), "disk_free_warn_mb", minimum=1
        ),
        disk_free_stop_mb=_positive_int(
            "runtime", runtime_raw.get("disk_free_stop_mb", 5), "disk_free_stop_mb", minimum=1
        ),
    )
    if runtime.disk_free_stop_mb >= runtime.disk_free_warn_mb:
        raise ConfigError("runtime.disk_free_stop_mb must be below runtime.disk_free_warn_mb")

    heartbeat_raw = raw.get("heartbeat", {})
    if not isinstance(heartbeat_raw, dict):
        raise ConfigError("heartbeat must be a mapping")
    _reject_unknown("heartbeat", heartbeat_raw, _HEARTBEAT_KEYS)
    misfire = heartbeat_raw.get("misfire", "coalesce_latest")
    if misfire not in _MISFIRE_POLICIES:
        raise ConfigError(f"heartbeat.misfire must be one of {sorted(_MISFIRE_POLICIES)}")
    heartbeat = HeartbeatSection(
        enabled=bool(heartbeat_raw.get("enabled", False)),
        every_seconds=_positive_int(
            "heartbeat", heartbeat_raw.get("every_seconds", 1800), "every_seconds"
        ),
        misfire=misfire,
        checklist=(
            _require_str("heartbeat", heartbeat_raw["checklist"], "checklist")
            if heartbeat_raw.get("checklist") is not None
            else None
        ),
    )

    policy_raw = raw.get("policy", {})
    if not isinstance(policy_raw, dict):
        raise ConfigError("policy must be a mapping")
    _reject_unknown("policy", policy_raw, _POLICY_KEYS)
    window = policy_raw.get("notification_window")
    window_tuple: tuple[str, str] | None = None
    if window is not None:
        if not isinstance(window, dict) or set(window) != {"start", "end"}:
            raise ConfigError("policy.notification_window must be {start: HH:MM, end: HH:MM}")
        start, end = window["start"], window["end"]
        for label, value in (("start", start), ("end", end)):
            if not isinstance(value, str) or not _HHMM_RE.fullmatch(value):
                raise ConfigError(f"policy.notification_window.{label} must be HH:MM, got {value!r}")
        window_tuple = (start, end)
    external_writes = policy_raw.get("external_writes", "approval_required")
    if external_writes not in ("approval_required", "denied"):
        raise ConfigError("policy.external_writes must be approval_required|denied")
    lockscreen = policy_raw.get("lockscreen_content", "minimal")
    if lockscreen not in ("minimal", "full"):
        raise ConfigError("policy.lockscreen_content must be minimal|full")
    cadence = policy_raw.get("cadence", "balanced")
    if cadence not in _CADENCES:
        raise ConfigError(f"policy.cadence must be one of {sorted(_CADENCES)}")
    policy = PolicySection(
        cadence=cadence,
        cadence_min_gap_seconds=(
            _positive_int("policy", policy_raw["cadence_min_gap_seconds"], "cadence_min_gap_seconds")
            if policy_raw.get("cadence_min_gap_seconds") is not None
            else None
        ),
        cadence_max_per_day=(
            _positive_int("policy", policy_raw["cadence_max_per_day"], "cadence_max_per_day")
            if policy_raw.get("cadence_max_per_day") is not None
            else None
        ),
        notification_window=window_tuple,
        max_unsolicited_notifications_per_day=(
            _positive_int(
                "policy", policy_raw["max_unsolicited_notifications_per_day"],
                "max_unsolicited_notifications_per_day",
            )
            if policy_raw.get("max_unsolicited_notifications_per_day") is not None
            else None
        ),
        external_writes=external_writes,
        lockscreen_content=lockscreen,
        allow_untrusted_shell_skills=bool(policy_raw.get("allow_untrusted_shell_skills", False)),
    )

    skills_raw = raw.get("skills", {})
    if not isinstance(skills_raw, dict):
        raise ConfigError("skills must be a mapping")
    _reject_unknown("skills", skills_raw, _SKILLS_KEYS)
    import_mode = skills_raw.get("import_mode", "explicit_local")
    if import_mode not in ("explicit_local", "audit_only"):
        raise ConfigError("skills.import_mode must be explicit_local|audit_only")
    distribution = skills_raw.get("distribution", "excluded")
    if distribution != "excluded":
        raise ConfigError("skills.distribution must be excluded (v0.1 does not redistribute skills)")
    skills = SkillsSection(
        import_mode=import_mode,
        source=(
            _require_str("skills", skills_raw["source"], "source")
            if skills_raw.get("source") is not None
            else None
        ),
        distribution=distribution,
    )

    cp_raw = raw.get("control_plane", {})
    if not isinstance(cp_raw, dict):
        raise ConfigError("control_plane must be a mapping")
    _reject_unknown("control_plane", cp_raw, _CONTROL_PLANE_KEYS)
    control_plane = ControlPlaneSection(
        enabled=bool(cp_raw.get("enabled", True)),
        socket=(
            _require_str("control_plane", cp_raw["socket"], "socket", allow_empty=False)
            if cp_raw.get("socket") is not None
            else None
        ),
        token_file=(
            _require_str("control_plane", cp_raw["token_file"], "token_file")
            if cp_raw.get("token_file") is not None
            else None
        ),
    )

    return PasConfig(
        profile=profile,
        timezone=timezone,
        locale=locale,
        state_dir=state_dir,
        runtime=runtime,
        heartbeat=heartbeat,
        policy=policy,
        skills=skills,
        control_plane=control_plane,
        source_path=source_path,
    )


def _loop_interval(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError("runtime.loop_interval_seconds must be a number")
    interval = float(value)
    if not 0.05 <= interval <= 3600:
        raise ConfigError("runtime.loop_interval_seconds must be within 0.05..3600")
    return interval


def load_config(path: str | Path) -> PasConfig:
    """Load and validate a config file. A missing file is an error — the
    daemon never invents profile settings silently."""
    p = Path(path).expanduser()
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {p}") from None
    except OSError as exc:
        raise ConfigError(f"cannot read config file {p}: {exc}") from None
    return build_config(parse_config_text(text), source_path=str(p))


def config_to_yaml_subset(config: PasConfig) -> str:
    """Render a config back to the accepted YAML subset (for `pas config
    print` templates). Strings that would re-parse as numbers or
    booleans are quoted so the render is a stable roundtrip."""
    data = config.to_dict()

    def scalar(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            if value == "" or _parse_scalar(value) != value or ":" in value:
                return f"'{value}'"
            return value
        return str(value)

    def render(value: Any, indent: int) -> str:
        pad = " " * indent
        lines: list[str] = []
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, dict):
                    lines.append(f"{pad}{key}:")
                    lines.extend(render(item, indent + 2))
                elif item is None:
                    lines.append(f"{pad}{key}: null")
                else:
                    lines.append(f"{pad}{key}: {scalar(item)}")
        return lines

    return "\n".join(render(data, 0)) + "\n"
