---
name: Worker ETA recovery
description: Durable handling for worker arrival-time input when the bot process loses in-memory FSM state.
---

Worker ETA entry must have a database-backed recovery path in addition to the normal FSM handler. If an active worker order is in `yo'lda` with no ETA, parse the worker's message and continue the ETA flow even when the FSM state is missing.

**Why:** The bot uses in-memory FSM storage, so a restart or state mismatch can otherwise make a worker's numeric ETA message unhandled.

**How to apply:** Keep the recovery handler after all state-specific text handlers so it cannot consume plate, payment, or completion messages. Accept plain minutes and common Uzbek minute suffixes, while enforcing the 1–1440 minute limit.