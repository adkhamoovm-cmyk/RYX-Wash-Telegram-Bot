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
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

- `ryx_wash_bot/handlers.py` — registration and order FSM flows
- `ryx_wash_bot/models.py` — SQLAlchemy `users` and `orders` models
- `ryx_wash_bot/catalog.py` — editable car categories, models, and prices
- `RYX_WASH_README.md` — bot setup and operation

## Architecture decisions

- New Telegram users are customers by default; the configured director is seeded as `direktor`.
- Customer phone and service location are accepted only through Telegram request buttons.
- Orders snapshot the selected car model and price so later catalog edits do not alter old orders.

## Product

Customers register with their name and Telegram contact, select a car and payment method, share a service location, and create an order. The director receives order details and a Telegram location.

## User preferences

- Do not add worker or director management features until requested separately.

## Gotchas

- Hosted PostgreSQL URLs may use `sslmode`; `Settings.async_database_url` normalizes this for asyncpg.

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
