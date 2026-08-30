---
name: Worker status reconciliation
description: Consistency rule for worker busy state after order deletion or external cleanup.
---

A worker marked `band` must be treated as busy only while a live nonterminal order is assigned to that worker. If no such order exists, reconcile the worker to an available state before handling shift start.

**Why:** Removing an order outside the normal completion flow can leave the persisted worker flag behind and block the worker from starting a new shift.

**How to apply:** Any administrative order cleanup should also reconcile assigned worker state; the shift-start path should defensively perform the same live-order check.