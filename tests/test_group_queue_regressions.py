"""Regression coverage for grouped orders, queue transitions, and Telegram limits."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ryx_wash_bot.config import Settings
from ryx_wash_bot.handlers import _new_router, send_group_summary
from ryx_wash_bot.keyboards import saved_cars_keyboard
from ryx_wash_bot.models import Base, CustomerCar, Order, User, Worker
from ryx_wash_bot.scheduler import configure_timeout_runtime, expire_worker_offer
from ryx_wash_bot import worker_handlers


DIRECTOR_ID = 9000
CUSTOMER_ID = 1001
WORKER_ONE_ID = 2001
WORKER_TWO_ID = 2002


class RecordingBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, object, dict]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs):
        if chat_id < 0:
            raise RuntimeError("offline customer cannot receive Telegram messages")
        self.calls.append(("send_message", chat_id, text, kwargs))
        return SimpleNamespace()

    async def send_location(self, chat_id: int, **kwargs):
        self.calls.append(("send_location", chat_id, kwargs, {}))

    async def send_photo(self, chat_id: int, photo: str, **kwargs):
        self.calls.append(("send_photo", chat_id, photo, kwargs))

    def messages(self) -> list[str]:
        return [
            call[2]
            for call in self.calls
            if call[0] == "send_message" and isinstance(call[2], str)
        ]


class RecordingMessage:
    def __init__(self, bot: RecordingBot, user_id: int, text: str = "") -> None:
        self.bot = bot
        self.from_user = SimpleNamespace(id=user_id)
        self.text = text
        self.answer_calls: list[tuple[object, dict]] = []
        self.edited_text: list[object] = []
        self.edited_markup: list[object] = []

    async def answer(self, text: object = "", **kwargs):
        self.answer_calls.append((text, kwargs))
        return self

    async def edit_text(self, text: object = "", **kwargs):
        self.edited_text.append(text)
        return self

    async def edit_reply_markup(self, **kwargs):
        self.edited_markup.append(kwargs.get("reply_markup"))
        return self


class RecordingCallback:
    def __init__(self, data: str, user_id: int, bot: RecordingBot) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.bot = bot
        self.message = RecordingMessage(bot, user_id)
        self.answers: list[tuple[object, dict]] = []

    async def answer(self, text: object = "", **kwargs):
        self.answers.append((text, kwargs))


class RecordingState:
    def __init__(self, data: dict | None = None) -> None:
        self.data = data or {}
        self.states: list[object] = []
        self.cleared = False

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def set_state(self, state):
        self.states.append(state)

    async def clear(self):
        self.cleared = True
        self.data.clear()


class RecordingScheduler:
    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}

    def add_job(self, callback, **kwargs):
        self.jobs[kwargs["id"]] = {"callback": callback, **kwargs}

    def remove_job(self, job_id: str):
        self.jobs.pop(job_id, None)


def run(coroutine):
    return asyncio.run(coroutine)


async def make_context():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(
        bot_token="test-token",
        database_url="sqlite+aiosqlite:///:memory:",
        director_id=DIRECTOR_ID,
    )
    scheduler = RecordingScheduler()
    router = _new_router(sessions, settings, scheduler)
    return engine, sessions, settings, scheduler, router


async def add_people(
    sessions: async_sessionmaker,
    *,
    customer_id: int = CUSTOMER_ID,
    worker_statuses: tuple[str, ...] = ("bo'sh",),
) -> None:
    async with sessions() as session:
        session.add(
            User(
                telegram_id=DIRECTOR_ID,
                name="Direktor",
                phone="+998900000000",
                rol="direktor",
            )
        )
        session.add(
            User(
                telegram_id=customer_id,
                name="Mijoz",
                phone="+998901234567",
                rol="mijoz",
            )
        )
        for index, status in enumerate(worker_statuses, 1):
            worker_id = WORKER_ONE_ID if index == 1 else WORKER_TWO_ID
            session.add(
                User(
                    telegram_id=worker_id,
                    name=f"Ishchi {index}",
                    phone=f"+9989012345{index:02d}",
                    rol="ishchi",
                )
            )
            session.add(
                Worker(
                    user_id=worker_id,
                    name=f"Ishchi {index}",
                    phone=f"+9989012345{index:02d}",
                    share_percent=Decimal("30"),
                    status=status,
                )
            )
        await session.commit()


async def add_group(
    sessions: async_sessionmaker,
    *,
    customer_id: int = CUSTOMER_ID,
    group_id: str = "group-regression",
    count: int = 2,
) -> list[int]:
    async with sessions() as session:
        orders = [
            Order(
                customer_id=customer_id,
                car_category="Sedan",
                car_model="Saved model" if index == 0 else "New model",
                car_price=Decimal("100000") + index * Decimal("25000"),
                plate_number=f"01A{index}BC",
                payment_method="Naqd",
                order_group_id=group_id,
                status="yangi",
                created_at=datetime(2026, 8, 30, 8, index + 1, tzinfo=timezone.utc),
            )
            for index in range(count)
        ]
        session.add_all(orders)
        await session.commit()
        return [order.id for order in orders]


def handler(router, observer: str, name: str):
    return next(
        item.callback
        for item in router.observers[observer].handlers
        if item.callback.__name__ == name
    )


async def load_orders(sessions, order_ids: list[int]) -> list[Order]:
    async with sessions() as session:
        return list(
            (
                await session.scalars(
                    select(Order).where(Order.id.in_(order_ids)).order_by(Order.id)
                )
            ).all()
        )


def test_mixed_saved_and_new_two_car_group_accepts_as_one_worker_unit():
    async def scenario():
        engine, sessions, _settings, scheduler, router = await make_context()
        try:
            await add_people(sessions)
            async with sessions() as session:
                session.add(
                    CustomerCar(
                        customer_id=CUSTOMER_ID,
                        car_category="Sedan",
                        model="Saved model",
                        plate_number="01ASAVED",
                        color="qora",
                    )
                )
                await session.commit()
            order_ids = await add_group(sessions)
            bot = RecordingBot()

            await handler(router, "callback_query", "assign_group_to_worker")(
                RecordingCallback(
                    f"group_worker:{order_ids[0]}:{WORKER_ONE_ID}",
                    DIRECTOR_ID,
                    bot,
                )
            )
            orders = await load_orders(sessions, order_ids)
            assert [(order.status, order.worker_id) for order in orders] == [
                ("ishchiga_yuborildi", WORKER_ONE_ID),
                ("navbatda", WORKER_ONE_ID),
            ]

            await handler(router, "callback_query", "accept_order")(
                RecordingCallback(
                    f"worker_accept:{order_ids[0]}", WORKER_ONE_ID, bot
                )
            )
            orders = await load_orders(sessions, order_ids)
            assert orders[0].status == "ishchi_qabul_qildi"
            assert orders[1].status == "navbatda"
            assert all(order.worker_id == WORKER_ONE_ID for order in orders)
            assert any(CUSTOMER_ID == call[1] for call in bot.calls)
            assert not scheduler.jobs
        finally:
            await engine.dispose()

    run(scenario())


def test_customer_location_submission_sends_group_summary_and_location():
    async def scenario():
        engine, sessions, _settings, _scheduler, router = await make_context()
        try:
            await add_people(sessions)
            bot = RecordingBot()
            state = RecordingState(
                {
                    "cars": [
                        {
                            "car_category": "Sedan",
                            "car_model": "Saved model",
                            "car_price": 100000,
                            "plate_number": "01ASAVED",
                            "color": "qora",
                        },
                        {
                            "car_category": "Sedan",
                            "car_model": "New model",
                            "car_price": 125000,
                            "plate_number": "01ANEW",
                            "color": "oq",
                        },
                    ],
                    "payment_method": "Naqd",
                    "latitude": 41.311081,
                    "longitude": 69.240562,
                }
            )
            message = RecordingMessage(bot, CUSTOMER_ID, "O'tkazib yuborish")

            await handler(router, "message", "receive_comment")(message, state)

            async with sessions() as session:
                orders = list(
                    (
                        await session.scalars(
                            select(Order)
                            .where(Order.customer_id == CUSTOMER_ID)
                            .order_by(Order.id)
                        )
                    ).all()
                )
            assert len(orders) == 2
            assert orders[0].order_group_id
            assert orders[0].order_group_id == orders[1].order_group_id
            assert any(
                call[0] == "send_message"
                and call[1] == DIRECTOR_ID
                and "Yangi guruh buyurtmasi" in str(call[2])
                for call in bot.calls
            )
            assert any(
                call[0] == "send_location" and call[1] == DIRECTOR_ID
                for call in bot.calls
            )
            assert state.cleared
            assert message.answer_calls
        finally:
            await engine.dispose()

    run(scenario())


def test_single_worker_reject_releases_every_group_sibling():
    async def scenario():
        engine, sessions, _settings, _scheduler, router = await make_context()
        try:
            await add_people(sessions)
            order_ids = await add_group(sessions)
            bot = RecordingBot()
            await handler(router, "callback_query", "assign_group_to_worker")(
                RecordingCallback(
                    f"group_worker:{order_ids[0]}:{WORKER_ONE_ID}",
                    DIRECTOR_ID,
                    bot,
                )
            )
            await handler(router, "callback_query", "reject_order")(
                RecordingCallback(
                    f"worker_reject:{order_ids[0]}", WORKER_ONE_ID, bot
                )
            )
            orders = await load_orders(sessions, order_ids)
            assert all(order.worker_id is None for order in orders)
            assert all(order.status in {"yangi", "navbatda"} for order in orders)
            async with sessions() as session:
                worker = await session.get(Worker, WORKER_ONE_ID)
                assert worker.status == "bo'sh"
        finally:
            await engine.dispose()

    run(scenario())


def test_single_worker_timeout_releases_pending_group_without_stale_binding():
    async def scenario():
        engine, sessions, settings, _scheduler, router = await make_context()
        try:
            await add_people(sessions)
            order_ids = await add_group(sessions)
            bot = RecordingBot()
            await handler(router, "callback_query", "assign_group_to_worker")(
                RecordingCallback(
                    f"group_worker:{order_ids[0]}:{WORKER_ONE_ID}",
                    DIRECTOR_ID,
                    bot,
                )
            )
            configure_timeout_runtime(sessions, bot, settings)
            # The timeout's queue hand-off is a separate concern; inspect the
            # persisted group immediately after its atomic release.
            worker_handlers._worker_available_handler = None
            await expire_worker_offer(order_ids[0])
            orders = await load_orders(sessions, order_ids)
            assert all(order.worker_id is None for order in orders)
            assert all(order.status == "navbatda" for order in orders)
            async with sessions() as session:
                worker = await session.get(Worker, WORKER_ONE_ID)
                assert worker.status == "bo'sh"
            assert any("muddati tugadi" in text for text in bot.messages())
        finally:
            await engine.dispose()

    run(scenario())


def test_single_worker_group_finishing_first_car_offers_next_car_in_order():
    async def scenario():
        engine, sessions, _settings, scheduler, router = await make_context()
        original_now = worker_handlers.now_tashkent
        worker_handlers.now_tashkent = lambda: datetime.now()
        try:
            await add_people(sessions)
            order_ids = await add_group(sessions)
            bot = RecordingBot()
            await handler(router, "callback_query", "assign_group_to_worker")(
                RecordingCallback(
                    f"group_worker:{order_ids[0]}:{WORKER_ONE_ID}",
                    DIRECTOR_ID,
                    bot,
                )
            )
            await handler(router, "callback_query", "accept_order")(
                RecordingCallback(
                    f"worker_accept:{order_ids[0]}", WORKER_ONE_ID, bot
                )
            )
            state = RecordingState()
            update_status = handler(router, "callback_query", "update_worker_status")
            for stage in ("route", "arrived", "washing"):
                await update_status(
                    RecordingCallback(
                        f"worker_status:{stage}:{order_ids[0]}",
                        WORKER_ONE_ID,
                        bot,
                    ),
                    state,
                )
            await update_status(
                RecordingCallback(
                    f"worker_status:complete:{order_ids[0]}",
                    WORKER_ONE_ID,
                    bot,
                ),
                state,
            )
            before = handler(router, "message", "receive_before_photo")
            after = handler(router, "message", "receive_after_photo")
            await before(
                SimpleNamespace(
                    photo=[SimpleNamespace(file_id="before-photo")],
                    answer=RecordingMessage(bot, WORKER_ONE_ID).answer,
                ),
                state,
            )
            await after(
                SimpleNamespace(
                    photo=[SimpleNamespace(file_id="after-photo")],
                    answer=RecordingMessage(bot, WORKER_ONE_ID).answer,
                ),
                state,
            )
            await handler(router, "message", "complete_order")(
                RecordingMessage(bot, WORKER_ONE_ID, "Yuvish tugadi"),
                state,
            )
            orders = await load_orders(sessions, order_ids)
            assert orders[0].status == "yakunlandi"
            assert orders[1].status == "ishchiga_yuborildi"
            assert orders[1].worker_id == WORKER_ONE_ID
            assert scheduler.jobs
            async with sessions() as session:
                worker = await session.get(Worker, WORKER_ONE_ID)
                assert worker.status == "band"
        finally:
            worker_handlers.now_tashkent = original_now
            await engine.dispose()

    run(scenario())


def test_split_group_assigns_each_car_to_a_different_worker_independently():
    async def scenario():
        engine, sessions, _settings, _scheduler, router = await make_context()
        try:
            await add_people(sessions, worker_statuses=("bo'sh", "bo'sh"))
            order_ids = await add_group(sessions)
            bot = RecordingBot()
            await handler(router, "callback_query", "split_group_orders")(
                RecordingCallback(
                    f"group_split:group-regression:{order_ids[0]}",
                    DIRECTOR_ID,
                    bot,
                )
            )
            assign = handler(router, "callback_query", "assign_order")
            await assign(
                RecordingCallback(
                    f"assign_worker:{order_ids[0]}:{WORKER_ONE_ID}",
                    DIRECTOR_ID,
                    bot,
                )
            )
            await assign(
                RecordingCallback(
                    f"assign_worker:{order_ids[1]}:{WORKER_TWO_ID}",
                    DIRECTOR_ID,
                    bot,
                )
            )
            orders = await load_orders(sessions, order_ids)
            assert [order.group_mode for order in orders] == ["split", "split"]
            assert [order.worker_id for order in orders] == [
                WORKER_ONE_ID,
                WORKER_TWO_ID,
            ]
            assert all(order.status == "ishchiga_yuborildi" for order in orders)
        finally:
            await engine.dispose()

    run(scenario())


def test_saved_car_keyboard_paginates_without_oversized_callback_or_page():
    cars = [
        SimpleNamespace(
            id=index,
            model=f"Model {index}",
            plate_number=f"01A{index:03d}BC",
        )
        for index in range(23)
    ]
    first = saved_cars_keyboard(cars, "customer", page=0)
    middle = saved_cars_keyboard(cars, "customer", page=1)
    last = saved_cars_keyboard(cars, "customer", page=2)

    assert len(first.inline_keyboard) == 10  # 8 cars, next, new
    assert len(middle.inline_keyboard) == 11  # 8 cars, previous, next, new
    assert len(last.inline_keyboard) == 9  # 7 cars, previous, new
    for markup in (first, middle, last):
        for row in markup.inline_keyboard:
            for button in row:
                assert len(button.callback_data or "") <= 64
                assert len(button.text) <= 60


def test_long_group_summary_and_statistics_split_every_telegram_message():
    async def scenario():
        engine, sessions, settings, _scheduler, router = await make_context()
        try:
            customer = User(
                telegram_id=CUSTOMER_ID,
                name="Mijoz " + "X" * 100,
                phone="+998901234567",
                rol="mijoz",
            )
            orders = [
                Order(
                    id=index + 1,
                    customer_id=CUSTOMER_ID,
                    car_category="Sedan",
                    car_model=f"Model-{index}-" + "M" * 40,
                    car_price=Decimal("100000"),
                    plate_number=f"01A{index:03d}BC",
                    payment_method="Naqd",
                    order_group_id="long-group",
                    status="yangi",
                )
                for index in range(180)
            ]
            bot = RecordingBot()
            await send_group_summary(
                bot, settings, "Uzun guruh", customer, orders, "long-group"
            )
            assert len(bot.messages()) > 1
            assert all(len(text) <= 4096 for text in bot.messages())

            await add_people(sessions)
            async with sessions() as session:
                for worker_index in range(70):
                    worker_id = 3000 + worker_index
                    session.add(
                        User(
                            telegram_id=worker_id,
                            name=f"Uzun statistik ishchi {worker_index}",
                            phone=f"+9989112{worker_index:04d}",
                            rol="ishchi",
                        )
                    )
                    session.add(
                        Worker(
                            user_id=worker_id,
                            name=f"Uzun statistik ishchi {worker_index}",
                            phone=f"+9989112{worker_index:04d}",
                            share_percent=Decimal("30"),
                            status="bo'sh",
                        )
                    )
                completed = [
                    Order(
                        customer_id=CUSTOMER_ID,
                        worker_id=3000 + (index % 70),
                        car_category="Sedan",
                        car_model=f"Completed-{index}-" + "C" * 35,
                        car_price=Decimal("100000"),
                        plate_number=f"10B{index:03d}CD",
                        payment_method="Naqd",
                        status="yakunlandi",
                        completed_at=datetime(
                            2026, 8, 30, 9, 0, tzinfo=timezone.utc
                        )
                        + timedelta(minutes=index),
                    )
                    for index in range(180)
                ]
                session.add_all(completed)
                await session.commit()
            message = RecordingMessage(bot, DIRECTOR_ID, "Statistika")
            await handler(router, "message", "director_statistics")(message)
            assert len(message.answer_calls) > 1
            assert all(len(str(text)) <= 4096 for text, _kwargs in message.answer_calls)
        finally:
            await engine.dispose()

    run(scenario())


def test_offline_customer_does_not_stop_worker_acceptance_flow():
    async def scenario():
        engine, sessions, _settings, _scheduler, router = await make_context()
        try:
            await add_people(sessions, customer_id=-1)
            order_ids = await add_group(
                sessions, customer_id=-1, group_id="offline-group", count=1
            )
            bot = RecordingBot()
            await handler(router, "callback_query", "assign_order")(
                RecordingCallback(
                    f"assign_worker:{order_ids[0]}:{WORKER_ONE_ID}",
                    DIRECTOR_ID,
                    bot,
                )
            )
            await handler(router, "callback_query", "accept_order")(
                RecordingCallback(
                    f"worker_accept:{order_ids[0]}", WORKER_ONE_ID, bot
                )
            )
            orders = await load_orders(sessions, order_ids)
            assert orders[0].status == "ishchi_qabul_qildi"
            assert orders[0].worker_id == WORKER_ONE_ID
            assert all(call[1] != -1 for call in bot.calls)
        finally:
            await engine.dispose()

    run(scenario())