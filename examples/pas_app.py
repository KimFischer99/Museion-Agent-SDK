"""Example app module for `pas serve --app examples.pas_app:build_agent`.

This is the user-side wiring the CLI expects: the CLI owns the event
loop, this module owns the component wiring (SPEC §14.1 嵌入模式由
Builder 拥有事件循环；daemon 模式由 pas serve 拥有).

The demo executor is deliberately inert (it answers `silent` with zero
proposals and zero model calls) so the example can run as a real daemon
without any provider credential — a real deployment replaces
`build_executor` with a ToolLoopExecutor wired to a real ModelPort and
tool broker.

    pas serve --app examples.pas_app:build_agent --config pas.yaml
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):  # direct-script import support
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk.contracts import RunRequest  # noqa: E402
from proactive_sdk.executor import ExecutorConfig, ExecutorOutcome, ToolLoopExecutor  # noqa: E402
from proactive_sdk.facade import ProactiveAgent  # noqa: E402
from proactive_sdk.tools import LocalToolBroker  # noqa: E402


class _NoModel:
    """Placeholder model — never consulted because ``execute`` is fully
    overridden; ``generate`` raises so a misuse is loud, not silent."""

    def generate(self, *_args, **_kwargs):  # noqa: D102
        raise RuntimeError("SilentDemoExecutor makes no model calls")


class SilentDemoExecutor(ToolLoopExecutor):
    """A real ToolLoopExecutor subclass that always answers `silent`
    without any model call. Labeled for what it is: a wiring demo."""

    def __init__(self) -> None:
        super().__init__(
            model=_NoModel(),
            broker=LocalToolBroker(capabilities=frozenset()),
            config=ExecutorConfig(),
        )

    async def execute(self, run_request: RunRequest, *args, **kwargs) -> ExecutorOutcome:  # noqa: D102, ARG002
        decision = Decision(
            decision="silent", summary="demo executor: nothing to act on", proposals=()
        )
        return ExecutorOutcome(
            decision=decision,
            events=(),
            usage={"protocol_version": "1.0", "model_turns": 0, "tool_calls": 0,
                   "wall_time_ms": 0, "pricing_basis": "unmeasured"},
            model_turns=0,
        )


def build_executor(_config=None) -> SilentDemoExecutor:
    return SilentDemoExecutor()


def build_agent(config=None) -> ProactiveAgent:
    """Factory for `pas serve --app` / `pas tick --app`. Accepts the
    loaded PasConfig (or None) and returns a fully wired agent."""
    state_dir = config.state_dir if config is not None else "./pas-state"
    timezone = config.timezone if config is not None else "UTC"
    locale = config.locale if config is not None else "en"
    profile = config.profile if config is not None else "personal"
    return ProactiveAgent(
        state_dir=state_dir,
        executor=build_executor(config),
        timezone=timezone,
        locale=locale,
        profile=profile,
        config=config,
    )
