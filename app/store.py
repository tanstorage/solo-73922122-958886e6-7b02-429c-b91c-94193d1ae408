"""SQLite storage for the BOM leaf-usage warehouse.

Design notes
------------
The SQLite database (WAL mode, ``busy_timeout``) is the single source of
truth and may be shared by several web processes.

Versioned model
    ``bom_version``    one row per committed structure version (BOM snapshot).
    ``bom_edge``       parent -> child relations, keyed by version.
    ``leaf_usage``     recursively expanded leaf usage, keyed by version.
    ``bom_head``       single row pointing at the currently published version.

A structural edit is one *batch*: it is applied in an ``IMMEDIATE``
transaction that writes both a brand-new ``bom_version`` (edges) and the
matching ``leaf_usage`` rows, then flips ``bom_head``.  Readers only ever
resolve ``bom_head`` inside their own read transaction, so they see one
complete version - never a mixture - and never trigger an expansion.
If anything fails the transaction rolls back and the previous version
stays published.

Concurrency safety without holding a write lock while expanding
---------------------------------------------------------------
Expansion (and cycle detection) runs on an in-memory snapshot of the batch's
final edges, *before* the write transaction starts, so no write lock is held
while it runs and other editors can commit meanwhile.  The final short
``BEGIN IMMEDIATE`` is a compare-and-swap on the published sequence number;
if another commit landed in between, it rolls back and re-plans from the
freshly published structure, re-running the cycle check over the combined
structure.  A stale plan can therefore neither overwrite newer structure,
drop newly added dependencies, nor let two individually acyclic edits
combine into a cycle.  Non-blocking lock attempts plus backoff keep two
web processes from serialising behind one stale plan.

Version-based storage also makes reads and downloads trivially consistent:
the API resolves the head once inside a single read transaction and only
selects already-materialised rows, so a response never mixes two commits.
"""

from __future__ import annotations

import csv
import io
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

MAX_PARTS = 100
MAX_EDGES = 200
MAX_QUANTITY = 1_000_000_000
MAX_TOTAL = 2 ** 63 - 1  # SQLite INTEGER is signed 64-bit
DEFAULT_DB_PATH = os.environ.get("BOM_DB_PATH", os.path.join("data", "bom.db"))
MAX_RETRIES = 25


class BomError(Exception):
    """Base class for domain errors surfaced as HTTP 4xx."""

    status_code = 400


class ValidationError(BomError):
    status_code = 422


class NotFoundError(BomError):
    status_code = 404


class CapacityError(BomError):
    status_code = 409


class ConflictError(BomError):
    status_code = 409


class CyclicStructureError(BomError):
    status_code = 409

    def __init__(self, paths: list[list[str]]):
        self.paths = paths
        shown = "；".join(" → ".join(path) for path in paths[:5])
        super().__init__(f"检测到环路：{shown}")


@dataclass(frozen=True)
class Edge:
    parent: str
    child: str
    qty: int

    @property
    def key(self) -> tuple[str, str]:
        return (self.parent, self.child)

    def as_dict(self) -> dict[str, Any]:
        return {"parent": self.parent, "child": self.child, "qty": self.qty}


@dataclass(frozen=True)
class BatchOp:
    """A single ordered batch operation (JSON body)."""

    op: str
    parent: str | None = None
    child: str | None = None
    qty: int | None = None
    upserts: tuple[Edge, ...] = ()
    deletes: tuple[tuple[str, str], ...] = ()


def _normalise_token(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} 必须是字符串")
    token = value.strip().upper()
    if not token:
        raise ValidationError(f"{field} 不能为空")
    if len(token) > 64:
        raise ValidationError(f"{field} 最长 64 个字符")
    if any(not (ch.isalnum() or ch in "-_.") for ch in token):
        raise ValidationError(f"{field} 只能包含字母、数字、- _ .")
    return token


def _normalise_qty(value: Any) -> int:
    if isinstance(value, bool):
        raise ValidationError("用量必须是正整数")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValidationError("用量必须是正整数")
        value = int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text.isdigit():
            raise ValidationError("用量必须是正整数")
        value = int(text)
    if not isinstance(value, int):
        raise ValidationError("用量必须是正整数")
    if value < 1:
        raise ValidationError("用量必须是正整数（>=1）")
    if value > MAX_QUANTITY:
        raise ValidationError(f"用量不能超过 {MAX_QUANTITY}")
    return value


def _edge_from_json(obj: Mapping[str, Any]) -> Edge:
    if not isinstance(obj, Mapping):
        raise ValidationError("关系条目必须是对象")
    return Edge(
        parent=_normalise_token(obj.get("parent"), "parent"),
        child=_normalise_token(obj.get("child"), "child"),
        qty=_normalise_qty(obj.get("qty")),
    )


def parse_ops(payload: Mapping[str, Any]) -> list[BatchOp]:
    reset_ops = parse_replace_reset(payload)
    if reset_ops is not None:
        return reset_ops
    ops = payload.get("ops")
    if not isinstance(ops, list) or not ops:
        raise ValidationError("ops 必须是非空数组")
    parsed: list[BatchOp] = []
    for index, raw in enumerate(ops, start=1):
        if not isinstance(raw, Mapping):
            raise ValidationError(f"第 {index} 条操作必须是对象")
        name = raw.get("op")
        if name == "upsert":
            parsed.append(BatchOp("upsert", upserts=(_edge_from_json(raw),)))
        elif name == "delete":
            parsed.append(
                BatchOp(
                    "delete",
                    parent=_normalise_token(raw.get("parent"), "parent"),
                    child=_normalise_token(raw.get("child"), "child"),
                )
            )
        elif name == "replace":
            raw_upserts = raw.get("upserts", [])
            raw_deletes = raw.get("deletes", [])
            if not isinstance(raw_upserts, list) or not isinstance(raw_deletes, list):
                raise ValidationError(f"第 {index} 条 replace 的 upserts/deletes 必须是数组")
            deletes: list[tuple[str, str]] = []
            for item in raw_deletes:
                if isinstance(item, Mapping):
                    deletes.append(
                        (
                            _normalise_token(item.get("parent"), "parent"),
                            _normalise_token(item.get("child"), "child"),
                        )
                    )
                elif isinstance(item, (list, tuple)) and len(item) == 2:
                    deletes.append(
                        (_normalise_token(item[0], "parent"), _normalise_token(item[1], "child"))
                    )
                else:
                    raise ValidationError(f"第 {index} 条 replace 的删除项格式错误")
            parsed.append(
                BatchOp(
                    "replace",
                    upserts=tuple(_edge_from_json(item) for item in raw_upserts),
                    deletes=tuple(deletes),
                )
            )
        else:
            raise ValidationError(f"第 {index} 条操作类型未知：{name!r}")
    return parsed


def apply_ops(base: Iterable[Edge], ops: Sequence[BatchOp]) -> tuple[list[Edge], list[tuple[str, str]]]:
    """Apply ordered ops to a base edge set.

    Returns the resulting edges (sorted) and the keys deleted during the
    batch (used to decide whether dependent usage rows must be refreshed).
    """
    edges: dict[tuple[str, str], Edge] = {edge.key: edge for edge in base}
    deleted: list[tuple[str, str]] = []
    for op in ops:
        if op.op == "upsert":
            for edge in op.upserts:
                edges[edge.key] = edge
        elif op.op == "delete":
            key = (op.parent, op.child)  # type: ignore[arg-type]
            edges.pop(key, None)
            deleted.append(key)
        elif op.op == "replace":
            if not op.deletes:
                # Whole-set replace: provided upserts become the full structure.
                deleted.extend(key for key in edges if key not in {e.key for e in op.upserts})
                edges = {}
            for key in op.deletes:
                edges.pop(key, None)
                deleted.append(key)
            for edge in op.upserts:
                edges[edge.key] = edge
        else:  # pragma: no cover - parse_ops rejects unknown ops
            raise ValidationError(f"未知操作 {op.op!r}")
    return sorted(edges.values(), key=lambda e: (e.parent, e.child)), deleted


def parts_from_edges(edges: Iterable[Edge]) -> list[str]:
    parts: set[str] = set()
    for edge in edges:
        parts.add(edge.parent)
        parts.add(edge.child)
    return sorted(parts)


def validate_limits(edges: Sequence[Edge], explicit_parts: Iterable[str] = ()) -> None:
    parts = set(parts_from_edges(edges)) | {p for p in explicit_parts}
    if len(parts) > MAX_PARTS:
        raise CapacityError(f"料号数量 {len(parts)} 超过上限 {MAX_PARTS}")
    if len(edges) > MAX_EDGES:
        raise CapacityError(f"父子关系数量 {len(edges)} 超过上限 {MAX_EDGES}")


def apply_replace(base: Iterable[Edge], op: BatchOp) -> list[Edge]:
    """Throw away the whole current structure in favour of ``upserts``."""
    del base  # ``op.deletes`` is only informational for a whole-set replace
    return sorted(
        {edge.key: edge for edge in op.upserts}.values(), key=lambda e: (e.parent, e.child)
    )


def parse_replace_reset(payload: Mapping[str, Any]) -> list[BatchOp] | None:
    """Detect the explicit ``{"reset": true, ...}`` batch form."""
    if not payload.get("reset"):
        return None
    raw_upserts = payload.get("upserts", [])
    if not isinstance(raw_upserts, list):
        raise ValidationError("upserts 必须是数组")
    upserts = tuple(_edge_from_json(item) for item in raw_upserts)
    if not upserts:
        raise ValidationError("reset 批次必须至少包含一条关系")
    return [BatchOp("replace", upserts=upserts)]


# --------------------------------------------------------------------------
# Graph algorithms (pure, run on snapshots - never under a DB write lock)
# --------------------------------------------------------------------------


def find_cycles(edges: Sequence[Edge], limit: int = 10) -> list[list[str]]:
    """Return up to ``limit`` distinct simple cycles as node paths.

    Each path starts and ends with the same part, e.g.
    ``["A", "B", "C", "A"]``.  Iterative DFS with a colour map keeps this
    O(V+E) and immune to recursion depth issues.
    """
    adjacency: dict[str, list[str]] = {}
    for edge in edges:
        adjacency.setdefault(edge.parent, []).append(edge.child)
    for children in adjacency.values():
        children.sort()

    WHITE, GREY, BLACK = 0, 1, 2
    colour: dict[str, int] = {}
    stack: list[str] = []
    position: dict[str, int] = {}
    cycles: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()

    def record(cycle: list[str]) -> bool:
        # Canonical rotation so A→B→A and B→A→B count as the same loop.
        body = cycle[:-1]
        rotations = [tuple(body[i:] + body[:i]) for i in range(len(body))]
        canonical = min(rotations)
        if canonical in seen:
            return False
        seen.add(canonical)
        cycles.append(cycle)
        return len(cycles) >= limit

    for root in sorted(adjacency):
        if colour.get(root, WHITE) != WHITE:
            continue
        # Explicit DFS frames: (node, iterator index)
        frames: list[tuple[str, int]] = [(root, 0)]
        colour[root] = GREY
        stack.append(root)
        position[root] = 0
        while frames:
            node, index = frames[-1]
            children = adjacency.get(node, [])
            if index >= len(children):
                frames.pop()
                colour[node] = BLACK
                stack.pop()
                position.pop(node, None)
                continue
            child = children[index]
            frames[-1] = (node, index + 1)
            state = colour.get(child, WHITE)
            if state == GREY:
                start = position[child]
                if start is not None:
                    if record(stack[start:] + [child]):
                        return cycles
            elif state == WHITE:
                colour[child] = GREY
                position[child] = len(stack)
                stack.append(child)
                frames.append((child, 0))
    return cycles


def _edge_signature(edges: Iterable[Edge]) -> tuple[tuple[str, str, int], ...]:
    return tuple((e.parent, e.child, e.qty) for e in edges)


@dataclass(frozen=True)
class TracedPath:
    """One BOM path and its exact contribution (product of edge quantities)."""

    path: tuple[str, ...]
    qty: int
    steps: tuple[tuple[str, str, int], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": list(self.path),
            "qty": self.qty,
            "steps": [
                {"parent": parent, "child": child, "qty": qty}
                for parent, child, qty in self.steps
            ],
        }


def trace_top_paths(
    edges: Sequence[Edge], top: str, leaf: str, limit: int
) -> tuple[list[TracedPath], int]:
    """Return the ``limit`` largest paths from ``top`` to ``leaf`` and the total.

    Only the best ``limit`` paths per node are kept, so the work is polynomial
    in the graph size (``O(V * out_degree * limit)``) and never enumerates the
    potentially exponential number of simple paths.  A suffix of a top-``k``
    path must itself be a top-``k`` suffix: scaling every suffix of a child by
    the same positive edge quantity preserves their order, so paths whose
    suffix is outside the child's top ``k`` can never enter the parent's top
    ``k``.

    Ties are broken by the complete part-number path (ascending).  The total
    is accumulated with exact Python integers, so
    ``sum(path.qty) + other_contributions == total`` holds exactly.
    """
    if limit < 1:
        raise ValidationError("limit 必须在 1..10 之间")
    children: dict[str, list[tuple[str, int]]] = {}
    for edge in edges:
        children.setdefault(edge.parent, []).append((edge.child, edge.qty))
    for kids in children.values():
        kids.sort()

    memo: dict[str, tuple[list[TracedPath], int]] = {}
    visiting: list[str] = []

    def walk(part: str) -> tuple[list[TracedPath], int]:
        # A path ends at the requested leaf, even if that part happens to have
        # children: its contribution is the product of quantities up to it.
        if part == leaf:
            return [TracedPath((leaf,), 1, ())], 1
        cached = memo.get(part)
        if cached is not None:
            return cached
        if part in visiting:
            start = visiting.index(part)
            raise CyclicStructureError([visiting[start:] + [part]])
        visiting.append(part)
        candidates: list[TracedPath] = []
        total = 0
        for child, edge_qty in children.get(part, []):
            child_paths, child_total = walk(child)
            total += edge_qty * child_total
            for item in child_paths:
                candidates.append(
                    TracedPath(
                        (part,) + item.path,
                        edge_qty * item.qty,
                        ((part, child, edge_qty),) + item.steps,
                    )
                )
        visiting.pop()
        candidates.sort(key=lambda item: (-item.qty, item.path))
        best = candidates[:limit]
        memo[part] = (best, total)
        return best, total

    paths, total = walk(top)
    return paths, total


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


@dataclass
class CommitResult:
    version_id: int
    version_seq: int
    created_at: str
    reused: bool
    retries: int
    leaf_rows: int


class BomStore:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        directory = os.path.dirname(os.path.abspath(self.db_path))
        os.makedirs(directory, exist_ok=True)
        self._init_schema()

    # -- connection helpers ------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            isolation_level=None,  # explicit transaction control
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        # WAL is a persistent database property, set once by _init_schema; it
        # must not be re-negotiated per request (that can need extra locks).
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def reader(self) -> Iterator[sqlite3.Connection]:
        """Consistent read snapshot: head is resolved once inside a txn."""
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS bom_version (
                    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at  TEXT NOT NULL,
                    edge_count  INTEGER NOT NULL,
                    part_count  INTEGER NOT NULL,
                    checksum    TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bom_edge (
                    version_seq INTEGER NOT NULL REFERENCES bom_version(seq),
                    parent      TEXT NOT NULL,
                    child       TEXT NOT NULL,
                    qty         INTEGER NOT NULL CHECK (qty > 0),
                    PRIMARY KEY (version_seq, parent, child)
                );

                CREATE TABLE IF NOT EXISTS leaf_usage (
                    version_seq INTEGER NOT NULL REFERENCES bom_version(seq),
                    top_part    TEXT NOT NULL,
                    leaf_part   TEXT NOT NULL,
                    qty         INTEGER NOT NULL CHECK (qty > 0),
                    PRIMARY KEY (version_seq, top_part, leaf_part)
                ) WITHOUT ROWID;

                CREATE INDEX IF NOT EXISTS ix_leaf_usage_leaf
                    ON leaf_usage (version_seq, leaf_part);

                CREATE TABLE IF NOT EXISTS bom_head (
                    id          INTEGER PRIMARY KEY CHECK (id = 1),
                    version_seq INTEGER NOT NULL REFERENCES bom_version(seq),
                    updated_at  TEXT NOT NULL
                );
                """
            )
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT version_seq FROM bom_head WHERE id = 1").fetchone()
            if row is None:
                seq = self._insert_empty_version(conn)
                conn.execute(
                    "INSERT INTO bom_head (id, version_seq, updated_at) VALUES (1, ?, ?)",
                    (seq, _now()),
                )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    # -- low-level snapshot helpers ---------------------------------------

    @staticmethod
    def _load_edges(conn: sqlite3.Connection, version_seq: int) -> list[Edge]:
        rows = conn.execute(
            "SELECT parent, child, qty FROM bom_edge WHERE version_seq = ? ORDER BY parent, child",
            (version_seq,),
        ).fetchall()
        return [Edge(r["parent"], r["child"], r["qty"]) for r in rows]

    @staticmethod
    def _insert_empty_version(conn: sqlite3.Connection) -> int:
        cursor = conn.execute(
            "INSERT INTO bom_version (created_at, edge_count, part_count, checksum) VALUES (?, 0, 0, ?)",
            (_now(), _checksum([])),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _insert_version(conn: sqlite3.Connection, edges: Sequence[Edge]) -> int:
        cursor = conn.execute(
            "INSERT INTO bom_version (created_at, edge_count, part_count, checksum) VALUES (?, ?, ?, ?)",
            (_now(), len(edges), len(set(parts_from_edges(edges))), _checksum(edges)),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _prune(conn: sqlite3.Connection, keep_seq: int) -> None:
        """Keep only the published version; results must persist across restarts."""
        conn.execute("DELETE FROM leaf_usage WHERE version_seq <> ?", (keep_seq,))
        conn.execute("DELETE FROM bom_edge WHERE version_seq <> ?", (keep_seq,))
        conn.execute("DELETE FROM bom_version WHERE seq <> ?", (keep_seq,))

    # -- expansion ---------------------------------------------------------

    def expand(self, edges: Sequence[Edge], top_part: str) -> dict[str, int]:
        """Multiply quantities down every path and sum across paths.

        Dynamic programming over the DAG (memoised per sub-assembly) keeps
        shared sub-assemblies from exploding into an exponential number of
        paths.  A part with no children is a leaf; the top part itself is
        not reported.  Cycles must already have been rejected; a leftover
        cycle raises ``CyclicStructureError`` defensively.
        """
        children: dict[str, list[tuple[str, int]]] = {}
        for edge in edges:
            children.setdefault(edge.parent, []).append((edge.child, edge.qty))
        memo: dict[str, dict[str, int]] = {}
        visiting: list[str] = []

        def leaves_of(part: str) -> dict[str, int]:
            cached = memo.get(part)
            if cached is not None:
                return cached
            if part in visiting:
                start = visiting.index(part)
                raise CyclicStructureError([visiting[start:] + [part]])
            kids = children.get(part)
            if not kids:
                result = {part: 1}
            else:
                visiting.append(part)
                result = {}
                for child, qty in kids:
                    for leaf, multiplier in leaves_of(child).items():
                        result[leaf] = result.get(leaf, 0) + multiplier * qty
                visiting.pop()
            memo[part] = result
            return result

        totals = dict(leaves_of(top_part))
        totals.pop(top_part, None)
        return totals

    def expand_all(self, edges: Sequence[Edge]) -> dict[tuple[str, str], int]:
        """Leaf usage for every assembly (every part that has children).

        The warehouse stores one row per (assembly, leaf), so a sub-assembly
        shared by two top items is expanded once for itself and again as part
        of each parent's own expansion.
        """
        assemblies = sorted({edge.parent for edge in edges})
        usage: dict[tuple[str, str], int] = {}
        for top in assemblies:
            for leaf, qty in self.expand(edges, top).items():
                if qty > MAX_TOTAL:
                    raise OverflowError("展开用量超过 64 位整数上限")
                usage[(top, leaf)] = qty
        return usage

    # -- reads -------------------------------------------------------------

    @staticmethod
    def _head(conn: sqlite3.Connection) -> sqlite3.Row:
        row = conn.execute(
            """
            SELECT h.version_seq, h.updated_at, v.created_at, v.edge_count, v.part_count, v.checksum
            FROM bom_head h JOIN bom_version v ON v.seq = h.version_seq
            WHERE h.id = 1
            """
        ).fetchone()
        if row is None:  # pragma: no cover - schema bootstrap guarantees a row
            raise RuntimeError("bom_head 未初始化")
        return row

    @staticmethod
    def _read_head(conn: sqlite3.Connection) -> sqlite3.Row:
        """Static alias so ``_claim_writer`` (also static) can read the head."""
        return BomStore._head(conn)

    @staticmethod
    def _claim_writer(conn: sqlite3.Connection, base_seq: int) -> bool:
        """Take the single-writer slot and CAS on the published version.

        ``BEGIN IMMEDIATE`` acquires SQLite's one write lock.  It is held only
        for the short pure-SQL work below; no expansion or cycle check ever
        runs inside it, so other web processes are not blocked while a batch
        is being planned.  If another commit landed since the snapshot, the
        caller rolls back and re-plans against the fresh structure.
        """
        conn.execute("BEGIN IMMEDIATE")
        head = BomStore._read_head(conn)
        if head["version_seq"] != base_seq:
            conn.execute("ROLLBACK")
            return False
        return True

    def snapshot(self) -> dict[str, Any]:
        with self.reader() as conn:
            head = self._head(conn)
            edges = self._load_edges(conn, head["version_seq"])
            usage = conn.execute(
                """
                SELECT top_part, leaf_part, qty FROM leaf_usage
                WHERE version_seq = ? ORDER BY top_part, leaf_part
                """,
                (head["version_seq"],),
            ).fetchall()
            return {
                "version": dict(head),
                "edges": [e.as_dict() for e in edges],
                "leaf_usage": [dict(row) for row in usage],
            }

    def parts(self) -> list[str]:
        with self.reader() as conn:
            head = self._head(conn)
            return parts_from_edges(self._load_edges(conn, head["version_seq"]))

    def usage_rows(
        self, top: str | None = None, leaf: str | None = None
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Read leaf usage for the published version inside one snapshot."""
        top = top.strip().upper() if top else None
        leaf = leaf.strip().upper() if leaf else None
        with self.reader() as conn:
            head = self._head(conn)
            sql = [
                "SELECT top_part, leaf_part, qty FROM leaf_usage WHERE version_seq = ?",
            ]
            params: list[Any] = [head["version_seq"]]
            if top:
                sql.append("AND top_part = ?")
                params.append(top)
            if leaf:
                sql.append("AND leaf_part = ?")
                params.append(leaf)
            sql.append("ORDER BY top_part, leaf_part")
            rows = conn.execute(" ".join(sql), params).fetchall()
            return dict(head), [dict(row) for row in rows]

    def trace_usage(
        self, top: str, leaf: str, limit: int = 10
    ) -> dict[str, Any]:
        """Trace the largest ``limit`` BOM paths of one *stored* usage row.

        Edges and the stored row are read inside a single read transaction, so
        the traced paths and the compared total both belong to the same
        published version.  Path enumeration never happens: only the best
        ``limit`` suffixes per node are carried (see ``trace_top_paths``).

        ``other_qty`` is always derived from the recomputed relations, never
        from ``stored_qty - shown_qty``.  If recomputation disagrees with the
        stored row the payload says so (``consistent: false``) and the
        difference is *not* attributed to unnamed paths.
        """
        top = _normalise_token(top, "top")
        leaf = _normalise_token(leaf, "leaf")
        if not 1 <= limit <= 10:
            raise ValidationError("路径条数必须在 1..10 之间")
        with self.reader() as conn:
            head = self._head(conn)
            version_seq = int(head["version_seq"])
            row = conn.execute(
                "SELECT qty FROM leaf_usage WHERE version_seq = ? AND top_part = ? AND leaf_part = ?",
                (version_seq, top, leaf),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"版本 v{version_seq} 中没有落库用量行 {top} → {leaf}")
            stored_qty = int(row["qty"])
            edges = self._load_edges(conn, version_seq)

        paths, recomputed_qty = trace_top_paths(edges, top, leaf, limit)
        shown_qty = sum(item.qty for item in paths)
        other_qty = recomputed_qty - shown_qty
        consistent = recomputed_qty == stored_qty
        return {
            "version_seq": version_seq,
            "published_at": head["updated_at"],
            "top_part": top,
            "leaf_part": leaf,
            "limit": limit,
            "stored_qty": stored_qty,
            "recomputed_qty": recomputed_qty,
            "consistent": consistent,
            "paths": [item.as_dict() for item in paths],
            "shown_qty": shown_qty,
            "other_qty": other_qty,
            "other_paths": other_qty != 0,
        }

    def usage_csv(self, top: str | None = None, leaf: str | None = None) -> tuple[str, int]:
        head, rows = self.usage_rows(top=top, leaf=leaf)
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(["top_part", "leaf_part", "qty", "version_seq"])
        for row in rows:
            writer.writerow([row["top_part"], row["leaf_part"], row["qty"], head["version_seq"]])
        return buffer.getvalue(), int(head["version_seq"])

    # -- writes ------------------------------------------------------------

    def commit_batch(
        self,
        ops: Sequence[BatchOp],
        pause_hook: Callable[[int, int], None] | None = None,
    ) -> CommitResult:
        """Apply ops atomically; retry with re-planning on concurrent commits.

        The expansion (expensive) happens outside the write transaction.
        The write transaction re-reads the published snapshot under
        ``BEGIN IMMEDIATE`` and compare-and-swaps on it, so a stale plan is
        discarded and rebuilt from the freshest structure.

        ``pause_hook`` (demo-only) runs *before* the write transaction with
        ``(attempt, base_seq)`` so tests can interleave a second writer while
        the first one is still expanding - proving no write lock is held.
        """
        if not ops:
            raise ValidationError("ops 不能为空")
        last_error: Exception | None = None
        jitter = (hash((ops[0].op, len(ops), ops[-1].parent)) % 100) / 1000
        for attempt in range(MAX_RETRIES):
            with self.reader() as conn:
                head = self._head(conn)
                base_seq = head["version_seq"]
                base_edges = self._load_edges(conn, base_seq)
            result = self._try_commit(ops, base_seq, base_edges, attempt, pause_hook)
            if isinstance(result, CommitResult):
                if attempt:
                    result.retries = attempt
                return result
            last_error = result
            # Back off with jitter to de-synchronise two writers.
            time.sleep(min(0.005 * (2 ** attempt), 0.2) + jitter * attempt)
        raise ConflictError(f"并发修改过于频繁，请重试（{last_error}）")

    def _try_commit(
        self,
        ops: Sequence[BatchOp],
        base_seq: int,
        base_edges: Sequence[Edge],
        attempt: int = 0,
        pause_hook: Callable[[int, int], None] | None = None,
    ) -> CommitResult | None:
        new_edges, _deleted = apply_ops(base_edges, ops)
        validate_limits(new_edges)
        cycles = find_cycles(new_edges)
        if cycles:
            raise CyclicStructureError(cycles)

        # Pure computation on the snapshot: no write lock is held here.
        if pause_hook is not None:
            pause_hook(attempt, base_seq)
        try:
            usage = self.expand_all(new_edges)
        except OverflowError as exc:
            raise ValidationError(f"用量溢出：{exc}") from None

        conn = self._connect()
        try:
            if not self._claim_writer(conn, base_seq):
                return None  # concurrent commit: re-plan on fresh data
            head = self._head(conn)
            current_edges = self._load_edges(conn, base_seq)
            if _edge_signature(current_edges) != _edge_signature(base_edges):  # pragma: no cover
                conn.execute("ROLLBACK")
                return None
            if _edge_signature(new_edges) == _edge_signature(current_edges):
                # No-op batch: keep the existing version untouched.
                conn.execute("COMMIT")
                return CommitResult(base_seq, base_seq, head["created_at"], True, 0, 0)

            new_seq = self._insert_version(conn, new_edges)
            conn.executemany(
                "INSERT INTO bom_edge (version_seq, parent, child, qty) VALUES (?, ?, ?, ?)",
                [(new_seq, e.parent, e.child, e.qty) for e in new_edges],
            )
            conn.executemany(
                "INSERT INTO leaf_usage (version_seq, top_part, leaf_part, qty) VALUES (?, ?, ?, ?)",
                [(new_seq, top, leaf, qty) for (top, leaf), qty in sorted(usage.items())],
            )
            conn.execute(
                "UPDATE bom_head SET version_seq = ?, updated_at = ? WHERE id = 1",
                (new_seq, _now()),
            )
            # Only the newly published version's rows survive (restart-safe,
            # bounded storage) - done in the same transaction so readers can
            # never observe a half-pruned snapshot.
            self._prune(conn, new_seq)
            created_row = conn.execute(
                "SELECT created_at FROM bom_version WHERE seq = ?", (new_seq,)
            ).fetchone()
            conn.execute("COMMIT")
            return CommitResult(
                new_seq, new_seq, created_row["created_at"], False, 0, len(usage)
            )
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def seed_if_empty(self, edges: Sequence[Edge]) -> bool:
        """Insert demo structure only when the warehouse is empty."""
        with self.reader() as conn:
            head = self._head(conn)
            if conn.execute(
                "SELECT 1 FROM bom_edge WHERE version_seq = ? LIMIT 1", (head["version_seq"],)
            ).fetchone():
                return False
        ops = [BatchOp("upsert", upserts=(edge,)) for edge in edges]
        self.commit_batch(ops)
        return True


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _checksum(edges: Sequence[Edge]) -> str:
    import hashlib

    payload = "\n".join(f"{e.parent},{e.child},{e.qty}" for e in edges)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
