"""proactive_sdk — proactive personal agent runtime kernel (PAS).

Design-stage package. P0 froze contracts, path safety and schema
validation; P1 adds the persistence layer (store, migrations, clock,
schedules, misfire, claim/fencing, jobs API); P2 adds the hook runtime
(legacy protocol parser, sandbox runner, staging + CAS state machine,
dry-run); P3 adds the independent agent loop (typed source/memory ports,
ContextPack, bounded ToolLoopExecutor, OpenAI-compatible ModelPort,
L0/L1 coordinator). Policy, grants, outbox and delivery continue with
P4. Importing this package must never start threads, daemons or I/O.
"""

from __future__ import annotations

PAS_PROTOCOL_VERSION = "1.0"
__version__ = "0.1.0.dev0"

from .clock import Clock, FakeClock, SystemClock
from .contracts import (
    ActionProposal,
    ContextPack,
    ContextSource,
    Decision,
    ErrorCode,
    ExecutorCapabilities,
    JobSpec,
    MemoryEntry,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    PASError,
    ProfileConfig,
    RunBudget,
    RunRequest,
    RuntimeConfig,
    SourceBatch,
    SourceItem,
    SourceRequest,
    assert_single_profile,
    canonical_json,
    content_hash,
    error_for_code,
    require_utc_timestamp,
    validate_context_pack,
    validate_decision,
    validate_schedule,
)
from .context import (
    ContextPackBuilder,
    EphemeralMemoryPort,
    MemoryPort,
    SnapshotMaterializer,
    SourceEntry,
    SourceRegistry,
    render_context_blocks,
)
from .coordinator import CoordinatorConfig, ProactiveCoordinator, RunReport
from .executor import ExecutorConfig, ExecutorOutcome, RunCancelled, ToolLoopExecutor
from .hooks import (
    BubblewrapSandbox,
    HookProtocolError,
    HookResult,
    HookRunner,
    HookRunnerConfig,
    HookRunReport,
    HookRunSummary,
    HookSandbox,
    HookSpec,
    PlainSubprocessSandbox,
    SeatbeltSandbox,
    parse_hook_logs,
    parse_hook_result,
    platform_sandbox,
)
from .model import OpenAICompatibleModel
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
from .store import (
    EventRecord,
    HookClaim,
    HookCommitResult,
    HookRecord,
    JobRecord,
    RunLease,
    SnapshotRecord,
    SourceStateRecord,
    Store,
)
from .tools import (
    AuthorizedToolCall,
    BrokerCallContext,
    LocalToolBroker,
    ToolCallAttempt,
    ToolResult,
    ToolSpec,
)

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
    "validate_context_pack",
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
    "EventRecord",
    "SourceStateRecord",
    "SnapshotRecord",
    # P1 scheduler
    "Schedule",
    "Scheduler",
    "AdmissionReport",
    "parse_schedule",
    "resolve_local_wall",
    "latest_due_slot",
    "next_occurrence_after",
    # P2 hooks
    "HookSpec",
    "HookResult",
    "HookProtocolError",
    "HookRunner",
    "HookRunnerConfig",
    "HookRunReport",
    "HookRunSummary",
    "HookSandbox",
    "PlainSubprocessSandbox",
    "SeatbeltSandbox",
    "BubblewrapSandbox",
    "platform_sandbox",
    "parse_hook_result",
    "parse_hook_logs",
    "HookRecord",
    "HookClaim",
    "HookCommitResult",
    # P3 contracts (typed records)
    "ActionProposal",
    "ContextPack",
    "ContextSource",
    "Decision",
    "ExecutorCapabilities",
    "MemoryEntry",
    "ModelRequest",
    "ModelResponse",
    "ModelToolCall",
    "RunBudget",
    "RunRequest",
    "SourceBatch",
    "SourceItem",
    "SourceRequest",
    # P3 context
    "ContextPackBuilder",
    "EphemeralMemoryPort",
    "MemoryPort",
    "SnapshotMaterializer",
    "SourceEntry",
    "SourceRegistry",
    "render_context_blocks",
    # P3 model / tools / executor / coordinator
    "OpenAICompatibleModel",
    "AuthorizedToolCall",
    "BrokerCallContext",
    "LocalToolBroker",
    "ToolCallAttempt",
    "ToolResult",
    "ToolSpec",
    "ExecutorConfig",
    "ExecutorOutcome",
    "RunCancelled",
    "ToolLoopExecutor",
    "CoordinatorConfig",
    "ProactiveCoordinator",
    "RunReport",
]
