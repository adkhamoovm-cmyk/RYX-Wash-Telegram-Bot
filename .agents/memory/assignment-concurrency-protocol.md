---
name: Assignment concurrency protocol
description: Database concurrency rules for order assignment and grouped-order transitions.
---

Direct order assignment must use a conditional database update so only one
callback can claim an unassigned order. Any flow touching a grouped order must
lock the complete group in ascending order ID before locking a worker.

**Why:** Row locks alone prevent duplicate assignment only when every competing
flow uses a consistent acquisition order. Locking an arbitrary group member or
mixing creation-time and ID ordering can deadlock against timeout processing.

**How to apply:** Keep direct claims and worker status changes in one
transaction. For group assignment, activation, rejection, timeout, or future
group mutations, lock all group rows by ID first and the worker row second.
Schedule notifications and timeout jobs only after the winning transaction
commits.