---
name: Persistent worker-offer timeouts
description: Reliability rule for worker assignment offer expirations.
---

Worker assignment offer timeouts must use APScheduler's PostgreSQL job store,
not in-memory timers or `asyncio.sleep`.

**Why:** A bot restart must not lose pending three-minute expirations or leave
workers permanently marked busy.

**How to apply:** Schedule one date job per assigned order, remove it when the
offer is accepted, rejected, or cancelled, and keep timeout handling idempotent
by checking the current persisted order status before changing anything.