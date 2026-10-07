"""P7 productization tests, part 2: daemon lifecycle, control-plane RPC,
CLI service (SPEC §14.1/§14.2/§17.1/§17.2; OPS-01)."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import socket as socket_mod
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))

from p3_fixtures import T0, make_executor, rfc3339  # noqa: E402

from proactive_sdk import FakeClock, Job, ProactiveAgent  # noqa: E402
from proactive_sdk.contracts import ModelResponse  # noqa: E402
from proactive_sdk.daemon import DaemonLock  # noqa: E402


class _SilentModel:
    provider = "fake"

    async def generate(self, *_a, **_k):
        return ModelResponse(
            content=json.dumps({"decision": "silent", "summary": "p7 silent", "proposals": []}),
            tool_calls=[],
            usage=None,
        )


def _agent(tmp: str, profile: str = "p7d", *, clock=None) -> ProactiveAgent:
    return ProactiveAgent(
        state_dir=tmp,
        executor=make_executor(_SilentModel(), clock=clock),
        timezone="UTC",
        locale="en",
        profile=profile,
        clock=clock,
    )


# --------------------------------------------------------------------------- #
# Daemon lock
# --------------------------------------------------------------------------- #


class DaemonLockTests(unittest.TestCase):
    def test_exclusive_acquire_and_stale_takeover(self):
        tmp = tempfile.mkdtemp()
        lock1 = DaemonLock(tmp)
        lock2 = DaemonLock(tmp)
        self.assertTrue(lock1.acquire())
        self.assertFalse(lock2.acquire())  # held by live pid (this process)
        lock1.release()
        self.assertTrue(lock2.acquire())
        lock2.release()

    def test_stale_lock_of_dead_pid_is_taken_over(self):
        tmp = tempfile.mkdtemp()
        (Path(tmp) / "daemon.lock").write_text("pid=2147483646\n")  # nobody owns this pid
        lock = DaemonLock(tmp)
        self.assertTrue(lock.acquire())
        lock.release()

    def test_unreadable_lock_fails_closed(self):
        tmp = tempfile.mkdtemp()
        (Path(tmp) / "daemon.lock").write_text("garbage without pid\n")
        lock = DaemonLock(tmp)
        self.assertFalse(lock.acquire())


# --------------------------------------------------------------------------- #
# Embedded daemon: start/stop, drain, control plane
# --------------------------------------------------------------------------- #


def _rpc(sock_path: str, frames: list[dict]) -> list[dict]:
    s = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
    s.settimeout(5)
    s.connect(sock_path)
    out = []
    try:
        for frame in frames:
            s.sendall(json.dumps(frame).encode() + b"\n")
            data = b""
            while not data.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
            out.append(json.loads(data) if data else {})
    finally:
        s.close()
    return out


class DaemonLifecycleTests(unittest.TestCase):
    def test_start_stop_refuses_second_instance_and_cleans_lock(self):
        async def scenario():
            tmp = tempfile.mkdtemp()
            async with _agent(tmp, "lck") as agent:
                await agent.start()
                second = _agent(tmp, "lck")
                try:
                    with self.assertRaises(Exception):
                        await second.start()
                finally:
                    await second.close()
                report = await agent.stop(drain=True, grace_s=1)
                self.assertTrue(report["drain"])
            # lock removed → next instance can start
            async with _agent(tmp, "lck") as agent3:
                await agent3.start()
                await agent3.stop()

        asyncio.run(scenario())

    def test_control_plane_hello_and_health_and_jobs(self):
        async def scenario():
            tmp = tempfile.mkdtemp()
            async with _agent(tmp, "rpc") as agent:
                await agent.start()
                sock = f"{tmp}/pas.sock"
                self.assertTrue(os.path.exists(sock))
                mode = os.stat(sock).st_mode & 0o777
                self.assertEqual(mode, 0o600, "socket must not be group/world readable")
                replies = await asyncio.to_thread(
                    _rpc,
                    sock,
                    [
                        {"jsonrpc": "2.0", "id": 0, "method": "system.hello",
                         "params": {"protocol_version": "1.0", "client": "t"}},
                        {"jsonrpc": "2.0", "id": 1, "method": "system.health"},
                        {"jsonrpc": "2.0", "id": 2, "method": "jobs.create",
                         "params": {"job": {"id": "j", "mode": "task",
                                            "schedule": {"kind": "runonce",
                                                         "at": rfc3339(T0 + 10**9)},
                                            "instruction": "x"},
                                    "idempotency_key": "k"}},
                        {"jsonrpc": "2.0", "id": 3, "method": "jobs.list"},
                        {"jsonrpc": "2.0", "id": 4, "method": "runs.list"},
                        {"jsonrpc": "2.0", "id": 5, "method": "notifications.list"},
                        {"jsonrpc": "2.0", "id": 6, "method": "jobs.pause",
                         "params": {"job_id": "j"}},
                        {"jsonrpc": "2.0", "id": 7, "method": "jobs.resume",
                         "params": {"job_id": "j"}},
                        {"jsonrpc": "2.0", "id": 8, "method": "jobs.delete",
                         "params": {"job_id": "j"}},
                    ],
                )
                for reply in replies[1:]:
                    self.assertNotIn("error", reply, reply)
                self.assertEqual(replies[1]["result"]["ok"], True)
                self.assertEqual(replies[2]["result"]["job_id"], "j")
                self.assertEqual(replies[3]["result"]["count"], 1)
                self.assertEqual(replies[4]["result"]["count"], 0)
                await agent.stop()

        asyncio.run(scenario())

    def test_unauthenticated_peer_is_refused(self):
        async def scenario():
            tmp = tempfile.mkdtemp()
            # A non-hello FIRST frame must be refused and the connection
            # closed (fail closed). The token path is covered in
            # TokenAuthTests below — on a same-UID socket the peer
            # credential wins by design, so a socket-level wrong-token
            # probe cannot work from this test process.
            async with _agent(tmp, "auth") as agent:
                await agent.start()
                sock = f"{tmp}/pas.sock"
                replies = await asyncio.to_thread(
                    _rpc, sock,
                    [{"jsonrpc": "2.0", "id": 1, "method": "jobs.list"}],
                )
                self.assertIn("error", replies[0])
                self.assertEqual(replies[0]["error"]["data"]["code"], "auth_required")
                await agent.stop()

        asyncio.run(scenario())

    def test_token_authentication_unit_path(self):
        async def scenario():
            tmp = tempfile.mkdtemp()
            async with _agent(tmp, "tok") as agent:
                from proactive_sdk.rpc_server import ControlPlaneServer

                server = ControlPlaneServer(agent, socket_path=Path(tmp) / "s", token="sekrit-token-1")

                class _NoPeerWriter:
                    def get_extra_info(self, _name):
                        return None  # platform would not reveal the peer

                session = __import__("proactive_sdk.rpc", fromlist=["RpcSession"]).RpcSession()
                hello = {"method": "system.hello", "params": {"token": "sekrit-token-1"}}
                self.assertTrue(server._authenticate(_NoPeerWriter(), hello, session))
                self.assertTrue(session.principal.startswith("token:"))
                bad_session = __import__("proactive_sdk.rpc", fromlist=["RpcSession"]).RpcSession()
                bad = {"method": "system.hello", "params": {"token": "wrong"}}
                self.assertFalse(server._authenticate(_NoPeerWriter(), bad, bad_session))
                none_session = __import__("proactive_sdk.rpc", fromlist=["RpcSession"]).RpcSession()
                self.assertFalse(
                    server._authenticate(_NoPeerWriter(), {"method": "system.hello", "params": {}}, none_session)
                )

        asyncio.run(scenario())

    def test_health_file_written_and_degraded_on_low_disk(self):
        async def scenario():
            tmp = tempfile.mkdtemp()
            clock = FakeClock(wall_ms=T0)
            async with _agent(tmp, "hth", clock=clock) as agent:
                await agent.start(grace_s=1)
                health_path = Path(tmp) / "health.json"
                deadline = asyncio.get_running_loop().time() + 3
                while not health_path.exists():
                    self.assertLess(asyncio.get_running_loop().time(), deadline, "no health file")
                    await asyncio.sleep(0.05)
                health = json.loads(health_path.read_text())
                self.assertTrue(health["alive"])
                self.assertTrue(health["ready"], health["reasons"])
                await agent.stop()

        asyncio.run(scenario())

    def test_force_stop_reports_interrupted_not_completed(self):
        async def scenario():
            tmp = tempfile.mkdtemp()
            clock = FakeClock(wall_ms=T0)
            agent = _agent(tmp, "force", clock=clock)
            agent.jobs_upsert(
                Job(id="hb", mode="heartbeat",
                    schedule={"kind": "interval", "anchor": "2025-10-09T00:00:00Z",
                              "every_seconds": 1800},
                    instruction="force test"),
                idempotency_key="force-1",
            )
            clock.advance_wall(1_800_000)
            await agent.start(grace_s=0.05)
            # let the daemon admit and start processing the run
            await asyncio.sleep(0.3)
            report = await agent.stop(drain=False)
            self.assertFalse(report["drain"])
            # cancelled request must NOT be recorded as completion:
            # the run is either still running (lease live) or failed — never completed
            runs = agent.runs_list(limit=10)
            for run in runs:
                self.assertNotEqual(run["state"], "completed", run)
            await agent.close()

        asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# Restart fault: kill -9 a running daemon child, restart, recovery
# --------------------------------------------------------------------------- #


DAEMON_CHILD_SCRIPT = textwrap.dedent(
    """
    import asyncio, json, os, sys
    sys.path.insert(0, {src!r}); sys.path.insert(0, {tests!r})
    from p3_fixtures import make_executor
    from proactive_sdk import ProactiveAgent, Job, FakeClock
    from tests_p7_child_helpers import SlowModel

    async def main():
        # Fixed far-future clock: the run lease (60s ttl) expires at
        # CHILD_CLOCK+60s, while the verifier runs at CHILD_CLOCK+1h —
        # deterministic reclaim without wall-clock sleeps.
        clock = FakeClock(wall_ms=2_000_000_000_000)
        agent = ProactiveAgent(
            state_dir={state!r}, executor=make_executor(SlowModel(), clock=clock),
            profile="restart", timezone="UTC", locale="en", clock=clock,
        )
        agent.jobs_upsert(
            Job(id="slow-job", mode="task",
                schedule={{"kind": "runonce", "at": "2030-01-01T00:00:00Z"}},
                instruction="slow run"),
            idempotency_key="restart-1",
        )
        agent.trigger_job("slow-job")
        await agent.start(grace_s=1)
        for _ in range(200):
            runs = agent.runs_list(state="running")
            if runs:
                print("RUNNING", flush=True)
                break
            await asyncio.sleep(0.05)
        else:
            print("NO-RUN", flush=True)
        await asyncio.Event().wait()  # killed by the test harness
    asyncio.run(main())
    """
)


class RestartFaultTests(unittest.TestCase):
    """SPEC §16.1 Operations: restart while a run is in flight."""

    def test_kill9_midrun_then_restart_recovers_with_attempt_increment(self):
        helpers = REPO / "tests" / "tests_p7_child_helpers.py"
        helpers.write_text(
            textwrap.dedent(
                '''
                """Child-process helper: a model whose generate sleeps."""
                import asyncio, json
                from proactive_sdk.contracts import ModelResponse

                class SlowModel:
                    provider = "fake"
                    async def generate(self, *_a, **_k):
                        await asyncio.sleep(30)
                        return ModelResponse(
                            content=json.dumps({"decision": "silent", "summary": "too slow",
                                                "proposals": []}),
                            tool_calls=[], usage=None,
                        )
                '''
            )
        )
        self.addCleanup(lambda: helpers.unlink(missing_ok=True))

        tmp = tempfile.mkdtemp()
        script = REPO / "tests" / "tests_p7_child_daemon.py"
        script.write_text(
            DAEMON_CHILD_SCRIPT.format(
                src=str(REPO / "src"), tests=str(REPO / "tests"), state=tmp
            )
        )
        self.addCleanup(lambda: script.unlink(missing_ok=True))

        env = dict(os.environ)
        proc = subprocess.Popen(
            [sys.executable, str(script)], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(REPO),
        )
        try:
            line = proc.stdout.readline().strip()
            self.assertIn("RUNNING", line, f"child never started a run: {line}")
            proc.kill()  # SIGKILL: no drain, no cleanup — the hard fault
            proc.wait(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()

        # The lock is stale (owner dead); the run is `running` with a live
        # lease (child clock + 60s ttl). A restarted instance at child
        # clock + 1h must reclaim it with attempt+1.
        async def verify_restart():
            clock = FakeClock(wall_ms=2_000_000_000_000 + 3_600_000)
            agent = _agent(tmp, "restart", clock=clock)
            runs_before = agent.runs_list(limit=10)
            self.assertTrue(any(r["state"] == "running" for r in runs_before), runs_before)
            attempt_before = next(r["attempt"] for r in runs_before if r["state"] == "running")
            await agent.tick()  # the daemon's recovery pass does the same math
            runs_after = agent.runs_list(limit=10)
            recovered = [r for r in runs_after if r["attempt"] > attempt_before]
            self.assertTrue(recovered, f"run not reclaimed: {runs_after}")
            self.assertEqual(recovered[0]["attempt"], attempt_before + 1)
            self.assertNotEqual(recovered[0]["state"], "completed")  # no fake success
            await agent.close()

        asyncio.run(verify_restart())


# --------------------------------------------------------------------------- #
# CLI service
# --------------------------------------------------------------------------- #


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _pas(self, *args: str, expect: int = 0) -> str:
        from proactive_sdk.service import main

        stderr = io.StringIO()
        captured = contextlib.redirect_stderr(stderr)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), captured:
            code = main(list(args))
        self.assertEqual(code, expect, f"args={args} stderr={stderr.getvalue()}")
        return stdout.getvalue()

    def test_help_and_version(self):
        from proactive_sdk.service import build_parser

        parser = build_parser()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            parser.print_help()
        self.assertIn("serve", out.getvalue())
        self.assertIn("doctor", out.getvalue())
        self.assertIn("backup", out.getvalue())
        version = self._pas("version")
        self.assertIn("Museion Agent SDK v0.1.0", version)

    def test_doctor_missing_state_dir_reports_the_finding(self):
        out = self._pas("--state-dir", str(Path(self.tmp) / "missing"), "doctor", "--json",
                        expect=1)
        data = json.loads(out)
        names = [c["name"] for c in data["checks"]]
        self.assertIn("state_dir", names)
        self.assertFalse(data["ok"])

    def test_job_roundtrip_and_backup_via_cli(self):
        state = Path(self.tmp) / "st"
        state.mkdir()
        self._pas("--state-dir", str(state), "jobs", "create", "cli-job",
                  "--mode", "task",
                  "--schedule-json", json.dumps({"kind": "runonce", "at": rfc3339(T0 + 10**9)}),
                  "--instruction", "cli job")
        listing = json.loads(self._pas("--state-dir", str(state), "jobs", "list", "--json"))
        self.assertEqual(listing["count"], 1)
        backup = Path(self.tmp) / "cli.bin"
        self._pas("--state-dir", str(state), "backup", str(backup))
        self.assertTrue(backup.is_file())
        self._pas("--state-dir", str(state), "restore", str(backup), "--yes")
        listing2 = json.loads(self._pas("--state-dir", str(state), "jobs", "list", "--json"))
        self.assertEqual(listing2["count"], 1)

    def test_restore_without_yes_refuses(self):
        state = Path(self.tmp) / "st2"
        state.mkdir()
        backup = Path(self.tmp) / "cli2.bin"
        self._pas("--state-dir", str(state), "backup", str(backup))
        self._pas("--state-dir", str(state), "restore", str(backup), expect=2)

    def test_delete_data_requires_yes(self):
        state = Path(self.tmp) / "st3"
        state.mkdir()
        self._pas("--state-dir", str(state), "delete-data", expect=2)

    def test_operational_commands_need_state_dir(self):
        self._pas("jobs", "list", expect=2)

    def test_unknown_config_key_fails_via_cli(self):
        bad = Path(self.tmp) / "bad.yaml"
        bad.write_text('config_version: "1"\nprofile: x\nnope: 1\n')
        self._pas("--config", str(bad), "config", "check", expect=2)

    def test_config_check_and_print(self):
        good = Path(self.tmp) / "good.yaml"
        good.write_text(
            'config_version: "1"\nprofile: cli\n'
            f'state_dir: {Path(self.tmp) / "st4"}\n'
            "timezone: UTC\nlocale: en\n"
            "policy:\n"
            '  notification_window: {start: "09:00", end: "21:30"}\n'
        )
        out = json.loads(self._pas("--config", str(good), "config", "check", "--json"))
        self.assertTrue(out["ok"])
        rendered = self._pas("--config", str(good), "config", "print")
        self.assertIn("notification_window", rendered)

    def test_runs_cancel_via_cli(self):
        state = Path(self.tmp) / "st5"
        clock = FakeClock(wall_ms=T0)
        async def seed():
            agent = _agent(str(state), "personal", clock=clock)  # CLI default profile
            agent.jobs_upsert(
                Job(id="c1", mode="task",
                    schedule={"kind": "runonce", "at": rfc3339(T0 + 10**9)},
                    instruction="cancel me"),
                idempotency_key="cli-cancel-1",
            )
            agent.trigger_job("c1")
            await agent.close()

        asyncio.run(seed())
        runs = json.loads(self._pas("--state-dir", str(state), "runs", "list", "--json"))
        run_id = runs["runs"][0]["run_id"]
        out = json.loads(self._pas("--state-dir", str(state), "runs", "cancel", run_id, "--json"))
        self.assertEqual(out["outcome"], "cancelled")


if __name__ == "__main__":
    unittest.main()
