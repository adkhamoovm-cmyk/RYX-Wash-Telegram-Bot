---
name: Asyncpg hosted PostgreSQL URLs
description: Compatibility rule for SQLAlchemy asyncpg connections to hosted PostgreSQL.
---

Normalize hosted PostgreSQL URL query parameters for the asyncpg dialect: translate
`sslmode` to `ssl` and omit unsupported channel-binding parameters.

**Why:** The runtime-managed PostgreSQL URL can be valid for libpq clients while
causing asyncpg to fail at startup with an unexpected keyword argument.

**How to apply:** Keep this normalization whenever the bot uses SQLAlchemy's
`postgresql+asyncpg` dialect with a platform-provided `DATABASE_URL`.