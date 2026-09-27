---
name: Operator review escalation
description: Timing and completion semantics for customer orders routed to operators.
---

A customer order is handled by operators first. Its 30-minute escalation to the director is satisfied only when a worker has actually been assigned; moving an order to a general queue without a worker is not approval. For multi-car orders, evaluate each car separately and escalate only the still-unassigned cars.

**Why:** The owner clarified that the operator approves by assigning a worker, not by pressing a separate acknowledgement button. Queued or partially assigned groups must not silently disappear from the director's view.

**How to apply:** Keep escalation jobs persistent across restarts and check current database state when they fire. If order assignment or queue semantics change, re-evaluate the escalation condition together with those changes.