"""Tests for graph algorithms, validation and the versioned store."""

from __future__ import annotations

import threading
import time

import pytest

from app.store import (
    BatchOp,
    BomStore,
    CapacityError,
    CyclicStructureError,
    Edge,
    ValidationError,
    apply_ops,
    find_cycles,
    parse_ops,
)


@pytest.fixture()
def store(tmp_path):
    return BomStore(str(tmp_path / "bom.db"))


def _ops(edges):
    return [BatchOp("upsert", upserts=(edge,)) for edge in edges]


def _usage(store):
    head, rows = store.usage_rows()
    assert head["version_seq"]
    return {(row["top_part"], row["leaf_part"]): row["qty"] for row in rows}


def _expander():
    return BomStore.__new__(BomStore)  # pure helpers need no DB


# -- expansion ------------------------------------------------------------


def test_quantity_multiplies_along_paths_and_sums():
    # A -> B(2) -> D(3) = 6 ; A -> C(1) -> D(5) = 5 ; total 11
    edges = [Edge("A", "B", 2), Edge("A", "C", 1), Edge("B", "D", 3), Edge("C", "D", 5)]
    assert _expander().expand(edges, "A") == {"D": 11}


def test_shared_subassembly_expanded_for_each_parent_and_itself():
    edges = [
        Edge("FG-1", "SA", 2),
        Edge("FG-1", "L1", 1),
        Edge("FG-2", "SA", 3),
        Edge("FG-2", "L2", 4),
        Edge("SA", "L3", 5),
    ]
    usage = _expander().expand_all(edges)
    assert usage[("FG-1", "L3")] == 10
    assert usage[("FG-2", "L3")] == 15
    assert usage[("FG-1", "L1")] == 1
    assert usage[("FG-2", "L2")] == 4
    assert usage[("SA", "L3")] == 5  # the shared sub-assembly has its own rows


def test_leaf_promoted_to_assembly_keeps_old_and_new_rows(store):
    store.commit_batch(_ops([Edge("FG", "L4", 2)]))
    assert _usage(store) == {("FG", "L4"): 2}
    store.commit_batch(_ops([Edge("L4", "L6", 3), Edge("L4", "L7", 1)]))
    assert _usage(store) == {
        ("FG", "L6"): 6,
        ("FG", "L7"): 2,
        ("L4", "L6"): 3,
        ("L4", "L7"): 1,
    }


def test_deep_shared_dag_is_memoised_not_exponential():
    # 35 levels, each assembly feeding both nodes of the next level: naive
    # path enumeration is 2^35 paths for a single top assembly.
    edges = []
    for level in range(35):
        for parent in (f"P{level}", f"Q{level}"):
            edges.append(Edge(parent, f"P{level + 1}", 1))
            edges.append(Edge(parent, f"Q{level + 1}", 1))
    started = time.monotonic()
    usage = _expander().expand_all(edges)
    assert time.monotonic() - started < 2.0
    # 2^34 distinct paths from P0 to P35 (last hop fixed), each qty 1.
    assert usage[("P0", "P35")] == 2 ** 34
    assert usage[("P0", "Q35")] == 2 ** 34


def test_ordered_ops_last_upsert_wins_and_delete_removes():
    base = [Edge("A", "B", 1)]
    ops = [
        BatchOp("upsert", upserts=(Edge("A", "B", 7),)),
        BatchOp("delete", parent="A", child="B"),
    ]
    edges, deleted = apply_ops(base, ops)
    assert edges == []
    assert deleted == [("A", "B")]


def test_replace_without_deletes_replaces_whole_structure():
    base = [Edge("A", "B", 1), Edge("B", "C", 1)]
    edges, _ = apply_ops(base, [BatchOp("replace", upserts=(Edge("X", "Y", 2),))])
    assert edges == [Edge("X", "Y", 2)]


def test_replace_with_deletes_applies_delta():
    base = [Edge("A", "B", 1), Edge("B", "C", 1)]
    edges, _ = apply_ops(
        base,
        [BatchOp("replace", upserts=(Edge("A", "B", 9),), deletes=(("B", "C"),))],
    )
    assert edges == [Edge("A", "B", 9)]


# -- cycle detection ------------------------------------------------------


def test_cycle_detection_reports_path_and_avoids_duplicate_rotations():
    edges = [Edge("A", "B", 1), Edge("B", "C", 1), Edge("C", "A", 1)]
    assert find_cycles(edges) == [["A", "B", "C", "A"]]
    assert find_cycles(edges + [Edge("B", "C", 1)]) == [["A", "B", "C", "A"]]


def test_cycle_detection_finds_self_loop():
    assert find_cycles([Edge("A", "A", 3)]) == [["A", "A"]]


def test_batch_containing_cycle_is_rejected_and_nothing_changes(store):
    store.commit_batch(_ops([Edge("A", "B", 2), Edge("B", "C", 3)]))
    before = store.snapshot()
    with pytest.raises(CyclicStructureError) as info:
        store.commit_batch(_ops([Edge("C", "A", 1)]))
    assert info.value.paths == [["A", "B", "C", "A"]]
    assert "A → B → C → A" in str(info.value)
    assert store.snapshot() == before


def test_multi_op_batch_cycle_judged_on_whole_final_structure(store):
    store.commit_batch(_ops([Edge("A", "B", 1), Edge("B", "C", 1)]))
    # Each upsert alone looks fine; the batch as a whole closes the loop.
    with pytest.raises(CyclicStructureError):
        store.commit_batch(_ops([Edge("X", "A", 1), Edge("C", "X", 1)]))
    assert not any(edge["parent"] == "X" for edge in store.snapshot()["edges"])


def test_batch_can_delete_and_recreate_while_remaining_acyclic(store):
    store.commit_batch(_ops([Edge("A", "B", 1)]))
    result = store.commit_batch(
        [
            BatchOp("delete", parent="A", child="B"),
            BatchOp("upsert", upserts=(Edge("A", "B", 4),)),
        ]
    )
    assert result.version_id > 0
    assert _usage(store) == {("A", "B"): 4}


# -- validation -----------------------------------------------------------


@pytest.mark.parametrize("qty", [0, -1, 1.5, "abc", True, None, "1.0"])
def test_invalid_quantities_rejected(qty):
    with pytest.raises(ValidationError):
        parse_ops({"ops": [{"op": "upsert", "parent": "A", "child": "B", "qty": qty}]})


def test_parse_ops_normalises_tokens_and_supports_replace():
    ops = parse_ops(
        {
            "ops": [
                {"op": "upsert", "parent": " a ", "child": "b", "qty": "2"},
                {"op": "delete", "parent": "A", "child": "B"},
                {
                    "op": "replace",
                    "deletes": [["C", "D"], {"parent": "E", "child": "F"}],
                    "upserts": [{"parent": "G", "child": "H", "qty": 3}],
                },
            ]
        }
    )
    assert ops[0].upserts[0].qty == 2
    assert ops[0].upserts[0].parent == "A"
    assert ops[1].parent == "A" and ops[1].child == "B"
    assert ops[2].deletes == (("C", "D"), ("E", "F"))
    assert ops[2].upserts[0].key == ("G", "H")


def test_bad_tokens_and_empty_ops_rejected():
    with pytest.raises(ValidationError):
        parse_ops({"ops": [{"op": "upsert", "parent": "", "child": "B", "qty": 1}]})
    with pytest.raises(ValidationError):
        parse_ops({"ops": [{"op": "upsert", "parent": "A B", "child": "C", "qty": 1}]})
    with pytest.raises(ValidationError):
        parse_ops({"ops": []})
    with pytest.raises(ValidationError):
        parse_ops({"ops": [{"op": "merge", "parent": "A", "child": "B"}]})


def _dense_acyclic_structure(parts, groups=5, group_size=20, target=200):
    """Build exactly ``target`` acyclic edges over ``groups * group_size`` parts.

    Every group is a forward-only DAG over a_i (edges always a_i -> a_j with
    j > i), so the union is acyclic by construction and each part's children
    are few, keeping expansion quantities small.
    """
    pairs: set[tuple[int, int]] = set()
    for group in range(groups):
        base = group * group_size
        tail = group_size - 1
        for offset in range(tail):
            pairs.add((base + offset, base + tail))
        for gap in range(2, group_size):
            for offset in range(0, group_size - gap):
                if len(pairs) >= target:
                    break
                pairs.add((base + offset, base + offset + gap))
            if len(pairs) >= target:
                break
        if len(pairs) >= target:
            break
    edges = [(parts[a], parts[b]) for a, b in sorted(pairs)]
    assert len(edges) == target
    assert len({p for pair in edges for p in pair}) <= groups * group_size
    return edges


def test_capacity_limits_enforced(store):
    chain = [f"P{i}" for i in range(101)]
    with pytest.raises(CapacityError):
        store.commit_batch(_ops([Edge(chain[i], chain[i + 1], 1) for i in range(100)]))

    parts = [f"N{i}" for i in range(100)]
    pairs = _dense_acyclic_structure(parts)
    assert find_cycles([Edge(a, b, 1) for a, b in pairs]) == []
    store.commit_batch(_ops([Edge(a, b, 1) for a, b in pairs]))
    assert len(store.snapshot()["edges"]) == 200
    with pytest.raises(CapacityError):
        store.commit_batch(_ops([Edge("ZZ", "N0", 1)]))
    assert len(store.snapshot()["edges"]) == 200


def test_noop_batch_keeps_version(store):
    store.commit_batch(_ops([Edge("A", "B", 2)]))
    seq = store.snapshot()["version"]["version_seq"]
    result = store.commit_batch(_ops([Edge("A", "B", 2)]))
    assert result.reused and result.version_id == seq


# -- atomicity / durability ------------------------------------------------


def test_failed_expansion_rolls_back_and_keeps_old_version(store, monkeypatch):
    store.commit_batch(_ops([Edge("A", "B", 2)]))
    before = store.snapshot()

    def boom(self, edges):
        raise RuntimeError("simulated expansion failure")

    monkeypatch.setattr(BomStore, "expand_all", boom)
    with pytest.raises(RuntimeError):
        store.commit_batch(_ops([Edge("C", "D", 3)]))
    assert store.snapshot() == before


def test_failure_between_row_writes_rolls_back(store, monkeypatch):
    store.commit_batch(_ops([Edge("A", "B", 2)]))
    before = store.snapshot()

    real_insert = BomStore._insert_version

    def fail_after_version(conn, edges):
        real_insert(conn, edges)
        raise RuntimeError("simulated crash after version insert")

    monkeypatch.setattr(BomStore, "_insert_version", staticmethod(fail_after_version))
    with pytest.raises(RuntimeError):
        store.commit_batch(_ops([Edge("C", "D", 3)]))
    assert store.snapshot() == before


def test_results_persist_across_new_store_instance(tmp_path):
    path = str(tmp_path / "persist.db")
    store = BomStore(path)
    store.commit_batch(_ops([Edge("A", "B", 2), Edge("B", "C", 3)]))
    replica = BomStore(path)
    assert _usage(replica) == {("A", "C"): 6, ("B", "C"): 3}
    assert replica.snapshot()["edges"] == [
        {"parent": "A", "child": "B", "qty": 2},
        {"parent": "B", "child": "C", "qty": 3},
    ]


# -- concurrency ----------------------------------------------------------


def test_first_writer_holds_no_lock_while_expanding(tmp_path):
    path = str(tmp_path / "nolock.db")
    store_a = BomStore(path)
    store_b = BomStore(path)
    store_a.commit_batch(_ops([Edge("A", "B", 1)]))
    observed = {}

    def slow_expansion(attempt, _base_seq):
        if attempt == 0:
            # Writer B commits while A is still "expanding" - proving no lock.
            observed["b"] = store_b.commit_batch(_ops([Edge("C", "D", 2)]))

    observed["a"] = store_a.commit_batch(
        _ops([Edge("E", "F", 3)]), pause_hook=slow_expansion
    )
    edges = {(e["parent"], e["child"]) for e in store_a.snapshot()["edges"]}
    assert {("A", "B"), ("C", "D"), ("E", "F")} <= edges
    assert observed["a"].retries >= 1
    assert observed["a"].version_id > observed["b"].version_id


def test_stale_second_writer_is_replanned_with_new_dependencies(tmp_path):
    """B commits a new child while A expands; A's plan must not drop it."""
    path = str(tmp_path / "stale.db")
    store_a = BomStore(path)
    store_b = BomStore(path)
    store_a.commit_batch(_ops([Edge("FG", "SA", 2), Edge("SA", "L1", 3)]))
    seen = {}

    def slow_expansion(attempt, _base_seq):
        if attempt == 0:
            seen["b"] = store_b.commit_batch(
                _ops([Edge("SA", "L2", 5), Edge("NEW", "L3", 1)])
            )

    seen["a"] = store_a.commit_batch(
        _ops([Edge("FG", "SA", 4)]), pause_hook=slow_expansion
    )
    usage = _usage(store_a)
    # L2 was added by B while A was expanding: A's stale plan must not win.
    assert usage[("FG", "L2")] == 20
    assert usage[("FG", "L1")] == 12
    assert usage[("NEW", "L3")] == 1
    assert seen["a"].version_id > seen["b"].version_id


def test_late_expansion_cannot_overwrite_newer_structure(tmp_path):
    path = str(tmp_path / "late.db")
    store_a = BomStore(path)
    store_b = BomStore(path)
    store_a.commit_batch(_ops([Edge("A", "B", 1)]))
    release = threading.Event()

    def slow_expansion(attempt, _base_seq):
        if attempt == 0:
            store_b.commit_batch(_ops([Edge("A", "B", 9)])
                                 + [BatchOp("upsert", upserts=(Edge("A", "C", 7),))])
            release.wait(1)

    result = store_a.commit_batch(_ops([Edge("A", "C", 2)]), pause_hook=slow_expansion)
    release.set()
    edges = {(e["parent"], e["child"]): e["qty"] for e in store_b.snapshot()["edges"]}
    assert edges[("A", "B")] == 9
    assert edges[("A", "C")] == 2
    assert _usage(store_a)[("A", "C")] == 2
    assert result.retries >= 1


def test_two_acyclic_edits_cannot_combine_into_cycle(tmp_path):
    """Writer B commits A->Z, Z->FG while A expands FG->A.

    Each edit is acyclic against the structure it saw.  A's re-plan after the
    CAS failure must detect the combined cycle and reject its own batch.
    """
    path = str(tmp_path / "compose.db")
    store_a = BomStore(path)
    store_b = BomStore(path)
    store_a.commit_batch(_ops([Edge("BASE", "FG", 1)]))
    b_done = threading.Event()
    a_expanding = threading.Event()
    errors: list[Exception] = []

    def writer_b():
        a_expanding.wait(2)
        try:
            built = store_b.commit_batch(_ops([Edge("A", "Z", 1), Edge("Z", "FG", 1)])
                                         + [BatchOp("delete", parent="BASE", child="FG")])
            assert built.version_id > 0
        except Exception as exc:  # pragma: no cover - diagnostics
            errors.append(exc)
        finally:
            b_done.set()

    def pause(attempt, _base_seq):
        if attempt == 0:
            a_expanding.set()
            assert b_done.wait(2), "writer B was blocked by a held write lock"

    thread = threading.Thread(target=writer_b)
    thread.start()
    try:
        store_a.commit_batch(_ops([Edge("FG", "A", 1)]), pause_hook=pause)
    except CyclicStructureError as exc:
        errors.append(exc)
    thread.join()
    assert len(errors) == 1 and isinstance(errors[0], CyclicStructureError), errors
    assert errors[0].paths and errors[0].paths[0][0] in {"A", "FG"}
    edges = [(e["parent"], e["child"]) for e in store_a.snapshot()["edges"]]
    assert find_cycles([Edge(*pair, 1) for pair in edges]) == []
    assert ("FG", "A") not in edges
    assert {("A", "Z"), ("Z", "FG")} <= set(edges)


def test_reads_never_see_mixed_versions(tmp_path):
    """Concurrent commits + reads: every read is one complete version."""
    path = str(tmp_path / "mixed.db")
    store = BomStore(path)
    store.commit_batch(_ops([Edge("A", "B", 1), Edge("B", "C", 1)]))
    stop = threading.Event()
    seen = set()

    def writer():
        qty = 2
        while not stop.is_set():
            try:
                store.commit_batch(
                    [
                        BatchOp("upsert", upserts=(Edge("A", "B", qty),)),
                        BatchOp("upsert", upserts=(Edge("B", "C", qty),)),
                    ]
                )
                qty += 1
            except Exception:  # pragma: no cover - retries handle contention
                pass

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        for _ in range(60):
            snapshot = store.snapshot()  # one atomic read of edges + usage + head
            edges = {(e["parent"], e["child"]): e["qty"] for e in snapshot["edges"]}
            rows = {(r["top_part"], r["leaf_part"]): r["qty"] for r in snapshot["leaf_usage"]}
            # A->B(q) and B->C(q) written in one batch: the stored expansion
            # must be q*q for one single q - never a mixture of two commits.
            q = edges[("A", "B")]
            assert edges[("B", "C")] == q, snapshot
            assert rows[("A", "C")] == q * q, snapshot
            assert rows[("B", "C")] == q, snapshot
            assert snapshot["version"]["edge_count"] == 2
            seen.add(q)
            time.sleep(0.002)
    finally:
        stop.set()
        thread.join()
    assert len(seen) >= 1


# -- usage trace (contribution paths) -------------------------------------


def _trace(store, top, leaf, limit=10):
    return store.trace_usage(top, leaf, limit)


def test_trace_lists_products_and_remainder_exactly(store):
    # FG -> SA(2) -> L(5) = 10 ; FG -> L(3) = 3 ; FG -> ALT(1) -> L(4) = 4
    store.commit_batch(
        _ops(
            [
                Edge("FG", "SA", 2),
                Edge("SA", "L", 5),
                Edge("FG", "L", 3),
                Edge("FG", "ALT", 1),
                Edge("ALT", "L", 4),
            ]
        )
    )
    result = _trace(store, "FG", "L", limit=2)
    assert result["stored_qty"] == 17
    assert result["recomputed_qty"] == 17
    assert result["consistent"] is True
    assert [(p["path"], p["qty"]) for p in result["paths"]] == [
        (["FG", "SA", "L"], 10),
        (["FG", "ALT", "L"], 4),
    ]
    assert result["shown_qty"] == 14
    assert result["other_qty"] == 3
    assert result["shown_qty"] + result["other_qty"] == result["stored_qty"]
    # Every step carries the parent/child edge quantity used in the product.
    assert result["paths"][0]["steps"] == [
        {"parent": "FG", "child": "SA", "qty": 2},
        {"parent": "SA", "child": "L", "qty": 5},
    ]


def test_trace_limit_one_and_full_listing_has_no_remainder(store):
    store.commit_batch(_ops([Edge("FG", "SA", 2), Edge("SA", "L", 5), Edge("FG", "L", 3)]))
    top1 = _trace(store, "FG", "L", limit=1)
    assert [p["qty"] for p in top1["paths"]] == [10]
    assert top1["other_qty"] == 3
    all_paths = _trace(store, "FG", "L", limit=10)
    assert [p["qty"] for p in all_paths["paths"]] == [10, 3]
    assert all_paths["other_qty"] == 0
    assert all_paths["shown_qty"] == all_paths["stored_qty"]


def test_trace_ties_break_on_complete_part_path(store):
    # Three equal contributions 2*3 = 6; sorted by full part path, not edge.
    store.commit_batch(
        _ops(
            [
                Edge("FG", "B-ASSY", 2),
                Edge("B-ASSY", "L", 3),
                Edge("FG", "A-ASSY", 2),
                Edge("A-ASSY", "L", 3),
                Edge("FG", "L", 6),
            ]
        )
    )
    result = _trace(store, "FG", "L", limit=10)
    assert [p["path"] for p in result["paths"]] == [
        ["FG", "A-ASSY", "L"],
        ["FG", "B-ASSY", "L"],
        ["FG", "L"],
    ]
    assert all(p["qty"] == 6 for p in result["paths"])


def test_trace_does_not_enumerate_exponential_shared_paths(store):
    # 60 levels of a 2-wide shared DAG: 2^59 paths, all quantity 1.  Pure
    # algorithm, so the 200-edge commit cap does not apply.
    from app.store import trace_top_paths as tracer

    edges = []
    for level in range(60):
        edges.append(Edge(f"P{level}", f"P{level + 1}", 1))
        edges.append(Edge(f"P{level}", f"Q{level + 1}", 1))
        edges.append(Edge(f"Q{level}", f"P{level + 1}", 1))
        edges.append(Edge(f"Q{level}", f"Q{level + 1}", 1))
    started = time.monotonic()
    paths, total = tracer(edges, "P0", "Q60", 5)
    assert time.monotonic() - started < 2.0
    assert total == 2 ** 59
    assert len(paths) == 5
    assert all(p.qty == 1 for p in paths)
    # Ties (all equal) are ordered by the complete part-number path.
    assert [p.path for p in paths] == sorted(p.path for p in paths)


def test_trace_top_k_suffix_pruning_is_exact():
    """Compared against brute-force enumeration on random small DAGs."""
    import itertools
    import random

    from app.store import trace_top_paths as tracer

    rng = random.Random(20261114)
    for _ in range(40):
        parts = [f"N{i:02d}" for i in range(9)]
        edges = []
        for a, b in itertools.combinations(range(len(parts)), 2):
            if rng.random() < 0.35:
                edges.append(Edge(parts[a], parts[b], rng.randint(1, 4)))
        if not edges:
            continue
        top, leaf = parts[0], parts[-1]
        children: dict[str, list[tuple[str, int]]] = {}
        for edge in edges:
            children.setdefault(edge.parent, []).append((edge.child, edge.qty))

        def all_paths(node: str) -> list[tuple[tuple[str, ...], int]]:
            if node == leaf:
                return [((leaf,), 1)]
            found = []
            for child, qty in children.get(node, []):
                for path, product in all_paths(child):
                    found.append(((node,) + path, qty * product))
            return found

        everything = all_paths(top)
        total = sum(product for _, product in everything)
        for limit in (1, 2, 3, 10):
            paths, got_total = tracer(edges, top, leaf, limit)
            expected = sorted(everything, key=lambda item: (-item[1], item[0]))[:limit]
            assert got_total == total
            assert [(p.path, p.qty) for p in paths] == expected
            assert sum(p.qty for p in paths) <= total


def test_trace_unknown_row_and_bad_limit(store):
    from app.store import NotFoundError, ValidationError

    store.commit_batch(_ops([Edge("FG", "L", 1)]))
    with pytest.raises(NotFoundError):
        _trace(store, "FG", "NOPE")
    with pytest.raises(ValidationError):
        _trace(store, "FG", "L", limit=0)
    with pytest.raises(ValidationError):
        _trace(store, "FG", "L", limit=11)


def test_trace_uses_stored_version_and_flags_inconsistency(store):
    """A tampered stored row is reported, never folded into ``other`` rows."""
    store.commit_batch(_ops([Edge("FG", "SA", 2), Edge("SA", "L", 5), Edge("FG", "L", 3)]))
    import sqlite3

    conn = sqlite3.connect(store.db_path)
    try:
        conn.execute("UPDATE leaf_usage SET qty = 99 WHERE top_part = 'FG' AND leaf_part = 'L'")
        conn.commit()
    finally:
        conn.close()
    result = _trace(store, "FG", "L", limit=1)
    assert result["recomputed_qty"] == 13
    assert result["stored_qty"] == 99
    assert result["consistent"] is False
    assert result["shown_qty"] == 10
    assert result["other_qty"] == 3  # the real remaining path, not 99 - 10
    assert result["other_paths"] is True


def test_trace_is_single_version_under_concurrent_commits(tmp_path):
    """While another process rewrites the BOM, each trace is self-consistent."""
    store = BomStore(str(tmp_path / "trace.db"))
    store.commit_batch(_ops([Edge("A", "B", 1), Edge("B", "C", 1)]))
    stop = threading.Event()

    def writer():
        qty = 2
        while not stop.is_set():
            try:
                store.commit_batch(
                    [
                        BatchOp("upsert", upserts=(Edge("A", "B", qty),)),
                        BatchOp("upsert", upserts=(Edge("B", "C", qty),)),
                    ]
                )
                qty += 1
            except Exception:  # pragma: no cover - contention only
                pass

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        for _ in range(50):
            result = store.trace_usage("A", "C", limit=10)
            assert result["consistent"], result
            assert result["paths"][0]["qty"] == result["stored_qty"]
            assert result["shown_qty"] == result["stored_qty"]
            assert result["other_qty"] == 0
            assert result["paths"][0]["path"] == ["A", "B", "C"]
            time.sleep(0.002)
    finally:
        stop.set()
        thread.join()
