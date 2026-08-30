---
name: Single-worker group queue atomicity
description: Queue transition rule for grouped orders assigned to one worker.
---

When a grouped order is assigned in single-worker mode, treat every not-yet-started
car as one queue unit for offer failure transitions. If the active offer is
rejected or expires, release the worker binding from all pending siblings and
return the whole pending group to the global queue together.

**Why:** Releasing only the offered car leaves its siblings attached to the old
worker, silently breaks the director's “one worker for the group” choice, and
changes FIFO order.

**How to apply:** Any rejection, timeout, or future offer-failure path that
clears the lead car must atomically clear pending sibling assignments before the
next queue offer is selected.