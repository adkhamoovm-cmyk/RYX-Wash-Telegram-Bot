import html
import logging
import re
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .catalog import categories, format_price, get_model, get_model_by_name
from .config import Settings
from .keyboards import (
    category_keyboard,
    contact_keyboard,
    customer_menu_keyboard,
    director_menu_keyboard,
    group_mode_keyboard,
    location_keyboard,
    manual_category_keyboard,
    manual_location_keyboard,
    manual_model_keyboard,
    model_keyboard,
    new_order_assignment_keyboard,
    payment_keyboard,
    next_car_keyboard,
    saved_cars_keyboard,
    skip_comment_keyboard,
    worker_menu_keyboard,
)
from .models import CustomerCar, Order, User, Worker
from .states import ManualOrderStates, OrderStates, RegistrationStates
from .worker_handlers import register_worker_routes

logger = logging.getLogger(__name__)


def _safe(value: object) -> str:
    return html.escape(str(value))


UZBEK_PHONE_RE = re.compile(r"^\+998\d{9}$")
TASHKENT = ZoneInfo("Asia/Tashkent")


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
        f"<b>{_safe(title)}</b>\n\n"
        f"<b>Mijoz:</b> {_safe(customer.name)}\n"
        f"<b>Telefon:</b> {_safe(customer.phone)}\n"
        f"<b>Mashinalar:</b> {len(orders)} ta\n"
        f"<b>Jami:</b> {_safe(format_price(total))}"
    )
    lines = [
        f"{index}. {_safe(order.car_model)} | "
        f"{_safe(order.plate_number)} | "
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
                "Hozircha botda faqat mijozlar uchun buyurtma qabul qilinadi."
            )
            return None
        return user

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext) -> None:
        if not message.from_user:
            return

        async with session_factory() as session:
            user = await find_user(session, message.from_user.id)

            if user is None:
                user = User(
                    telegram_id=message.from_user.id,
                    rol="direktor" if message.from_user.id == settings.director_id else "mijoz",
                )
                session.add(user)
                await session.commit()

            if user.rol == "direktor":
                await state.clear()
                await message.answer(
                    "Direktor paneli.",
                    reply_markup=director_menu_keyboard(),
                )
                return

            if user.rol == "ishchi":
                await state.clear()
                await message.answer(
                    "Ishchi paneli.",
                    reply_markup=worker_menu_keyboard(),
                )
                return

            if user.rol != "mijoz":
                await state.clear()
                await message.answer(
                    "Sizning rolingiz bazada mijoz emas. "
                    "Direktor va ishchi funksiyalari keyingi bosqichda qo'shiladi."
                )
                return

            if not user.name:
                await state.set_state(RegistrationStates.waiting_name)
                await message.answer(
                    "RYX Wash xizmatiga xush kelibsiz.\n\n"
                    "Ro'yxatdan o'tish uchun ism-familyangizni yozing:",
                    reply_markup=ReplyKeyboardRemove(),
                )
                return

            if not user.phone:
                await state.set_state(RegistrationStates.waiting_phone)
                await message.answer(
                    "Telefon raqamingizni Telegram tugmasi orqali yuboring:",
                    reply_markup=contact_keyboard(),
                )
                return

        await state.clear()
        await message.answer(
            "RYX Wash xizmatiga xush kelibsiz.",
            reply_markup=customer_menu_keyboard(),
        )

    @router.message(RegistrationStates.waiting_name, F.text)
    async def receive_name(message: Message, state: FSMContext) -> None:
        name = message.text.strip()
        if len(name) < 2 or len(name) > 150:
            await message.answer("Iltimos, ism-familyangizni to'g'ri kiriting.")
            return

        async with session_factory() as session:
            user = await find_user(session, message.from_user.id)
            if user is None:
                await message.answer("Avval /start buyrug'ini bosing.")
                return
            user.name = name
            await session.commit()

        await state.set_state(RegistrationStates.waiting_phone)
        await message.answer(
            "Endi telefon raqamingizni quyidagi Telegram tugmasi orqali yuboring. "
            "Raqamni qo'lda yozib bo'lmaydi:",
            reply_markup=contact_keyboard(),
        )

    @router.message(RegistrationStates.waiting_phone, F.contact)
    async def receive_phone(message: Message, state: FSMContext) -> None:
        contact = message.contact
        if not message.from_user or contact.user_id != message.from_user.id:
            await message.answer(
                "Faqat o'zingizning telefon raqamingizni Telegram tugmasi "
                "orqali yuboring."
            )
            return

        async with session_factory() as session:
            user = await find_user(session, message.from_user.id)
            if user is None:
                await message.answer("Avval /start buyrug'ini bosing.")
                return
            user.phone = contact.phone_number
            await session.commit()

        await state.clear()
        await message.answer(
            "Ro'yxatdan o'tish yakunlandi.",
            reply_markup=customer_menu_keyboard(),
        )

    @router.message(RegistrationStates.waiting_phone, F.text)
    async def reject_manual_phone(message: Message) -> None:
        await message.answer(
            "Telefon raqamini qo'lda yozmang. "
            "Faqat «Telefon raqamimni yuborish» tugmasidan foydalaning.",
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
            "Joriy amal bekor qilindi.",
            reply_markup=menu,
        )

    @router.message(F.text == "Buyurtmalar tarixi")
    async def customer_order_history(message: Message) -> None:
        async with session_factory() as session:
            customer = await find_user(session, message.from_user.id)
            if not customer or customer.rol != "mijoz":
                await message.answer("Bu bo'lim faqat mijozlar uchun.")
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
            await message.answer("Sizda hali buyurtmalar yo'q.")
            return

        date_groups: dict[str, list[Order]] = {}
        for order in orders:
            date_key = order.created_at.astimezone(TASHKENT).strftime("%d.%m.%Y")
            date_groups.setdefault(date_key, []).append(order)
        parts = ["<b>Buyurtmalar tarixi</b>"]
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
                        f"Guruh buyurtmasi — {_safe(format_price(total))}:"
                    )
                for order in reversed(group_orders):
                    parts.append(
                        f"• {_safe(order.car_model)} | "
                        f"{_safe(order.plate_number)} | "
                        f"{_safe(order.status)} | "
                        f"{_safe(format_price(int(order.car_price)))}"
                    )
        history_text = "\n".join(parts)
        if len(history_text) > 4000:
            history_text = history_text[:3950] + "\n\n… faqat so'nggi buyurtmalar ko'rsatildi."
        await message.answer(history_text)

    @router.message(F.text == "Statistika")
    async def director_statistics(message: Message) -> None:
        async with session_factory() as session:
            director = await find_user(session, message.from_user.id)
            if not director or director.rol != "direktor":
                await message.answer("Bu bo'lim faqat direktor uchun.")
                return
            rows = (
                await session.execute(
                    select(Order, Worker)
                    .outerjoin(Worker, Order.worker_id == Worker.user_id)
                    .where(Order.status == "yakunlandi")
                    .order_by(Order.completed_at)
                )
            ).all()
        if not rows:
            await message.answer("Hali yakunlangan buyurtmalar yo'q.")
            return
        total_revenue = sum(Decimal(order.car_price) for order, _worker in rows)
        worker_stats: dict[int, dict[str, object]] = {}
        for order, worker in rows:
            if not worker:
                continue
            stats = worker_stats.setdefault(
                worker.user_id,
                {
                    "name": worker.name,
                    "cars": 0,
                    "share": Decimal("0"),
                },
            )
            stats["cars"] = int(stats["cars"]) + 1
            stats["share"] = Decimal(stats["share"]) + (
                Decimal(order.car_price) * Decimal(worker.share_percent) / Decimal("100")
            )
        lines = [
            "<b>Umumiy statistika</b>",
            f"Yuvilgan mashinalar: <b>{len(rows)}</b>",
            f"Daromad: <b>{_safe(format_price(int(total_revenue)))}</b>",
            "",
            "<b>Ishchilar:</b>",
        ]
        worker_lines = []
        for stats in worker_stats.values():
            worker_lines.append(
                f"• {_safe(stats['name'])}: {stats['cars']} ta mashina, "
                f"ulushi {_safe(format_price(int(Decimal(stats['share']))))}"
            )
        current = "\n".join(lines)
        for line in worker_lines:
            if len(current) + len(line) + 1 > 3800:
                await message.answer(current)
                current = "<b>Ishchilar (davomi):</b>\n" + line
            else:
                current += "\n" + line
        await message.answer(current)

    @router.message(F.text == "Qo'lda buyurtma qo'shish")
    async def start_manual_order(message: Message, state: FSMContext) -> None:
        async with session_factory() as session:
            director = await find_user(session, message.from_user.id)
            if not director or director.rol != "direktor":
                await message.answer("Bu funksiya faqat direktor uchun.")
                return
        await state.clear()
        await state.set_state(ManualOrderStates.waiting_customer_name)
        await message.answer(
            "Mijozning ism-familyasini kiriting:",
            reply_markup=ReplyKeyboardRemove(),
        )

    @router.message(ManualOrderStates.waiting_customer_name, F.text)
    async def manual_customer_name(message: Message, state: FSMContext) -> None:
        name = " ".join(message.text.split())
        if len(name) < 2 or len(name) > 150:
            await message.answer("Ism-familya 2–150 belgi bo'lishi kerak.")
            return
        await state.update_data(customer_name=name)
        await state.set_state(ManualOrderStates.waiting_customer_phone)
        await message.answer(
            "Mijoz telefon raqamini +998XXXXXXXXX formatida kiriting:"
        )

    @router.message(ManualOrderStates.waiting_customer_phone, F.text)
    async def manual_customer_phone(message: Message, state: FSMContext) -> None:
        phone = re.sub(r"[\s()-]", "", message.text)
        if not UZBEK_PHONE_RE.fullmatch(phone):
            await message.answer(
                "Telefon raqami +998 bilan boshlanib, jami 12 raqamdan iborat "
                "bo'lishi kerak. Masalan: +998901234567"
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
                "Mijozning saqlangan mashinasini tanlang yoki yangi mashina qo'shing:",
                reply_markup=saved_cars_keyboard(cars, "manual"),
            )
        else:
            await state.set_state(ManualOrderStates.waiting_new_category)
            await message.answer(
                "Mashina kategoriyasini tanlang:",
                reply_markup=manual_category_keyboard(),
            )

    async def append_manual_car(
        state: FSMContext,
        car: dict,
        car_photo_id: str,
    ) -> None:
        data = await state.get_data()
        cars = list(data.get("cars", []))
        cars.append({**car, "car_photo_id": car_photo_id})
        await state.update_data(cars=cars, current_car=None)

    @router.callback_query(
        ManualOrderStates.waiting_car_choice,
        F.data.startswith("manual_saved_car:"),
    )
    async def choose_saved_manual_car(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        car_id = int(callback.data.split(":", 1)[1])
        data = await state.get_data()
        async with session_factory() as session:
            car = await session.get(CustomerCar, car_id)
            if not car or car.customer_id != data.get("customer_id"):
                await callback.answer("Mashina topilmadi.", show_alert=True)
                return
        model = get_model_by_name(car.car_category, car.model)
        if model is None:
            await callback.answer(
                "Bu mashina katalogda topilmadi. Yangi mashina qo'shing.",
                show_alert=True,
            )
            return
        await state.update_data(
            current_car={
                "car_category": car.car_category,
                "car_model": car.model,
                "car_price": model.price,
                "plate_number": car.plate_number,
                "color": car.color,
            }
        )
        await state.set_state(ManualOrderStates.waiting_car_photo)
        await callback.answer()
        await callback.message.edit_text(
            f"{_safe(car.model)} ({_safe(car.plate_number)}) tanlandi."
        )
        await callback.message.answer(
            "Shu mashinaning referens rasmini yuboring (majburiy):"
        )

    @router.callback_query(
        ManualOrderStates.waiting_car_choice,
        F.data.startswith("manual_cars_page:"),
    )
    async def page_manual_cars(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        page = int(callback.data.split(":", 1)[1])
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
        await state.set_state(ManualOrderStates.waiting_new_category)
        await callback.answer()
        await callback.message.edit_text(
            "Yangi mashina kategoriyasini tanlang:",
            reply_markup=manual_category_keyboard(),
        )

    @router.callback_query(
        ManualOrderStates.waiting_new_category,
        F.data.startswith("manual_category:"),
    )
    async def manual_car_category(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        category = callback.data.split(":", 1)[1]
        if category not in categories():
            await callback.answer("Kategoriya topilmadi.", show_alert=True)
            return
        await state.update_data(car_category=category)
        await state.set_state(ManualOrderStates.waiting_new_model)
        await callback.answer()
        await callback.message.edit_text(
            f"{_safe(category)} kategoriyasidan modelni tanlang:",
            reply_markup=manual_model_keyboard(category),
        )

    @router.callback_query(
        ManualOrderStates.waiting_new_model,
        F.data.startswith("manual_model:"),
    )
    async def manual_car_model(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        model = get_model(callback.data.split(":", 1)[1])
        if model is None:
            await callback.answer("Model topilmadi.", show_alert=True)
            return
        await state.update_data(
            car_model=model.name,
            car_price=model.price,
        )
        await state.set_state(ManualOrderStates.waiting_plate)
        await callback.answer()
        await callback.message.edit_text(
            f"Tanlangan model: <b>{_safe(model.name)}</b>\n"
            f"Narxi: <b>{_safe(format_price(model.price))}</b>\n\n"
            "Mashina davlat raqamini kiriting (majburiy):",
            parse_mode=ParseMode.HTML,
        )

    @router.message(ManualOrderStates.waiting_plate, F.text)
    async def manual_plate(message: Message, state: FSMContext) -> None:
        plate = " ".join(message.text.split()).upper()
        if not plate or len(plate) > 30:
            await message.answer(
                "Davlat raqami majburiy va 30 belgidan oshmasligi kerak."
            )
            return
        await state.update_data(plate_number=plate)
        await state.set_state(ManualOrderStates.waiting_new_color)
        await message.answer(
            "Mashina rangini yozing yoki o'tkazib yuboring (ixtiyoriy):",
            reply_markup=skip_comment_keyboard(),
        )

    @router.message(ManualOrderStates.waiting_new_color, F.text)
    async def manual_car_color(message: Message, state: FSMContext) -> None:
        color = None if message.text.strip() == "O'tkazib yuborish" else message.text.strip()
        if color and len(color) > 50:
            await message.answer("Rang 50 belgidan oshmasin.")
            return
        data = await state.get_data()
        async with session_factory() as session:
            car = CustomerCar(
                customer_id=data["customer_id"],
                car_category=data["car_category"],
                model=data["car_model"],
                plate_number=data["plate_number"],
                color=color,
            )
            session.add(car)
            await session.commit()
        await state.update_data(
            current_car={
                "car_category": data["car_category"],
                "car_model": data["car_model"],
                "car_price": data["car_price"],
                "plate_number": data["plate_number"],
                "color": color,
            }
        )
        await state.set_state(ManualOrderStates.waiting_car_photo)
        await message.answer(
            "Mashina rasmini yuboring. Rasm majburiy, keyingi bosqichga "
            "rasmsiz o'tib bo'lmaydi.",
            reply_markup=ReplyKeyboardRemove(),
        )

    @router.message(ManualOrderStates.waiting_car_photo, F.photo)
    async def manual_car_photo(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        current_car = data.get("current_car")
        if not current_car:
            await message.answer("Mashina ma'lumotlari topilmadi. /cancel bosing.")
            return
        await append_manual_car(state, current_car, message.photo[-1].file_id)
        await state.set_state(ManualOrderStates.waiting_next_car)
        await message.answer(
            "Mashina buyurtmaga qo'shildi. Yana mashina qo'shasizmi?",
            reply_markup=next_car_keyboard("manual"),
        )

    @router.message(ManualOrderStates.waiting_car_photo)
    async def manual_require_car_photo(message: Message) -> None:
        await message.answer(
            "Mashina rasmi majburiy. Iltimos, Telegram orqali bitta rasm yuboring."
        )

    @router.callback_query(
        ManualOrderStates.waiting_next_car,
        F.data == "manual_add_car",
    )
    async def add_manual_car(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
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
        data = await state.get_data()
        if not data.get("cars"):
            await callback.answer("Avval mashina qo'shing.", show_alert=True)
            return
        await state.set_state(ManualOrderStates.waiting_payment)
        await callback.answer()
        await callback.message.edit_text("Mashinalar tanlandi.")
        await callback.message.answer(
            "To'lov usulini tanlang:",
            reply_markup=payment_keyboard(),
        )

    @router.callback_query(
        ManualOrderStates.waiting_payment,
        F.data.startswith("payment:"),
    )
    async def manual_payment(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        payment_method = callback.data.split(":", 1)[1]
        if payment_method not in {"Naqd", "Karta"}:
            await callback.answer("To'lov usuli topilmadi.", show_alert=True)
            return
        await state.update_data(payment_method=payment_method)
        await state.set_state(ManualOrderStates.waiting_location)
        await callback.answer()
        await callback.message.answer(
            "Lokatsiyani Telegram tugmasi orqali yuboring yoki manzilni "
            "matn qilib yozish variantini tanlang:",
            reply_markup=manual_location_keyboard(),
        )

    @router.message(
        ManualOrderStates.waiting_location,
        F.text == "Manzilni matn qilib yozish",
    )
    async def manual_choose_address(message: Message, state: FSMContext) -> None:
        await state.set_state(ManualOrderStates.waiting_address)
        await message.answer(
            "Mijoz manzilini matn qilib kiriting:",
            reply_markup=ReplyKeyboardRemove(),
        )

    @router.message(ManualOrderStates.waiting_location, F.location)
    async def manual_location(message: Message, state: FSMContext) -> None:
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
    async def manual_require_location_choice(message: Message) -> None:
        await message.answer(
            "Lokatsiya tugmasini bosing yoki «Manzilni matn qilib yozish» "
            "variantini tanlang.",
            reply_markup=manual_location_keyboard(),
        )

    @router.message(ManualOrderStates.waiting_address, F.text)
    async def manual_address(message: Message, state: FSMContext) -> None:
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
        async with session_factory() as session:
            customer = await session.get(User, data["customer_id"])
            if customer is None:
                await message.answer("Mijoz topilmadi. Qayta boshlang.")
                return
            cars = data["cars"]
            group_id = str(uuid4()) if len(cars) > 1 else None
            orders: list[Order] = []
            for car in cars:
                order = Order(
                    customer_id=customer.telegram_id,
                    car_category=car["car_category"],
                    car_model=car["car_model"],
                    car_price=Decimal(str(car["car_price"])),
                    plate_number=car["plate_number"],
                    car_color=car.get("color"),
                    payment_method=data["payment_method"],
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
                    car_photo_id=car["car_photo_id"],
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
                    f"<b>Qo'lda kiritilgan buyurtma #{order.id}</b>\n\n"
                    f"<b>Mijoz:</b> {_safe(customer.name)}\n"
                    f"<b>Telefon:</b> {_safe(customer.phone)}\n"
                    f"<b>Kategoriya:</b> {_safe(order.car_category)}\n"
                    f"<b>Model:</b> {_safe(order.car_model)}\n"
                    f"<b>Davlat raqami:</b> {_safe(order.plate_number)}\n"
                    f"<b>Rang:</b> {_safe(order.car_color or '—')}\n"
                    f"<b>Narx:</b> {_safe(format_price(int(order.car_price)))}\n"
                    f"<b>To'lov:</b> {_safe(order.payment_method)}\n"
                    f"<b>Manzil:</b> {_safe(order.address or 'Telegram lokatsiyasi')}\n"
                    f"<b>Izoh:</b> {_safe(order.comment or '—')}"
                )
                await bot.send_message(
                    settings.director_id,
                    director_text,
                    reply_markup=new_order_assignment_keyboard(order.id),
                )
                await bot.send_photo(
                    settings.director_id,
                    order.car_photo_id,
                    caption=f"Buyurtma #{order.id} mashina rasmi",
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
                    f"<b>Qo'lda kiritilgan manzil:</b> {_safe(orders[0].address)}",
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
        comment = (
            None if message.text.strip() == "O'tkazib yuborish"
            else message.text.strip()
        )
        if comment and len(comment) > 2000:
            await message.answer("Izoh 2000 belgidan oshmasin. Qaytadan kiriting:")
            return
        await save_manual_and_notify(message, state, comment)

    @router.message(F.text == "Yangi buyurtma")
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
                await callback.answer("Mashina topilmadi.", show_alert=True)
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
            await callback.answer("Kategoriya topilmadi.", show_alert=True)
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
            await callback.answer("Model topilmadi.", show_alert=True)
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
            await message.answer("Davlat raqami majburiy. Qaytadan kiriting:")
            return

        await state.update_data(plate_number=plate)
        await state.set_state(OrderStates.waiting_car_color)
        await message.answer(
            "Mashina rangini yozing yoki o'tkazib yuboring (ixtiyoriy):",
            reply_markup=skip_comment_keyboard(),
        )

    @router.message(OrderStates.waiting_car_color, F.text)
    async def receive_car_color(message: Message, state: FSMContext) -> None:
        color = None if message.text.strip() == "O'tkazib yuborish" else message.text.strip()
        if color and len(color) > 50:
            await message.answer("Rang 50 belgidan oshmasin.")
            return
        data = await state.get_data()
        async with session_factory() as session:
            customer = await find_user(session, message.from_user.id)
            if not customer:
                await message.answer("Mijoz ma'lumotlari topilmadi.")
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
            await callback.answer("Avval mashina tanlang.", show_alert=True)
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
                        f"<b>Yangi buyurtma #{order.id}</b>\n\n"
                        f"<b>Mijoz:</b> {_safe(user.name)}\n"
                        f"<b>Telefon:</b> {_safe(user.phone)}\n"
                        f"<b>Kategoriya:</b> {_safe(order.car_category)}\n"
                        f"<b>Model:</b> {_safe(order.car_model)}\n"
                        f"<b>Davlat raqami:</b> {_safe(order.plate_number)}\n"
                        f"<b>Rang:</b> {_safe(order.car_color or '—')}\n"
                        f"<b>Narx:</b> {_safe(format_price(int(order.car_price)))}\n"
                        f"<b>To'lov:</b> {_safe(order.payment_method)}\n"
                        f"<b>Izoh:</b> {_safe(order.comment or '—')}"
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
        comment = None if message.text.strip() == "O'tkazib yuborish" else message.text.strip()
        if comment and len(comment) > 2000:
            await message.answer("Izoh 2000 belgidan oshmasin. Qaytadan kiriting:")
            return
        await save_and_notify(message, state, comment)

    register_worker_routes(router, session_factory, settings, scheduler)
    return router
