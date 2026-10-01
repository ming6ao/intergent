"""Intergent — local coordination for parallel coding agents.

This package implements the **local plane** described in ``docs/local-plane.md``
together with the campaign orchestration in ``docs/orchestration.md``:
worktree-per-unit isolation, plan-time directory ownership, verification pinned
to content fingerprints, DAG wave planning, and agent-callable integration onto a
campaign feature branch.

The ``docs/operations.md`` production stack (Rust/Go) is a future port; this tree
is a dependency-free Python reference implementation so the behaviour can be
exercised end to end.  The service layer (``intergent.service``) is the single
owner of state and is called by every adapter (CLI, pi), matching the "one
engine, many adapters" rule in ``docs/architecture.md``.
"""

__version__ = "0.3.0"
