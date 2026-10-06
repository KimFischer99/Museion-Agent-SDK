"""proactive_sdk — proactive personal agent runtime kernel (PAS).

Design-stage package. P0 froze contracts, path safety and schema
validation; P1 adds the persistence layer (store, migrations, clock,
schedules, misfire, claim/fencing, jobs API). Scheduler/hooks/executor/
policy service layers continue with P2–P4. Importing this package must
never start threads, daemons or I/O.
"""

from __future__ import annotations

PAS_PROTOCOL_VERSION = "1.0"
__version__ = "0.1.0.dev0"

from .clock import Clock, FakeClock, SystemClock
from .contracts import (
    ErrorCode,
    JobSpec,
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
from .scheduler import (
    AdmissionReport,
    Schedule,
    Scheduler,
    latest_due_slot,
    next_occurrence_after,
    parse_schedule,
    resolve_local_wall,
)
from .store import JobRecord, RunLease, Store

__all__ = [
    "PAS_PROTOCOL_VERSION",
    "__version__",
    # P0 contracts
    "ErrorCode",
    "PASError",
    "ProfileConfig",
    "RuntimeConfig",
    "JobSpec",
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
    # P1 clock
    "Clock",
    "SystemClock",
    "FakeClock",
    # P1 store
    "Store",
    "JobRecord",
    "RunLease",
    # P1 scheduler
    "Schedule",
    "Scheduler",
    "AdmissionReport",
    "parse_schedule",
    "resolve_local_wall",
    "latest_due_slot",
    "next_occurrence_after",
]
