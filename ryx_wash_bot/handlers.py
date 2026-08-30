import html
import logging
from decimal import Decimal

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .catalog import format_price, get_model
from .config import Settings
from .keyboards import (
    category_keyboard,
    contact_keyboard,
    customer_menu_keyboard,
    director_menu_keyboard,
    location_keyboard,
    model_keyboard,
    new_order_assignment_keyboard,
    payment_keyboard,
    skip_comment_keyboard,
    worker_menu_keyboard,
)
from .models import Order, User
from .states import OrderStates, RegistrationStates
from .worker_handlers import register_worker_routes

logger = logging.getLogger(__name__)


def _safe(value: object) -> str:
    return html.escape(str(value))


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
        await message.answer(
            "Joriy amal bekor qilindi.",
            reply_markup=customer_menu_keyboard(),
        )

    @router.message(F.text == "Yangi buyurtma")
    async def new_order(message: Message, state: FSMContext) -> None:
        async with session_factory() as session:
            if await customer_or_reject(message, session) is None:
                return
        await state.clear()
        await message.answer(
            "Mashina kategoriyasini tanlang:",
            reply_markup=category_keyboard(),
        )

    @router.callback_query(F.data.startswith("category:"))
    async def choose_category(callback: CallbackQuery, state: FSMContext) -> None:
        category = callback.data.split(":", 1)[1]
        await state.update_data(car_category=category)
        await callback.answer()
        await callback.message.edit_text(
            f"{_safe(category)} kategoriyasidan modelni tanlang:",
            reply_markup=model_keyboard(category),
        )

    @router.callback_query(F.data.startswith("model:"))
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
        await state.set_state(OrderStates.waiting_payment)
        await message.answer(
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

            order = Order(
                customer_id=user.telegram_id,
                car_category=data["car_category"],
                car_model=data["car_model"],
                car_price=Decimal(str(data["car_price"])),
                plate_number=data["plate_number"],
                payment_method=data["payment_method"],
                latitude=Decimal(str(data["latitude"])),
                longitude=Decimal(str(data["longitude"])),
                comment=comment,
                status="yangi",
            )
            session.add(order)
            await session.commit()
            await session.refresh(order)

            director_text = (
                f"<b>Yangi buyurtma #{order.id}</b>\n\n"
                f"<b>Mijoz:</b> {_safe(user.name)}\n"
                f"<b>Telefon:</b> {_safe(user.phone)}\n"
                f"<b>Kategoriya:</b> {_safe(order.car_category)}\n"
                f"<b>Model:</b> {_safe(order.car_model)}\n"
                f"<b>Davlat raqami:</b> {_safe(order.plate_number)}\n"
                f"<b>Narx:</b> {_safe(format_price(int(order.car_price)))}\n"
                f"<b>To'lov:</b> {_safe(order.payment_method)}\n"
                f"<b>Izoh:</b> {_safe(order.comment or '—')}"
            )

            try:
                bot = message.bot
                director_message = await bot.send_message(
                    settings.director_id,
                    director_text,
                    reply_markup=new_order_assignment_keyboard(order.id),
                )
                await bot.send_location(
                    settings.director_id,
                    latitude=float(order.latitude),
                    longitude=float(order.longitude),
                )
            except Exception:
                logger.exception(
                    "Could not notify director about order %s", order.id
                )

        await state.clear()
        await message.answer(
            "So'rovingiz qabul qilindi. Tez orada siz bilan bog'lanamiz.",
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
