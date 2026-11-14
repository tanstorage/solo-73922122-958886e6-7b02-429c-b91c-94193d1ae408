"""HTTP-level tests: batch API, cycle reporting, CSV snapshots, two web apps."""

from __future__ import annotations

import csv
import io
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.store import BomStore


def _client(tmp_path, name, demo=True):
    store = BomStore(str(tmp_path / "shared.db"))
    app = create_app(store, instance=name, demo_mode=demo, seed_demo=False)
    return TestClient(app, base_url=f"http://{name}.local")


def _upsert_ops(*edges):
    return [{"op": "upsert", "parent": p, "child": c, "qty": q} for p, c, q in edges]


@pytest.fixture()
def client(tmp_path):
    with _client(tmp_path, "alpha") as test_client:
        yield test_client


def test_index_page_and_static_assets(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "BOM 叶件用量数仓" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_batch_commit_and_state(client):
    response = client.post(
        "/api/batch", json={"ops": _upsert_ops(("FG", "SA", 2), ("SA", "L", 3), ("FG", "X", 1))}
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["leaf_rows"] == 3
    assert payload["version_seq"] >= 1

    state = client.get("/api/state").json()
    assert state["edges"] == [
        {"parent": "FG", "child": "SA", "qty": 2},
        {"parent": "FG", "child": "X", "qty": 1},
        {"parent": "SA", "child": "L", "qty": 3},
    ]
    usage = {(row["top_part"], row["leaf_part"]): row["qty"] for row in state["leaf_usage"]}
    assert usage == {("FG", "L"): 6, ("FG", "X"): 1, ("SA", "L"): 3}


def test_cycle_returns_paths_and_leaves_db_untouched(client):
    client.post("/api/batch", json={"ops": _upsert_ops(("A", "B", 1), ("B", "C", 1))})
    version = client.get("/api/state").json()["version"]["seq"]
    response = client.post("/api/batch", json={"ops": _upsert_ops(("C", "A", 2))})
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "CyclicStructureError"
    assert body["paths"] == [["A", "B", "C", "A"]]
    assert client.get("/api/state").json()["version"]["seq"] == version


def test_multiple_cycles_are_all_reported(client):
    response = client.post(
        "/api/batch",
        json={
            "ops": _upsert_ops(
                ("A", "B", 1),
                ("B", "A", 1),
                ("C", "D", 1),
                ("D", "E", 1),
                ("E", "C", 1),
            )
        },
    )
    assert response.status_code == 409
    paths = response.json()["paths"]
    assert [["A", "B", "A"], ["C", "D", "E", "C"]] == paths or len(paths) == 2
    assert all(path[0] == path[-1] for path in paths)


@pytest.mark.parametrize(
    "payload",
    [
        {"ops": []},
        {"ops": [{"op": "upsert", "parent": "A", "child": "B", "qty": 0}]},
        {"ops": [{"op": "upsert", "parent": "A", "child": "B", "qty": -3}]},
        {"ops": [{"op": "upsert", "parent": "A", "child": "B"}]},
        {"ops": [{"op": "unknown", "parent": "A", "child": "B", "qty": 1}]},
        {"ops": [{"op": "delete", "parent": "", "child": "B"}]},
        {"reset": True, "upserts": []},
    ],
)
def test_invalid_batches_return_422(client, payload):
    response = client.post("/api/batch", json=payload)
    assert response.status_code == 422, response.text
    assert client.get("/api/state").json()["edges"] == []


def test_reset_batch_replaces_whole_structure(client):
    client.post("/api/batch", json={"ops": _upsert_ops(("A", "B", 1), ("B", "C", 1))})
    response = client.post(
        "/api/batch",
        json={"reset": True, "upserts": [{"parent": "R", "child": "S", "qty": 2}]},
    )
    assert response.status_code == 200, response.text
    state = client.get("/api/state").json()
    assert state["edges"] == [{"parent": "R", "child": "S", "qty": 2}]
    assert state["leaf_usage"] == [{"top_part": "R", "leaf_part": "S", "qty": 2}]


def test_batch_delete_and_capacity(client):
    client.post("/api/batch", json={"ops": _upsert_ops(("A", "B", 4))})
    response = client.post(
        "/api/batch", json={"ops": [{"op": "delete", "parent": "A", "child": "B"}]}
    )
    assert response.status_code == 200
    assert client.get("/api/state").json()["edges"] == []
    assert client.get("/api/leaf-usage").json()["rows"] == []


def test_csv_download_is_a_single_snapshot(client):
    client.post("/api/batch", json={"ops": _upsert_ops(("A", "B", 2), ("B", "C", 3), ("A", "D", 1))})
    response = client.get("/api/leaf-usage.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    version = response.headers["X-Bom-Version"]
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert {(r["top_part"], r["leaf_part"], int(r["qty"])) for r in rows} == {
        ("A", "C", 6),
        ("A", "D", 1),
        ("B", "C", 3),
    }
    assert {r["version_seq"] for r in rows} == {version}

    filtered = client.get("/api/leaf-usage.csv", params={"top": "A"}).text
    filtered_rows = list(csv.DictReader(io.StringIO(filtered)))
    assert {r["leaf_part"] for r in filtered_rows} == {"C", "D"}


def test_filters_on_json_endpoint(client):
    client.post("/api/batch", json={"ops": _upsert_ops(("A", "B", 2), ("A", "C", 3), ("C", "D", 1))})
    payload = client.get("/api/leaf-usage", params={"top": "a", "leaf": "b"}).json()
    assert payload["rows"] == [{"top_part": "A", "leaf_part": "B", "qty": 2}]


def test_expansion_does_not_hold_write_lock_during_demo_pause(tmp_path):
    """Two separate web apps on one DB: the second commits while the first pauses."""
    with _client(tmp_path, "alpha") as alpha, _client(tmp_path, "beta") as beta:
        alpha.post("/api/batch", json={"ops": _upsert_ops(("A", "B", 1))})
        result = {}

        def slow_alpha():
            result["response"] = alpha.post(
                "/api/batch",
                json={"ops": _upsert_ops(("C", "D", 2))},
                headers={"X-Demo-Pause": "1"},
            )

        handler = threading.Thread(target=slow_alpha)
        handler.start()
        time.sleep(0.2)  # alpha is now inside its pause, before BEGIN IMMEDIATE
        started = time.monotonic()
        response_b = beta.post("/api/batch", json={"ops": _upsert_ops(("E", "F", 3))})
        elapsed = time.monotonic() - started
        handler.join(timeout=10)
        assert response_b.status_code == 200
        assert elapsed < 0.9, "second writer was blocked by the first writer's expansion"
        assert result["response"].status_code == 200
        edges = {(e["parent"], e["child"]) for e in beta.get("/api/state").json()["edges"]}
        assert {("A", "B"), ("C", "D"), ("E", "F")} <= edges


def test_two_web_apps_interleaved_edits_combine_cycle_rejected(tmp_path):
    """A adds FG->X, B adds X->FG while A expands. One of them must fail."""
    with _client(tmp_path, "alpha") as alpha, _client(tmp_path, "beta") as beta:
        beta.post("/api/batch", json={"ops": _upsert_ops(("BASE", "FG", 1))})
        errors = {}

        def slow_alpha():
            errors["alpha"] = alpha.post(
                "/api/batch",
                json={"ops": _upsert_ops(("FG", "X", 1))},
                headers={"X-Demo-Pause": "1"},
            )

        handler = threading.Thread(target=slow_alpha)
        handler.start()
        time.sleep(0.2)
        response_b = beta.post("/api/batch", json={"ops": _upsert_ops(("X", "FG", 1))})
        handler.join(timeout=10)
        assert response_b.status_code == 200
        alpha_response = errors["alpha"]
        assert alpha_response.status_code == 409, alpha_response.text
        assert alpha_response.json()["paths"]
        state = alpha.get("/api/state").json()
        edges = {(e["parent"], e["child"]) for e in state["edges"]}
        assert ("FG", "X") not in edges and ("X", "FG") in edges
        # Stored usage must exactly match the surviving structure, not the
        # rejected plan: BASE -> FG stays, X -> FG is B's edit, and FG is a
        # leaf in that structure.
        assert state["leaf_usage"] == [
            {"top_part": "BASE", "leaf_part": "FG", "qty": 1},
            {"top_part": "X", "leaf_part": "FG", "qty": 1},
        ], state["leaf_usage"]


def test_stale_expansion_keeps_new_dependency(tmp_path):
    """B adds a new leaf under SA while A re-quantifies FG->SA."""
    with _client(tmp_path, "alpha") as alpha, _client(tmp_path, "beta") as beta:
        alpha.post("/api/batch", json={"ops": _upsert_ops(("FG", "SA", 2), ("SA", "L1", 3))})
        holder = {}

        def slow_alpha():
            holder["response"] = alpha.post(
                "/api/batch",
                json={"ops": _upsert_ops(("FG", "SA", 4))},
                headers={"X-Demo-Pause": "1"},
            )

        handler = threading.Thread(target=slow_alpha)
        handler.start()
        time.sleep(0.2)
        beta.post("/api/batch", json={"ops": _upsert_ops(("SA", "L2", 5), ("NEW", "L3", 1))})
        handler.join(timeout=10)
        assert holder["response"].status_code == 200
        rows = beta.get("/api/leaf-usage").json()["rows"]
        usage = {(r["top_part"], r["leaf_part"]): r["qty"] for r in rows}
        assert usage[("FG", "L1")] == 12
        assert usage[("FG", "L2")] == 20, "new dependency added during expansion was lost"
        assert usage[("NEW", "L3")] == 1
        assert usage[("SA", "L1")] == 3 and usage[("SA", "L2")] == 5


def test_downloads_never_mix_two_commits(tmp_path):
    """Hammer CSV/JSON reads while commits land; each response is one version."""
    with _client(tmp_path, "alpha") as alpha:
        alpha.post("/api/batch", json={"ops": _upsert_ops(("A", "B", 1), ("B", "C", 1))})
        stop = threading.Event()

        def writer():
            with _client(tmp_path, "writer") as w:
                qty = 2
                while not stop.is_set():
                    response = w.post(
                        "/api/batch",
                        json={"ops": _upsert_ops(("A", "B", qty), ("B", "C", qty))},
                    )
                    if response.status_code == 200:
                        qty += 1
                    time.sleep(0.005)

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            for _ in range(30):
                csv_text = alpha.get("/api/leaf-usage.csv").text
                rows = list(csv.DictReader(io.StringIO(csv_text)))
                assert len({r["version_seq"] for r in rows}) == 1
                q = int(next(r for r in rows if r["top_part"] == "B")["qty"])
                ac = int(
                    next(
                        r for r in rows
                        if r["top_part"] == "A" and r["leaf_part"] == "C"
                    )["qty"]
                )
                assert ac == q * q, (q, ac, csv_text)
                a_leaves = {r["leaf_part"]: int(r["qty"]) for r in rows if r["top_part"] == "A"}
                assert a_leaves == {"C": q * q}, (q, a_leaves, csv_text)
                payload = alpha.get("/api/leaf-usage").json()
                json_rows = {
                    (r["top_part"], r["leaf_part"]): r["qty"] for r in payload["rows"]
                }
                # Same invariant on the JSON path: A->B(q), B->C(q) => A->C(q*q).
                assert json_rows[("A", "C")] == json_rows[("B", "C")] ** 2, payload
                assert len(
                    {r["top_part"] for r in payload["rows"]}
                ) == len(payload["rows"])  # each top has exactly one leaf here
        finally:
            stop.set()
            thread.join()


def test_results_survive_process_restart(tmp_path):
    with _client(tmp_path, "alpha") as alpha:
        alpha.post("/api/batch", json={"ops": _upsert_ops(("A", "B", 2), ("B", "C", 3))})
        version_before = alpha.get("/api/state").json()["version"]["seq"]
        assert alpha.get("/api/leaf-usage").json()["rows"] == [
            {"top_part": "A", "leaf_part": "C", "qty": 6},
            {"top_part": "B", "leaf_part": "C", "qty": 3},
        ]
    # A fresh process/store against the same file: rows come from disk, no
    # expansion is run on startup.
    with _client(tmp_path, "restarted") as restarted:
        state = restarted.get("/api/state").json()
        assert state["version"]["seq"] == version_before
        assert state["leaf_usage"] == [
            {"top_part": "A", "leaf_part": "C", "qty": 6},
            {"top_part": "B", "leaf_part": "C", "qty": 3},
        ]


def test_demo_mode_off_ignores_pause_header(tmp_path):
    with _client(tmp_path, "plain", demo=False) as plain:
        plain.post("/api/batch", json={"ops": _upsert_ops(("A", "B", 1))})
        started = time.monotonic()
        response = plain.post(
            "/api/batch",
            json={"ops": _upsert_ops(("C", "D", 1))},
            headers={"X-Demo-Pause": "3"},
        )
        assert response.status_code == 200
        assert time.monotonic() - started < 1.0


def test_demo_seed_and_leaf_promotion(tmp_path):
    with _client(tmp_path, "demo") as demo:
        comment = demo.post("/api/demo/seed")
        assert comment.status_code == 200
        state = demo.get("/api/state").json()
        usage = {(r["top_part"], r["leaf_part"]): r["qty"] for r in state["leaf_usage"]}
        # Shared sub-assembly SA-1 used by FG-1 (2x) and FG-2 (3x): L-3 = 5.
        assert usage[("FG-1", "L-3")] == 10
        assert usage[("FG-2", "L-3")] == 15
        assert usage[("SA-1", "L-3")] == 5
        assert usage[("FG-1", "L-4")] == 4
        # Promote leaf L-4 into an assembly in one batch.
        response = demo.post("/api/demo/promote-leaf")
        assert response.status_code == 200
        usage = {
            (r["top_part"], r["leaf_part"]): r["qty"]
            for r in demo.get("/api/state").json()["leaf_usage"]
        }
        assert ("FG-1", "L-4") not in usage
        assert usage[("FG-1", "L-6")] == 12  # 2 * 2 * 3
        assert usage[("FG-1", "L-7")] == 4
        assert usage[("L-4", "L-6")] == 3


def test_usage_trace_endpoint(client):
    client.post(
        "/api/batch",
        json={
            "ops": _upsert_ops(
                ("FG", "SA", 2),
                ("SA", "L", 5),
                ("FG", "L", 3),
                ("FG", "ALT", 1),
                ("ALT", "L", 4),
            )
        },
    )
    response = client.get(
        "/api/usage-trace", params={"top": "fg", "leaf": "l", "limit": 2}
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["top_part"] == "FG" and payload["leaf_part"] == "L"
    assert payload["version_seq"] == client.get("/api/state").json()["version"]["seq"]
    assert payload["stored_qty"] == 17
    assert payload["recomputed_qty"] == 17
    assert payload["consistent"] is True
    assert [p["path"] for p in payload["paths"]] == [
        ["FG", "SA", "L"],
        ["FG", "ALT", "L"],
    ]
    assert [p["qty"] for p in payload["paths"]] == [10, 4]
    assert payload["other_qty"] == 3
    assert payload["shown_qty"] + payload["other_qty"] == payload["stored_qty"]
    assert payload["published_at"]


@pytest.mark.parametrize("limit", [0, 11, -1])
def test_usage_trace_limit_is_validated(client, limit):
    client.post("/api/batch", json={"ops": _upsert_ops(("FG", "L", 3))})
    response = client.get(
        "/api/usage-trace", params={"top": "FG", "leaf": "L", "limit": limit}
    )
    assert response.status_code == 422
    assert response.json()["code"] == "ValidationError"


def test_usage_trace_unknown_or_unpublished_row_is_404(client):
    client.post("/api/batch", json={"ops": _upsert_ops(("FG", "SA", 2), ("SA", "L", 5))})
    missing = client.get("/api/usage-trace", params={"top": "FG", "leaf": "NOPE"})
    assert missing.status_code == 404
    # SA itself has a stored row; FG is a top part but not a leaf here.
    assert client.get(
        "/api/usage-trace", params={"top": "SA", "leaf": "L"}
    ).status_code == 200
    assert client.get(
        "/api/usage-trace", params={"top": "FG", "leaf": "SA"}
    ).status_code == 404


def test_usage_trace_requires_parameters(client):
    assert client.get("/api/usage-trace", params={"top": "FG"}).status_code == 422
    assert client.get("/api/usage-trace", params={"leaf": "L"}).status_code == 422


def test_usage_trace_reads_one_version_while_other_process_commits(tmp_path):
    """A different web process rewrites the BOM; B only shows its read version."""
    with _client(tmp_path, "alpha") as alpha, _client(tmp_path, "beta") as beta:
        beta.post("/api/batch", json={"ops": _upsert_ops(("A", "B", 1), ("B", "C", 1))})
        versions = set()
        for qty in range(2, 12):
            alpha.post(
                "/api/batch", json={"ops": _upsert_ops(("A", "B", qty), ("B", "C", qty))}
            )
            payload = beta.get(
                "/api/usage-trace", params={"top": "A", "leaf": "C", "limit": 3}
            ).json()
            versions.add(payload["version_seq"])
            # Every number comes from the same committed structure: stored row,
            # path product, and total are all q*q from one version.
            assert payload["consistent"] is True, payload
            assert payload["stored_qty"] == payload["paths"][0]["qty"]
            assert payload["paths"][0]["path"] == ["A", "B", "C"]
            assert payload["shown_qty"] == payload["stored_qty"]
            assert payload["other_qty"] == 0
            q = int(payload["stored_qty"] ** 0.5)
            assert payload["stored_qty"] == q * q
        assert len(versions) >= 2


def test_trace_page_and_script_reference_each_other(tmp_path):
    with _client(tmp_path, "alpha") as client:
        page = client.get("/trace")
        assert page.status_code == 200
        assert "用量溯源" in page.text
        assert client.get("/static/trace.js").status_code == 200
        assert client.get("/static/style.css").status_code == 200


def test_usage_trace_single_snapshot_while_writer_hammers(tmp_path):
    """One process commits continuously; another's traces stay one version."""
    with _client(tmp_path, "alpha") as alpha, _client(tmp_path, "beta") as beta:
        beta.post("/api/batch", json={"ops": _upsert_ops(("A", "B", 1), ("B", "C", 1))})
        stop = threading.Event()
        failures = []
        versions = set()

        def writer():
            qty = 2
            while not stop.is_set():
                response = beta.post(
                    "/api/batch", json={"ops": _upsert_ops(("A", "B", qty), ("B", "C", qty))}
                )
                if response.status_code == 200:
                    qty += 1
                else:
                    failures.append(response.text)
                time.sleep(0.005)

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            for _ in range(25):
                response = alpha.get(
                    "/api/usage-trace", params={"top": "A", "leaf": "C", "limit": 3}
                )
                assert response.status_code == 200, response.text
                payload = response.json()
                versions.add(payload["version_seq"])
                assert payload["consistent"] is True, payload
                assert [p["path"] for p in payload["paths"]] == [["A", "B", "C"]]
                qty = payload["stored_qty"]
                root = int(qty ** 0.5)
                # Both edges share one quantity within one committed version.
                assert qty == root * root, payload
                assert payload["paths"][0]["qty"] == qty
                assert payload["shown_qty"] == qty
                assert payload["other_qty"] == 0
        finally:
            stop.set()
            thread.join()
        assert not failures, failures[:3]
        assert len(versions) >= 2
