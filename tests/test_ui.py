"""Render check: the page must reference every element the script binds.

Guards against a template/JS mismatch that would break the UI in a browser
(there is no headless browser in CI, so ids are verified statically).
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app.main import create_app
from app.store import BomStore


def _ids(source: str) -> set[str]:
    return set(re.findall(r'id="([^"]+)"', source))


def _check_ids(html: str, script: str) -> None:
    html_ids = _ids(html)
    referenced = set(re.findall(r'$\("([^"]+)"\)', script))
    missing = referenced - html_ids
    assert not missing, f"script references ids absent from the template: {sorted(missing)}"


def test_template_ids_cover_all_script_references(tmp_path):
    store = BomStore(str(tmp_path / "ui.db"))
    app = create_app(store, instance="ui", demo_mode=True, seed_demo=False)
    with TestClient(app) as client:
        html = client.get("/").text
        script = client.get("/static/app.js").text
    _check_ids(html, script)


def test_trace_template_ids_cover_trace_script(tmp_path):
    store = BomStore(str(tmp_path / "trace-ui.db"))
    app = create_app(store, instance="ui", demo_mode=False, seed_demo=False)
    with TestClient(app) as client:
        html = client.get("/trace").text
        script = client.get("/static/trace.js").text
        index = client.get("/").text
    _check_ids(html, script)
    # The main page links to the new trace page.
    assert "/trace" in index


def test_sample_data_renders_correct_usage(tmp_path):
    """The bundled sample numbers in the README/UI match real expansion."""
    from app.demo import DEMO_EDGES, PROMOTE_LEAF_OPS
    from app.store import BatchOp

    store = BomStore(str(tmp_path / "sample.db"))
    store.commit_batch([BatchOp("upsert", upserts=(edge,)) for edge in DEMO_EDGES])
    usage = {(r["top_part"], r["leaf_part"]): r["qty"] for r in store.snapshot()["leaf_usage"]}
    assert usage[("FG-1", "L-3")] == 10
    assert usage[("FG-2", "L-3")] == 15
    assert usage[("SA-1", "L-3")] == 5
    assert usage[("FG-1", "L-4")] == 4
    assert usage[("FG-3", "L-5")] == 14

    store.commit_batch(PROMOTE_LEAF_OPS)
    usage = {(r["top_part"], r["leaf_part"]): r["qty"] for r in store.snapshot()["leaf_usage"]}
    assert ("FG-1", "L-4") not in usage
    assert usage[("FG-1", "L-6")] == 12
    assert usage[("FG-1", "L-7")] == 4
    assert usage[("FG-2", "L-6")] == 18
    assert usage[("L-4", "L-6")] == 3
    assert usage[("SA-1", "L-6")] == 6
