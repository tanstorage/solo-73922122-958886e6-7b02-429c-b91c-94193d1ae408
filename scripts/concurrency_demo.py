"""Two real uvicorn processes, one SQLite database, interleaved edits.

Run from the project root:

    python scripts/concurrency_demo.py

It starts two web processes on different ports against the same file, uses
the demo pause header (``X-Demo-Pause``) to stretch the expansion phase of
one writer and proves:

1. the other process can commit while the first is expanding (no write lock);
2. a late expansion is re-planned, so a dependency added meanwhile is kept;
3. two individually acyclic edits that combine into a cycle are rejected;
4. every read/download is a single database snapshot.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = sys.executable


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Web:
    def __init__(self, name: str, db_path: str, stop_file: str, demo: bool = True):
        self.name = name
        self.port = free_port()
        env = os.environ.copy()
        env.update(
            {
                "PYTHONPATH": ROOT + os.pathsep + env.get("PYTHONPATH", ""),
                "BOM_DB_PATH": db_path,
                "BOM_INSTANCE": name,
                "BOM_DEMO_MODE": "1" if demo else "0",
                "BOM_SEED_DEMO": "0",
            }
        )
        self.proc = subprocess.Popen(
            [
                PYTHON,
                os.path.join(ROOT, "scripts", "web_worker.py"),
                db_path,
                str(self.port),
                name,
                stop_file,
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.stop_file = stop_file
        self._wait_ready()

    def _wait_ready(self, timeout: float = 15) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self.get("/api/health")
                return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError(f"{self.name} did not start")

    def request(self, method: str, path: str, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def get(self, path: str, headers=None):
        return self.request("GET", path, headers=headers)

    def post(self, path: str, body, headers=None):
        return self.request("POST", path, body=body, headers=headers)

    def close(self) -> None:
        """Ask the worker to exit via its stop-file sentinel."""
        try:
            open(self.stop_file, "a").close()
            self.proc.wait(timeout=8)
        except Exception:
            pass


def ops(*edges):
    return {
        "ops": [
            {"op": "upsert", "parent": parent, "child": child, "qty": qty}
            for parent, child, qty in edges
        ]
    }


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not condition:
        raise SystemExit(1)


def scenario_no_lock_and_replan(alpha: Web, beta: Web) -> None:
    print("\n1) Expansion holds no write lock; a late expansion is re-planned")
    alpha.post("/api/batch", ops(("FG", "SA", 2), ("SA", "L1", 3)))
    holder = {}

    def slow_writer():
        holder["response"] = alpha.post(
            "/api/batch", ops(("FG", "SA", 4)), headers={"X-Demo-Pause": "1.5"}
        )

    thread = threading.Thread(target=slow_writer)
    thread.start()
    time.sleep(0.3)  # alpha is inside its paused expansion, before BEGIN IMMEDIATE
    started = time.monotonic()
    status_b, body_b = beta.post("/api/batch", ops(("SA", "L2", 5), ("NEW", "L3", 1)))
    elapsed = time.monotonic() - started
    thread.join(timeout=30)
    check("beta committed while alpha expanded", status_b == 200, f"{elapsed:.2f}s")
    check("beta did not wait for alpha's write", elapsed < 1.0, f"{elapsed:.2f}s")
    status_a, body_a = holder["response"]
    check("alpha committed after re-plan", status_a == 200, body_a[:120])
    rows = json.loads(alpha.get("/api/leaf-usage")[1])["rows"]
    usage = {(r["top_part"], r["leaf_part"]): r["qty"] for r in rows}
    check("alpha's new quantity applied", usage.get(("FG", "L1")) == 12, str(usage))
    check("beta's new dependency kept", usage.get(("FG", "L2")) == 20, str(usage))
    check("beta's new assembly kept", usage.get(("NEW", "L3")) == 1, str(usage))
    retries = json.loads(body_a).get("retries", 0)
    check("alpha re-planned rather than overwriting", retries >= 1, f"retries={retries}")


def scenario_composed_cycle(alpha: Web, beta: Web) -> None:
    print("\n2) Two individually acyclic edits cannot combine into a cycle")
    alpha.post("/api/batch", ops(("BASE", "FG2", 1)))
    holder = {}

    def slow_writer():
        holder["response"] = alpha.post(
            "/api/batch", ops(("FG2", "X", 1)), headers={"X-Demo-Pause": "1.5"}
        )

    thread = threading.Thread(target=slow_writer)
    thread.start()
    time.sleep(0.3)
    status_b, body_b = beta.post("/api/batch", ops(("X", "FG2", 1)))
    thread.join(timeout=30)
    check("beta's edit committed", status_b == 200, body_b[:120])
    status_a, body_a = holder["response"]
    payload = json.loads(body_a)
    check("alpha's batch rejected", status_a == 409, body_a[:160])
    check(
        "cycle path reported",
        payload.get("code") == "CyclicStructureError" and payload.get("paths"),
        str(payload.get("paths")),
    )
    edges = {(e["parent"], e["child"]) for e in json.loads(alpha.get("/api/state")[1])["edges"]}
    check("no edge from the rejected batch", ("FG2", "X") not in edges, str(sorted(edges)))


def scenario_snapshot_reads(alpha: Web, beta: Web) -> None:
    print("\n3) Concurrent reads/downloads never mix two commits")
    beta.post("/api/batch", ops(("M", "N", 1), ("N", "P", 1)))
    stop = threading.Event()
    failures = []

    def writer():
        qty = 2
        while not stop.is_set():
            status, _ = beta.post("/api/batch", ops(("M", "N", qty), ("N", "P", qty)))
            if status == 200:
                qty += 1
            time.sleep(0.01)

    reader = threading.Thread(target=writer)
    reader.start()
    checks = 0
    try:
        for _ in range(40):
            status, body = alpha.get("/api/leaf-usage.csv")
            if status != 200:
                failures.append(body)
                continue
            lines = [line for line in body.splitlines() if line.strip()]
            rows = [line.split(",") for line in lines[1:]]
            versions = {row[3] for row in rows}
            if len(versions) != 1:
                failures.append(f"mixed versions: {versions}")
            q = int(next(r[2] for r in rows if r[0] == "N"))
            product = int(next(r[2] for r in rows if r[0] == "M"))
            if product != q * q:
                failures.append(f"mixed quantities: q={q} M={product}")
            checks += 1
            time.sleep(0.01)
    finally:
        stop.set()
        reader.join(timeout=10)
    check(f"{checks} downloads were single snapshots", not failures, "; ".join(failures[:3]))


def scenario_restart(alpha: Web, beta: Web, db_path: str, tmp: str) -> None:
    print("\n4) Results survive a restart; the restarted process reads no re-expansion")
    status, body = alpha.get("/api/state")
    version = json.loads(body)["version"]["seq"]
    usage_before = json.loads(alpha.get("/api/leaf-usage")[1])["rows"]
    alpha.close()
    replacement = Web("alpha-restarted", db_path, os.path.join(tmp, "restart.stop"))
    try:
        status, body = replacement.get("/api/state")
        state = json.loads(body)
        check("version kept after restart", state["version"]["seq"] == version)
        check("leaf usage kept after restart",
              json.loads(replacement.get("/api/leaf-usage")[1])["rows"] == usage_before)
        check("edges kept after restart", bool(state["edges"]))
        status, body = replacement.post("/api/batch", ops(("R", "S", 1)))
        check("restarted process can still commit", status == 200, body[:120])
    finally:
        replacement.close()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="bom-demo-") as tmp:
        db_path = os.path.join(tmp, "bom.db")
        alpha = Web("alpha", db_path, os.path.join(tmp, "alpha.stop"))
        beta = Web("beta", db_path, os.path.join(tmp, "beta.stop"))
        try:
            print(f"alpha={alpha.port} beta={beta.port} db={db_path}")
            scenario_no_lock_and_replan(alpha, beta)
            scenario_composed_cycle(alpha, beta)
            scenario_snapshot_reads(alpha, beta)
            scenario_restart(alpha, beta, db_path, tmp)
            print("\nAll interleaving scenarios passed.")
        finally:
            beta.close()


if __name__ == "__main__":
    main()
