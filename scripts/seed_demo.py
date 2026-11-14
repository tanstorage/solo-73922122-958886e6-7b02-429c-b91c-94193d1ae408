"""Seed the demo warehouse through HTTP (used by docker compose).

Waits for the web service, loads the sample structure once (if empty) and
promotes the leaf L-4 into an assembly so both sample scenarios are present
in the database.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("BOM_URL", "http://web:8000")


def call(method: str, path: str) -> dict:
    request = urllib.request.Request(
        BASE + path, method=method, data=b"{}" if method == "POST" else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def main() -> int:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            call("GET", "/api/health")
            break
        except Exception:
            time.sleep(0.5)
    else:
        print("web service did not become ready", file=sys.stderr)
        return 1

    seeded = call("POST", "/api/demo/seed")
    print(json.dumps(seeded, ensure_ascii=False))
    # Only promote when the demo structure is actually present.
    state = call("GET", "/api/state")
    parents = {edge["parent"] for edge in state["edges"]}
    if {"FG-1", "SA-1"} <= parents and "L-4" in {e["child"] for e in state["edges"]}:
        try:
            promoted = call("POST", "/api/demo/promote-leaf")
            print(json.dumps(promoted, ensure_ascii=False))
        except urllib.error.HTTPError as exc:
            print(exc.read().decode(), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
