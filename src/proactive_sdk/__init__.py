"""proactive_sdk — proactive personal agent runtime kernel (PAS).

Design-stage package (SPEC P0). Only contracts, path-safety and schema
validation exist so far; scheduler/hooks/executor/policy arrive with P1–P4.
Importing this package must never start threads, daemons or I/O.
"""

from __future__ import annotations

PAS_PROTOCOL_VERSION = "1.0"
__version__ = "0.1.0.dev0"

from .contracts import (
    ErrorCode,
    PASError,
    ProfileConfig,
    RuntimeConfig,
    assert_single_profile,
    canonical_json,
    content_hash,
    error_for_code,
    require_utc_timestamp,
    validate_decision,
    validate_schedule,
)
from .pathsafe import (
    PathSafetyError,
    ensure_within,
    safe_join,
    validate_zip_member,
)

__all__ = [
    "PAS_PROTOCOL_VERSION",
    "__version__",
    "ErrorCode",
    "PASError",
    "ProfileConfig",
    "RuntimeConfig",
    "assert_single_profile",
    "canonical_json",
    "content_hash",
    "error_for_code",
    "require_utc_timestamp",
    "validate_decision",
    "validate_schedule",
    "PathSafetyError",
    "ensure_within",
    "safe_join",
    "validate_zip_member",
]
