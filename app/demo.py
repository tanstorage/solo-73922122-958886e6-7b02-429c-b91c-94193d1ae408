"""Sample data: shared sub-assembly, leaf promoted to assembly, interleaving edits."""

from __future__ import annotations

from .store import BatchOp, BomStore, Edge

# A shared sub-assembly (SA-1) is used by two different top assemblies;
# leaf L-4 is promoted to an assembly through a later batch (see below).
DEMO_EDGES = [
    Edge("FG-1", "SA-1", 2),
    Edge("FG-1", "L-1", 1),
    Edge("FG-2", "SA-1", 3),
    Edge("FG-2", "L-2", 4),
    Edge("SA-1", "L-3", 5),
    Edge("SA-1", "L-4", 2),
    Edge("SA-2", "L-5", 7),
    Edge("FG-3", "SA-2", 2),
]

# Batch 1: L-4 stops being a leaf - it becomes an assembly of L-6/L-7, so
# FG-1's L-4 rows disappear and L-6/L-7 appear instead (all in one commit).
PROMOTE_LEAF_OPS = [
    BatchOp("upsert", upserts=(Edge("L-4", "L-6", 3),)),
    BatchOp("upsert", upserts=(Edge("L-4", "L-7", 1),)),
]

# Interleaving pair that is individually acyclic but jointly cyclic:
# A: FG-9 -> NEW-1, B: NEW-1 -> FG-9.
INTERLEAVE_A = [
    BatchOp("upsert", upserts=(Edge("FG-9", "NEW-1", 1),)),
]
INTERLEAVE_B = [
    BatchOp("upsert", upserts=(Edge("NEW-1", "FG-9", 1),)),
]


def seed_demo(store: BomStore) -> bool:
    """Load the demo structure only if the warehouse has never been populated."""
    return store.seed_if_empty(DEMO_EDGES)
