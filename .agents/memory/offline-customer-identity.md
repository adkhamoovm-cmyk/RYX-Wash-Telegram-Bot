---
name: Offline customer identity
description: Identity and notification rules for director-created phone-call customers.
---

Customers created manually by the director without a Telegram account use
negative internal IDs in the existing user/customer key space. Match and reuse
them by normalized phone number.

**Why:** Orders require a customer foreign key, while Telegram user IDs are not
available for phone-call customers. Sending messages to an internal negative ID
would fail the worker flow.

**How to apply:** Treat positive IDs as Telegram-reachable and negative IDs as
database-only customers. Any new customer notification path must skip Telegram
API calls for database-only IDs.