#!/usr/bin/env python3
"""A deliberately minimal "black-box agent" for host-form tests.

It knows nothing about PAS: it reads one string from stdin, prints some
chatter to stderr and stdout, and (usually) ends with a JSON object. That
is exactly the shape of a CLI agent someone already has.

Trigger tokens are looked for in the *stdin prompt*:
  SLOW_TOKEN      sleep, so the caller can exercise the deadline / cancel
  FAIL_TOKEN      exit non-zero without an envelope
  GARBAGE_TOKEN   print no JSON object at all
  PROPOSE_TOKEN   chatter, then a valid envelope carrying a draft proposal
  (default)       chatter, then a valid silent envelope
"""

import json
import sys
import time

MARKER = "PAS-FAKE-HOST-"


def main() -> int:
    prompt = sys.stdin.read()
    print(f"[fake-host] received {len(prompt)} chars", file=sys.stderr)
    print("[fake-host] thinking")

    if MARKER + "SLOW" in prompt:
        time.sleep(30)
        print(json.dumps({"decision": "silent", "summary": "slow done", "proposals": []}))
        return 0
    if MARKER + "FAIL" in prompt:
        print("[fake-host] simulated failure", file=sys.stderr)
        return 3
    if MARKER + "GARBAGE" in prompt:
        print("I could not produce a structured answer, sorry.")
        return 0
    if MARKER + "CRASH" in prompt:
        raise RuntimeError("simulated crash after partial output")

    if MARKER + "PROPOSE" in prompt:
        # A `draft` needs no evidence refs, so a black box that knows nothing
        # about this run can still produce a valid proposal.
        print("[fake-host] found something worth recording")
        print(json.dumps({
            "decision": "propose",
            "summary": "one item worth noting",
            "proposals": [{"kind": "draft", "fact_id": "fact-1",
                           "revision": "1", "body": "local note"}],
        }))
        return 0

    # Chatter, then the envelope: the extractor must find the latter.
    print("[fake-host] no changes observed")
    print(json.dumps({"decision": "silent", "summary": "nothing to act on", "proposals": []}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
