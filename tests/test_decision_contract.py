"""SPEC §22.1 item 2: the decision contract must not lie.

The prompt tells a host what shape its answer must have. This module proves
the prompt is *true*: for every rule it states, there is a document that
violates exactly that rule, and the layer the rule names actually rejects
it with the expected message.

Without this, the contract is a comment: someone edits a rule sentence, or
edits `validate_decision`, and nothing notices until a host misbehaves in
production.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk import (  # noqa: E402
    CONTRACT_RULES,
    DECISION_CONTRACT_VERSION,
    KIND_ENUM,
    LocalToolBroker,
    OwnerChannelRegistry,
    PASError,
    PolicyEngine,
    PolicyConfig,
    RunBudget,
    Store,
    ToolLoopExecutor,
    FakeClock,
    agent_system_prompt,
    decision_contract,
    validate_decision,
)
from proactive_sdk.schema_validate import validate as validate_schema  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DECISION_SCHEMA = json.loads((ROOT / "schemas" / "v1" / "decision.json").read_text())
T0 = 1_760_000_000_000


def _schema_errors(document: dict) -> str:
    return "; ".join(validate_schema(DECISION_SCHEMA, document))


def _decision_errors(document: dict) -> str:
    return "; ".join(validate_decision(document))


def _executor_errors(document: dict) -> str:
    executor = ToolLoopExecutor(
        model=type("M", (), {"generate": staticmethod(lambda *a, **k: None)})(),
        broker=LocalToolBroker(capabilities=frozenset()),
    )
    # The built-in loop parses the model's raw text, which never carries
    # protocol_version — the control plane stamps it afterwards.
    model_text = {k: v for k, v in document.items() if k != "protocol_version"}
    try:
        executor._parse_decision(
            json.dumps(model_text, ensure_ascii=False),
            evidence=frozenset({"snapshot:x"}),
            max_proposals=8,
        )
    except PASError as exc:
        return exc.safe_message
    return ""


def _policy_errors(document: dict) -> str:
    store = Store(
        ":memory:", profile="contract", owner_destination="local-inbox:contract",
        clock=FakeClock(wall_ms=T0),
    )
    channels = OwnerChannelRegistry(store)
    channels.register(
        channel_ref="local-inbox:contract", kind="local_inbox",
        push_summary_only=False, now_ms=T0,
    )
    policy = PolicyEngine(store, channels=channels, config=PolicyConfig())
    verdict, _ = policy.evaluate_proposal(
        {"proposal_id": "p-contract", **document["proposals"][0]},
        goal_id="g",
        delivery_policy={"timezone": "UTC"},
        grant_refs=(),
        run_id="r",
        now_ms=T0,
    )
    return str(verdict.get("reason") or verdict.get("outcome") or "")


_LAYER_DRIVERS = {
    "schema": _schema_errors,
    "decision": _decision_errors,
    "executor": _executor_errors,
    "policy": _policy_errors,
}


class ContractTruthTests(unittest.TestCase):
    """Every stated rule is (a) in the prompt and (b) actually enforced."""

    def test_rules_are_unique_and_cover_every_layer(self):
        ids = [rule.rule_id for rule in CONTRACT_RULES]
        self.assertEqual(len(ids), len(set(ids)), "rule ids must be unique")
        layers = {rule.layer for rule in CONTRACT_RULES}
        self.assertEqual(layers, {"schema", "decision", "executor", "policy"})

    def test_every_rule_sentence_appears_in_the_prompt(self):
        text = decision_contract()
        for rule in CONTRACT_RULES:
            with self.subTest(rule=rule.rule_id):
                self.assertIn(rule.text, text)

    def test_every_rule_is_enforced_by_its_named_layer(self):
        for rule in CONTRACT_RULES:
            with self.subTest(rule=rule.rule_id, layer=rule.layer):
                reported = _LAYER_DRIVERS[rule.layer](rule.probe())
                self.assertIn(
                    rule.expect,
                    reported,
                    f"{rule.rule_id}: {rule.layer} did not reject the probe as promised"
                    f" (got {reported!r})",
                )

    def test_the_contract_does_not_promise_a_rule_nobody_enforces(self):
        """A sentence in the prompt that no layer implements would be a lie."""
        enforced = {rule.expect for rule in CONTRACT_RULES}
        for rule in CONTRACT_RULES:
            reported = _LAYER_DRIVERS[rule.layer](rule.probe())
            self.assertTrue(any(expect in reported for expect in enforced))


class ContractDriftTests(unittest.TestCase):
    """The contract, the typed validator and the wire schema must agree."""

    def test_kind_enum_matches_the_typed_validator(self):
        from proactive_sdk.contracts import _PROPOSAL_KINDS

        self.assertEqual(set(KIND_ENUM), set(_PROPOSAL_KINDS))

    def test_kind_enum_matches_the_wire_schema(self):
        schema_kinds = DECISION_SCHEMA["$defs"]["action_proposal"]["properties"]["kind"]["enum"]
        self.assertEqual(list(KIND_ENUM), schema_kinds)

    def test_decision_enum_matches_the_wire_schema(self):
        self.assertEqual(DECISION_SCHEMA["properties"]["decision"]["enum"], ["propose", "silent"])

    def test_summary_bounds_match_the_wire_schema(self):
        summary = DECISION_SCHEMA["properties"]["summary"]
        self.assertEqual((summary["minLength"], summary["maxLength"]), (1, 500))

    def test_notify_self_required_fields_match_the_wire_schema(self):
        from proactive_sdk.contracts import _NOTIFY_SELF_REQUIRED

        proposal = DECISION_SCHEMA["$defs"]["action_proposal"]["properties"]
        for field_name in _NOTIFY_SELF_REQUIRED:
            self.assertIn(field_name, proposal)

    def test_the_contract_version_is_not_the_protocol_version_binding(self):
        from proactive_sdk import PAS_PROTOCOL_VERSION

        self.assertRegex(DECISION_CONTRACT_VERSION, r"^\d+\.\d+$")
        self.assertRegex(PAS_PROTOCOL_VERSION, r"^\d+\.\d+$")


class ContractSurfaceTests(unittest.TestCase):
    """The contract must be obtainable by a host, not just by the built-in loop."""

    def test_system_prompt_is_composed_from_the_contract(self):
        budget = RunBudget(max_model_turns=3)
        prompt = agent_system_prompt("检查一下", budget)
        self.assertIn("检查一下", prompt)
        self.assertIn(decision_contract(), prompt)
        self.assertIn("at most 3 model turns", prompt)

    def test_system_prompt_rejects_empty_input(self):
        for bad in ("", None):
            with self.assertRaises(ValueError):
                agent_system_prompt(bad, RunBudget())  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            agent_system_prompt("x", {"max_model_turns": 1})  # type: ignore[arg-type]

    def test_builtin_loop_uses_the_shared_contract(self):
        executor = ToolLoopExecutor(
            model=type("M", (), {"generate": staticmethod(lambda *a, **k: None)})(),
            broker=LocalToolBroker(capabilities=frozenset()),
        )
        prompt = executor._system_prompt("做点事", RunBudget())
        self.assertEqual(prompt, agent_system_prompt("做点事", RunBudget()))

    def test_rpc_capabilities_expose_the_contract_version(self):
        from proactive_sdk.rpc import RpcDispatcher, RpcSession

        dispatcher = RpcDispatcher()
        session = RpcSession(principal="tester")
        result = self._run(dispatcher._system_capabilities({}, session))
        self.assertEqual(result["decision_contract_version"], DECISION_CONTRACT_VERSION)

    @staticmethod
    def _run(coro):
        import asyncio

        return asyncio.run(coro)

    def test_cli_version_prints_the_contract_version(self):
        import contextlib
        import io

        from proactive_sdk.service import build_parser, main

        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = main(["version"])
        self.assertEqual(code, 0)
        self.assertIn(f"decision contract {DECISION_CONTRACT_VERSION}", out.getvalue())
        self.assertIsNotNone(build_parser().parse_args(["version"]))


if __name__ == "__main__":
    unittest.main()
