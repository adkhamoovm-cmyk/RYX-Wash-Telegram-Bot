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

The initial vehicle catalog is seeded from `ryx_wash_bot/catalog.py` into the
`service_models` table. After that, the director manages active models and
prices from Telegram; existing orders keep their original price snapshot.

## Price management

- **Narxlarni boshqarish** shows the active category/model price list.
- The director can add a model with category, name, and price.
- Existing prices can be changed without altering old orders.
- Deleting a model removes it from new customer/manual order choices while
  preserving past order data.

## Expenses and financial reports

- **Xarajat qo'shish** stores an amount, description, director ID, and Tashkent
  timestamp in the `expenses` table.
- **Hisobot** supports today, current week, current month, 3 months, 6 months,
  and an inclusive manually entered date range.
- Reports can include all workers or one worker. They show total income,
  expenses, net profit, cash/card income, completed car count, cancelled order
  count and reasons, plus the selected worker's percentage share.
- Expenses are business-wide; this is stated in single-worker reports.
- APScheduler sends the current day's report to the director every day at
  21:00 in `Asia/Tashkent`. The cron job uses the existing PostgreSQL job store.

## Worker flow

- The director uses **Ishchi qo'shish** and enters the worker's Telegram ID,
  name, phone number, and share percentage.
- A worker starts or ends a shift with **Ishga keldim** and **Ishdan ketdim**.
- A new order can be sent only to a worker whose status is `bo'sh`.
- The worker accepts or rejects the order. The customer's phone is hidden until
  the worker reaches the customer's location.
- Completion requires sequential **Oldin** and **Keyin** photos and a short
  comment. The final report time uses the `Asia/Tashkent` timezone.

## Offer timeout and cancellation

- Worker offers expire after three minutes. APScheduler stores timeout jobs in
  PostgreSQL, so pending timeouts survive bot restarts.
- An expired offer cannot be accepted later; the worker becomes available
  again and the director can resend the original order.
- Directors and the worker assigned to an order can cancel it. A predefined or
  custom non-empty reason is stored in the `cancellations` table.

## Order queue

- When no worker is available, the director can leave an order in the global
  queue or attach it to a busy worker as that worker's next order.
- A busy worker sees only the number of queued orders, not their details.
- Worker-specific queued orders are opened in FIFO order after the current job
  finishes. Each opened offer gets its own persistent three-minute timeout.
- If a newly available worker has no preassigned order, the director receives a
  yes/no prompt for the oldest global queued order.
- Rejected or expired queued offers return to the global queue and can be
  reassigned through the original **Ishchilarga yuborish** button.

## Director-created orders

- The director can create an order for a customer who called by phone.
- The flow requires customer name, a normalized `+998` phone number, catalog
  category/model, plate number, one reference car photo, payment method, and
  either Telegram coordinates or a written address.
- Existing customers are reused by normalized phone number. Customers without
  Telegram receive an internal negative identifier and are never sent Telegram
  messages.
- The reference car photo is stored separately from the worker's before/after
  completion photos and is shown to the worker after accepting the order.
- Created orders reuse the existing worker assignment and queue controls.

## Saved cars and grouped orders

- Every customer can keep multiple vehicles in `customer_cars`, including the
  catalog model, plate number, and optional color.
- Customer and director order flows can reuse a saved vehicle or add a new one.
  Large saved-car collections are paginated in Telegram.
- A single request can contain any number of cars. Payment, location/address,
  and comment are collected once and copied to each per-car order row.
- Multi-car requests share an `order_group_id`. Each car remains an independent
  order for status, revenue, worker share, cancellation, photos, and reports.
- The director can assign the whole group to one worker (the existing FIFO queue
  opens cars sequentially) or split cars across workers with the normal
  assignment controls.
- **Buyurtmalar tarixi** groups a customer's cars by date and group. Director
  **Statistika** counts every completed car and its worker share separately.