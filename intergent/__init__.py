"""Intergent — local coordination for parallel coding agents.

This package implements the **local plane** described in ``docs/local-plane.md``:
worktree-per-unit isolation, declared intent, hierarchical scope leases, local
verification pinned to content fingerprints, combined-tree simulation, and
approval-gated landing onto the local main branch.

The design's reference stack is Rust (see ``docs/operations.md``).  This tree is
a dependency-free Python reference implementation of the local plane so the
behaviour can be exercised end to end before the systems implementation lands.
The service layer (``intergent.service``) is the single owner of state and is
called by every adapter (CLI, MCP), matching the "one engine, many adapters"
rule in ``docs/architecture.md``.
"""

__version__ = "0.1.0"
