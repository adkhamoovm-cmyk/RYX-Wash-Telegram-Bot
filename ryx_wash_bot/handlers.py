import html
import logging
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .catalog import (
    CarModel,
    add_model,
    categories,
    format_price,
    get_model,
    get_model_by_name,
    remove_model,
    replace_model,
)
from .config import Settings
from .keyboards import (
    category_keyboard,
    contact_keyboard,
    crm_card_keyboard,
    crm_customers_keyboard,
    crm_menu_keyboard,
    customer_menu_keyboard,
    director_menu_keyboard,
    expense_delete_confirm_keyboard,
    expense_items_keyboard,
    group_mode_keyboard,
    location_keyboard,
    manual_category_keyboard,
    manual_location_keyboard,
    manual_model_keyboard,
    model_keyboard,
    new_order_assignment_keyboard,
    payment_keyboard,
    next_car_keyboard,
    price_management_keyboard,
    price_models_keyboard,
    report_period_keyboard,
    report_workers_keyboard,
    saved_cars_keyboard,
    skip_comment_keyboard,
    worker_menu_keyboard,
)
from .models import CustomerCar, Expense, Order, ServiceModel, User, Worker
from .reports import build_financial_report, period_bounds
from .states import (
    ExpenseStates,
    ManualOrderStates,
    OrderStates,
    PriceManagementStates,
    RegistrationStates,
    ReportStates,
    CrmStates,
)
from .worker_handlers import register_worker_routes

logger = logging.getLogger(__name__)


def _safe(value: object) -> str:
    return html.escape(str(value))


TASHKENT = ZoneInfo("Asia/Tashkent")
CUSTOM_RANGE_RE = re.compile(
    r"^\s*(\d{2}\.\d{2}\.\d{4})\s*[-–—]\s*(\d{2}\.\d{2}\.\d{4})\s*$"
)
CUSTOMER_STATUS_LABELS = {
    "yangi": "🕐 Direktor ko‘rib chiqmoqda",
    "navbatda": "⏳ Ishchi navbati kutilmoqda",
    "ishchiga_yuborildi": "📤 Ishchiga yuborildi",
    "ishchi_qabul_qildi": "✅ Ishchi buyurtmani qabul qildi",
    "yo‘lda": "🚗 Ishchi yo‘lda",
    "yo'lda": "🚗 Ishchi yo‘lda",
    "yetib_keldi": "📍 Ishchi manzilga yetib keldi",
    "yuvish_boshlandi": "🧼 Yuvish boshlandi",
    "yakunlanmoqda": "📸 Yakunlanmoqda",
    "yakunlandi": "🎉 Buyurtma yakunlandi",
    "bekor_qilindi": "🚫 Buyurtma bekor qilindi",
}


def _format_eta_time(value: datetime) -> str:
    """Format an ETA as a Tashkent (UTC+5) clock time."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=TASHKENT)
    return value.astimezone(TASHKENT).strftime("%H:%M")
MAIN_MENU_TEXTS = frozenset(
    {
        "🚗➕ Yangi buyurtma",
        "Yangi buyurtma",
        "📋 Buyurtmalar tarixi",
        "Buyurtmalar tarixi",
        "➕👷 Ishchi qo'shish",
        "Ishchi qo'shish",
        "👷 Ishchilarni boshqarish",
        "📝 Qo'lda buyurtma qo'shish",
        "Qo'lda buyurtma qo'shish",
        "🏷️ Narxlarni boshqarish",
        "Narxlarni boshqarish",
        "📉 Xarajat qo'shish",
        "Xarajat qo'shish",
        "🧾 Xarajatlarni boshqarish",
        "Xarajatlarni boshqarish",
        "📊 Hisobot",
        "Hisobot",
        "Statistika",
        "🗂️ Mijozlar bazasi",
        "Mijozlar bazasi",
        "👤 Mening kabinetim",
        "Mening kabinetim",
        "🟢 Ishga keldim",
        "Ishga keldim",
        "🔴 Ishdan ketdim",
        "Ishdan ketdim",
    }
)


class MainMenuStateResetMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data: dict[str, Any]) -> Any:
        if getattr(event, "text", None) in MAIN_MENU_TEXTS:
            state = data.get("state")
            if state is not None:
                await state.clear()
        return await handler(event, data)


def normalize_uzbek_phone(value: str) -> str | None:
    digits = re.sub(r"\D", "", value)
    if len(digits) == 9:
        return f"+998{digits}"
    if len(digits) == 12 and digits.startswith("998"):
        return f"+{digits}"
    return None


def _parse_money(value: str, *, whole_only: bool = False) -> Decimal | None:
    normalized = value.strip().replace(" ", "").replace(",", ".")
    try:
        amount = Decimal(normalized)
    except InvalidOperation:
        return None
    if amount <= 0 or amount > Decimal("9999999999.99"):
        return None
    if whole_only and amount != amount.to_integral_value():
        return None
    return amount.quantize(Decimal("0.01"))


VISIT_TIME_RE = re.compile(r"^\s*([01]\d|2[0-3])[:.]([0-5]\d)\s*$")


def _parse_manual_visit_time(
    value: str,
    *,
    now: datetime | None = None,
) -> datetime | None:
    """Parse a future same-day visit time in the Tashkent timezone."""
    match = VISIT_TIME_RE.fullmatch(value)
    if not match:
        return None
    current = now.astimezone(TASHKENT) if now else datetime.now(TASHKENT)
    visit_at = current.replace(
        hour=int(match.group(1)),
        minute=int(match.group(2)),
        second=0,
        microsecond=0,
    )
    if visit_at <= current:
        return None
    return visit_at


def _format_visit_at(value: datetime | None) -> str:
    if value is None:
        return "Vaqt belgilanmagan"
    if value.tzinfo is None:
        value = value.replace(tzinfo=TASHKENT)
    return value.astimezone(TASHKENT).strftime("%d.%m.%Y %H:%M")


async def send_group_summary(
    bot,
    settings: Settings,
    title: str,
    customer: User,
    orders: list[Order],
    group_id: str,
) -> None:
    """Send a grouped order without exceeding Telegram's message limit."""
    total = sum(int(order.car_price) for order in orders)
    header = (
        f"<b>📋 {_safe(title)}</b>\n\n"
        f"<b>👤 Mijoz:</b> {_safe(customer.name)}\n"
        f"<b>📞 Telefon:</b> {_safe(customer.phone)}\n"
        f"<b>🚗 Mashinalar:</b> {len(orders)} ta\n"
        f"<b>💰 Jami:</b> {_safe(format_price(total))}"
    )
    if orders[0].visit_at is not None:
        header += (
            f"\n<b>🕔 Tashrif vaqti:</b> "
            f"{_safe(_format_visit_at(orders[0].visit_at))}"
        )
    lines = [
        f"{index}. {_safe(order.car_model)} | "
        f"{_safe(order.plate_number or 'Ishchi manzilda kiritadi')} | "
        f"{_safe(format_price(int(order.car_price)))}"
        for index, order in enumerate(orders, 1)
    ]
    full_text = header + "\n\n" + "\n".join(lines)
    if len(full_text) <= 4000:
        await bot.send_message(
            settings.director_id,
            full_text,
            reply_markup=group_mode_keyboard(group_id, orders[0].id),
        )
        return

    await bot.send_message(
        settings.director_id,
        header + "\n\nRo'yxat keyingi xabarlarda davom etadi.",
        reply_markup=group_mode_keyboard(group_id, orders[0].id),
    )
    chunk: list[str] = []
    chunk_length = 0
    for line in lines:
        if chunk and chunk_length + len(line) + 1 > 3800:
            await bot.send_message(settings.director_id, "\n".join(chunk))
            chunk = []
            chunk_length = 0
        chunk.append(line)
        chunk_length += len(line) + 1
    if chunk:
        await bot.send_message(settings.director_id, "\n".join(chunk))


def _new_router(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    scheduler: AsyncIOScheduler,
) -> Router:
    router = Router(name="ryx-wash")
    router.message.outer_middleware(MainMenuStateResetMiddleware())

    async def find_user(session: AsyncSession, telegram_id: int) -> User | None:
        return await session.scalar(
            select(User).where(User.telegram_id == telegram_id)
        )

    async def customer_or_reject(message: Message, session: AsyncSession) -> User | None:
        telegram_id = message.from_user.id if message.from_user else None
        if telegram_id is None:
            return None
        user = await find_user(session, telegram_id)
        if user is None or user.rol != "mijoz":
            await message.answer(
                "⚠️ Hozircha botda faqat mijozlar uchun buyurtma qabul qilinadi."
            )
            return None
        return user

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext) -> None:
        if not message.from_user:
            return

        telegram_id = message.from_user.id
        async with session_factory() as session:
            # Resolve the role in priority order. The director ID is authoritative,
            # followed by the workers table, then the users/customer table.
            if telegram_id == settings.director_id:
                user = await find_user(session, telegram_id)
                if user is None:
                    user = User(
                        telegram_id=telegram_id,
                        rol="direktor",
                    )
                    session.add(user)
                elif user.rol != "direktor":
                    user.rol = "direktor"
                await session.commit()
                await state.clear()
                await message.answer(
                    "👔 Direktor paneli.",
                    reply_markup=director_menu_keyboard(),
                )
                return

            worker = await session.get(Worker, telegram_id)
            if worker is not None:
                if not worker.active:
                    await state.clear()
                    await message.answer(
                        "⚠️ Sizning ishchi profilingiz hozir faol emas."
                    )
                    return
                user = await find_user(session, telegram_id)
                if user is None:
                    user = User(
                        telegram_id=telegram_id,
                        name=worker.name,
                        phone=worker.phone,
                        rol="ishchi",
                    )
                    session.add(user)
                elif user.rol != "ishchi":
                    user.rol = "ishchi"
                await session.commit()
                await state.clear()
                await message.answer(
                    "👷 Ishchi paneli.",
                    reply_markup=worker_menu_keyboard(),
                )
                return

            user = await find_user(session, telegram_id)
            if user is not None:
                if user.rol == "direktor":
                    await state.clear()
                    await message.answer(
                        "👔 Direktor paneli.",
                        reply_markup=director_menu_keyboard(),
                    )
                    return

                if user.rol == "ishchi":
                    await state.clear()
                    await message.answer(
                        "👷 Ishchi paneli.",
                        reply_markup=worker_menu_keyboard(),
                    )
                    return

                if user.rol != "mijoz":
                    await state.clear()
                    await message.answer(
                        "❌ Sizning rolingiz bazada mijoz emas. "
                        "Direktor va ishchi funksiyalari keyingi bosqichda qo'shiladi."
                    )
                    return

                # An existing customer record is enough to bypass registration.
                await state.clear()
                await message.answer(
                    "👋 RYX Wash xizmatiga xush kelibsiz.",
                    reply_markup=customer_menu_keyboard(),
                )
                return

            # No record exists in any role table: create a customer record and
            # begin the first-time registration flow.
            user = User(
                telegram_id=telegram_id,
                rol="mijoz",
            )
            session.add(user)
            await session.commit()

            await state.set_state(RegistrationStates.waiting_name)
            await message.answer(
                "👋 RYX Wash xizmatiga xush kelibsiz.\n\n"
                "📝 Ro'yxatdan o'tish uchun ism-familyangizni yozing:",
                reply_markup=ReplyKeyboardRemove(),
            )
            return

    @router.message(RegistrationStates.waiting_name, F.text)
    async def receive_name(message: Message, state: FSMContext) -> None:
        name = message.text.strip()
        if len(name) < 2 or len(name) > 150:
            await message.answer("❌ Iltimos, ism-familyangizni to'g'ri kiriting.")
            return

        async with session_factory() as session:
            user = await find_user(session, message.from_user.id)
            if user is None:
                await message.answer("⚠️ Avval /start buyrug'ini bosing.")
                return
            user.name = name
            await session.commit()

        await state.set_state(RegistrationStates.waiting_phone)
        await message.answer(
            "📞 Endi telefon raqamingizni quyidagi Telegram tugmasi orqali yuboring. "
            "⚠️ Raqamni qo'lda yozib bo'lmaydi:",
            reply_markup=contact_keyboard(),
        )

    @router.message(RegistrationStates.waiting_phone, F.contact)
    async def receive_phone(message: Message, state: FSMContext) -> None:
        contact = message.contact
        if not message.from_user or contact.user_id != message.from_user.id:
            await message.answer(
                "❌ Faqat o'zingizning telefon raqamingizni Telegram tugmasi "
                "orqali yuboring."
            )
            return

        async with session_factory() as session:
            user = await find_user(session, message.from_user.id)
            if user is None:
                await message.answer("⚠️ Avval /start buyrug'ini bosing.")
                return
            user.phone = contact.phone_number
            await session.commit()

        await state.clear()
        await message.answer(
            "✅ Ro'yxatdan o'tish yakunlandi.",
            reply_markup=customer_menu_keyboard(),
        )

    @router.message(RegistrationStates.waiting_phone, F.text)
    async def reject_manual_phone(message: Message) -> None:
        await message.answer(
            "❌ Telefon raqamini qo'lda yozmang. "
            "📞 Faqat «Telefon raqamimni yuborish» tugmasidan foydalaning.",
            reply_markup=contact_keyboard(),
        )

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        menu = customer_menu_keyboard()
        if message.from_user:
            async with session_factory() as session:
                user = await find_user(session, message.from_user.id)
                if user and user.rol == "direktor":
                    menu = director_menu_keyboard()
                elif user and user.rol == "ishchi":
                    menu = worker_menu_keyboard()
        await message.answer(
            "🚫 Joriy amal bekor qilindi.",
            reply_markup=menu,
        )

    @router.message(F.text.in_({"📋 Buyurtmalar tarixi", "Buyurtmalar tarixi"}))
    async def customer_order_history(message: Message) -> None:
        async with session_factory() as session:
            customer = await find_user(session, message.from_user.id)
            if not customer or customer.rol != "mijoz":
                await message.answer("❌ Bu bo'lim faqat mijozlar uchun.")
                return
            orders = list(
                (
                    await session.scalars(
                        select(Order)
                        .where(Order.customer_id == customer.telegram_id)
                        .order_by(Order.created_at.desc(), Order.id.desc())
                        .limit(100)
                    )
                ).all()
            )
        if not orders:
            await message.answer("📭 Sizda hali buyurtmalar yo'q.")
            return

        date_groups: dict[str, list[Order]] = {}
        for order in orders:
            date_key = order.created_at.astimezone(TASHKENT).strftime("%d.%m.%Y")
            date_groups.setdefault(date_key, []).append(order)
        parts = ["<b>📋 Buyurtmalar tarixi</b>"]
        for date_key, dated_orders in date_groups.items():
            parts.append(f"\n<b>{_safe(date_key)}</b>")
            grouped: dict[str, list[Order]] = {}
            for order in dated_orders:
                key = order.order_group_id or f"single:{order.id}"
                grouped.setdefault(key, []).append(order)
            for group_orders in grouped.values():
                if len(group_orders) > 1:
                    total = sum(int(order.car_price) for order in group_orders)
                    parts.append(
                        f"🧾 Guruh buyurtmasi — {_safe(format_price(total))}:"
                    )
                for order in reversed(group_orders):
                    status = CUSTOMER_STATUS_LABELS.get(
                        order.status,
                        f"❔ {_safe(order.status)}",
                    )
                    eta_line = (
                        f"  ⏱ Taxminiy yetib kelish: "
                        f"{_format_eta_time(order.arrival_eta_at)} gacha\n"
                        if order.arrival_eta_at is not None
                        and order.status == "yo'lda"
                        else ""
                    )
                    parts.append(
                        f"• 🚗 {_safe(order.car_model)} | "
                        f"{_safe(order.plate_number or 'Hali kiritilmagan')}\n"
                        f"  {status}\n"
                        f"{eta_line}"
                        f"  💰 {_safe(format_price(int(order.car_price)))}"
                    )
        history_text = "\n".join(parts)
        if len(history_text) > 4000:
            history_text = history_text[:3950] + "\n\n… faqat so'nggi buyurtmalar ko'rsatildi."
        await message.answer(history_text)

    async def director_allowed(user_id: int) -> bool:
        async with session_factory() as session:
            user = await find_user(session, user_id)
            return bool(user and user.rol == "direktor")

    async def require_director_message(
        message: Message,
        state: FSMContext,
    ) -> bool:
        if (
            message.from_user is not None
            and await director_allowed(message.from_user.id)
        ):
            return True
        await state.clear()
        await message.answer("❌ Bu amal faqat direktor uchun.")
        return False

    async def require_director_callback(callback: CallbackQuery) -> bool:
        if (
            callback.from_user is not None
            and await director_allowed(callback.from_user.id)
        ):
            return True
        await callback.answer("❌ Bu amal faqat direktor uchun.", show_alert=True)
        return False

    async def crm_top_customers() -> list[tuple[User, int, Decimal]]:
        paid_sum = func.coalesce(
            func.sum(
                case(
                    (Order.status == "yakunlandi", Order.car_price),
                    else_=0,
                )
            ),
            0,
        )
        order_count = func.count(Order.id)
        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(User, order_count, paid_sum)
                    .join(Order, Order.customer_id == User.telegram_id)
                    .where(User.rol == "mijoz")
                    .group_by(User.telegram_id)
                    .order_by(order_count.desc(), paid_sum.desc(), User.name)
                    .limit(15)
                )
            ).all()
        return [
            (customer, int(count or 0), Decimal(str(total or 0)))
            for customer, count, total in rows
        ]

    async def crm_customer_card(
        customer_id: int,
    ) -> tuple[str, str | None] | None:
        async with session_factory() as session:
            customer = await session.get(User, customer_id)
            if not customer or customer.rol != "mijoz":
                return None
            cars = list(
                (
                    await session.scalars(
                        select(CustomerCar)
                        .where(CustomerCar.customer_id == customer_id)
                        .order_by(CustomerCar.created_at, CustomerCar.id)
                    )
                ).all()
            )
            orders = list(
                (
                    await session.scalars(
                        select(Order)
                        .where(Order.customer_id == customer_id)
                        .order_by(Order.created_at.desc(), Order.id.desc())
                    )
                ).all()
            )

        status_labels = {
            "yakunlandi": "yakunlandi",
            "bekor_qilindi": "bekor qilindi",
        }
        lines = [
            "<b>👤 Mijoz kartochkasi</b>",
            "",
            f"<b>👤 Ism:</b> {_safe(customer.name or '—')}",
            f"<b>📞 Telefon:</b> {_safe(customer.phone or '—')}",
            "",
            "<b>🚗 Saqlangan mashinalar:</b>",
        ]
        if not cars:
            lines.append("Saqlangan mashinalar yo'q.")
        else:
            for car in cars:
                lines.append(
                    f"• {_safe(car.car_category)} / {_safe(car.model)} | "
                    f"{_safe(car.plate_number)}"
                )

        lines.extend(["", "<b>📋 Buyurtmalar tarixi:</b>"])
        if not orders:
            lines.append("Buyurtmalar yo'q.")
        else:
            for order in orders:
                order_date = order.created_at.astimezone(TASHKENT)
                status = status_labels.get(order.status, order.status)
                lines.append(
                    f"• {order_date:%d.%m.%Y} | {_safe(order.car_model)} | "
                    f"{_safe(format_price(int(order.car_price)))} | "
                    f"{_safe(status)}"
                )
        return "\n".join(lines), customer.phone

    async def send_crm_customer_card(target: Message, customer_id: int) -> None:
        card = await crm_customer_card(customer_id)
        if card is None:
            await target.answer("❌ Mijoz topilmadi.")
            return
        text, phone = card
        chunks: list[str] = []
        current = ""
        for line in text.splitlines():
            addition = line if not current else f"{current}\n{line}"
            if current and len(addition) > 3800:
                chunks.append(current)
                current = line
            else:
                current = addition
        if current:
            chunks.append(current)
        for index, chunk in enumerate(chunks):
            await target.answer(
                chunk,
                reply_markup=crm_card_keyboard(phone) if index == 0 else None,
            )

    @router.message(F.text.in_({"🗂️ Mijozlar bazasi", "Mijozlar bazasi"}))
    async def open_crm(message: Message, state: FSMContext) -> None:
        if not await director_allowed(message.from_user.id):
            await message.answer("❌ Bu bo'lim faqat direktor uchun.")
            return
        await state.clear()
        await message.answer(
            "🗂️ Mijozlar bazasi:",
            reply_markup=crm_menu_keyboard(),
        )

    @router.callback_query(F.data == "crm_menu")
    async def crm_menu(callback: CallbackQuery, state: FSMContext) -> None:
        if not await director_allowed(callback.from_user.id):
            await callback.answer("Bu amal faqat direktor uchun.", show_alert=True)
            return
        await state.clear()
        await callback.answer()
        await callback.message.answer(
            "🗂️ Mijozlar bazasi:",
            reply_markup=crm_menu_keyboard(),
        )

    @router.callback_query(F.data == "crm_top")
    async def show_crm_top(callback: CallbackQuery, state: FSMContext) -> None:
        if not await director_allowed(callback.from_user.id):
            await callback.answer("Bu amal faqat direktor uchun.", show_alert=True)
            return
        await state.clear()
        rows = await crm_top_customers()
        await callback.answer()
        if not rows:
            await callback.message.answer("📭 Buyurtma qilgan mijozlar hali yo'q.")
            return
        lines = ["<b>⭐ Eng faol mijozlar — top-15</b>", ""]
        for index, (customer, count, total) in enumerate(rows, 1):
            lines.append(
                f"<b>{index}. {_safe(customer.name or 'Nomsiz mijoz')}</b>\n"
                f"Telefon: {_safe(customer.phone or '—')}\n"
                f"Buyurtmalar: {count} ta | "
                f"Jami to'lagan: {_safe(format_price(int(total)))}"
            )
        await callback.message.answer(
            "\n\n".join(lines),
            reply_markup=crm_customers_keyboard([row[0] for row in rows]),
        )

    @router.callback_query(F.data == "crm_search")
    async def start_crm_search(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        if not await director_allowed(callback.from_user.id):
            await callback.answer("Bu amal faqat direktor uchun.", show_alert=True)
            return
        await state.set_state(CrmStates.waiting_search)
        await callback.answer()
        await callback.message.answer(
            "🔎 Mijozning ism yoki telefon raqamini kiriting:"
        )

    @router.message(CrmStates.waiting_search, F.text)
    async def search_crm_customers(message: Message, state: FSMContext) -> None:
        if not await director_allowed(message.from_user.id):
            await state.clear()
            await message.answer("❌ Bu bo'lim faqat direktor uchun.")
            return
        search_term = " ".join(message.text.split())
        if len(search_term) < 2:
            await message.answer("⚠️ Kamida 2 ta belgi kiriting.")
            return
        name_term = search_term.lower()
        phone_digits = re.sub(r"\D", "", search_term)
        conditions = [
            func.lower(User.name).contains(name_term),
            func.lower(User.phone).contains(name_term),
        ]
        if phone_digits:
            conditions.append(
                func.regexp_replace(User.phone, "[^0-9]", "", "g").contains(
                    phone_digits
                )
            )
        async with session_factory() as session:
            customers = list(
                (
                    await session.scalars(
                        select(User)
                        .where(User.rol == "mijoz", or_(*conditions))
                        .order_by(User.name, User.telegram_id)
                        .limit(30)
                    )
                ).all()
            )
        if not customers:
            await message.answer("❌ Mos mijoz topilmadi. Qidiruvni qayta kiriting.")
            return
        await state.clear()
        await message.answer(
            f"✅ {len(customers)} ta mijoz topildi:",
            reply_markup=crm_customers_keyboard(customers),
        )

    @router.callback_query(F.data.startswith("crm_customer:"))
    async def show_crm_customer(callback: CallbackQuery) -> None:
        if not await director_allowed(callback.from_user.id):
            await callback.answer("Bu amal faqat direktor uchun.", show_alert=True)
            return
        try:
            customer_id = int(callback.data.split(":", 1)[1])
        except (TypeError, ValueError):
            await callback.answer("Mijoz identifikatori noto'g'ri.", show_alert=True)
            return
        await callback.answer()
        await send_crm_customer_card(callback.message, customer_id)

    async def active_service_models(session: AsyncSession) -> list[ServiceModel]:
        return list(
            (
                await session.scalars(
                    select(ServiceModel)
                    .where(ServiceModel.active.is_(True))
                    .order_by(
                        ServiceModel.category,
                        ServiceModel.created_at,
                        ServiceModel.id,
                    )
                )
            ).all()
        )

    @router.message(F.text.in_({"🏷️ Narxlarni boshqarish", "Narxlarni boshqarish"}))
    async def price_management(message: Message, state: FSMContext) -> None:
        if not await director_allowed(message.from_user.id):
            await message.answer("❌ Bu bo'lim faqat direktor uchun.")
            return
        await state.clear()
        await message.answer(
            "🏷️ Narxlar boshqaruvi:",
            reply_markup=price_management_keyboard(),
        )

    @router.callback_query(F.data == "price_list")
    async def list_prices(callback: CallbackQuery) -> None:
        if not await director_allowed(callback.from_user.id):
            await callback.answer("Bu amal faqat direktor uchun.", show_alert=True)
            return
        async with session_factory() as session:
            models = await active_service_models(session)
        await callback.answer()
        if not models:
            await callback.message.answer("📭 Faol model va narxlar yo'q.")
            return
        chunks: list[str] = []
        current = "<b>🏷️ Amaldagi narxlar</b>"
        current_category = None
        for model in models:
            lines: list[str] = []
            if model.category != current_category:
                current_category = model.category
                lines.append(f"\n<b>{_safe(model.category)}</b>")
            lines.append(
                f"• {_safe(model.name)} — "
                f"{_safe(format_price(int(model.price)))}"
            )
            addition = "\n".join(lines)
            if len(current) + len(addition) + 1 > 3800:
                chunks.append(current)
                current = "<b>🏷️ Narxlar davomi</b>\n" + addition
            else:
                current += "\n" + addition
        chunks.append(current)
        for chunk in chunks:
            await callback.message.answer(chunk)

    @router.callback_query(F.data == "price_add")
    async def start_add_price_model(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        if not await director_allowed(callback.from_user.id):
            await callback.answer("Bu amal faqat direktor uchun.", show_alert=True)
            return
        await state.set_state(PriceManagementStates.waiting_new_category)
        await callback.answer()
        existing = ", ".join(categories()) or "hali kategoriya yo'q"
        await callback.message.answer(
            "Yangi model kategoriyasini kiriting.\n"
            f"Mavjud kategoriyalar: {_safe(existing)}"
        )

    @router.message(PriceManagementStates.waiting_new_category, F.text)
    async def receive_new_model_category(
        message: Message, state: FSMContext
    ) -> None:
        if not await director_allowed(message.from_user.id):
            await state.clear()
            await message.answer("❌ Bu amal faqat direktor uchun.")
            return
        category = " ".join(message.text.split())
        if not category or len(category) > 50:
            await message.answer("❌ Kategoriya 1–50 belgi bo'lishi kerak.")
            return
        await state.update_data(price_category=category)
        await state.set_state(PriceManagementStates.waiting_new_name)
        await message.answer("📝 Yangi model nomini kiriting:")

    @router.message(PriceManagementStates.waiting_new_name, F.text)
    async def receive_new_model_name(message: Message, state: FSMContext) -> None:
        if not await director_allowed(message.from_user.id):
            await state.clear()
            await message.answer("❌ Bu amal faqat direktor uchun.")
            return
        name = " ".join(message.text.split())
        if not name or len(name) > 100:
            await message.answer("❌ Model nomi 1–100 belgi bo'lishi kerak.")
            return
        await state.update_data(price_model_name=name)
        await state.set_state(PriceManagementStates.waiting_new_price)
        await message.answer("💰 Model narxini so'mda kiriting, masalan 70000:")

    @router.message(PriceManagementStates.waiting_new_price, F.text)
    async def receive_new_model_price(message: Message, state: FSMContext) -> None:
        if not await director_allowed(message.from_user.id):
            await state.clear()
            await message.answer("❌ Bu amal faqat direktor uchun.")
            return
        price = _parse_money(message.text, whole_only=True)
        if price is None:
            await message.answer("❌ Musbat, butun narx kiriting, masalan 70000.")
            return
        data = await state.get_data()
        category = data["price_category"]
        name = data["price_model_name"]
        async with session_factory() as session:
            duplicate = await session.scalar(
                select(ServiceModel).where(
                    ServiceModel.active.is_(True),
                    func.lower(ServiceModel.category) == category.lower(),
                    func.lower(ServiceModel.name) == name.lower(),
                )
            )
            if duplicate:
                await message.answer("⚠️ Bu kategoriya va model allaqachon mavjud.")
                return
            model_id = f"custom-{uuid4().hex}"
            service_model = ServiceModel(
                id=model_id,
                category=category,
                name=name,
                price=price,
            )
            session.add(service_model)
            await session.commit()
        add_model(CarModel(model_id, category, name, int(price)))
        await state.clear()
        await message.answer(
            f"✅ {_safe(category)} / {_safe(name)} qo'shildi: "
            f"{_safe(format_price(int(price)))}",
            reply_markup=director_menu_keyboard(),
        )

    @router.callback_query(F.data == "price_edit")
    async def choose_price_model_to_edit(callback: CallbackQuery) -> None:
        if not await director_allowed(callback.from_user.id):
            await callback.answer("Bu amal faqat direktor uchun.", show_alert=True)
            return
        async with session_factory() as session:
            models = await active_service_models(session)
        await callback.answer()
        if not models:
            await callback.message.answer("📭 O'zgartirish uchun model yo'q.")
            return
        await callback.message.answer(
            "✏️ Narxi o'zgartiriladigan modelni tanlang:",
            reply_markup=price_models_keyboard(models, "edit"),
        )

    @router.callback_query(F.data.startswith("price_edit_model:"))
    async def start_price_update(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        if not await director_allowed(callback.from_user.id):
            await callback.answer("Bu amal faqat direktor uchun.", show_alert=True)
            return
        model_id = callback.data.split(":", 1)[1]
        async with session_factory() as session:
            model = await session.get(ServiceModel, model_id)
            if not model or not model.active:
                await callback.answer("❌ Model topilmadi.", show_alert=True)
                return
        await state.update_data(price_model_id=model_id)
        await state.set_state(PriceManagementStates.waiting_updated_price)
        await callback.answer()
        await callback.message.answer(
            f"{_safe(model.category)} / {_safe(model.name)}\n"
            f"Joriy narx: {_safe(format_price(int(model.price)))}\n"
            "Yangi narxni kiriting:"
        )

    @router.message(PriceManagementStates.waiting_updated_price, F.text)
    async def update_model_price(message: Message, state: FSMContext) -> None:
        if not await director_allowed(message.from_user.id):
            await state.clear()
            await message.answer("❌ Bu amal faqat direktor uchun.")
            return
        price = _parse_money(message.text, whole_only=True)
        if price is None:
            await message.answer("❌ Musbat, butun narx kiriting.")
            return
        data = await state.get_data()
        async with session_factory() as session:
            model = await session.get(ServiceModel, data["price_model_id"])
            if not model or not model.active:
                await state.clear()
                await message.answer("❌ Model topilmadi.")
                return
            model.price = price
            await session.commit()
            model_name = model.name
        replace_model(data["price_model_id"], price=int(price))
        await state.clear()
        await message.answer(
            f"✅ {_safe(model_name)} narxi "
            f"{_safe(format_price(int(price)))} ga o'zgartirildi.",
            reply_markup=director_menu_keyboard(),
        )

    @router.callback_query(F.data == "price_delete")
    async def choose_price_model_to_delete(callback: CallbackQuery) -> None:
        if not await director_allowed(callback.from_user.id):
            await callback.answer("Bu amal faqat direktor uchun.", show_alert=True)
            return
        async with session_factory() as session:
            models = await active_service_models(session)
        await callback.answer()
        if not models:
            await callback.message.answer("📭 O'chirish uchun model yo'q.")
            return
        await callback.message.answer(
            "🗑️ O'chiriladigan modelni tanlang:",
            reply_markup=price_models_keyboard(models, "delete"),
        )

    @router.callback_query(F.data.startswith("price_delete_model:"))
    async def delete_price_model(callback: CallbackQuery) -> None:
        if not await director_allowed(callback.from_user.id):
            await callback.answer("Bu amal faqat direktor uchun.", show_alert=True)
            return
        model_id = callback.data.split(":", 1)[1]
        async with session_factory() as session:
            model = await session.get(ServiceModel, model_id)
            if not model or not model.active:
                await callback.answer("Model topilmadi.", show_alert=True)
                return
            model.active = False
            model_name = model.name
            await session.commit()
        remove_model(model_id)
        await callback.answer("✅ Model o'chirildi.")
        await callback.message.answer(
            f"✅ {_safe(model_name)} faol narxlar ro'yxatidan o'chirildi.",
            reply_markup=director_menu_keyboard(),
        )

    @router.message(F.text.in_({"📉 Xarajat qo'shish", "Xarajat qo'shish"}))
    async def start_expense(message: Message, state: FSMContext) -> None:
        if not await director_allowed(message.from_user.id):
            await message.answer("❌ Bu bo'lim faqat direktor uchun.")
            return
        await state.clear()
        await state.set_state(ExpenseStates.waiting_amount)
        await message.answer(
            "💸 Xarajat summasini so'mda kiriting:",
            reply_markup=ReplyKeyboardRemove(),
        )

    @router.message(ExpenseStates.waiting_amount, F.text)
    async def receive_expense_amount(message: Message, state: FSMContext) -> None:
        if not await director_allowed(message.from_user.id):
            await state.clear()
            await message.answer("❌ Bu amal faqat direktor uchun.")
            return
        amount = _parse_money(message.text, whole_only=True)
        if amount is None:
            await message.answer("❌ Musbat summa kiriting, masalan 150000.")
            return
        await state.update_data(expense_amount=str(amount))
        await state.set_state(ExpenseStates.waiting_description)
        await message.answer("📝 Xarajat tavsifini kiriting:")

    @router.message(ExpenseStates.waiting_description, F.text)
    async def receive_expense_description(
        message: Message, state: FSMContext
    ) -> None:
        if not await director_allowed(message.from_user.id):
            await state.clear()
            await message.answer("❌ Bu amal faqat direktor uchun.")
            return
        description = " ".join(message.text.split())
        if not description or len(description) > 500:
            await message.answer("❌ Tavsif 1–500 belgi bo'lishi kerak.")
            return
        data = await state.get_data()
        amount = Decimal(data["expense_amount"])
        async with session_factory() as session:
            session.add(
                Expense(
                    amount=amount,
                    description=description,
                    spent_at=datetime.now(TASHKENT),
                    created_by=message.from_user.id,
                )
            )
            await session.commit()
        await state.clear()
        await message.answer(
            f"✅ Xarajat saqlandi: {_safe(format_price(int(amount)))}\n"
            f"Tavsif: {_safe(description)}",
            reply_markup=director_menu_keyboard(),
        )

    @router.message(
        F.text.in_({"🧾 Xarajatlarni boshqarish", "Xarajatlarni boshqarish"})
    )
    async def manage_expenses(message: Message, state: FSMContext) -> None:
        if not await director_allowed(message.from_user.id):
            await message.answer("❌ Bu bo'lim faqat direktor uchun.")
            return
        await state.clear()
        async with session_factory() as session:
            expenses = list(
                (
                    await session.scalars(
                        select(Expense)
                        .order_by(Expense.spent_at.desc(), Expense.id.desc())
                        .limit(20)
                    )
                ).all()
            )
        if not expenses:
            await message.answer(
                "📭 Hali xarajatlar kiritilmagan.",
                reply_markup=director_menu_keyboard(),
            )
            return
        lines = ["<b>🧾 Xarajatlarni boshqarish</b>", ""]
        for expense in expenses:
            date_text = expense.spent_at.astimezone(TASHKENT).strftime("%d.%m.%Y")
            lines.append(
                f"• {_safe(date_text)} | "
                f"{_safe(format_price(int(expense.amount)))} | "
                f"{_safe(expense.description)}"
            )
        await message.answer(
            "\n".join(lines),
            reply_markup=expense_items_keyboard(expenses),
        )

    @router.callback_query(F.data.startswith("expense_edit:"))
    async def start_expense_edit(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        if not await director_allowed(callback.from_user.id):
            await callback.answer("Bu amal faqat direktor uchun.", show_alert=True)
            return
        try:
            expense_id = int(callback.data.split(":", 1)[1])
        except (TypeError, ValueError):
            await callback.answer("Xarajat identifikatori noto'g'ri.", show_alert=True)
            return
        async with session_factory() as session:
            expense = await session.get(Expense, expense_id)
        if expense is None:
            await callback.answer("Xarajat topilmadi.", show_alert=True)
            return
        await state.clear()
        await state.update_data(expense_id=expense_id)
        await state.set_state(ExpenseStates.waiting_edit_amount)
        await callback.answer()
        await callback.message.answer(
            f"✏️ Joriy summa: <b>{_safe(format_price(int(expense.amount)))}</b>\n"
            "Yangi summani kiriting:"
        )

    @router.message(ExpenseStates.waiting_edit_amount, F.text)
    async def receive_expense_edit_amount(
        message: Message, state: FSMContext
    ) -> None:
        if not await director_allowed(message.from_user.id):
            await state.clear()
            await message.answer("❌ Bu amal faqat direktor uchun.")
            return
        amount = _parse_money(message.text, whole_only=True)
        if amount is None:
            await message.answer("❌ Musbat, butun summa kiriting.")
            return
        await state.update_data(expense_amount=str(amount))
        await state.set_state(ExpenseStates.waiting_edit_description)
        await message.answer("📝 Yangi tavsifni kiriting:")

    @router.message(ExpenseStates.waiting_edit_description, F.text)
    async def receive_expense_edit_description(
        message: Message, state: FSMContext
    ) -> None:
        if not await director_allowed(message.from_user.id):
            await state.clear()
            await message.answer("❌ Bu amal faqat direktor uchun.")
            return
        description = " ".join(message.text.split())
        if not description or len(description) > 500:
            await message.answer("❌ Tavsif 1–500 belgi bo'lishi kerak.")
            return
        data = await state.get_data()
        expense_id = data.get("expense_id")
        if not isinstance(expense_id, int):
            await state.clear()
            await message.answer("❌ Xarajat ma'lumoti topilmadi.")
            return
        async with session_factory() as session:
            expense = await session.get(Expense, expense_id)
            if expense is None:
                await state.clear()
                await message.answer("❌ Xarajat topilmadi.")
                return
            expense.amount = Decimal(data["expense_amount"])
            expense.description = description
            await session.commit()
        await state.clear()
        await message.answer(
            f"✅ Xarajat yangilandi: "
            f"{_safe(format_price(int(Decimal(data['expense_amount']))))}\n"
            f"Tavsif: {_safe(description)}",
            reply_markup=director_menu_keyboard(),
        )

    @router.callback_query(F.data.startswith("expense_delete:"))
    async def ask_delete_expense(callback: CallbackQuery) -> None:
        if not await director_allowed(callback.from_user.id):
            await callback.answer("Bu amal faqat direktor uchun.", show_alert=True)
            return
        try:
            expense_id = int(callback.data.split(":", 1)[1])
        except (TypeError, ValueError):
            await callback.answer("Xarajat identifikatori noto'g'ri.", show_alert=True)
            return
        async with session_factory() as session:
            expense = await session.get(Expense, expense_id)
        if expense is None:
            await callback.answer("Xarajat topilmadi.", show_alert=True)
            return
        await callback.answer()
        await callback.message.answer(
            f"🗑️ {_safe(format_price(int(expense.amount)))} — "
            f"{_safe(expense.description)}\n"
            "Ushbu xarajatni o‘chirishni tasdiqlaysizmi?",
            reply_markup=expense_delete_confirm_keyboard(expense_id),
        )

    @router.callback_query(F.data.startswith("expense_delete_confirm:"))
    async def delete_expense(callback: CallbackQuery) -> None:
        if not await director_allowed(callback.from_user.id):
            await callback.answer("Bu amal faqat direktor uchun.", show_alert=True)
            return
        try:
            expense_id = int(callback.data.split(":", 1)[1])
        except (TypeError, ValueError):
            await callback.answer("Xarajat identifikatori noto'g'ri.", show_alert=True)
            return
        async with session_factory() as session:
            expense = await session.get(Expense, expense_id)
            if expense is None:
                await callback.answer("Xarajat topilmadi.", show_alert=True)
                return
            description = expense.description
            await session.delete(expense)
            await session.commit()
        await callback.answer("✅ Xarajat o'chirildi.")
        await callback.message.answer(
            f"✅ Xarajat o‘chirildi: {_safe(description)}",
            reply_markup=director_menu_keyboard(),
        )

    @router.callback_query(F.data == "expense_delete_cancel")
    async def cancel_delete_expense(callback: CallbackQuery) -> None:
        if not await director_allowed(callback.from_user.id):
            await callback.answer("Bu amal faqat direktor uchun.", show_alert=True)
            return
        await callback.answer("O‘chirish bekor qilindi.")

    async def ask_report_worker(message: Message, state: FSMContext) -> None:
        async with session_factory() as session:
            workers = list(
                (
                    await session.scalars(select(Worker).order_by(Worker.name))
                ).all()
            )
        await state.set_state(ReportStates.waiting_worker)
        await message.answer(
            "📊 Hisobot uchun ishchi filtrini tanlang:",
            reply_markup=report_workers_keyboard(workers),
        )

    @router.message(F.text.in_({"📊 Hisobot", "Hisobot", "Statistika"}))
    async def start_report(message: Message, state: FSMContext) -> None:
        if not await director_allowed(message.from_user.id):
            await message.answer("❌ Bu bo'lim faqat direktor uchun.")
            return
        await state.clear()
        await message.answer(
            "📊 Hisobot davrini tanlang:",
            reply_markup=report_period_keyboard(),
        )

    @router.callback_query(F.data.startswith("report_period:"))
    async def choose_report_period(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        if not await director_allowed(callback.from_user.id):
            await callback.answer("Bu amal faqat direktor uchun.", show_alert=True)
            return
        period = callback.data.split(":", 1)[1]
        await callback.answer()
        if period == "custom":
            await state.set_state(ReportStates.waiting_custom_range)
            await callback.message.answer(
                "🗓️ Sana oralig'ini quyidagi formatda kiriting:\n"
                "<code>01.08.2026 - 30.08.2026</code>"
            )
            return
        try:
            start, end = period_bounds(period)
        except ValueError:
            await callback.message.answer("❌ Hisobot davri topilmadi.")
            return
        await state.update_data(report_start=start.isoformat(), report_end=end.isoformat())
        await ask_report_worker(callback.message, state)

    @router.message(ReportStates.waiting_custom_range, F.text)
    async def receive_custom_report_range(
        message: Message, state: FSMContext
    ) -> None:
        if not await director_allowed(message.from_user.id):
            await state.clear()
            await message.answer("❌ Bu amal faqat direktor uchun.")
            return
        match = CUSTOM_RANGE_RE.fullmatch(message.text)
        if not match:
            await message.answer(
                "❌ Format noto'g'ri. Masalan: 01.08.2026 - 30.08.2026"
            )
            return
        try:
            start = datetime.strptime(match.group(1), "%d.%m.%Y").replace(
                tzinfo=TASHKENT
            )
            last_day = datetime.strptime(match.group(2), "%d.%m.%Y").replace(
                tzinfo=TASHKENT
            )
        except ValueError:
            await message.answer("❌ Sanalardan biri noto'g'ri.")
            return
        if last_day < start:
            await message.answer("⚠️ Tugash sanasi boshlanish sanasidan oldin bo'lmasin.")
            return
        end = last_day + timedelta(days=1)
        await state.update_data(report_start=start.isoformat(), report_end=end.isoformat())
        await ask_report_worker(message, state)

    @router.callback_query(
        ReportStates.waiting_worker,
        F.data.startswith("report_worker:"),
    )
    async def generate_report(callback: CallbackQuery, state: FSMContext) -> None:
        if not await director_allowed(callback.from_user.id):
            await callback.answer("Bu amal faqat direktor uchun.", show_alert=True)
            return
        worker_value = callback.data.split(":", 1)[1]
        worker_id = None if worker_value == "all" else int(worker_value)
        data = await state.get_data()
        start = datetime.fromisoformat(data["report_start"])
        end = datetime.fromisoformat(data["report_end"])
        async with session_factory() as session:
            if worker_id is not None and await session.get(Worker, worker_id) is None:
                await callback.answer("❌ Ishchi topilmadi.", show_alert=True)
                return
            chunks = await build_financial_report(
                session,
                start,
                end,
                worker_id=worker_id,
            )
        await callback.answer()
        await state.clear()
        for chunk in chunks:
            await callback.message.answer(chunk)
        await callback.message.answer(
            "Hisobot tayyor.",
            reply_markup=director_menu_keyboard(),
        )

    @router.message(
        F.text.in_({"📝 Qo'lda buyurtma qo'shish", "Qo'lda buyurtma qo'shish"})
    )
    async def start_manual_order(message: Message, state: FSMContext) -> None:
        async with session_factory() as session:
            director = await find_user(session, message.from_user.id)
            if not director or director.rol != "direktor":
                await message.answer("❌ Bu funksiya faqat direktor uchun.")
                return
        await state.clear()
        await state.set_state(ManualOrderStates.waiting_customer_name)
        await message.answer(
            "👤 Mijozning ism-familyasini kiriting:",
            reply_markup=ReplyKeyboardRemove(),
        )

    @router.message(ManualOrderStates.waiting_customer_name, F.text)
    async def manual_customer_name(message: Message, state: FSMContext) -> None:
        if not await require_director_message(message, state):
            return
        name = " ".join(message.text.split())
        if len(name) < 2 or len(name) > 150:
            await message.answer("❌ Ism-familya 2–150 belgi bo'lishi kerak.")
            return
        await state.update_data(customer_name=name)
        await state.set_state(ManualOrderStates.waiting_customer_phone)
        await message.answer(
            "📞 Mijoz telefonini kiriting:\n"
            "Masalan: <code>901234567</code> yoki <code>998901234567</code>",
            parse_mode=ParseMode.HTML,
        )

    @router.message(ManualOrderStates.waiting_customer_phone, F.text)
    async def manual_customer_phone(message: Message, state: FSMContext) -> None:
        if not await require_director_message(message, state):
            return
        phone = normalize_uzbek_phone(message.text)
        if phone is None:
            await message.answer(
                "❌ Telefon raqamini 9 raqamli ko‘rinishda "
                "(901234567) yoki 998 kodi bilan (998901234567) kiriting."
            )
            return
        data = await state.get_data()
        async with session_factory() as session:
            customer = await session.scalar(
                select(User)
                .where(
                    func.regexp_replace(User.phone, "[^0-9]", "", "g")
                    == phone.lstrip("+"),
                    User.rol == "mijoz",
                )
                .order_by(User.created_at, User.telegram_id)
                .limit(1)
            )
            if customer is None:
                lowest_id = await session.scalar(select(func.min(User.telegram_id)))
                synthetic_id = (
                    -1 if lowest_id is None or lowest_id >= 0 else lowest_id - 1
                )
                customer = User(
                    telegram_id=synthetic_id,
                    name=data["customer_name"],
                    phone=phone,
                    rol="mijoz",
                )
                session.add(customer)
                await session.flush()
            else:
                customer.name = data["customer_name"]
                customer.phone = phone
            cars = list(
                (
                    await session.scalars(
                        select(CustomerCar)
                        .where(CustomerCar.customer_id == customer.telegram_id)
                        .order_by(CustomerCar.created_at, CustomerCar.id)
                    )
                ).all()
            )
            customer_id = customer.telegram_id
            await session.commit()
        await state.update_data(customer_phone=phone, customer_id=customer_id)
        if cars:
            await state.set_state(ManualOrderStates.waiting_car_choice)
            await message.answer(
                "🚗 Mijozning saqlangan mashinasini tanlang yoki yangi mashina qo'shing:",
                reply_markup=saved_cars_keyboard(cars, "manual"),
            )
        else:
            await state.set_state(ManualOrderStates.waiting_new_category)
            await message.answer(
                "🚗 Mashina kategoriyasini tanlang:",
                reply_markup=manual_category_keyboard(),
            )

    async def append_manual_car(
        state: FSMContext,
        car: dict,
    ) -> None:
        data = await state.get_data()
        cars = list(data.get("cars", []))
        cars.append(
            {
                "car_category": car["car_category"],
                "car_model": car["car_model"],
                "car_price": car["car_price"],
                "plate_number": car.get("plate_number"),
                "color": None,
                "car_photo_id": None,
            }
        )
        await state.update_data(cars=cars, current_car=None)

    @router.callback_query(
        ManualOrderStates.waiting_car_choice,
        F.data.startswith("manual_saved_car:"),
    )
    async def choose_saved_manual_car(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        if not await require_director_callback(callback):
            return
        try:
            car_id = int(callback.data.split(":", 1)[1])
        except (AttributeError, IndexError, TypeError, ValueError):
            await callback.answer("❌ Mashina tugmasi eskirgan.", show_alert=True)
            return
        data = await state.get_data()
        async with session_factory() as session:
            car = await session.get(CustomerCar, car_id)
            if not car or car.customer_id != data.get("customer_id"):
                await callback.answer("❌ Mashina topilmadi.", show_alert=True)
                return
        model = get_model_by_name(car.car_category, car.model)
        if model is None:
            await callback.answer(
                "❌ Bu mashina katalogda topilmadi. Yangi mashina qo'shing.",
                show_alert=True,
            )
            return
        await state.update_data(
            current_car={
                "car_category": car.car_category,
                "car_model": car.model,
                "car_price": model.price,
                "plate_number": None,
            }
        )
        await append_manual_car(
            state,
            {
                "car_category": car.car_category,
                "car_model": car.model,
                "car_price": model.price,
                "plate_number": None,
            },
        )
        await state.set_state(ManualOrderStates.waiting_next_car)
        await callback.answer()
        await callback.message.edit_text(
            f"✅ {_safe(car.model)} ({_safe(car.plate_number)}) tanlandi."
        )
        await callback.message.answer(
            "✅ Mashina buyurtmaga qo'shildi. Yana mashina qo'shasizmi?",
            reply_markup=next_car_keyboard("manual"),
        )

    @router.callback_query(
        ManualOrderStates.waiting_car_choice,
        F.data.startswith("manual_cars_page:"),
    )
    async def page_manual_cars(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        if not await require_director_callback(callback):
            return
        try:
            page = int(callback.data.split(":", 1)[1])
        except (AttributeError, IndexError, TypeError, ValueError):
            await callback.answer("❌ Sahifa tugmasi eskirgan.", show_alert=True)
            return
        data = await state.get_data()
        async with session_factory() as session:
            cars = list(
                (
                    await session.scalars(
                        select(CustomerCar)
                        .where(CustomerCar.customer_id == data["customer_id"])
                        .order_by(CustomerCar.created_at, CustomerCar.id)
                    )
                ).all()
            )
        await callback.message.edit_reply_markup(
            reply_markup=saved_cars_keyboard(cars, "manual", page=page)
        )
        await callback.answer()

    @router.callback_query(
        ManualOrderStates.waiting_car_choice,
        F.data == "manual_new_car",
    )
    async def choose_new_manual_car(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        if not await require_director_callback(callback):
            return
        await state.set_state(ManualOrderStates.waiting_new_category)
        await callback.answer()
        await callback.message.edit_text(
            "🚗 Yangi mashina kategoriyasini tanlang:",
            reply_markup=manual_category_keyboard(),
        )

    @router.callback_query(
        ManualOrderStates.waiting_new_category,
        F.data.startswith("manual_category:"),
    )
    async def manual_car_category(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        if not await require_director_callback(callback):
            return
        category = callback.data.split(":", 1)[1]
        if category not in categories():
            await callback.answer("❌ Kategoriya topilmadi.", show_alert=True)
            return
        await state.update_data(car_category=category)
        await state.set_state(ManualOrderStates.waiting_new_model)
        await callback.answer()
        await callback.message.edit_text(
            f"🚗 {_safe(category)} kategoriyasidan modelni tanlang:",
            reply_markup=manual_model_keyboard(category),
        )

    @router.callback_query(
        ManualOrderStates.waiting_new_model,
        F.data.startswith("manual_model:"),
    )
    async def manual_car_model(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        if not await require_director_callback(callback):
            return
        model = get_model(callback.data.split(":", 1)[1])
        if model is None:
            await callback.answer("❌ Model topilmadi.", show_alert=True)
            return
        data = await state.get_data()
        await append_manual_car(
            state,
            {
                "car_category": data["car_category"],
                "car_model": model.name,
                "car_price": model.price,
                "plate_number": None,
            },
        )
        await state.set_state(ManualOrderStates.waiting_next_car)
        await callback.answer()
        await callback.message.edit_text(
            f"✅ Tanlangan model: <b>{_safe(model.name)}</b>\n"
            f"💰 Narxi: <b>{_safe(format_price(model.price))}</b>",
            parse_mode=ParseMode.HTML,
        )
        await callback.message.answer(
            "✅ Mashina buyurtmaga qo'shildi. Yana mashina qo'shasizmi?",
            reply_markup=next_car_keyboard("manual"),
        )

    @router.callback_query(
        ManualOrderStates.waiting_next_car,
        F.data == "manual_add_car",
    )
    async def add_manual_car(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        if not await require_director_callback(callback):
            return
        data = await state.get_data()
        async with session_factory() as session:
            cars = list(
                (
                    await session.scalars(
                        select(CustomerCar)
                        .where(CustomerCar.customer_id == data["customer_id"])
                        .order_by(CustomerCar.created_at, CustomerCar.id)
                    )
                ).all()
            )
        await state.set_state(ManualOrderStates.waiting_car_choice)
        await callback.answer()
        await callback.message.edit_text(
            "Mavjud mashinadan tanlang yoki yangi mashina qo'shing:",
            reply_markup=saved_cars_keyboard(cars, "manual"),
        )

    @router.callback_query(
        ManualOrderStates.waiting_next_car,
        F.data == "manual_finish_cars",
    )
    async def finish_manual_cars(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        if not await require_director_callback(callback):
            return
        data = await state.get_data()
        if not data.get("cars"):
            await callback.answer("⚠️ Avval mashina qo'shing.", show_alert=True)
            return
        await state.set_state(ManualOrderStates.waiting_visit_time)
        await callback.answer()
        await callback.message.edit_text("Mashinalar tanlandi.")
        await callback.message.answer(
            "🕔 Mijoz manziliga bugun qaysi vaqtda borish kerak?\n"
            "Soatni <code>HH:MM</code> formatida kiriting, masalan: <code>17:00</code>.\n"
            "Faqat hali o'tmagan bugungi vaqtni kiriting:",
            parse_mode=ParseMode.HTML,
        )

    @router.message(ManualOrderStates.waiting_visit_time, F.text)
    async def manual_visit_time(message: Message, state: FSMContext) -> None:
        if not await require_director_message(message, state):
            return
        visit_at = _parse_manual_visit_time(message.text)
        if visit_at is None:
            await message.answer(
                "❌ Vaqt noto'g'ri yoki o'tib ketgan. "
                "Bugungi kelajakdagi vaqtni <code>HH:MM</code> formatida "
                "kiriting, masalan: <code>17:00</code>.",
                parse_mode=ParseMode.HTML,
            )
            return
        await state.update_data(visit_at=visit_at.isoformat())
        await state.set_state(ManualOrderStates.waiting_location)
        await message.answer(
            f"✅ Tashrif vaqti: <b>{_safe(_format_visit_at(visit_at))}</b>\n"
            "Lokatsiyani Telegram tugmasi orqali yuboring yoki manzilni "
            "matn qilib yozish variantini tanlang:",
            parse_mode=ParseMode.HTML,
            reply_markup=manual_location_keyboard(),
        )

    @router.message(
        ManualOrderStates.waiting_location,
        F.text.in_({"📝 Manzilni matn qilib yozish", "Manzilni matn qilib yozish"}),
    )
    async def manual_choose_address(message: Message, state: FSMContext) -> None:
        if not await require_director_message(message, state):
            return
        await state.set_state(ManualOrderStates.waiting_address)
        await message.answer(
            "Mijoz manzilini matn qilib kiriting:",
            reply_markup=ReplyKeyboardRemove(),
        )

    @router.message(ManualOrderStates.waiting_location, F.location)
    async def manual_location(message: Message, state: FSMContext) -> None:
        if not await require_director_message(message, state):
            return
        await state.update_data(
            latitude=message.location.latitude,
            longitude=message.location.longitude,
            address=None,
        )
        await state.set_state(ManualOrderStates.waiting_comment)
        await message.answer(
            "Ixtiyoriy izoh yozing yoki o'tkazib yuboring:",
            reply_markup=skip_comment_keyboard(),
        )

    @router.message(ManualOrderStates.waiting_location)
    async def manual_require_location_choice(
        message: Message, state: FSMContext
    ) -> None:
        if not await require_director_message(message, state):
            return
        await message.answer(
            "Lokatsiya tugmasini bosing yoki «Manzilni matn qilib yozish» "
            "variantini tanlang.",
            reply_markup=manual_location_keyboard(),
        )

    @router.message(ManualOrderStates.waiting_address, F.text)
    async def manual_address(message: Message, state: FSMContext) -> None:
        if not await require_director_message(message, state):
            return
        address = " ".join(message.text.split())
        if not address or len(address) > 2000:
            await message.answer(
                "Manzil bo'sh bo'lmasin va 2000 belgidan oshmasin."
            )
            return
        await state.update_data(latitude=None, longitude=None, address=address)
        await state.set_state(ManualOrderStates.waiting_comment)
        await message.answer(
            "Ixtiyoriy izoh yozing yoki o'tkazib yuboring:",
            reply_markup=skip_comment_keyboard(),
        )

    async def save_manual_and_notify(
        message: Message, state: FSMContext, comment: str | None
    ) -> None:
        data = await state.get_data()
        customer_id = data.get("customer_id")
        cars = data.get("cars")
        if not isinstance(customer_id, int) or not isinstance(cars, list) or not cars:
            await state.clear()
            await message.answer(
                "❌ Qo'lda buyurtma ma'lumotlari to'liq emas. Qaytadan boshlang.",
                reply_markup=director_menu_keyboard(),
            )
            return
        async with session_factory() as session:
            customer = await session.get(User, customer_id)
            if customer is None:
                await message.answer("❌ Mijoz topilmadi. Qayta boshlang.")
                return
            visit_at_raw = data.get("visit_at")
            try:
                visit_at = (
                    datetime.fromisoformat(visit_at_raw)
                    if isinstance(visit_at_raw, str)
                    else None
                )
            except ValueError:
                visit_at = None
            if visit_at is None:
                await message.answer(
                    "❌ Tashrif vaqti topilmadi. Qo'lda buyurtmani qaytadan boshlang."
                )
                return
            group_id = str(uuid4()) if len(cars) > 1 else None
            orders: list[Order] = []
            for car in cars:
                order = Order(
                    customer_id=customer.telegram_id,
                    car_category=car["car_category"],
                    car_model=car["car_model"],
                    car_price=Decimal(str(car["car_price"])),
                    plate_number=car["plate_number"],
                    car_color=None,
                    payment_method=None,
                    visit_at=visit_at,
                    latitude=(
                        Decimal(str(data["latitude"]))
                        if data.get("latitude") is not None
                        else None
                    ),
                    longitude=(
                        Decimal(str(data["longitude"]))
                        if data.get("longitude") is not None
                        else None
                    ),
                    address=data.get("address"),
                    car_photo_id=None,
                    comment=comment,
                    order_group_id=group_id,
                    status="yangi",
                )
                session.add(order)
                orders.append(order)
            await session.commit()
            for order in orders:
                await session.refresh(order)

            bot = message.bot
            if len(orders) == 1:
                order = orders[0]
                director_text = (
                    f"<b>📝 Qo'lda kiritilgan buyurtma #{order.id}</b>\n\n"
                    f"<b>👤 Mijoz:</b> {_safe(customer.name)}\n"
                    f"<b>📞 Telefon:</b> {_safe(customer.phone)}\n"
                    f"<b>🚗 Kategoriya:</b> {_safe(order.car_category)}\n"
                    f"<b>🚗 Model:</b> {_safe(order.car_model)}\n"
                    f"<b>🪪 Davlat raqami:</b> {_safe(order.plate_number or 'Ishchi manzilda kiritadi')}\n"
                    f"<b>💰 Narx:</b> {_safe(format_price(int(order.car_price)))}\n"
                    "<b>💳 To'lov:</b> Ishchi mijoz oldida aniqlaydi\n"
                    f"<b>🕔 Tashrif vaqti:</b> {_safe(_format_visit_at(order.visit_at))}\n"
                    f"<b>📍 Manzil:</b> {_safe(order.address or 'Telegram lokatsiyasi')}\n"
                    f"<b>📝 Izoh:</b> {_safe(order.comment or '—')}"
                )
                await bot.send_message(
                    settings.director_id,
                    director_text,
                    reply_markup=new_order_assignment_keyboard(order.id),
                )
            else:
                await send_group_summary(
                    bot,
                    settings,
                    "Qo'lda kiritilgan guruh buyurtmasi",
                    customer,
                    orders,
                    group_id,
                )
            if orders[0].latitude is not None and orders[0].longitude is not None:
                await bot.send_location(
                    settings.director_id,
                    latitude=float(orders[0].latitude),
                    longitude=float(orders[0].longitude),
                )
            else:
                await bot.send_message(
                    settings.director_id,
                    f"<b>📍 Qo'lda kiritilgan manzil:</b> {_safe(orders[0].address)}",
                    parse_mode=ParseMode.HTML,
                )

        await state.clear()
        await message.answer(
            (
                f"{len(orders)} ta mashina uchun buyurtma yaratildi. "
                "Endi taqsimlash usulini tanlang."
            ),
            reply_markup=director_menu_keyboard(),
        )

    @router.message(ManualOrderStates.waiting_comment, F.text)
    async def manual_comment(message: Message, state: FSMContext) -> None:
        if not await require_director_message(message, state):
            return
        comment = (
            None
            if message.text.strip() in {"⏭️ O'tkazib yuborish", "O'tkazib yuborish"}
            else message.text.strip()
        )
        if comment and len(comment) > 2000:
            await message.answer("❌ Izoh 2000 belgidan oshmasin. Qaytadan kiriting:")
            return
        await save_manual_and_notify(message, state, comment)

    @router.message(F.text.in_({"🚗➕ Yangi buyurtma", "Yangi buyurtma"}))
    async def new_order(message: Message, state: FSMContext) -> None:
        async with session_factory() as session:
            customer = await customer_or_reject(message, session)
            if customer is None:
                return
            cars = list(
                (
                    await session.scalars(
                        select(CustomerCar)
                        .where(CustomerCar.customer_id == customer.telegram_id)
                        .order_by(CustomerCar.created_at, CustomerCar.id)
                    )
                ).all()
            )
        await state.clear()
        if cars:
            await state.set_state(OrderStates.waiting_car_choice)
            await message.answer(
                "Saqlangan mashinangiz bor. Mavjud mashinadan tanlang "
                "yoki yangi mashina qo'shing:",
                reply_markup=saved_cars_keyboard(cars, "customer"),
            )
        else:
            await state.set_state(OrderStates.waiting_car_category)
            await message.answer(
                "Mashina kategoriyasini tanlang:",
                reply_markup=category_keyboard(),
            )

    async def append_customer_car(
        state: FSMContext,
        car_category: str,
        car_model: str,
        car_price: int,
        plate_number: str,
        color: str | None,
    ) -> None:
        data = await state.get_data()
        cars = list(data.get("cars", []))
        cars.append(
            {
                "car_category": car_category,
                "car_model": car_model,
                "car_price": car_price,
                "plate_number": plate_number,
                "color": color,
            }
        )
        await state.update_data(cars=cars)

    @router.callback_query(
        OrderStates.waiting_car_choice,
        F.data.startswith("customer_saved_car:"),
    )
    async def choose_saved_customer_car(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        car_id = int(callback.data.split(":", 1)[1])
        async with session_factory() as session:
            car = await session.get(CustomerCar, car_id)
            if not car or car.customer_id != callback.from_user.id:
                await callback.answer("❌ Mashina topilmadi.", show_alert=True)
                return
        model = get_model_by_name(car.car_category, car.model)
        if model is None:
            await callback.answer(
                "Bu mashina katalogda topilmadi. Yangi mashina qo'shing.",
                show_alert=True,
            )
            return
        await append_customer_car(
            state,
            car.car_category,
            car.model,
            model.price,
            car.plate_number,
            car.color,
        )
        await state.set_state(OrderStates.waiting_next_car)
        await callback.answer()
        await callback.message.edit_text(
            f"{_safe(car.model)} ({_safe(car.plate_number)}) tanlandi.",
            reply_markup=next_car_keyboard("customer"),
        )

    @router.callback_query(
        OrderStates.waiting_car_choice,
        F.data.startswith("customer_cars_page:"),
    )
    async def page_customer_cars(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        page = int(callback.data.split(":", 1)[1])
        async with session_factory() as session:
            cars = list(
                (
                    await session.scalars(
                        select(CustomerCar)
                        .where(CustomerCar.customer_id == callback.from_user.id)
                        .order_by(CustomerCar.created_at, CustomerCar.id)
                    )
                ).all()
            )
        await callback.message.edit_reply_markup(
            reply_markup=saved_cars_keyboard(cars, "customer", page=page)
        )
        await callback.answer()

    @router.callback_query(
        OrderStates.waiting_car_choice,
        F.data == "customer_new_car",
    )
    async def choose_new_customer_car(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        await state.set_state(OrderStates.waiting_car_category)
        await callback.answer()
        await callback.message.edit_text(
            "Yangi mashina kategoriyasini tanlang:",
            reply_markup=category_keyboard(),
        )

    @router.callback_query(
        OrderStates.waiting_car_category,
        F.data.startswith("category:"),
    )
    async def choose_category(callback: CallbackQuery, state: FSMContext) -> None:
        category = callback.data.split(":", 1)[1]
        if category not in categories():
            await callback.answer("❌ Kategoriya topilmadi.", show_alert=True)
            return
        await state.update_data(car_category=category)
        await state.set_state(OrderStates.waiting_car_model)
        await callback.answer()
        await callback.message.edit_text(
            f"{_safe(category)} kategoriyasidan modelni tanlang:",
            reply_markup=model_keyboard(category),
        )

    @router.callback_query(
        OrderStates.waiting_car_model,
        F.data.startswith("model:"),
    )
    async def choose_model(callback: CallbackQuery, state: FSMContext) -> None:
        model = get_model(callback.data.split(":", 1)[1])
        if model is None:
            await callback.answer("❌ Model topilmadi.", show_alert=True)
            return

        await state.update_data(
            car_model=model.name,
            car_price=model.price,
        )
        await state.set_state(OrderStates.waiting_plate)
        await callback.answer()
        await callback.message.edit_text(
            f"Tanlangan model: <b>{_safe(model.name)}</b>\n"
            f"Narxi: <b>{_safe(format_price(model.price))}</b>\n\n"
            "Mashina davlat raqamini kiriting:",
            parse_mode=ParseMode.HTML,
        )

    @router.message(OrderStates.waiting_plate, F.text)
    async def receive_plate(message: Message, state: FSMContext) -> None:
        plate = " ".join(message.text.split()).upper()
        if not plate or len(plate) > 30:
            await message.answer("❌ Davlat raqami majburiy. Qaytadan kiriting:")
            return

        await state.update_data(plate_number=plate)
        await state.set_state(OrderStates.waiting_car_color)
        await message.answer(
            "Mashina rangini yozing yoki o'tkazib yuboring (ixtiyoriy):",
            reply_markup=skip_comment_keyboard(),
        )

    @router.message(OrderStates.waiting_car_color, F.text)
    async def receive_car_color(message: Message, state: FSMContext) -> None:
        color = (
            None
            if message.text.strip() in {"⏭️ O'tkazib yuborish", "O'tkazib yuborish"}
            else message.text.strip()
        )
        if color and len(color) > 50:
            await message.answer("❌ Rang 50 belgidan oshmasin.")
            return
        data = await state.get_data()
        async with session_factory() as session:
            customer = await find_user(session, message.from_user.id)
            if not customer:
                await message.answer("❌ Mijoz ma'lumotlari topilmadi.")
                return
            car = CustomerCar(
                customer_id=customer.telegram_id,
                car_category=data["car_category"],
                model=data["car_model"],
                plate_number=data["plate_number"],
                color=color,
            )
            session.add(car)
            await session.commit()
        await append_customer_car(
            state,
            data["car_category"],
            data["car_model"],
            data["car_price"],
            data["plate_number"],
            color,
        )
        await state.set_state(OrderStates.waiting_next_car)
        await message.answer(
            "Mashina buyurtmaga qo'shildi. Yana mashina qo'shasizmi?",
            reply_markup=next_car_keyboard("customer"),
        )

    @router.callback_query(
        OrderStates.waiting_next_car,
        F.data == "customer_add_car",
    )
    async def add_customer_car(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        await state.set_state(OrderStates.waiting_car_choice)
        async with session_factory() as session:
            cars = list(
                (
                    await session.scalars(
                        select(CustomerCar)
                        .where(CustomerCar.customer_id == callback.from_user.id)
                        .order_by(CustomerCar.created_at, CustomerCar.id)
                    )
                ).all()
            )
        await callback.answer()
        await callback.message.edit_text(
            "Mavjud mashinadan tanlang yoki yangi mashina qo'shing:",
            reply_markup=saved_cars_keyboard(cars, "customer"),
        )

    @router.callback_query(
        OrderStates.waiting_next_car,
        F.data == "customer_finish_cars",
    )
    async def finish_customer_cars(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        data = await state.get_data()
        if not data.get("cars"):
            await callback.answer("⚠️ Avval mashina tanlang.", show_alert=True)
            return
        await state.set_state(OrderStates.waiting_payment)
        await callback.answer()
        await callback.message.edit_text("Mashinalar tanlandi.")
        await callback.message.answer(
            "To'lov usulini tanlang:",
            reply_markup=payment_keyboard(),
        )

    @router.callback_query(OrderStates.waiting_payment, F.data.startswith("payment:"))
    async def choose_payment(callback: CallbackQuery, state: FSMContext) -> None:
        payment_method = callback.data.split(":", 1)[1]
        if payment_method not in {"Naqd", "Karta"}:
            await callback.answer("❌ To'lov usuli noto'g'ri.", show_alert=True)
            return
        await state.update_data(payment_method=payment_method)
        await state.set_state(OrderStates.waiting_location)
        await callback.answer()
        await callback.message.answer(
            "Xizmat ko'rsatiladigan joylashuvingizni yuboring:",
            reply_markup=location_keyboard(),
        )

    @router.message(OrderStates.waiting_location, F.location)
    async def receive_location(message: Message, state: FSMContext) -> None:
        if not message.location:
            return
        await state.update_data(
            latitude=message.location.latitude,
            longitude=message.location.longitude,
        )
        await state.set_state(OrderStates.waiting_comment)
        await message.answer(
            "Izoh qoldirasizmi? Ixtiyoriy izoh yozing yoki o'tkazib yuboring:",
            reply_markup=skip_comment_keyboard(),
        )

    @router.message(OrderStates.waiting_location)
    async def reject_manual_location(message: Message) -> None:
        await message.answer(
            "Lokatsiyani qo'lda yozmang. Faqat «Lokatsiyamni yuborish» "
            "tugmasidan foydalaning.",
            reply_markup=location_keyboard(),
        )

    async def save_and_notify(
        message: Message, state: FSMContext, comment: str | None
    ) -> None:
        if not message.from_user:
            return
        data = await state.get_data()
        async with session_factory() as session:
            user = await find_user(session, message.from_user.id)
            if user is None or user.rol != "mijoz":
                await message.answer(
                    "Mijoz ma'lumotlari topilmadi. Qayta boshlash uchun /start bosing."
                )
                return

            cars = data["cars"]
            group_id = str(uuid4()) if len(cars) > 1 else None
            orders: list[Order] = []
            for car in cars:
                order = Order(
                    customer_id=user.telegram_id,
                    car_category=car["car_category"],
                    car_model=car["car_model"],
                    car_price=Decimal(str(car["car_price"])),
                    plate_number=car["plate_number"],
                    car_color=car.get("color"),
                    payment_method=data["payment_method"],
                    latitude=Decimal(str(data["latitude"])),
                    longitude=Decimal(str(data["longitude"])),
                    comment=comment,
                    order_group_id=group_id,
                    status="yangi",
                )
                session.add(order)
                orders.append(order)
            await session.commit()
            for order in orders:
                await session.refresh(order)

            try:
                bot = message.bot
                if len(orders) == 1:
                    order = orders[0]
                    director_text = (
                        f"<b>🆕 Yangi buyurtma #{order.id}</b>\n\n"
                        f"<b>👤 Mijoz:</b> {_safe(user.name)}\n"
                        f"<b>📞 Telefon:</b> {_safe(user.phone)}\n"
                        f"<b>🚗 Kategoriya:</b> {_safe(order.car_category)}\n"
                        f"<b>🚗 Model:</b> {_safe(order.car_model)}\n"
                        f"<b>🪪 Davlat raqami:</b> {_safe(order.plate_number)}\n"
                        f"<b>🎨 Rang:</b> {_safe(order.car_color or '—')}\n"
                        f"<b>💰 Narx:</b> {_safe(format_price(int(order.car_price)))}\n"
                        f"<b>💳 To'lov:</b> {_safe(order.payment_method)}\n"
                        f"<b>📝 Izoh:</b> {_safe(order.comment or '—')}"
                    )
                    await bot.send_message(
                        settings.director_id,
                        director_text,
                        reply_markup=new_order_assignment_keyboard(order.id),
                    )
                else:
                    await send_group_summary(
                        bot,
                        settings,
                        "Yangi guruh buyurtmasi",
                        user,
                        orders,
                        group_id,
                    )
                await bot.send_location(
                    settings.director_id,
                    latitude=float(orders[0].latitude),
                    longitude=float(orders[0].longitude),
                )
            except Exception:
                logger.exception(
                    "Could not notify director about order group %s",
                    group_id or orders[0].id,
                )

        await state.clear()
        await message.answer(
            (
                f"{len(orders)} ta mashina uchun so'rovingiz qabul qilindi. "
                "Tez orada siz bilan bog'lanamiz."
            ),
            reply_markup=customer_menu_keyboard(),
        )

    @router.message(OrderStates.waiting_comment, F.text)
    async def receive_comment(message: Message, state: FSMContext) -> None:
        comment = (
            None
            if message.text.strip() in {"⏭️ O'tkazib yuborish", "O'tkazib yuborish"}
            else message.text.strip()
        )
        if comment and len(comment) > 2000:
            await message.answer("❌ Izoh 2000 belgidan oshmasin. Qaytadan kiriting:")
            return
        await save_and_notify(message, state, comment)

    register_worker_routes(router, session_factory, settings, scheduler)
    return router
