"""Regression coverage for grouped orders, queue transitions, and Telegram limits."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ryx_wash_bot.config import Settings
from ryx_wash_bot.handlers import (
    MainMenuStateResetMiddleware,
    _parse_manual_visit_time,
    _new_router,
    send_group_summary,
)
from ryx_wash_bot.keyboards import saved_cars_keyboard
from ryx_wash_bot.models import (
    Base,
    Cancellation,
    CustomerCar,
    Expense,
    Order,
    User,
    Worker,
    WorkerAdditionalIncome,
)
from ryx_wash_bot.main import reconcile_staff_roles
from ryx_wash_bot.scheduler import (
    configure_wash_timer_runtime,
    expire_wash_timeout,
)
from ryx_wash_bot.reports import (
    build_customer_history_report,
    build_financial_report,
    period_bounds,
)
from ryx_wash_bot.states import (
    ManualOrderStates,
    WorkerCompletionStates,
    WorkerFinanceStates,
    WorkerOrderStates,
)
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
        self.add_job_calls: list[dict] = []

    def add_job(self, callback, **kwargs):
        self.add_job_calls.append({"callback": callback, **kwargs})
        self.jobs[kwargs["id"]] = {"callback": callback, **kwargs}

    def remove_job(self, job_id: str):
        self.jobs.pop(job_id, None)


def run(coroutine):
    return asyncio.run(coroutine)


async def make_context(database_url: str = "sqlite+aiosqlite:///:memory:"):
    engine = create_async_engine(database_url)
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


async def assign_group_share(
    router,
    bot,
    lead_order_id: int,
    worker_id: int,
    *,
    share_type: str = "none",
    value: str | None = None,
) -> None:
    await handler(router, "callback_query", "choose_group_wash_duration")(
        RecordingCallback(
            f"group_worker:{lead_order_id}:{worker_id}",
            DIRECTOR_ID,
            bot,
        )
    )
    state = RecordingState()
    await handler(router, "callback_query", "choose_order_share")(
        RecordingCallback(
            f"group_share:{share_type}:{lead_order_id}:{worker_id}",
            DIRECTOR_ID,
            bot,
        ),
        state,
    )
    if value is not None:
        await handler(router, "message", "receive_order_share_value")(
            RecordingMessage(bot, DIRECTOR_ID, value),
            state,
        )


async def assign_direct_share(
    router,
    bot,
    order_id: int,
    worker_id: int,
    *,
    share_type: str = "none",
    value: str | None = None,
) -> None:
    await handler(router, "callback_query", "choose_wash_duration")(
        RecordingCallback(
            f"assign_worker:{order_id}:{worker_id}",
            DIRECTOR_ID,
            bot,
        )
    )
    state = RecordingState()
    await handler(router, "callback_query", "choose_order_share")(
        RecordingCallback(
            f"order_share:{share_type}:{order_id}:{worker_id}",
            DIRECTOR_ID,
            bot,
        ),
        state,
    )
    if value is not None:
        await handler(router, "message", "receive_order_share_value")(
            RecordingMessage(bot, DIRECTOR_ID, value),
            state,
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
            async with sessions() as session:
                for order_id in order_ids:
                    order = await session.get(Order, order_id)
                    assert order is not None
                    order.visit_at = datetime(
                        2026,
                        8,
                        30,
                        17,
                        0,
                        tzinfo=timezone(timedelta(hours=5)),
                    )
                await session.commit()
            bot = RecordingBot()

            await assign_group_share(
                router,
                bot,
                order_ids[0],
                WORKER_ONE_ID,
                share_type="percent",
                value="30",
            )
            orders = await load_orders(sessions, order_ids)
            assert [(order.status, order.worker_id) for order in orders] == [
                ("ishchiga_yuborildi", WORKER_ONE_ID),
                ("navbatda", WORKER_ONE_ID),
            ]
            assert [order.worker_share_type for order in orders] == [
                "percent",
                "percent",
            ]
            assert [order.worker_share_amount for order in orders] == [
                Decimal("30000.00"),
                Decimal("37500.00"),
            ]
            assert not scheduler.jobs
            assert not any("daqiqa" in text for text in bot.messages())

            accept_callback = RecordingCallback(
                f"worker_accept:{order_ids[0]}", WORKER_ONE_ID, bot
            )
            await handler(router, "callback_query", "accept_order")(
                accept_callback
            )
            orders = await load_orders(sessions, order_ids)
            assert orders[0].status == "ishchi_qabul_qildi"
            assert orders[1].status == "navbatda"
            assert all(order.worker_id == WORKER_ONE_ID for order in orders)
            assert any(CUSTOMER_ID == call[1] for call in bot.calls)
            assert "30.08.2026 17:00" in str(accept_callback.message.edited_text)
            assert not scheduler.jobs
        finally:
            await engine.dispose()

    run(scenario())


def test_start_routes_existing_customer_worker_and_director_to_their_menus():
    async def scenario():
        engine, sessions, settings, scheduler, router = await make_context()
        try:
            async with sessions() as session:
                session.add_all(
                    [
                        User(
                            telegram_id=CUSTOMER_ID,
                            name="Mijoz",
                            phone="+998901234567",
                            rol="mijoz",
                        ),
                        # Deliberately keep the users role stale: the workers
                        # table must take precedence for /start.
                        User(
                            telegram_id=WORKER_ONE_ID,
                            name="Ishchi",
                            phone="+998901234568",
                            rol="mijoz",
                        ),
                        User(
                            telegram_id=DIRECTOR_ID,
                            name="Direktor",
                            phone="+998900000000",
                            rol="direktor",
                        ),
                        Worker(
                            user_id=WORKER_ONE_ID,
                            name="Ishchi",
                            phone="+998901234568",
                            share_percent=Decimal("30"),
                            status="smenada_emas",
                        ),
                    ]
                )
                await session.commit()

            start = handler(router, "message", "start")
            bot = RecordingBot()

            customer_message = RecordingMessage(bot, CUSTOMER_ID, "/start")
            customer_state = RecordingState()
            await start(customer_message, customer_state)
            assert customer_message.answer_calls[0][0] == (
                "👋 RYX Wash xizmatiga xush kelibsiz."
            )
            assert customer_state.cleared is True

            worker_message = RecordingMessage(bot, WORKER_ONE_ID, "/start")
            worker_state = RecordingState()
            await start(worker_message, worker_state)
            assert worker_message.answer_calls[0][0] == "👷 Ishchi paneli."
            assert worker_state.cleared is True

            director_message = RecordingMessage(bot, DIRECTOR_ID, "/start")
            director_state = RecordingState()
            await start(director_message, director_state)
            assert director_message.answer_calls[0][0] == "👔 Direktor paneli."
            assert director_state.cleared is True

            async with sessions() as session:
                worker_user = await session.get(User, WORKER_ONE_ID)
                assert worker_user is not None
                assert worker_user.rol == "ishchi"
        finally:
            await engine.dispose()

    run(scenario())


def test_customer_order_history_shows_friendly_status():
    async def scenario():
        engine, sessions, _settings, scheduler, router = await make_context()
        try:
            async with sessions() as session:
                session.add(
                    User(
                        telegram_id=CUSTOMER_ID,
                        name="Mijoz",
                        phone="+998901234567",
                        rol="mijoz",
                    )
                )
                session.add(
                    Order(
                        customer_id=CUSTOMER_ID,
                        car_category="Sedan",
                        car_model="Test model",
                        car_price=Decimal("100000"),
                        plate_number="01A123BC",
                        payment_method="Naqd",
                        status="ishchiga_yuborildi",
                        created_at=datetime(
                            2026, 8, 30, 8, 0, tzinfo=timezone.utc
                        ),
                    )
                )
                await session.commit()

            message = RecordingMessage(
                RecordingBot(),
                CUSTOMER_ID,
                "📋 Buyurtmalar tarixi",
            )
            await handler(router, "message", "customer_order_history")(message)
            text = str(message.answer_calls[0][0])
            assert "📤 Ishchiga yuborildi" in text
            assert "🚗 Test model" in text
            assert "💰" in text
        finally:
            await engine.dispose()

    run(scenario())


def test_director_manual_order_skips_color_and_photo():
    async def scenario():
        engine, sessions, _settings, scheduler, router = await make_context()
        try:
            await add_people(sessions)
            state = RecordingState(
                {
                    "car_category": "Sedan",
                    "car_model": "Test model",
                    "car_price": 100000,
                    "plate_number": "01A123BC",
                }
            )
            bot = RecordingBot()
            callback = RecordingCallback(
                "manual_model:sedan-cobalt",
                DIRECTOR_ID,
                bot,
            )
            await handler(router, "callback_query", "manual_car_model")(
                callback, state
            )

            data = await state.get_data()
            assert data["cars"][0]["car_photo_id"] is None
            assert data["cars"][0]["car_model"] == "Chevrolet Cobalt"
            assert data["cars"][0]["color"] is None
            assert callback.message.answer_calls
            assert "Yana mashina qo'shasizmi?" in str(
                callback.message.answer_calls[0][0]
            )
            assert "rang" not in str(callback.message.edited_text[0]).lower()
            assert "rasm" not in str(callback.message.answer_calls[0][0]).lower()
        finally:
            await engine.dispose()

    run(scenario())


def test_manual_visit_time_requires_a_future_tashkent_time():
    tashkent = timezone(timedelta(hours=5))
    now = datetime(2026, 8, 30, 13, 15, tzinfo=tashkent)

    visit_at = _parse_manual_visit_time("17:00", now=now)
    assert visit_at == datetime(2026, 8, 30, 17, 0, tzinfo=tashkent)
    assert _parse_manual_visit_time("17.00", now=now) == visit_at
    assert _parse_manual_visit_time("13:15", now=now) is None
    assert _parse_manual_visit_time("25:00", now=now) is None
    assert _parse_manual_visit_time("17:60", now=now) is None


def test_manual_order_skips_payment_and_requests_visit_time_before_location():
    async def scenario():
        engine, sessions, settings, scheduler, router = await make_context()
        try:
            await add_people(sessions)
            bot = RecordingBot()
            state = RecordingState(
                {
                    "cars": [
                        {
                            "car_category": "Sedan",
                            "car_model": "Test",
                            "car_price": 100_000,
                            "plate_number": None,
                            "color": None,
                        }
                    ]
                }
            )

            finish_callback = RecordingCallback(
                "manual_finish_cars", DIRECTOR_ID, bot
            )
            await handler(router, "callback_query", "finish_manual_cars")(
                finish_callback, state
            )
            assert state.states[-1] == ManualOrderStates.waiting_visit_time
            assert "payment_method" not in state.data
            prompt = str(finish_callback.message.answer_calls[-1][0])
            assert "HH:MM" in prompt
            assert "To'lov" not in prompt
            assert "Naqd" not in prompt
        finally:
            await engine.dispose()

    run(scenario())


def test_malformed_assignment_and_payment_callbacks_are_rejected_safely():
    async def scenario():
        engine, _sessions, _settings, _scheduler, router = await make_context()
        try:
            bot = RecordingBot()
            assignment = RecordingCallback(
                "assign_worker_wash_duration:not-an-id:2:60",
                DIRECTOR_ID,
                bot,
            )
            await handler(router, "callback_query", "assign_order")(assignment)
            assert assignment.answers
            assert assignment.answers[-1][1].get("show_alert") is True

            state = RecordingState()
            payment = RecordingCallback("payment:bogus", CUSTOMER_ID, bot)
            await handler(router, "callback_query", "choose_payment")(
                payment, state
            )
            assert "payment_method" not in state.data
            assert payment.answers[-1][1].get("show_alert") is True
        finally:
            await engine.dispose()

    run(scenario())


def test_start_shift_recovers_stale_busy_status_without_active_order():
    async def scenario():
        engine, sessions, _settings, _scheduler, router = await make_context()
        try:
            await add_people(sessions)
            async with sessions() as session:
                worker = await session.get(Worker, WORKER_ONE_ID)
                worker.status = "band"
                await session.commit()

            bot = RecordingBot()
            message = RecordingMessage(bot, WORKER_ONE_ID, "🟢 Ishga keldim")
            await handler(router, "message", "start_shift")(message)

            assert "Smena boshlandi" in str(message.answer_calls[0][0])
            async with sessions() as session:
                worker = await session.get(Worker, WORKER_ONE_ID)
                assert worker.status == "bo'sh"
        finally:
            await engine.dispose()

    run(scenario())


def test_main_menu_text_clears_any_active_fsm_state_before_handler():
    async def scenario():
        state = RecordingState({"pending_input": "CRM search"})
        message = RecordingMessage(
            RecordingBot(),
            DIRECTOR_ID,
            "➕👷 Ishchi qo'shish",
        )
        called = False

        async def next_handler(event, data):
            nonlocal called
            called = True
            return event

        result = await MainMenuStateResetMiddleware()(
            next_handler,
            message,
            {"state": state},
        )

        assert result is message
        assert called is True
        assert state.cleared is True
        assert state.data == {}

    run(scenario())


def test_crm_search_state_then_worker_menu_starts_worker_registration():
    async def scenario():
        engine, sessions, _settings, _scheduler, router = await make_context()
        try:
            async with sessions() as session:
                session.add(
                    User(
                        telegram_id=DIRECTOR_ID,
                        name="Direktor",
                        phone="+998900000000",
                        rol="direktor",
                    )
                )
                await session.commit()

            state = RecordingState({"pending_input": "CRM search"})
            message = RecordingMessage(
                RecordingBot(),
                DIRECTOR_ID,
                "➕👷 Ishchi qo'shish",
            )
            start_worker_registration = handler(
                router, "message", "start_worker_registration"
            )

            async def next_handler(event, data):
                await start_worker_registration(event, data["state"])
                return event

            await MainMenuStateResetMiddleware()(
                next_handler,
                message,
                {"state": state},
            )

            assert state.cleared is True
            assert state.states
            assert "Mijoz" not in str(message.answer_calls[0][0])
            assert "Telegram ID" in str(message.answer_calls[0][0])
        finally:
            await engine.dispose()

    run(scenario())


def test_director_can_edit_an_expense_amount_and_description():
    async def scenario():
        engine, sessions, _settings, _scheduler, router = await make_context()
        try:
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
                    Expense(
                        amount=Decimal("100000"),
                        description="Eski tavsif",
                        spent_at=datetime(
                            2026, 8, 30, 8, 0, tzinfo=timezone.utc
                        ),
                        created_by=DIRECTOR_ID,
                    )
                )
                await session.commit()
                expense_id = await session.scalar(
                    select(Expense.id).order_by(Expense.id.desc())
                )

            bot = RecordingBot()
            state = RecordingState()
            edit_callback = RecordingCallback(
                f"expense_edit:{expense_id}",
                DIRECTOR_ID,
                bot,
            )
            await handler(router, "callback_query", "start_expense_edit")(
                edit_callback, state
            )

            await handler(router, "message", "receive_expense_edit_amount")(
                RecordingMessage(bot, DIRECTOR_ID, "250000"),
                state,
            )
            await handler(router, "message", "receive_expense_edit_description")(
                RecordingMessage(bot, DIRECTOR_ID, "Yangi tavsif"),
                state,
            )

            async with sessions() as session:
                expense = await session.get(Expense, expense_id)
                assert expense is not None
                assert expense.amount == Decimal("250000.00")
                assert expense.description == "Yangi tavsif"
            assert state.cleared is True
        finally:
            await engine.dispose()

    run(scenario())


def test_worker_enters_missing_manual_order_plate_after_washing():
    async def scenario():
        engine, sessions, settings, _scheduler, router = await make_context()
        try:
            async with sessions() as session:
                session.add_all(
                    [
                        User(
                            telegram_id=CUSTOMER_ID,
                            name="Mijoz",
                            phone="+998901234567",
                            rol="mijoz",
                        ),
                        User(
                            telegram_id=WORKER_ONE_ID,
                            name="Ishchi",
                            phone="+998901234568",
                            rol="ishchi",
                        ),
                        Worker(
                            user_id=WORKER_ONE_ID,
                            name="Ishchi",
                            phone="+998901234568",
                            share_percent=Decimal("30"),
                            status="band",
                        ),
                        Order(
                            customer_id=CUSTOMER_ID,
                            worker_id=WORKER_ONE_ID,
                            car_category="Sedan",
                            car_model="Chevrolet Cobalt",
                            car_price=Decimal("50000"),
                            plate_number=None,
                            payment_method=None,
                            status="yakunlanmoqda",
                        ),
                    ]
                )
                await session.commit()
                order_id = await session.scalar(
                    select(Order.id)
                    .where(Order.customer_id == CUSTOMER_ID)
                    .order_by(Order.id.desc())
                )

            bot = RecordingBot()
            state = RecordingState({"order_id": order_id})
            message = RecordingMessage(
                bot,
                WORKER_ONE_ID,
                "01 A 123 BC",
            )
            await handler(router, "message", "receive_order_plate")(
                message, state
            )

            async with sessions() as session:
                order = await session.get(Order, order_id)
                assert order is not None
                assert order.plate_number == "01 A 123 BC"
                assert order.payment_method is None
                assert order.status == "yakunlanmoqda"

            assert state.cleared is False
            assert any("to‘lov" in str(text) for text, _ in message.answer_calls)

            payment_callback = RecordingCallback(
                f"worker_payment:Karta:{order_id}",
                WORKER_ONE_ID,
                bot,
            )
            await handler(router, "callback_query", "receive_worker_payment")(
                payment_callback,
                state,
            )
            async with sessions() as session:
                order = await session.get(Order, order_id)
                assert order is not None
                assert order.payment_method == "Karta"
                assert order.status == "yakunlanmoqda"
            assert state.cleared is False
            assert any(
                "Oldin" in str(text)
                for text, _ in payment_callback.message.answer_calls
            )
            before = handler(router, "message", "receive_before_photo")
            after = handler(router, "message", "receive_after_photo")
            await before(
                SimpleNamespace(
                    from_user=SimpleNamespace(id=WORKER_ONE_ID),
                    photo=[SimpleNamespace(file_id="before-photo")],
                    answer=RecordingMessage(bot, WORKER_ONE_ID).answer,
                ),
                state,
            )
            await after(
                SimpleNamespace(
                    from_user=SimpleNamespace(id=WORKER_ONE_ID),
                    photo=[SimpleNamespace(file_id="after-photo")],
                    answer=RecordingMessage(bot, WORKER_ONE_ID).answer,
                ),
                state,
            )
            photo_data = await state.get_data()
            assert photo_data["before_photo_id"] == "before-photo"
            assert photo_data["after_photo_id"] == "after-photo"
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


def test_custom_cancellation_reason_is_saved_and_not_silent():
    async def scenario():
        engine, sessions, settings, scheduler, router = await make_context()
        try:
            await add_people(sessions)
            async with sessions() as session:
                order = Order(
                    customer_id=CUSTOMER_ID,
                    worker_id=WORKER_ONE_ID,
                    car_category="Sedan",
                    car_model="Cancellation model",
                    car_price=Decimal("100000"),
                    status="ishchi_qabul_qildi",
                )
                session.add(order)
                await session.commit()
                order_id = order.id
                worker = await session.get(Worker, WORKER_ONE_ID)
                worker.status = "band"
                await session.commit()
            bot = RecordingBot()
            state = RecordingState({"cancel_order_id": order_id})
            message = RecordingMessage(bot, WORKER_ONE_ID, "Mijoz boshqa vaqtga qoldirdi")

            await handler(router, "message", "custom_cancellation_reason")(
                message, state
            )

            async with sessions() as session:
                saved_order = await session.get(Order, order_id)
                cancellation = await session.scalar(
                    select(worker_handlers.Cancellation).where(
                        worker_handlers.Cancellation.order_id == order_id
                    )
                )
                worker = await session.get(Worker, WORKER_ONE_ID)
            assert saved_order.status == "bekor_qilindi"
            assert cancellation.reason == "Mijoz boshqa vaqtga qoldirdi"
            assert worker.status == "bo'sh"
            assert state.cleared
            assert message.answer_calls
            assert any(
                call[1] == settings.director_id
                and "Mijoz boshqa vaqtga qoldirdi" in str(call[2])
                for call in bot.calls
            )
        finally:
            await engine.dispose()

    run(scenario())


def test_director_can_safely_deactivate_worker_without_deleting_history():
    async def scenario():
        engine, sessions, _settings, _scheduler, router = await make_context()
        try:
            await add_people(sessions)
            async with sessions() as session:
                order = Order(
                    customer_id=CUSTOMER_ID,
                    worker_id=WORKER_ONE_ID,
                    car_category="Sedan",
                    car_model="Historical model",
                    car_price=Decimal("100000"),
                    status="yakunlandi",
                )
                session.add(order)
                await session.commit()
                order_id = order.id
            bot = RecordingBot()

            await handler(router, "message", "manage_workers")(
                RecordingMessage(bot, DIRECTOR_ID, "👷 Ishchilarni boshqarish")
            )
            await handler(router, "callback_query", "choose_worker_management_action")(
                RecordingCallback(
                    f"worker_manage:deactivate:{WORKER_ONE_ID}",
                    DIRECTOR_ID,
                    bot,
                )
            )
            await handler(router, "callback_query", "deactivate_worker")(
                RecordingCallback(
                    f"worker_deactivate_confirm:{WORKER_ONE_ID}",
                    DIRECTOR_ID,
                    bot,
                )
            )

            async with sessions() as session:
                worker = await session.get(Worker, WORKER_ONE_ID)
                saved_order = await session.get(Order, order_id)
            assert worker.active is False
            assert worker.status == "smenada_emas"
            assert saved_order is not None
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
            await assign_group_share(
                router,
                bot,
                order_ids[0],
                WORKER_ONE_ID,
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


def test_worker_lifecycle_is_arrival_then_completion_without_wash_timer():
    async def scenario():
        engine, sessions, _settings, scheduler, router = await make_context()
        try:
            await add_people(sessions)
            order_ids = await add_group(sessions)
            bot = RecordingBot()
            await assign_group_share(
                router,
                bot,
                order_ids[0],
                WORKER_ONE_ID,
            )
            await handler(router, "callback_query", "accept_order")(
                RecordingCallback(
                    f"worker_accept:{order_ids[0]}", WORKER_ONE_ID, bot
                )
            )
            state = RecordingState()
            update_status = handler(
                router, "callback_query", "update_worker_status"
            )
            await update_status(
                RecordingCallback(
                    f"worker_status:arrived:{order_ids[0]}",
                    WORKER_ONE_ID,
                    bot,
                ),
                state,
            )
            orders = await load_orders(sessions, order_ids)
            assert all(order.worker_id == WORKER_ONE_ID for order in orders)
            assert orders[0].arrived_at is not None
            assert orders[0].status == "yetib_keldi"
            assert not scheduler.jobs
            async with sessions() as session:
                worker = await session.get(Worker, WORKER_ONE_ID)
                assert worker.status == "band"
            legacy_callback = RecordingCallback(
                f"worker_status:washing:{order_ids[0]}",
                WORKER_ONE_ID,
                bot,
            )
            await update_status(legacy_callback, state)
            orders = await load_orders(sessions, order_ids)
            assert orders[0].status == "yuvish_boshlandi"
            assert not scheduler.jobs
        finally:
            await engine.dispose()

    run(scenario())


def test_arrival_eta_recovers_when_fsm_state_is_missing():
    async def scenario():
        engine, sessions, _settings, _scheduler, router = await make_context()
        try:
            await add_people(sessions)
            order_id = (await add_group(sessions, count=1))[0]
            async with sessions() as session:
                order = await session.get(Order, order_id)
                order.worker_id = WORKER_ONE_ID
                order.status = "yo'lda"
                order.route_started_at = datetime(
                    2026, 8, 30, 8, 30, tzinfo=timezone.utc
                )
                await session.commit()

            bot = RecordingBot()
            message = RecordingMessage(bot, WORKER_ONE_ID, "25 daqiqa")
            await handler(router, "message", "recover_arrival_eta")(
                message
            )

            order = (await load_orders(sessions, [order_id]))[0]
            assert order.arrival_eta_minutes == 25
            assert order.arrival_eta_at is not None
            assert any("ETA saqlandi" in str(text) for text, _ in message.answer_calls)
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
            await assign_group_share(
                router,
                bot,
                order_ids[0],
                WORKER_ONE_ID,
            )
            await handler(router, "callback_query", "accept_order")(
                RecordingCallback(
                    f"worker_accept:{order_ids[0]}", WORKER_ONE_ID, bot
                )
            )
            state = RecordingState()
            update_status = handler(router, "callback_query", "update_worker_status")
            await update_status(
                RecordingCallback(
                    f"worker_status:arrived:{order_ids[0]}",
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
                    from_user=SimpleNamespace(id=WORKER_ONE_ID),
                    photo=[SimpleNamespace(file_id="before-photo")],
                    answer=RecordingMessage(bot, WORKER_ONE_ID).answer,
                ),
                state,
            )
            await after(
                SimpleNamespace(
                    from_user=SimpleNamespace(id=WORKER_ONE_ID),
                    photo=[SimpleNamespace(file_id="after-photo")],
                    answer=RecordingMessage(bot, WORKER_ONE_ID).answer,
                ),
                state,
            )
            original_send_message = bot.send_message

            async def assert_state_cleared_before_queue_offer(
                chat_id: int, text: str, **kwargs
            ):
                if chat_id == WORKER_ONE_ID and "Yangi buyurtma" in text:
                    assert state.cleared is True
                return await original_send_message(chat_id, text, **kwargs)

            bot.send_message = assert_state_cleared_before_queue_offer
            await handler(router, "message", "complete_order")(
                RecordingMessage(bot, WORKER_ONE_ID, "Yuvish tugadi"),
                state,
            )
            orders = await load_orders(sessions, order_ids)
            assert orders[0].status == "yakunlandi"
            assert orders[0].arrived_at is not None
            assert orders[0].completed_at >= orders[0].arrived_at
            assert orders[1].status == "ishchiga_yuborildi"
            assert orders[1].worker_id == WORKER_ONE_ID
            assert not scheduler.jobs
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
            await assign_direct_share(
                router,
                bot,
                order_ids[0],
                WORKER_ONE_ID,
                share_type="percent",
                value="25",
            )
            await assign_direct_share(
                router,
                bot,
                order_ids[1],
                WORKER_TWO_ID,
                share_type="amount",
                value="15000",
            )
            orders = await load_orders(sessions, order_ids)
            assert [order.group_mode for order in orders] == ["split", "split"]
            assert [order.worker_id for order in orders] == [
                WORKER_ONE_ID,
                WORKER_TWO_ID,
            ]
            assert all(order.status == "ishchiga_yuborildi" for order in orders)
            assert [order.worker_share_amount for order in orders] == [
                Decimal("25000.00"),
                Decimal("15000.00"),
            ]
        finally:
            await engine.dispose()

    run(scenario())


def test_director_can_enter_custom_worker_share():
    async def scenario():
        engine, sessions, _settings, _scheduler, router = await make_context()
        try:
            await add_people(sessions)
            order_id = (await add_group(sessions, count=1))[0]
            bot = RecordingBot()
            await assign_direct_share(
                router,
                bot,
                order_id,
                WORKER_ONE_ID,
                share_type="percent",
                value="75",
            )
            orders = await load_orders(sessions, [order_id])
            assert orders[0].worker_share_type == "percent"
            assert orders[0].worker_share_value == Decimal("75.00")
            assert orders[0].worker_share_amount == Decimal("75000.00")
            assert orders[0].status == "ishchiga_yuborildi"
        finally:
            await engine.dispose()

    run(scenario())


def test_parallel_assign_callbacks_only_allow_one_worker_to_claim_order(tmp_path):
    async def scenario():
        database_path = tmp_path / "parallel-assignment.db"
        engine, sessions, _settings, scheduler, router = await make_context(
            f"sqlite+aiosqlite:///{database_path}"
        )
        try:
            await add_people(sessions, worker_statuses=("bo'sh", "bo'sh"))
            order_id = (
                await add_group(
                    sessions,
                    group_id="parallel-assignment",
                    count=1,
                )
            )[0]
            bot = RecordingBot()
            callbacks = [
                RecordingCallback(
                    f"assign_worker:{order_id}:{worker_id}",
                    DIRECTOR_ID,
                    bot,
                )
                for worker_id in (WORKER_ONE_ID, WORKER_TWO_ID)
            ]
            await asyncio.gather(
                *(
                    handler(router, "callback_query", "choose_wash_duration")(callback)
                    for callback in callbacks
                )
            )
            share_callbacks = [
                RecordingCallback(
                    f"order_share:none:{order_id}:{worker_id}",
                    DIRECTOR_ID,
                    bot,
                )
                for worker_id in (WORKER_ONE_ID, WORKER_TWO_ID)
            ]
            await asyncio.gather(
                *(
                    handler(router, "callback_query", "choose_order_share")(
                        callback, RecordingState()
                    )
                    for callback in share_callbacks
                )
            )

            async with sessions() as session:
                order = await session.get(Order, order_id)
                workers = list(
                    (
                        await session.scalars(
                            select(Worker)
                            .where(
                                Worker.user_id.in_(
                                    {WORKER_ONE_ID, WORKER_TWO_ID}
                                )
                            )
                            .order_by(Worker.user_id)
                        )
                    ).all()
                )

            assert order is not None
            assert order.status == "ishchiga_yuborildi"
            assert order.worker_id in {WORKER_ONE_ID, WORKER_TWO_ID}
            assert {
                worker.user_id: worker.status
                for worker in workers
            } == {
                order.worker_id: "band",
                (
                    WORKER_TWO_ID
                    if order.worker_id == WORKER_ONE_ID
                    else WORKER_ONE_ID
                ): "bo'sh",
            }
            assert sum(
                call[0] == "send_message"
                and call[1] in {WORKER_ONE_ID, WORKER_TWO_ID}
                for call in bot.calls
            ) == 1
            assert not scheduler.jobs
            assert not scheduler.add_job_calls
            assert sum(
                kwargs.get("show_alert") is True
                for callback in share_callbacks
                for _answer, kwargs in callback.answers
            ) == 1
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
            state = RecordingState()
            await handler(router, "message", "start_report")(message, state)
            await handler(router, "callback_query", "choose_report_period")(
                RecordingCallback("report_period:month", DIRECTOR_ID, bot),
                state,
            )
            report_callback = RecordingCallback(
                "report_worker:all", DIRECTOR_ID, bot
            )
            await handler(router, "callback_query", "generate_report")(
                report_callback,
                state,
            )
            assert len(report_callback.message.answer_calls) > 1
            assert all(
                len(str(text)) <= 4096
                for text, _kwargs in report_callback.message.answer_calls
            )
            assert all(
                len(str(text)) <= 4096
                for text, _kwargs in report_callback.message.answer_calls
            )
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
            await assign_direct_share(
                router,
                bot,
                order_ids[0],
                WORKER_ONE_ID,
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


def test_worker_cabinet_resumes_photo_step_after_menu_state_reset():
    async def scenario():
        engine, sessions, _settings, _scheduler, router = await make_context()
        try:
            await add_people(sessions, worker_statuses=("band",))
            async with sessions() as session:
                order = Order(
                    customer_id=CUSTOMER_ID,
                    worker_id=WORKER_ONE_ID,
                    car_category="Sedan",
                    car_model="Cobalt",
                    car_price=Decimal("50000"),
                    status="yakunlanmoqda",
                )
                session.add(order)
                await session.commit()
                order_id = order.id

            state = RecordingState({"stale": "photo"})
            message = RecordingMessage(
                RecordingBot(), WORKER_ONE_ID, "👤 Mening kabinetim"
            )
            cabinet = handler(router, "message", "show_worker_cabinet")

            async def next_handler(event, data):
                await cabinet(event, data["state"])

            await MainMenuStateResetMiddleware()(
                next_handler, message, {"state": state}
            )

            assert state.cleared is True
            assert state.data == {"order_id": order_id}
            assert state.states[-1] == WorkerCompletionStates.waiting_before_photo
            assert any(
                "Oldin" in str(text)
                for text, _ in message.answer_calls
            )
        finally:
            await engine.dispose()

    run(scenario())


def test_stale_payment_callback_cannot_mutate_another_order():
    async def scenario():
        engine, sessions, _settings, _scheduler, router = await make_context()
        try:
            await add_people(sessions, worker_statuses=("band",))
            async with sessions() as session:
                orders = [
                    Order(
                        customer_id=CUSTOMER_ID,
                        worker_id=WORKER_ONE_ID,
                        car_category="Sedan",
                        car_model=f"Cobalt {index}",
                        car_price=Decimal("50000"),
                        plate_number=f"01 A 00{index} AA",
                        status="yakunlanmoqda",
                    )
                    for index in (1, 2)
                ]
                session.add_all(orders)
                await session.commit()
                first_id, second_id = (order.id for order in orders)

            state = RecordingState({"order_id": first_id})
            callback = RecordingCallback(
                f"worker_payment:Naqd:{second_id}",
                WORKER_ONE_ID,
                RecordingBot(),
            )
            await handler(router, "callback_query", "receive_worker_payment")(
                callback, state
            )

            async with sessions() as session:
                second = await session.get(Order, second_id)
                assert second.payment_method is None
            assert callback.answers[-1][1].get("show_alert") is True
        finally:
            await engine.dispose()

    run(scenario())


def test_single_photo_without_payment_is_accepted_and_duplicate_rejected():
    async def scenario():
        engine, sessions, _settings, _scheduler, router = await make_context()
        try:
            await add_people(sessions, worker_statuses=("band",))
            async with sessions() as session:
                order = Order(
                    customer_id=CUSTOMER_ID,
                    worker_id=WORKER_ONE_ID,
                    car_category="Sedan",
                    car_model="Cobalt",
                    car_price=Decimal("50000"),
                    plate_number="01 A 123 BC",
                    payment_method=None,
                    status="yakunlanmoqda",
                )
                session.add(order)
                await session.commit()
                order_id = order.id

            state = RecordingState({"order_id": order_id})
            message = SimpleNamespace(
                from_user=SimpleNamespace(id=WORKER_ONE_ID),
                photo=[SimpleNamespace(file_id="out-of-order")],
                answer=RecordingMessage(
                    RecordingBot(), WORKER_ONE_ID
                ).answer,
            )
            before = handler(router, "message", "receive_before_photo")
            await before(message, state)
            async with sessions() as session:
                order = await session.get(Order, order_id)
                assert order.before_photo_id == "out-of-order"
                assert order.payment_method is None
            assert state.states[-1] == WorkerCompletionStates.waiting_comment

            duplicate_state = RecordingState({"order_id": order_id})
            await before(message, duplicate_state)
            async with sessions() as session:
                order = await session.get(Order, order_id)
                assert order.before_photo_id == "out-of-order"
        finally:
            await engine.dispose()

    run(scenario())


def test_worker_report_breaks_down_orders_and_revenue_by_worker():
    async def scenario():
        engine, sessions, _settings, _scheduler, _router = await make_context()
        try:
            await add_people(sessions)
            async with sessions() as session:
                worker_two_user = User(
                    telegram_id=WORKER_TWO_ID,
                    name="Ishchi Ikki",
                    phone="+998901234569",
                    rol="ishchi",
                )
                session.add(worker_two_user)
                session.add(
                    Worker(
                        user_id=WORKER_TWO_ID,
                        name="Ishchi Ikki",
                        phone="+998901234569",
                        share_percent=Decimal("40"),
                        status="smenada_emas",
                    )
                )
                session.add_all(
                    [
                        Order(
                            customer_id=CUSTOMER_ID,
                            worker_id=WORKER_ONE_ID,
                            car_category="Sedan",
                            car_model="Cobalt",
                            car_price=Decimal("50000"),
                            plate_number="01 A 111 AA",
                            payment_method="Naqd",
                            status="yakunlandi",
                            worker_share_type="percent",
                            worker_share_value=Decimal("30"),
                            worker_share_amount=Decimal("15000"),
                            completed_at=datetime(
                                2026, 8, 31, 5, 0, tzinfo=timezone.utc
                            ),
                        ),
                        Order(
                            customer_id=CUSTOMER_ID,
                            worker_id=WORKER_TWO_ID,
                            car_category="SUV",
                            car_model="Tracker",
                            car_price=Decimal("70000"),
                            plate_number="01 B 222 BB",
                            payment_method="Karta",
                            status="yakunlandi",
                            worker_share_type="percent",
                            worker_share_value=Decimal("40"),
                            worker_share_amount=Decimal("28000"),
                            completed_at=datetime(
                                2026, 8, 31, 6, 0, tzinfo=timezone.utc
                            ),
                        ),
                    ]
                )
                await session.commit()
                start = datetime(2026, 8, 31, tzinfo=worker_handlers.TASHKENT)
                end = start + timedelta(days=1)
                all_chunks = await build_financial_report(session, start, end)
                worker_chunks = await build_financial_report(
                    session, start, end, worker_id=WORKER_TWO_ID
                )

            all_report = "\n".join(all_chunks)
            worker_report = "\n".join(worker_chunks)
            assert "Ishchi" in all_report
            assert "Ishchi Ikki" in all_report
            assert "Yuvilgan: 1 ta" in all_report
            assert "120 000" in all_report
            assert "Tracker" in all_report
            assert "01 B 222 BB" in worker_report
            assert "70 000" in worker_report
            assert "28 000" in worker_report
            assert "Cobalt" not in worker_report
        finally:
            await engine.dispose()

    run(scenario())


def test_worker_report_handles_inactive_history_cancellations_and_boundaries():
    async def scenario():
        engine, sessions, _settings, _scheduler, _router = await make_context()
        try:
            await add_people(sessions)
            start = datetime(2026, 8, 31, tzinfo=worker_handlers.TASHKENT)
            end = start + timedelta(days=1)
            async with sessions() as session:
                worker = await session.get(Worker, WORKER_ONE_ID)
                worker.name = "<Faol emas>"
                worker.active = False
                worker.share_percent = Decimal("33.33")
                included = Order(
                    customer_id=CUSTOMER_ID,
                    worker_id=WORKER_ONE_ID,
                    car_category="Sedan",
                    car_model="<Cobalt>",
                    car_price=Decimal("5"),
                    plate_number="01 < 02",
                    payment_method="Naqd",
                    status="yakunlandi",
                    completed_at=start,
                    worker_share_type="percent",
                    worker_share_value=Decimal("33.33"),
                    worker_share_amount=Decimal("1.67"),
                )
                excluded = Order(
                    customer_id=CUSTOMER_ID,
                    worker_id=WORKER_ONE_ID,
                    car_category="SUV",
                    car_model="Chegaradan tashqari",
                    car_price=Decimal("999"),
                    plate_number="01 Z 999 ZZ",
                    payment_method="Karta",
                    status="yakunlandi",
                    completed_at=end,
                )
                cancelled = Order(
                    customer_id=CUSTOMER_ID,
                    worker_id=WORKER_ONE_ID,
                    car_category="Sedan",
                    car_model="Nexia",
                    car_price=Decimal("50"),
                    status="bekor_qilindi",
                )
                session.add_all([included, excluded, cancelled])
                await session.flush()
                session.add(
                    Cancellation(
                        order_id=cancelled.id,
                        reason="<Mijoz bekor qildi>",
                        cancelled_by=DIRECTOR_ID,
                        cancelled_at=start + timedelta(hours=1),
                    )
                )
                await session.commit()
                chunks = await build_financial_report(
                    session, start, end, worker_id=WORKER_ONE_ID
                )
                empty_chunks = await build_financial_report(
                    session,
                    end + timedelta(days=1),
                    end + timedelta(days=2),
                    worker_id=WORKER_ONE_ID,
                )

            report = "\n".join(chunks)
            assert "&lt;Faol emas&gt;" in report
            assert "&lt;Cobalt&gt;" in report
            assert "&lt;Mijoz bekor qildi&gt;" in report
            assert "Bekor qilingan:</b> 1 ta" in report
            assert "Ishchiga to‘lov: 2 " in report
            assert "Chegaradan tashqari" not in report
            assert "yakunlangan buyurtmalar topilmadi" in "\n".join(empty_chunks)
            assert all(len(chunk) <= 4096 for chunk in chunks)
        finally:
            await engine.dispose()

    run(scenario())


def test_report_rejects_malformed_worker_and_expired_session_callbacks():
    async def scenario():
        engine, sessions, _settings, _scheduler, router = await make_context()
        try:
            await add_people(sessions)
            generate = handler(router, "callback_query", "generate_report")
            malformed = RecordingCallback(
                "report_worker:not-a-worker", DIRECTOR_ID, RecordingBot()
            )
            await generate(malformed, RecordingState())
            assert malformed.answers[-1][1].get("show_alert") is True

            expired = RecordingCallback(
                f"report_worker:{WORKER_ONE_ID}", DIRECTOR_ID, RecordingBot()
            )
            state = RecordingState()
            await generate(expired, state)
            assert expired.answers[-1][1].get("show_alert") is True
            assert state.cleared is True
        finally:
            await engine.dispose()

    run(scenario())


def test_configured_director_demotes_previous_director_to_operator():
    async def scenario():
        engine, sessions, _settings, _scheduler, _router = await make_context()
        try:
            await add_people(sessions)
            new_director_id = 9100
            await reconcile_staff_roles(sessions, new_director_id)
            async with sessions() as session:
                old_director = await session.get(User, DIRECTOR_ID)
                new_director = await session.get(User, new_director_id)
            assert old_director.rol == "operator"
            assert new_director.rol == "direktor"
        finally:
            await engine.dispose()

    run(scenario())


def test_director_manages_operators_and_operator_can_use_crm():
    async def scenario():
        engine, sessions, _settings, _scheduler, router = await make_context()
        try:
            await add_people(sessions)
            operator_id = 7100
            bot = RecordingBot()
            state = RecordingState()
            await handler(router, "callback_query", "start_operator_add")(
                RecordingCallback("operator_add", DIRECTOR_ID, bot),
                state,
            )
            await handler(router, "message", "receive_operator_id")(
                RecordingMessage(bot, DIRECTOR_ID, str(operator_id)),
                state,
            )
            async with sessions() as session:
                operator = await session.get(User, operator_id)
            assert operator is not None and operator.rol == "operator"

            crm_message = RecordingMessage(bot, operator_id, "Mijozlar bazasi")
            await handler(router, "message", "open_crm")(
                crm_message,
                RecordingState(),
            )
            assert crm_message.answer_calls
            search_callback = RecordingCallback("crm_search", operator_id, bot)
            search_state = RecordingState()
            await handler(router, "callback_query", "start_crm_search")(
                search_callback,
                search_state,
            )
            assert not any(
                kwargs.get("show_alert") for _text, kwargs in search_callback.answers
            )

            denied = RecordingMessage(bot, operator_id, "Operatorlarni boshqarish")
            await handler(router, "message", "manage_operators")(
                denied,
                RecordingState(),
            )
            assert "faqat direktor" in str(denied.answer_calls[-1][0])

            await handler(router, "callback_query", "remove_operator")(
                RecordingCallback(
                    f"operator_remove:{operator_id}",
                    DIRECTOR_ID,
                    bot,
                )
            )
            async with sessions() as session:
                removed = await session.get(User, operator_id)
            assert removed.rol == "mijoz"
        finally:
            await engine.dispose()

    run(scenario())


def test_worker_expenses_additional_income_and_profit_math():
    async def scenario():
        engine, sessions, _settings, _scheduler, router = await make_context()
        try:
            await add_people(sessions)
            operator_id = 7200
            now = datetime.now(worker_handlers.TASHKENT)
            async with sessions() as session:
                session.add(User(telegram_id=operator_id, name="Operator", rol="operator"))
                session.add(
                    Order(
                        customer_id=CUSTOMER_ID,
                        worker_id=WORKER_ONE_ID,
                        car_category="Sedan",
                        car_model="Cobalt",
                        car_price=Decimal("100000"),
                        payment_method="Naqd",
                        status="yakunlandi",
                        arrived_at=now - timedelta(minutes=90),
                        completed_at=now,
                        worker_share_type="amount",
                        worker_share_value=Decimal("30000"),
                        worker_share_amount=Decimal("30000"),
                    )
                )
                await session.commit()

            bot = RecordingBot()
            for owner_data, amount, description in (
                (f"expense_owner:worker:{WORKER_ONE_ID}", "10000", "Kimyoviy vosita"),
                ("expense_owner:general", "5000", "Ofis xarajati"),
            ):
                state = RecordingState()
                await handler(router, "callback_query", "choose_expense_owner")(
                    RecordingCallback(owner_data, DIRECTOR_ID, bot),
                    state,
                )
                await handler(router, "message", "receive_expense_amount")(
                    RecordingMessage(bot, DIRECTOR_ID, amount),
                    state,
                )
                await handler(router, "message", "receive_expense_description")(
                    RecordingMessage(bot, DIRECTOR_ID, description),
                    state,
                )

            async def add_income(
                description: str,
                amount: str,
                share_type: str,
                share_value: str,
            ) -> None:
                state = RecordingState()
                await handler(
                    router, "callback_query", "choose_additional_income_worker"
                )(
                    RecordingCallback(
                        f"additional_income_worker:{WORKER_ONE_ID}",
                        operator_id,
                        bot,
                    ),
                    state,
                )
                await handler(
                    router, "message", "receive_additional_income_description"
                )(
                    RecordingMessage(bot, operator_id, description),
                    state,
                )
                await handler(router, "message", "receive_additional_income_amount")(
                    RecordingMessage(bot, operator_id, amount),
                    state,
                )
                await handler(
                    router, "callback_query", "choose_additional_income_share"
                )(
                    RecordingCallback(
                        f"additional_income_share:{share_type}",
                        operator_id,
                        bot,
                    ),
                    state,
                )
                await handler(router, "message", "receive_additional_income_share")(
                    RecordingMessage(bot, operator_id, share_value),
                    state,
                )

            await add_income("Polirovka", "40000", "percent", "25")
            await add_income("Salon tozalash", "20000", "amount", "5000")

            async with sessions() as session:
                expenses = list((await session.scalars(select(Expense))).all())
                incomes = list(
                    (await session.scalars(select(WorkerAdditionalIncome))).all()
                )
                start = now - timedelta(days=1)
                end = now + timedelta(days=1)
                all_report = "\n".join(
                    await build_financial_report(session, start, end)
                )
                worker_report = "\n".join(
                    await build_financial_report(
                        session,
                        start,
                        end,
                        worker_id=WORKER_ONE_ID,
                    )
                )

            assert {expense.worker_id for expense in expenses} == {
                None,
                WORKER_ONE_ID,
            }
            assert [(item.share_type, item.worker_amount) for item in incomes] == [
                ("percent", Decimal("10000.00")),
                ("amount", Decimal("5000.00")),
            ]
            assert "Sof foyda:</b> 100 000" in all_report
            assert "biznes foydasi: 105 000" in worker_report
            assert "Ishchi xarajati: 10 000" in worker_report
            assert "1 soat 30 daqiqa" in worker_report
        finally:
            await engine.dispose()

    run(scenario())


def test_skip_share_uses_registered_percent_snapshot():
    async def scenario():
        engine, sessions, _settings, _scheduler, router = await make_context()
        try:
            await add_people(sessions)
            order_id = (await add_group(sessions, count=1))[0]
            callback = RecordingCallback(
                f"order_share:default:{order_id}:{WORKER_ONE_ID}",
                DIRECTOR_ID,
                RecordingBot(),
            )
            await handler(router, "callback_query", "choose_order_share")(
                callback, RecordingState()
            )
            async with sessions() as session:
                order = await session.get(Order, order_id)
                assert order.worker_share_type == "percent"
                assert order.worker_share_value == Decimal("30.00")
                assert order.worker_share_amount == Decimal("30000.00")
                worker = await session.get(Worker, WORKER_ONE_ID)
                worker.share_percent = Decimal("50")
                await session.commit()
            async with sessions() as session:
                order = await session.get(Order, order_id)
                assert order.worker_share_amount == Decimal("30000.00")
        finally:
            await engine.dispose()

    run(scenario())


def test_worker_finance_buttons_save_own_entries_with_registered_share():
    async def scenario():
        engine, sessions, _settings, _scheduler, router = await make_context()
        try:
            await add_people(sessions)
            bot = RecordingBot()
            for kind, amount, description in (
                ("expense", "12000", "Kimyoviy vosita"),
                ("income", "50000", "Polirovka"),
            ):
                state = RecordingState()
                await handler(router, "callback_query", "start_worker_finance_from_cabinet")(
                    RecordingCallback(f"cabinet_finance:{kind}", WORKER_ONE_ID, bot),
                    state,
                )
                assert state.states[-1] == WorkerFinanceStates.waiting_amount
                await handler(router, "message", "worker_finance_amount")(
                    RecordingMessage(bot, WORKER_ONE_ID, amount), state
                )
                await handler(router, "message", "worker_finance_description")(
                    RecordingMessage(bot, WORKER_ONE_ID, description), state
                )
                assert state.cleared is True
            async with sessions() as session:
                expense = await session.scalar(select(Expense))
                income = await session.scalar(select(WorkerAdditionalIncome))
                assert expense.worker_id == WORKER_ONE_ID
                assert expense.amount == Decimal("12000")
                assert income.worker_id == WORKER_ONE_ID
                assert income.share_value == Decimal("30")
                assert income.worker_amount == Decimal("15000")
        finally:
            await engine.dispose()

    run(scenario())


def test_operator_can_choose_manual_car_category_and_model():
    async def scenario():
        engine, sessions, _settings, _scheduler, router = await make_context()
        try:
            operator_id = 8200
            async with sessions() as session:
                session.add(User(telegram_id=operator_id, name="Operator", rol="operator"))
                await session.commit()
            bot = RecordingBot()
            state = RecordingState()
            category = "Sedan"
            await handler(router, "callback_query", "manual_car_category")(
                RecordingCallback(f"manual_category:{category}", operator_id, bot),
                state,
            )
            assert state.states[-1] == ManualOrderStates.waiting_new_model
            await handler(router, "callback_query", "manual_car_model")(
                RecordingCallback("manual_model:sedan-cobalt", operator_id, bot),
                state,
            )
            assert state.data["cars"][0]["car_model"] == "Chevrolet Cobalt"
            assert state.states[-1] == ManualOrderStates.waiting_next_car
        finally:
            await engine.dispose()

    run(scenario())


def test_operator_creates_manual_order_without_director_approval():
    async def scenario():
        engine, sessions, _settings, _scheduler, router = await make_context()
        try:
            await add_people(sessions)
            operator_id = 8200
            async with sessions() as session:
                session.add(User(telegram_id=operator_id, name="Operator", rol="operator"))
                await session.commit()
            bot = RecordingBot()
            state = RecordingState({
                "customer_id": CUSTOMER_ID,
                "cars": [{
                    "car_category": "Sedan",
                    "car_model": "Cobalt",
                    "car_price": 50000,
                    "plate_number": None,
                }],
                "visit_at": (datetime.now(worker_handlers.TASHKENT) + timedelta(hours=2)).isoformat(),
                "address": "Toshkent",
            })
            message = RecordingMessage(bot, operator_id, "⏭️ O'tkazib yuborish")
            await handler(router, "message", "manual_comment")(message, state)
            async with sessions() as session:
                orders = list((await session.scalars(select(Order))).all())
                assert len(orders) == 1
                assert orders[0].status == "yangi"
                order_id = orders[0].id
            assert state.cleared
            assert any("Direktor tasdig‘i talab qilinmaydi" in str(text)
                       for text, _ in message.answer_calls)
            summaries = [call for call in bot.calls
                         if call[0] == "send_message" and "Qo'lda kiritilgan buyurtma" in call[2]]
            assert {call[1] for call in summaries} == {operator_id, DIRECTOR_ID}
            assert next(call for call in summaries if call[1] == DIRECTOR_ID)[3]["reply_markup"] is None
            assert next(call for call in summaries if call[1] == operator_id)[3]["reply_markup"] is not None
            await handler(router, "callback_query", "show_available_workers")(
                RecordingCallback(f"assign_workers:{order_id}", operator_id, bot)
            )
            await handler(router, "callback_query", "choose_wash_duration")(
                RecordingCallback(f"assign_worker:{order_id}:{WORKER_ONE_ID}", operator_id, bot)
            )
            await handler(router, "callback_query", "choose_order_share")(
                RecordingCallback(
                    f"order_share:default:{order_id}:{WORKER_ONE_ID}", operator_id, bot
                ),
                RecordingState(),
            )
            async with sessions() as session:
                order = await session.get(Order, order_id)
                assert order.worker_id == WORKER_ONE_ID
                assert order.status == "ishchiga_yuborildi"
        finally:
            await engine.dispose()

    run(scenario())


def test_operator_manual_group_can_split_without_director_action():
    async def scenario():
        engine, sessions, _settings, _scheduler, router = await make_context()
        try:
            await add_people(sessions)
            operator_id = 8200
            async with sessions() as session:
                session.add(User(telegram_id=operator_id, name="Operator", rol="operator"))
                await session.commit()
            bot = RecordingBot()
            state = RecordingState({
                "customer_id": CUSTOMER_ID,
                "cars": [
                    {"car_category": "Sedan", "car_model": model,
                     "car_price": 50000, "plate_number": None}
                    for model in ("Cobalt", "Nexia")
                ],
                "visit_at": (datetime.now(worker_handlers.TASHKENT) + timedelta(hours=2)).isoformat(),
                "address": "Toshkent",
            })
            await handler(router, "message", "manual_comment")(
                RecordingMessage(bot, operator_id, "⏭️ O'tkazib yuborish"), state
            )
            async with sessions() as session:
                orders = list((await session.scalars(select(Order).order_by(Order.id))).all())
                assert len(orders) == 2
                group_id = orders[0].order_group_id
                lead_id = orders[0].id
            summaries = [call for call in bot.calls
                         if call[0] == "send_message" and "guruh buyurtmasi" in call[2]]
            assert {call[1] for call in summaries} == {operator_id, DIRECTOR_ID}
            assert next(call for call in summaries if call[1] == DIRECTOR_ID)[3]["reply_markup"] is None
            assert next(call for call in summaries if call[1] == operator_id)[3]["reply_markup"] is not None
            await handler(router, "callback_query", "split_group_orders")(
                RecordingCallback(f"group_split:{group_id}:{lead_id}", operator_id, bot)
            )
            async with sessions() as session:
                orders = list((await session.scalars(select(Order).order_by(Order.id))).all())
                assert all(order.group_mode == "split" for order in orders)
            assert not any(call[1] == DIRECTOR_ID and "📋 Buyurtma #" in call[2]
                           for call in bot.calls if call[0] == "send_message")
        finally:
            await engine.dispose()

    run(scenario())


def test_customer_order_goes_to_operator_and_assignment_prevents_escalation():
    async def scenario():
        engine, sessions, settings, scheduler, router = await make_context()
        try:
            await add_people(sessions)
            operator_id = 8200
            async with sessions() as session:
                session.add(User(telegram_id=operator_id, name="Operator", rol="operator"))
                await session.commit()
            bot = RecordingBot()
            configure_wash_timer_runtime(sessions, bot, settings)
            state = RecordingState({
                "cars": [{"car_category": "Sedan", "car_model": "Cobalt",
                          "car_price": 50000, "plate_number": "01 A 123 BC"}],
                "payment_method": "Naqd",
                "latitude": 41.3,
                "longitude": 69.2,
            })
            await handler(router, "message", "receive_comment")(
                RecordingMessage(bot, CUSTOMER_ID, "⏭️ O'tkazib yuborish"), state
            )
            async with sessions() as session:
                order_id = (await session.scalar(select(Order))).id
            assert any(call[1] == operator_id and "Yangi buyurtma" in call[2]
                       for call in bot.calls if call[0] == "send_message")
            assert not any(call[1] == DIRECTOR_ID for call in bot.calls)
            job = scheduler.jobs[f"operator-review:{order_id}"]
            assert timedelta(minutes=29) <= job["run_date"] - datetime.now(worker_handlers.TASHKENT) <= timedelta(minutes=30)
            await handler(router, "callback_query", "show_available_workers")(
                RecordingCallback(f"assign_workers:{order_id}", operator_id, bot)
            )
            await handler(router, "callback_query", "choose_wash_duration")(
                RecordingCallback(f"assign_worker:{order_id}:{WORKER_ONE_ID}", operator_id, bot)
            )
            await handler(router, "callback_query", "choose_order_share")(
                RecordingCallback(f"order_share:default:{order_id}:{WORKER_ONE_ID}", operator_id, bot),
                RecordingState(),
            )
            async with sessions() as session:
                order = await session.get(Order, order_id)
                assert order.worker_id == WORKER_ONE_ID
                assert order.status == "ishchiga_yuborildi"
            await job["callback"](order_id)
            assert not any(call[1] == DIRECTOR_ID for call in bot.calls)
        finally:
            await engine.dispose()

    run(scenario())


def test_unassigned_customer_order_escalates_to_director_after_review():
    async def scenario():
        engine, sessions, settings, scheduler, router = await make_context()
        try:
            await add_people(sessions)
            async with sessions() as session:
                session.add(User(telegram_id=8200, name="Operator", rol="operator"))
                await session.commit()
            bot = RecordingBot()
            configure_wash_timer_runtime(sessions, bot, settings)
            state = RecordingState({
                "cars": [{"car_category": "Sedan", "car_model": "Cobalt",
                          "car_price": 50000, "plate_number": "01 A 123 BC"}],
                "payment_method": "Naqd", "latitude": 41.3, "longitude": 69.2,
            })
            await handler(router, "message", "receive_comment")(
                RecordingMessage(bot, CUSTOMER_ID, "Izoh"), state
            )
            async with sessions() as session:
                order_id = (await session.scalar(select(Order))).id
            assert not any(call[1] == DIRECTOR_ID for call in bot.calls)
            await scheduler.jobs[f"operator-review:{order_id}"]["callback"](order_id)
            director_messages = [call for call in bot.calls
                                 if call[0] == "send_message" and call[1] == DIRECTOR_ID]
            assert len(director_messages) == 1
            assert "Operator 30 daqiqada biriktirmadi" in director_messages[0][2]
            assert "01 A 123 BC" in director_messages[0][2]
            assert director_messages[0][3]["reply_markup"] is not None
        finally:
            await engine.dispose()

    run(scenario())


def test_customer_order_goes_directly_to_director_when_no_operator_exists():
    async def scenario():
        engine, sessions, _settings, scheduler, router = await make_context()
        try:
            await add_people(sessions)
            bot = RecordingBot()
            await handler(router, "message", "receive_comment")(
                RecordingMessage(bot, CUSTOMER_ID, "⏭️ O'tkazib yuborish"),
                RecordingState({
                    "cars": [{"car_category": "Sedan", "car_model": "Cobalt",
                              "car_price": 50000, "plate_number": "01 A 123 BC"}],
                    "payment_method": "Naqd", "latitude": 41.3, "longitude": 69.2,
                }),
            )
            assert any(call[0] == "send_message" and call[1] == DIRECTOR_ID
                       and "Yangi buyurtma" in call[2] for call in bot.calls)
            assert not any(job_id.startswith("operator-review:") for job_id in scheduler.jobs)
        finally:
            await engine.dispose()

    run(scenario())


def test_group_split_stays_with_operator_and_only_unassigned_car_escalates():
    async def scenario():
        engine, sessions, settings, scheduler, router = await make_context()
        try:
            await add_people(sessions)
            operator_id = 8200
            async with sessions() as session:
                session.add(User(telegram_id=operator_id, name="Operator", rol="operator"))
                await session.commit()
            bot = RecordingBot()
            configure_wash_timer_runtime(sessions, bot, settings)
            cars = [
                {"car_category": "Sedan", "car_model": model,
                 "car_price": 50000, "plate_number": f"01 A 12{i} BC"}
                for i, model in enumerate(("Cobalt", "Nexia"), 1)
            ]
            await handler(router, "message", "receive_comment")(
                RecordingMessage(bot, CUSTOMER_ID, "Izoh"),
                RecordingState({"cars": cars, "payment_method": "Naqd",
                                "latitude": 41.3, "longitude": 69.2}),
            )
            async with sessions() as session:
                orders = list((await session.scalars(select(Order).order_by(Order.id))).all())
                group_id = orders[0].order_group_id
                order_ids = [order.id for order in orders]
            await handler(router, "callback_query", "split_group_orders")(
                RecordingCallback(f"group_split:{group_id}:{order_ids[0]}", operator_id, bot)
            )
            assert not any(call[1] == DIRECTOR_ID for call in bot.calls)
            async with sessions() as session:
                first = await session.get(Order, order_ids[0])
                first.worker_id = WORKER_ONE_ID
                first.status = "ishchiga_yuborildi"
                await session.commit()
            for order_id in order_ids:
                await scheduler.jobs[f"operator-review:{order_id}"]["callback"](order_id)
            director_messages = [call for call in bot.calls
                                 if call[0] == "send_message" and call[1] == DIRECTOR_ID]
            assert len(director_messages) == 1
            assert f"Buyurtma #{order_ids[1]}" in director_messages[0][2]
        finally:
            await engine.dispose()

    run(scenario())


def test_worker_completes_with_only_before_photo_and_skipped_comment():
    async def scenario():
        engine, sessions, _settings, _scheduler, router = await make_context()
        try:
            await add_people(sessions, worker_statuses=("band",))
            async with sessions() as session:
                session.add_all([
                    User(telegram_id=8200, name="Operator 1", rol="operator"),
                    User(telegram_id=8201, name="Operator 2", rol="operator"),
                ])
                order = Order(
                    customer_id=CUSTOMER_ID,
                    worker_id=WORKER_ONE_ID,
                    car_category="Sedan",
                    car_model="Cobalt",
                    car_price=Decimal("50000"),
                    status="yetib_keldi",
                    arrived_at=datetime.now(worker_handlers.TASHKENT) - timedelta(minutes=20),
                )
                session.add(order)
                await session.commit()
                order_id = order.id
            bot = RecordingBot()
            state = RecordingState()
            await handler(router, "callback_query", "update_worker_status")(
                RecordingCallback(f"worker_status:complete:{order_id}", WORKER_ONE_ID, bot),
                state,
            )
            assert state.states[-1] == WorkerCompletionStates.waiting_before_photo
            photo_message = SimpleNamespace(
                from_user=SimpleNamespace(id=WORKER_ONE_ID),
                photo=[SimpleNamespace(file_id="photo-before")],
                answer=RecordingMessage(bot, WORKER_ONE_ID).answer,
            )
            await handler(router, "message", "receive_before_photo")(photo_message, state)
            assert state.states[-1] == WorkerCompletionStates.waiting_comment
            await handler(router, "message", "complete_order")(
                RecordingMessage(bot, WORKER_ONE_ID, "⏭️ O'tkazib yuborish"), state
            )
            async with sessions() as session:
                order = await session.get(Order, order_id)
                assert order.status == "yakunlandi"
                assert order.plate_number is None
                assert order.payment_method is None
                assert order.after_photo_id is None
                assert order.worker_comment is None
            recipients = {DIRECTOR_ID, 8200, 8201}
            photos = [call for call in bot.calls if call[0] == "send_photo"]
            assert {call[1] for call in photos} == recipients
            assert all(call[2] == "photo-before" for call in photos)
            reports = [
                call for call in bot.calls
                if call[0] == "send_message" and "Yakuniy hisobot" in call[2]
            ]
            assert {call[1] for call in reports} == recipients
            assert all("Mijoz" in call[2] and "Davlat raqami" in call[2]
                       and f"Buyurtma #{order_id}" in call[2] for call in reports)
        finally:
            await engine.dispose()

    run(scenario())


def test_customer_history_report_shows_customer_details_and_cancellations():
    async def scenario():
        engine, sessions, _settings, _scheduler, router = await make_context()
        try:
            await add_people(sessions)
            async with sessions() as session:
                customer = await session.get(User, CUSTOMER_ID)
                assert customer is not None
                customer.name = "<Ali>"
                customer.phone = "+998 90 123 45 67"
                completed = Order(
                    customer_id=CUSTOMER_ID,
                    car_category="Sedan",
                    car_model="<Cobalt>",
                    car_price=Decimal("50000"),
                    plate_number="01 < 111 AA",
                    payment_method="Naqd",
                    status="yakunlandi",
                    created_at=datetime(
                        2026, 8, 30, 19, 0, tzinfo=timezone.utc
                    ),
                )
                cancelled = Order(
                    customer_id=CUSTOMER_ID,
                    car_category="SUV",
                    car_model="Tracker",
                    car_price=Decimal("70000"),
                    plate_number="01 B 222 BB",
                    status="bekor_qilindi",
                    created_at=datetime(
                        2026, 8, 30, 20, 0, tzinfo=timezone.utc
                    ),
                )
                session.add_all([completed, cancelled])
                await session.flush()
                session.add(
                    Cancellation(
                        order_id=cancelled.id,
                        reason="<Mijoz fikrini o‘zgartirdi>",
                        cancelled_by=CUSTOMER_ID,
                        cancelled_at=datetime(
                            2026, 8, 30, 21, 0, tzinfo=timezone.utc
                        ),
                    )
                )
                await session.commit()
                start = datetime(
                    2026, 8, 31, tzinfo=worker_handlers.TASHKENT
                ) - timedelta(days=1)
                end = start + timedelta(days=1)
                chunks = await build_customer_history_report(
                    session, start, end
                )

            report = "\n".join(chunks)
            assert "Unikal mijozlar:</b> 1 ta" in report
            assert "Jami buyurtmalar:</b> 2 ta" in report
            assert "Yakunlangan:</b> 1 ta" in report
            assert "Bekor qilingan:</b> 1 ta" in report
            assert "&lt;Ali&gt;" in report
            assert "+998 90 123 45 67" in report
            assert "&lt;Cobalt&gt;" in report
            assert "01 &lt; 111 AA" in report
            assert "&lt;Mijoz fikrini o‘zgartirdi&gt;" in report
            assert all(len(chunk) <= 4096 for chunk in chunks)
        finally:
            await engine.dispose()

    run(scenario())


def test_customer_history_custom_range_handler_and_twelve_month_period():
    async def scenario():
        engine, sessions, _settings, _scheduler, router = await make_context()
        try:
            await add_people(sessions)
            message = RecordingMessage(
                RecordingBot(), DIRECTOR_ID, "👥 Mijozlar tarixi"
            )
            state = RecordingState()
            await handler(router, "message", "start_customer_history")(
                message, state
            )
            assert message.answer_calls
            assert "davrni tanlang" in str(message.answer_calls[0][0])

            custom = handler(
                router,
                "message",
                "receive_customer_history_range",
            )
            message.text = "30.08.2026 - 31.08.2026"
            await custom(message, state)
            assert state.cleared is True
            assert any(
                "Mijozlar tarixi tayyor" in str(text)
                for text, _kwargs in message.answer_calls
            )

            now = datetime(
                2026, 8, 31, 13, 0, tzinfo=worker_handlers.TASHKENT
            )
            start, end = period_bounds("12months", now=now)
            assert start == datetime(
                2025, 9, 1, tzinfo=worker_handlers.TASHKENT
            )
            assert end == datetime(
                2026, 9, 1, tzinfo=worker_handlers.TASHKENT
            )
        finally:
            await engine.dispose()

    run(scenario())


def test_customer_history_counts_cancellation_only_manual_customer_safely():
    async def scenario():
        engine, sessions, _settings, _scheduler, _router = await make_context()
        try:
            start = datetime(2026, 8, 31, tzinfo=worker_handlers.TASHKENT)
            end = start + timedelta(days=1)
            async with sessions() as session:
                manual_id = -7001
                session.add(
                    User(
                        telegram_id=manual_id,
                        name='"' * 150,
                        phone='"' * 40,
                        rol="mijoz",
                    )
                )
                old_order = Order(
                    customer_id=manual_id,
                    car_category="Sedan",
                    car_model='"' * 100,
                    car_price=Decimal("50000"),
                    plate_number='"' * 30,
                    status="bekor_qilindi",
                    created_at=start - timedelta(days=10),
                )
                session.add(old_order)
                await session.flush()
                session.add(
                    Cancellation(
                        order_id=old_order.id,
                        reason='"' * 2000,
                        cancelled_by=DIRECTOR_ID,
                        cancelled_at=start + timedelta(hours=2),
                    )
                )
                await session.commit()
                chunks = await build_customer_history_report(
                    session, start, end
                )

            report = "\n".join(chunks)
            assert "Unikal mijozlar:</b> 1 ta" in report
            assert "Jami buyurtmalar:</b> 0 ta" in report
            assert "Bekor qilingan:</b> 1 ta" in report
            assert "&quot;" in report
            assert "cancellation faolligi mavjud" in report
            assert "..." in report
            assert all(len(chunk) <= 4096 for chunk in chunks)
        finally:
            await engine.dispose()

    run(scenario())