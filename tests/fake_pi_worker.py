#!/usr/bin/env python3
"""Scripted stand-in for pi_worker.ts speaking the same JSON-RPC protocol.

Deterministic triggers inside ``instruction``:
- RETURN_SILENT        → valid silent envelope
- RETURN_PROPOSE       → valid propose envelope
- BAD_ENVELOPE         → envelope-invalid error
- NO_FINAL             → no_final_message error
- SLOW:<seconds>       → run sleeps, cancellable (fake abort always lands)
- SLOW_STUCK:<seconds> → cancel replies cancellation_unconfirmed
- CRASH                → worker dies mid-run (simulated process failure)

Run/cancel concurrency mirrors the real worker: requests are read on the
main thread; slow runs settle on their own thread.
"""

import json
import sys
import threading

SILENT = {"decision": "silent", "summary": "No change", "proposals": []}
PROPOSE = {"decision": "propose", "summary": "One item", "proposals": [
    {"kind": "draft", "fact_id": "fact-1", "revision": "r1", "body": "text"}]}

lock = threading.Lock()
runs = {}  # run_id -> {"event": Event, "stuck": bool, "seconds": float}


def reply(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def error(code, message, data=None):
    err = {"code": code, "message": message}
    if data:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": None, "error": err}


def handle_run(req):
    run_id = req["params"]["run_id"]
    instruction = req["params"]["instruction"]
    if "CRASH" in instruction:
        sys.stdout.flush()
        import os

        os._exit(1)
    if "BAD_ENVELOPE" in instruction:
        reply({"jsonrpc": "2.0", "id": req["id"], "error": {
            "code": -32603, "message": "decision envelope invalid",
            "data": {"reason": "decision_invalid", "detail": "Invalid decision envelope"}}})
        return
    if "NO_FINAL" in instruction:
        reply({"jsonrpc": "2.0", "id": req["id"], "error": {
            "code": -32603, "message": "no final assistant message",
            "data": {"reason": "no_final_message"}}})
        return
    seconds = 0.0
    stuck = False
    if "SLOW_STUCK:" in instruction:
        seconds = float(instruction.split("SLOW_STUCK:")[1].split()[0])
        stuck = True
    elif "SLOW:" in instruction:
        seconds = float(instruction.split("SLOW:")[1].split()[0])
    if seconds:
        event = threading.Event()
        with lock:
            runs[run_id] = {"event": event, "stuck": stuck, "seconds": seconds}
        import time

        started = time.monotonic()
        event.wait(seconds)
        elapsed = time.monotonic() - started
        with lock:
            entry = runs.pop(run_id, None)
        if elapsed < seconds:
            # woken early: the abort landed
            reply({"jsonrpc": "2.0", "id": req["id"], "error": {
                "code": -32603, "message": "run cancelled",
                "data": {"reason": "cancelled"}}})
            return
        if stuck:
            reply({"jsonrpc": "2.0", "id": req["id"], "error": {
                "code": -32603, "message": "cancellation_unconfirmed",
                "data": {"reason": "cancellation_unconfirmed", "detail": "still busy"}}})
            return
    envelope = PROPOSE if "RETURN_PROPOSE" in instruction else SILENT
    reply({"jsonrpc": "2.0", "id": req["id"], "result": {
        "state": "completed", "envelope": envelope,
        "usage": {"input_tokens": 11, "output_tokens": 7, "cache_read_tokens": 0, "tool_calls": 0}}})


def handle_cancel(req):
    run_id = req["params"]["run_id"]
    with lock:
        entry = runs.get(run_id)
    if entry is None:
        reply({"jsonrpc": "2.0", "id": req["id"], "error": {
            "code": -32602, "message": f"no active run {run_id}"}})
        return
    if entry["stuck"]:
        # Simulated abort failure: session never reaches idle; the run
        # keeps going and the cancel itself reports the uncertainty.
        reply({"jsonrpc": "2.0", "id": req["id"], "error": {
            "code": -32603, "message": "cancellation_unconfirmed",
            "data": {"reason": "cancellation_unconfirmed", "detail": "still busy"}}})
        return
    entry["event"].set()
    reply({"jsonrpc": "2.0", "id": req["id"], "result": {"state": "cancelled"}})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            reply({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        method = req.get("method")
        if method == "initialize":
            reply({"jsonrpc": "2.0", "id": req["id"], "result": {
                "pi_version": "fake-1.0.0",
                "allowed_tools": req["params"]["allowed_tools"]}})
        elif method == "run":
            threading.Thread(target=handle_run, args=(req,), daemon=True).start()
        elif method == "cancel":
            threading.Thread(target=handle_cancel, args=(req,), daemon=True).start()
        elif method == "shutdown":
            return


if __name__ == "__main__":
    main()
