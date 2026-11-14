"""FastAPI application for multi-level BOM leaf usage.

``create_app`` makes the store and demo switches injectable so tests can run
several independent web processes against the same SQLite file.
"""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from typing import Any, Callable, Mapping

from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .demo import DEMO_EDGES, INTERLEAVE_A, INTERLEAVE_B, PROMOTE_LEAF_OPS
from .store import (
    MAX_EDGES,
    MAX_PARTS,
    BomError,
    BomStore,
    CommitResult,
    NotFoundError,
    parse_ops,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_DEMO_PAUSE = 15.0


def create_app(
    store: BomStore | None = None,
    *,
    instance: str | None = None,
    demo_mode: bool | None = None,
    seed_demo: bool | None = None,
) -> FastAPI:
    active_store = store or BomStore(os.environ.get("BOM_DB_PATH"))
    active_instance = instance or os.environ.get("BOM_INSTANCE", "web")
    demo_enabled = (
        os.environ.get("BOM_DEMO_MODE", "0") == "1" if demo_mode is None else demo_mode
    )
    seed_enabled = (
        os.environ.get("BOM_SEED_DEMO", "1") == "1" if seed_demo is None else seed_demo
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if seed_enabled:
            active_store.seed_if_empty(DEMO_EDGES)
        yield

    app = FastAPI(title="BOM 叶件用量数仓", version="1.0.0", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
    templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

    @app.exception_handler(BomError)
    async def bom_error_handler(_: Request, exc: BomError) -> JSONResponse:
        payload: dict[str, Any] = {"error": str(exc), "code": type(exc).__name__}
        paths = getattr(exc, "paths", None)
        if paths:
            payload["paths"] = paths
        return JSONResponse(status_code=exc.status_code, content=payload)

    def state_payload() -> dict[str, Any]:
        snapshot = active_store.snapshot()
        version = snapshot["version"]
        part_set = {edge["parent"] for edge in snapshot["edges"]} | {
            edge["child"] for edge in snapshot["edges"]
        }
        return {
            "instance": active_instance,
            "limits": {"max_parts": MAX_PARTS, "max_edges": MAX_EDGES},
            "demo_mode": demo_enabled,
            "version": {
                "seq": version["version_seq"],
                "published_at": version["updated_at"],
                "created_at": version["created_at"],
                "edge_count": version["edge_count"],
                "part_count": version["part_count"],
                "checksum": version["checksum"],
            },
            "edges": snapshot["edges"],
            "leaf_usage": snapshot["leaf_usage"],
            "parts": sorted(part_set),
        }

    def pause_hook_from(request: Request) -> Callable[[int, int], None] | None:
        header = request.headers.get("x-demo-pause")
        if not demo_enabled or not header:
            return None
        try:
            seconds = float(header)
        except ValueError:
            raise BomError("X-Demo-Pause 必须是秒数") from None
        if not 0 < seconds <= MAX_DEMO_PAUSE:
            return None

        def hook(attempt: int, _base_seq: int) -> None:
            # Runs before BEGIN IMMEDIATE: simulates a slow in-memory expansion.
            if attempt == 0:
                time.sleep(seconds)

        return hook

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "index.html", {"instance": active_instance, "demo_mode": demo_enabled})

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "instance": active_instance}

    @app.get("/api/state")
    def state() -> dict[str, Any]:
        return state_payload()

    @app.post("/api/batch")
    def commit_batch(
        request: Request, payload: Mapping[str, Any] = Body(...)
    ) -> dict[str, Any]:
        ops = parse_ops(payload)
        result: CommitResult = active_store.commit_batch(
            ops, pause_hook=pause_hook_from(request)
        )
        return {
            "instance": active_instance,
            "version_seq": result.version_id,
            "reused": result.reused,
            "retries": result.retries,
            "leaf_rows": result.leaf_rows,
        }

    @app.get("/api/leaf-usage")
    def leaf_usage(
        top: str | None = Query(default=None, max_length=64),
        leaf: str | None = Query(default=None, max_length=64),
    ) -> dict[str, Any]:
        head, rows = active_store.usage_rows(top=top, leaf=leaf)
        return {
            "version_seq": head["version_seq"],
            "published_at": head["updated_at"],
            "rows": rows,
        }

    @app.get("/api/leaf-usage.csv")
    def leaf_usage_csv(
        top: str | None = Query(default=None, max_length=64),
        leaf: str | None = Query(default=None, max_length=64),
    ) -> Response:
        body, version_seq = active_store.usage_csv(top=top, leaf=leaf)
        filename = f"bom_leaf_usage_v{version_seq}.csv"
        return Response(
            content=body,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Bom-Version": str(version_seq),
                "Cache-Control": "no-store",
            },
        )

    @app.get("/api/usage-summary")
    def usage_summary() -> dict[str, Any]:
        """Read-only aggregate for the dashboard; never triggers an expansion."""
        head, rows = active_store.usage_rows()
        by_top: dict[str, int] = {}
        for row in rows:
            by_top[row["top_part"]] = by_top.get(row["top_part"], 0) + 1
        return {
            "version_seq": head["version_seq"],
            "row_count": len(rows),
            "tops": [
                {"top_part": top, "leaf_count": count}
                for top, count in sorted(by_top.items())
            ],
        }

    @app.post("/api/demo/seed")
    def seed_demo_endpoint() -> dict[str, Any]:
        if not (demo_enabled or os.environ.get("BOM_ALLOW_SEED", "1") == "1"):
            raise NotFoundError("演示数据未启用")
        created = active_store.seed_if_empty(DEMO_EDGES)
        head, _ = active_store.usage_rows()
        return {"seeded": created, "version_seq": head["version_seq"]}

    @app.post("/api/demo/promote-leaf")
    def demo_promote_leaf() -> dict[str, Any]:
        """Sample: leaf L-4 becomes an assembly (L-6/L-7) in one atomic batch."""
        if not demo_enabled:
            raise NotFoundError("演示数据未启用")
        result = active_store.commit_batch(PROMOTE_LEAF_OPS)
        return {"version_seq": result.version_id, "leaf_rows": result.leaf_rows}

    @app.post("/api/demo/interleave/{side}")
    def demo_interleave(side: str, request: Request) -> dict[str, Any]:
        """Sample pair of individually-acyclic edits for the interleaving test.

        ``side=a`` adds FG-9 → NEW-1; ``side=b`` adds NEW-1 → FG-9.  Each is
        acyclic alone; together they form FG-9 → NEW-1 → FG-9.  A stale plan
        must not be committed, so the second writer has to see the cycle.
        """
        if not demo_enabled:
            raise NotFoundError("演示数据未启用")
        if side == "a":
            ops = INTERLEAVE_A
        elif side == "b":
            ops = INTERLEAVE_B
        else:
            raise NotFoundError("side 只能是 a 或 b")
        result = active_store.commit_batch(ops, pause_hook=pause_hook_from(request))
        return {
            "version_seq": result.version_id,
            "retries": result.retries,
            "reused": result.reused,
        }

    @app.get("/api/demo/scenarios")
    def demo_scenarios() -> dict[str, Any]:
        """Describe the bundled interleaving sample so the UI can offer it."""
        return {
            "shared_subassembly": {
                "description": "SA-1 同时被 FG-1、FG-2 使用；其叶件在顶层与自身两个维度各有一行",
                "edges": [
                    {"parent": "FG-1", "child": "SA-1", "qty": 2},
                    {"parent": "FG-2", "child": "SA-1", "qty": 3},
                    {"parent": "SA-1", "child": "L-3", "qty": 5},
                ],
            },
            "leaf_promoted": {
                "description": "叶件 L-4 改为由 L-6/L-7 组成的装配件，整批提交",
                "endpoint": "/api/demo/promote-leaf",
            },
            "interleaving_cycle": {
                "description": "A 加 FG-9→NEW-1，B 加 NEW-1→FG-9；单独无环，交错后成环",
                "endpoint": "/api/demo/interleave/a|b",
            },
        }

    return app


app = create_app()
