# RYX Wash Telegram bot

Minimal customer order flow built with Python, aiogram 3.x, SQLAlchemy ORM and
PostgreSQL.

## Environment

The bot reads these values from the environment:

- `BOT_TOKEN` — Telegram bot token, stored as a Replit Secret.
- `DATABASE_URL` — PostgreSQL connection URL provided by Replit.
- `DIRECTOR_ID` — Telegram ID of the director.

## Run

```bash
python -m ryx_wash_bot
```

The first start creates the `users` and `orders` tables. The director record is
created with `rol="direktor"` if it does not already exist. All other new
Telegram users are registered as `rol="mijoz"` and must complete registration
before creating an order.

The initial vehicle catalog and prices are in `ryx_wash_bot/catalog.py`.