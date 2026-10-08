"""Legacy Skill importer (SPEC §11; P6 Skills 能力).

显式导入流程（§11.2）：选择随包部署参考或其他本地目录 → 安全遍历与 hash → legacy
frontmatter 解析 → 无碰撞 canonical id / aliases → 依赖闭包 →
compatibility sidecar → 导入报告。原目录只读；本模块从不写入被导入的
目录；随包参考资源由构建配置附带，本模块不会自动加载或执行它们。

设计要点：

- 零第三方依赖：legacy frontmatter 是 ``key: value`` 加 inline JSON 的
  受限方言，这里自写解析器；解析失败是显式 issue，不是猜测。
- canonical id：`google_calendar` → `google-calendar`；与目录名/别名冲突
  时用短 hash 后缀保证无碰撞（§11.2）。
- 依赖闭包：SKILL.md 所在目录树 + 正文引用的共享 references/scripts
  （嵌套 artifacts 层的共享资产必须进闭包，不能只复制单个 SKILL.md）。
- 技术状态与再分发状态独立（§11.3）：``technical_status`` 从 parsed 起
  随真实验证推进；``distribution.status`` 恒为 permission_unverified，
  导入不构成任何授权核验。
- ``includeInPrompt`` 只进 sidecar 记录，永不解释为每轮全文注入（§11.2）。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import ErrorCode, PASError
from .pathsafe import PathSafetyError, ensure_within, safe_join

__all__ = [
    "SKILL_FILE_NAME",
    "LegacySkillImporter",
    "ScannedSkill",
    "ImportReport",
    "audit_consistency",
]

SKILL_FILE_NAME = "SKILL.md"
_MANIFEST_NAME = "manifest.yaml"

# Agent Skills 严格命名：小写字母数字、连字符分段。
_STRICT_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
#: 审计口径的 description 上限（meta-ads 1247 字符即触发 invalid_description）。
_MAX_DESCRIPTION_CHARS = 1024
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_FILES_PER_SKILL = 256

_SHARED_ASSET_RE = re.compile(
    r"(?<![\w-])((?:references|scripts|eval|assets)/[A-Za-z0-9_./-]+\.[A-Za-z0-9]{1,8})"
)

_TOOL_TOKENS = (
    "hatch_gws_cli",
    "hatch_action",
    "hatch_permission",
    "hatch_permission_label",
    "hatch_permission_overrides",
    "hatch_command",
    "authd",
)

#: §11.4 首版能力映射：只覆盖确定需要的子命令族；其余如实 adapter_required。
_CAPABILITY_MAP: dict[str, tuple[list[str], list[str], str]] = {
    # canonical_name → (capabilities, tools, compatibility_status)
    "gmail": (["gmail.read"], ["hatch_gws_cli"], "adapter_required"),
    "google-calendar": (["calendar.read"], ["hatch_gws_cli"], "adapter_required"),
    "outlook-mail": (["mail.read"], ["hatch_gws_cli"], "adapter_required"),
    "outlook-calendar": (["calendar.read"], ["hatch_gws_cli"], "adapter_required"),
    "google-contacts": (["contacts.read"], ["hatch_gws_cli"], "adapter_required"),
    "google-tasks": (["tasks.read"], ["hatch_gws_cli"], "adapter_required"),
    "authd": ([], [], "blocked"),
    "wide-research": ([], [], "unsupported"),
}


class SkillImportError(PASError):
    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.INVALID_CONFIG, message, scope="skills")


# --------------------------------------------------------------------------- #
# Frontmatter (legacy dialect)
# --------------------------------------------------------------------------- #


def _scalar(raw: str) -> Any:
    """One legacy scalar: quoted string, inline JSON, or JSON-style atom."""
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        return raw[1:-1]
    if raw.startswith("{") or raw.startswith("["):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    try:
        return json.loads(raw)  # true/false/null/numbers
    except ValueError:
        return raw


_BLOCK = object()


def parse_legacy_frontmatter(text: str) -> tuple[dict[str, Any], list[str]]:
    """Parse the legacy ``---`` frontmatter. Values are quoted strings,
    inline JSON (``metadata: { ... }``), a block mapping, or a block list;
    indented plain text continues the previous value. Returns (fields,
    issues); malformed entries are recorded, never guessed."""
    lines = text.splitlines()
    issues: list[str] = []
    if not lines or lines[0].strip() != "---":
        return {}, ["missing_frontmatter"]
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, ["unterminated_frontmatter"]

    fields: dict[str, Any] = {}
    current_key: str | None = None
    continuation: list[str] = []
    block_lines: list[str] = []

    def _finish_previous() -> None:
        nonlocal continuation, block_lines
        if current_key is None:
            return
        if continuation:
            fields[current_key] = " ".join([str(fields[current_key]), *continuation]).strip()
            continuation = []
        elif fields.get(current_key, None) is _BLOCK:
            if block_lines:
                fields[current_key] = _resolve_block(block_lines, issues, current_key)
            else:
                fields.pop(current_key)  # empty container with no children
            block_lines = []

    for line in lines[1:end]:
        if not line.strip():
            continue
        indented = line[:1] in (" ", "\t")
        stripped = line.strip()
        if indented:
            if current_key is None:
                issues.append(f"unparseable_frontmatter_line:{stripped[:40]}")
                continue
            if fields.get(current_key, None) is _BLOCK:
                block_lines.append(stripped)
            else:
                continuation.append(stripped)
            continue
        _finish_previous()
        if ":" not in line:
            issues.append(f"unparseable_frontmatter_line:{stripped[:40]}")
            current_key = None
            continue
        key, _, raw = line.partition(":")
        current_key = key.strip()
        raw = raw.strip()
        fields[current_key] = _scalar(raw) if raw else _BLOCK
    _finish_previous()
    return fields, issues



def _resolve_block(block_lines: list[str], issues: list[str], key: str) -> Any:
    """An empty-valued key followed by indented lines: a block list
    (``- item``), a block mapping (``k: v``), or a plain continuation."""
    if block_lines[0].startswith("-"):
        return [_scalar(item[1:].strip()) for item in block_lines if item.startswith("-")]
    if all(":" in item for item in block_lines):
        mapping: dict[str, Any] = {}
        for item in block_lines:
            nested_key, _, raw = item.partition(":")
            mapping[nested_key.strip()] = _scalar(raw.strip())
        return mapping
    issues.append(f"unparseable_frontmatter_block:{key}")
    return " ".join(block_lines)


def _normalize_metadata(raw: Any) -> tuple[dict[str, str], dict[str, str]]:
    """Boolean/other metadata values normalize to strings; the raw type is
    preserved for the sidecar (§11.2: 布尔值可规范化，原始语义进 sidecar)."""
    normalized: dict[str, str] = {}
    raw_types: dict[str, str] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            raw_types[str(key)] = type(value).__name__
            normalized[str(key)] = str(value).lower() if isinstance(value, bool) else str(value)
    return normalized, raw_types


# --------------------------------------------------------------------------- #
# Scanned skill
# --------------------------------------------------------------------------- #


@dataclass
class ScannedSkill:
    """One discovered legacy skill with its derived compat metadata."""

    original_name: str
    canonical_name: str
    aliases: tuple[str, ...]
    rel_path: str  # posix, relative to the import root
    sha256: str
    description: str
    metadata: dict[str, str]
    non_string_metadata: dict[str, str]
    extra_fields: dict[str, str]
    issues: list[str]
    tool_tokens: tuple[str, ...]
    capability_requirements: list[str]
    tool_requirements: list[str]
    compatibility_status: str
    technical_status: str = "parsed"
    distribution_status: str = "permission_unverified"
    dependency_files: tuple[str, ...] = ()
    dependency_bytes: int = 0
    body_chars: int = 0

    def sidecar(self) -> dict[str, Any]:
        """§11.3 compatibility sidecar. PAS 扩展元数据，不冒充标准字段。"""
        return {
            "schema_version": "1.0",
            "source": {
                "format": "legacy",
                "original_name": self.original_name,
                "original_path": self.rel_path,
                "sha256": self.sha256,
            },
            "canonical_name": self.canonical_name,
            "aliases": list(self.aliases),
            "requirements": {
                "capabilities": list(self.capability_requirements),
                "tools": list(self.tool_requirements),
                "grants": _grants_for(self.canonical_name, self.capability_requirements),
            },
            "compatibility": {
                "status": self.compatibility_status,
                "path_strategy": "isolated_virtual_mount",
            },
            "distribution": {"status": self.distribution_status},
            "legacy": {
                "issues": list(self.issues),
                "non_string_metadata": dict(self.non_string_metadata),
                "metadata_normalized": dict(self.metadata),
                "extra_fields": dict(self.extra_fields),
                "dependency_files": list(self.dependency_files),
                "dependency_bytes": self.dependency_bytes,
                "technical_status": self.technical_status,
                "include_in_prompt_normalized": self.metadata.get("includeInPrompt", ""),
                "note": "includeInPrompt is a legacy record; PAS never injects full text per turn",
            },
        }


def _grants_for(canonical_name: str, capabilities: list[str]) -> list[str]:
    if not capabilities:
        return []
    if canonical_name in ("gmail", "outlook-mail"):
        return ["selected_mail_account"]
    if canonical_name in ("google-calendar", "outlook-calendar"):
        return ["selected_calendar_account"]
    return [f"selected_{canonical_name.replace('-', '_')}_account"]


def _strict_name_invalid(name: str) -> bool:
    return not bool(_STRICT_NAME_RE.fullmatch(name or ""))


class LegacySkillImporter:
    """Read-only scanner for a user-provided legacy skills directory.

    The scan base is the ``skills/`` subtree when present (paths in the
    report then match the audit's ``skills/<name>/SKILL.md`` scheme);
    otherwise the given root itself."""

    def __init__(self, *, root: Path | str) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise SkillImportError(f"import root is not a directory: {self.root.name}")
        self._base = self.root / "skills" if (self.root / "skills").is_dir() else self.root

    # -- traversal ---------------------------------------------------------

    def _skill_paths(self) -> list[Path]:
        """Every SKILL.md inside the base, refusing symlink escapes."""
        found: list[Path] = []
        for path in sorted(self._base.rglob(SKILL_FILE_NAME)):
            resolved = path.resolve()
            try:
                ensure_within(self._base, resolved)
            except (PASError, PathSafetyError):
                continue  # symlink escape: outside the audited tree entirely
            found.append(path)
        return found

    def scan(self) -> list[ScannedSkill]:
        scanned: list[ScannedSkill] = []
        canonical_seen: dict[str, str] = {}
        for path in self._skill_paths():
            rel = path.parent.relative_to(self.root).as_posix()
            raw = path.read_bytes()
            if len(raw) > _MAX_FILE_BYTES:
                raise SkillImportError(f"skill file exceeds {_MAX_FILE_BYTES} bytes: {rel}")
            sha = hashlib.sha256(raw).hexdigest()
            text = raw.decode("utf-8", errors="replace")
            fields, fm_issues = parse_legacy_frontmatter(text)
            body_after_fm = text.split("---", 2)[-1]
            original_name = str(fields.get("name", "") or "")
            description = str(fields.get("description", "") or "")
            metadata, raw_types = _normalize_metadata(fields.get("metadata"))
            extra = {
                k: str(v)
                for k, v in fields.items()
                if k not in ("name", "description", "metadata")
            }

            issues: list[str] = []
            if _strict_name_invalid(original_name):
                issues.append("invalid_name")
            parent_dir = Path(rel).name
            if original_name != parent_dir:
                issues.append("name_directory_mismatch")
            if raw_types:
                issues.append("metadata_values_not_all_strings")
            if not description or len(description) > _MAX_DESCRIPTION_CHARS:
                issues.append("invalid_description")
            issues.extend(fm_issues)

            canonical, aliases = self._canonical_and_aliases(
                original_name, rel, issues, canonical_seen
            )
            tokens = tuple(t for t in _TOOL_TOKENS if t in text)
            capabilities, tools, compat_status = _CAPABILITY_MAP.get(
                canonical, ([], [], "adapter_required")
            )
            if not tools and tokens:
                tools = [tokens[0]]

            scanned.append(
                ScannedSkill(
                    original_name=original_name,
                    canonical_name=canonical,
                    aliases=aliases,
                    rel_path=rel + "/" + SKILL_FILE_NAME,
                    sha256=sha,
                    description=description,
                    metadata=metadata,
                    non_string_metadata=raw_types,
                    extra_fields=extra,
                    issues=issues,
                    tool_tokens=tokens,
                    capability_requirements=list(capabilities),
                    tool_requirements=list(tools),
                    compatibility_status=compat_status,
                    body_chars=len(body_after_fm),
                )
            )
        for skill in scanned:
            self._attach_dependency_closure(skill)
        return scanned

    def _canonical_and_aliases(
        self,
        original_name: str,
        rel: str,
        issues: list[str],
        canonical_seen: dict[str, str],
    ) -> tuple[str, tuple[str, ...]]:
        """Canonical id: hyphenated name, collision-free (§11.2). The
        directory name enters the alias set (name/directory mismatches stay
        reachable under both spellings)."""
        base = original_name.replace("_", "-").strip().lower()
        if not _STRICT_NAME_RE.fullmatch(base):
            base = Path(rel).name.replace("_", "-").strip().lower()
        if not _STRICT_NAME_RE.fullmatch(base):
            base = "skill"
        canonical = base
        suffix = 2
        while canonical in canonical_seen and canonical_seen[canonical] != rel:
            canonical = f"{base}-{suffix}"
            suffix += 1
        canonical_seen.setdefault(canonical, rel)
        aliases: list[str] = []
        for candidate in (original_name, Path(rel).name):
            if candidate and candidate != canonical and candidate not in aliases:
                aliases.append(candidate)
        return canonical, tuple(aliases)

    # -- dependency closure (§11.2) ----------------------------------------

    def _attach_dependency_closure(self, skill: ScannedSkill) -> None:
        rel_dir = skill.rel_path.rsplit("/", 1)[0]
        own_dir = safe_join(self.root, rel_dir)
        files: list[str] = []
        total = 0
        candidates = sorted(own_dir.rglob("*")) if own_dir.is_dir() else []
        for path in candidates:
            if not path.is_file() or path.is_symlink():
                continue
            resolved = path.resolve()
            try:
                ensure_within(self._base, resolved)
            except (PASError, PathSafetyError):
                continue
            rel = resolved.relative_to(self.root).as_posix()
            size = resolved.stat().st_size
            total += size
            files.append(rel)
            if len(files) >= _MAX_FILES_PER_SKILL:
                break
        # Shared assets referenced from the body: resolved against the
        # skill's own directory first, then the parent layer (nested
        # artifacts share references/scripts across sibling skills).
        body = (self.root / skill.rel_path).read_text(encoding="utf-8", errors="replace")
        for match in _SHARED_ASSET_RE.finditer(body):
            target_rel = match.group(1)
            for base in (own_dir, own_dir.parent):
                target = safe_join(base, target_rel)
                if target.is_file():
                    rel = target.resolve().relative_to(self.root).as_posix()
                    if rel not in files:
                        files.append(rel)
                        total += target.stat().st_size
                    break
        skill.dependency_files = tuple(sorted(files))
        skill.dependency_bytes = total

    # -- report ------------------------------------------------------------

    def report(self, skills: list[ScannedSkill]) -> dict[str, Any]:
        issue_counts: dict[str, int] = {}
        for skill in skills:
            for issue in skill.issues:
                issue_counts[issue] = issue_counts.get(issue, 0) + 1
        return {
            "schema_version": "1.0",
            "count": len(skills),
            "issue_counts": issue_counts,
            "canonical_names": sorted(s.canonical_name for s in skills),
            "compatibility": {
                status: sum(1 for s in skills if s.compatibility_status == status)
                for status in ("adapter_required", "blocked", "unsupported")
            },
            "technical_status": {
                status: sum(1 for s in skills if s.technical_status == status)
                for status in sorted({s.technical_status for s in skills})
            },
            "distribution_status": "permission_unverified (import does not verify any grant)",
            "skills": [
                {
                    "source_path": s.rel_path,
                    "sha256": s.sha256,
                    "original_name": s.original_name,
                    "canonical_name": s.canonical_name,
                    "aliases": list(s.aliases),
                    "description_characters": len(s.description),
                    "issues": list(s.issues),
                    "non_string_metadata": dict(s.non_string_metadata),
                    "tool_tokens": list(s.tool_tokens),
                    "requirements": {
                        "capabilities": list(s.capability_requirements),
                        "tools": list(s.tool_requirements),
                        "grants": _grants_for(s.canonical_name, s.capability_requirements),
                    },
                    "compatibility_status": s.compatibility_status,
                    "technical_status": s.technical_status,
                    "dependency_files": len(s.dependency_files),
                    "dependency_bytes": s.dependency_bytes,
                    "sidecar": s.sidecar(),
                }
                for s in skills
            ],
            "note": "原始正文不进报告；原目录只读；导入不构成授权或再分发核验。",
        }


# --------------------------------------------------------------------------- #
# Audit consistency (§11.5: 88 个入口可发现、可审计、可说明缺口)
# --------------------------------------------------------------------------- #


def audit_consistency(
    report: dict[str, Any], audit: dict[str, Any]
) -> dict[str, Any]:
    """Compare an import report against tests/fixtures/skills.json. Recomputable
    fields only; platform/token heuristics stay informational (same rule as
    tools/reproduce_audit.py)."""
    by_path = {s["source_path"]: s for s in report["skills"]}
    audit_by_path = {s["source_path"]: s for s in audit["skills"]}
    mismatches: list[dict[str, Any]] = []
    for path, audit_entry in audit_by_path.items():
        entry = by_path.get(path)
        if entry is None:
            mismatches.append({"source_path": path, "field": "missing"})
            continue
        for field in ("sha256", "original_name"):
            if entry.get(field) != audit_entry.get(field):
                mismatches.append(
                    {"source_path": path, "field": field,
                     "import": entry.get(field), "audit": audit_entry.get(field)}
                )
        for issue in audit_entry.get("issues", []):
            if issue not in entry.get("issues", []):
                mismatches.append(
                    {"source_path": path, "field": "issues", "audit": issue}
                )
    extra = sorted(set(by_path) - set(audit_by_path))
    return {
        "audit_count": audit["count"],
        "report_count": report["count"],
        "match": not mismatches and not extra and report["count"] == audit["count"],
        "mismatches": mismatches[:64],
        "unexpected_paths": extra[:64],
    }
