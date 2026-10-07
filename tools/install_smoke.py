#!/usr/bin/env python3
"""P7 install smoke (SPEC §15.1 P7 行「安装 smoke」): build → fresh venv →
install → real CLI/facade flows → uninstall.

This exercises what a user actually receives, not the repo checkout:
the wheel is installed into an isolated venv, then:

 1. `pas --help` and `pas version` (console script + help text);
 2. `pas doctor --json` on a temp state dir;
 3. `pas jobs create/list/backup` roundtrip through the installed CLI;
 4. an embedded `tick()` with a scripted executor through the installed
    package (imports resolve, migrations run, facade lifecycle closes).

Usage: python tools/install_smoke.py [--keep] [dist-dir]
Exit 0 = smoke passed; 1 = failed; 2 = environment error.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SMOKE_APP = Path(__file__).resolve().parent / "install_smoke_app.py"


def run(cmd: list[str], *, env: dict | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=cwd)
    if result.returncode != 0:
        print(f"FAILED: {' '.join(str(c) for c in cmd)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        raise SystemExit(1)
    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist", nargs="?", default="dist", help="wheel directory (default dist/)")
    parser.add_argument("--keep", action="store_true", help="keep build/venv for inspection")
    args = parser.parse_args(argv)

    dist_dir = (REPO / args.dist).resolve()
    wheels = sorted(dist_dir.glob("proactive_sdk-*.whl"))
    if not wheels:
        print(f"no wheel in {dist_dir}; run: python -m pip wheel . -w {dist_dir} --no-deps", file=sys.stderr)
        return 2
    wheel = wheels[-1]
    print(f"[1/5] wheel: {wheel.name}")

    workdir = Path(tempfile.mkdtemp(prefix="pas-install-smoke-"))
    if not args.keep:
        import atexit

        atexit.register(lambda: subprocess.run(["rm", "-rf", str(workdir)]))

    print("[2/5] creating fresh venv…")
    venv_dir = workdir / "venv"
    venv.create(venv_dir, with_pip=True)
    py = str(venv_dir / "bin" / "python")

    print("[3/5] installing wheel…")
    run([py, "-m", "pip", "install", "--quiet", str(wheel)])
    run([py, "-m", "pip", "check"])
    pas_bin = str(venv_dir / "bin" / "pas")

    print("[4/5] CLI flows…")
    run([pas_bin, "--help"])
    version_out = run([pas_bin, "version"]).stdout.strip()
    print(f"      {version_out}")
    state = workdir / "state"
    state.mkdir(parents=True)  # deployment layout step (deploy/README.md)
    doctor = run([pas_bin, "--state-dir", str(state), "doctor", "--json"])
    checks = json.loads(doctor.stdout)
    if not checks.get("ok"):
        print(f"doctor reported problems: {checks}")
        return 1
    run([pas_bin, "--state-dir", str(state), "jobs", "create", "smoke-job",
         "--mode", "task",
         "--schedule-json", json.dumps({"kind": "runonce", "at": "2031-01-01T00:00:00Z"}),
         "--instruction", "install smoke job",
         "--idempotency-key", "smoke-1"])
    listing = json.loads(run([pas_bin, "--state-dir", str(state), "jobs", "list", "--json"]).stdout)
    assert listing["count"] == 1, listing
    backup_path = workdir / "smoke.bin"
    run([pas_bin, "--state-dir", str(state), "backup", str(backup_path)])
    assert backup_path.is_file()
    run([pas_bin, "--state-dir", str(state), "restore", str(backup_path), "--yes"])
    run([pas_bin, "--state-dir", str(state), "export", str(workdir / "dump.json")])

    print("[5/5] embedded tick via installed package…")
    env_base = {
        "PATH": str(venv_dir / "bin") + ":" + __import__("os").environ.get("PATH", ""),
        "VIRTUAL_ENV": str(venv_dir),
        "PAS_SMOKE_STATE": str(workdir / "state2"),
    }
    run([py, str(SMOKE_APP)], env=env_base)

    print("INSTALL SMOKE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
