# RYX Wash

Telegram bot that registers car wash customers and sends new service orders to the director.

## Run & Operate

- `pnpm --filter @workspace/api-server run dev` — run the API server (port 5000)
- `python -m ryx_wash_bot` — run the RYX Wash Telegram bot
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Required env: `BOT_TOKEN`, `DATABASE_URL`, `DIRECTOR_ID`

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: Express 5
- DB: PostgreSQL + Drizzle ORM
- Bot: Python 3.11, aiogram 3.x, SQLAlchemy 2 async ORM, asyncpg
- Scheduling: APScheduler with a PostgreSQL SQLAlchemy job store
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

- `ryx_wash_bot/handlers.py` — registration and order FSM flows
- `ryx_wash_bot/models.py` — SQLAlchemy bot, catalog, and expense models
- `ryx_wash_bot/catalog.py` — initial catalog plus the runtime catalog cache
- `ryx_wash_bot/reports.py` — financial report calculations and daily delivery
- `RYX_WASH_README.md` — bot setup and operation

## Architecture decisions

- New Telegram users are customers by default; the configured director is seeded as `direktor`.
- Customer phone and service location are accepted only through Telegram request buttons.
- Orders snapshot the selected car model and price so later catalog edits do not alter old orders.
- Active catalog models and prices persist in PostgreSQL; the director manages them from Telegram.
- Financial reports count completed orders by `completed_at`, expenses by `spent_at`, and cancellations by `cancelled_at` in `Asia/Tashkent`.

## Product

Customers register with their name and Telegram contact, select a car and payment method, share a service location, and create an order. Customer orders go to all registered operators first; operators can assign them to workers. Any car not assigned to a worker after 30 minutes is forwarded to the director with its details and location. With no operators, it goes straight to the director.
The director can register workers and assign orders to available staff. Operators may create manual orders without director approval and assign their own manual orders to workers; the director gets an informational copy without assignment controls. Director-created manual orders retain director assignment controls. Skipping an order-share override snapshots the worker's registered percentage. Workers manage shifts, accept or reject assignments, report progress, and finish with one "before" photo and an optional comment; plate and payment are not required during completion. The final order summary and before photo go to the director and all registered operators.
Worker offers expire after three minutes even across bot restarts. Directors and assigned workers can cancel active orders with a required reason.
When everyone is busy, orders can enter a global FIFO queue or a worker-specific queue. A worker never receives a second active order; queued details open only after the current order ends.
The director can create phone-call orders manually. These require a plate and reference car photo, accept either Telegram coordinates or a written address, and reuse customers by normalized phone number.
Customers can save multiple vehicles and create grouped requests. Each car is a separate order row linked by `order_group_id`; a whole group can be queued for one worker or split across workers.
The director can add expenses, run date/worker-filtered financial reports, and manage catalog prices. Workers can add their own expenses and additional income; additional income uses their registered percentage. Operators can create manual orders through all car-selection steps. A persistent APScheduler cron sends the daily report at 21:00 Tashkent time.

## Gotchas

- Hosted PostgreSQL URLs may use `sslmode`; `Settings.async_database_url` normalizes this for asyncpg.
- APScheduler uses the synchronous psycopg URL while bot ORM queries use asyncpg.

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
