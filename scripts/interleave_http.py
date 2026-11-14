"""Interleave edits across the two compose web containers.

    docker compose --profile demos run --rm interleave

Uses only HTTP, so it works from inside the compose network.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

A = os.environ.get("BOM_URL_A", "http://web-a:8000")
B = os.environ.get("BOM_URL_B", "http://web-b:8000")


def call(base: str, method: str, path: str, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        base + path,
        method=method,
        data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def batch(base, ops, pause=None):
    headers = {"X-Demo-Pause": str(pause)} if pause else None
    return call(base, "POST", "/api/batch", {"ops": ops}, headers)


def upsert(parent, child, qty):
    return {"op": "upsert", "parent": parent, "child": child, "qty": qty}


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not condition:
        raise SystemExit(1)


def main() -> None:
    for base in (A, B):
        status, body = call(base, "GET", "/api/health")
        check(f"reachable: {base}", status == 200, str(body))

    # Reset to a deterministic tiny structure (one atomic batch).
    status, body = call(A, "POST", "/api/batch", {"reset": True, "upserts": [
        {"parent": "SEED", "child": "SEED-LEAF", "qty": 1},
    ]})
    check("reset structure", status == 200, str(body)[:120])

    # 1) B commits while A is expanding; A must re-plan and keep B's work.
    result = {}

    def slow_a():
        result["a"] = batch(A, [upsert("FG", "SA", 4)], pause=1.5)

    thread = threading.Thread(target=slow_a)
    thread.start()
    time.sleep(0.3)
    status_b, body_b = batch(B, [upsert("SA", "L1", 5), upsert("NEW", "L2", 1)])
    thread.join(timeout=30)
    status_a, body_a = result["a"]
    check("B committed during A's expansion", status_b == 200, str(body_b)[:120])
    check("A committed after re-plan", status_a == 200, str(body_a)[:120])
    _, usage = call(A, "GET", "/api/leaf-usage")
    rows = {(r["top_part"], r["leaf_part"]): r["qty"] for r in usage["rows"]}
    check("A's qty and B's new dependency both present",
          rows.get(("FG", "L1")) == 20 and rows.get(("NEW", "L2")) == 1, str(rows))

    # 2) Individually acyclic edits that compose into a cycle.
    batch(A, [upsert("BASE", "FG2", 1)])

    def slow_a2():
        result["a2"] = batch(A, [upsert("FG2", "X", 1)], pause=1.5)

    thread = threading.Thread(target=slow_a2)
    thread.start()
    time.sleep(0.3)
    status_b, body_b = batch(B, [upsert("X", "FG2", 1)])
    thread.join(timeout=30)
    status_a, body_a = result["a2"]
    check("B's edit accepted", status_b == 200, str(body_b)[:120])
    check("A's edit rejected with a cycle path", status_a == 409 and body_a.get("paths"),
          str(body_a)[:160])

    # 3) Downloads on both processes show exactly one version.
    for base in (A, B):
        request = urllib.request.Request(base + "/api/leaf-usage.csv")
        with urllib.request.urlopen(request, timeout=10) as response:
            header = response.headers.get("X-Bom-Version")
            text = response.read().decode()
        versions = {line.split(",")[3] for line in text.splitlines()[1:] if line.strip()}
        check(f"{base} CSV is a single version", versions <= {header} and bool(header), text[:80])

    print("All HTTP interleaving checks passed.")


if __name__ == "__main__":
    main()
