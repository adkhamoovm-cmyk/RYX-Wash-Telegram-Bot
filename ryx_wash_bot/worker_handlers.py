import html
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .catalog import format_price
from .config import Settings
from .keyboards import (
    available_workers_keyboard,
    busy_workers_keyboard,
    cancellation_reasons_keyboard,
    cancel_only_keyboard,
    director_menu_keyboard,
    no_available_workers_keyboard,
    queue_offer_decision_keyboard,
    worker_menu_keyboard,
    worker_order_decision_keyboard,
    worker_status_keyboard,
)
from .models import Cancellation, Order, User, Worker
from .scheduler import (
    configure_worker_available_handler,
    remove_offer_timeout,
    schedule_offer_timeout,
)
from .states import (
    CancellationStates,
    WorkerCompletionStates,
    WorkerRegistrationStates,
)

logger = logging.getLogger(__name__)
TASHKENT = ZoneInfo("Asia/Tashkent")
ACTIVE_ACCEPTED_STATUSES = {
    "ishchi_qabul_qildi",
    "yo'lda",
    "yetib_keldi",
    "yuvish_boshlandi",
    "yakunlanmoqda",
}


def _safe(value: object) -> str:
    return html.escape(str(value))


def now_tashkent() -> datetime:
    return datetime.now(TASHKENT)


def _duration_text(start: datetime | None, end: datetime | None) -> str:
    if not start or not end:
        return "—"
    seconds = max(0, int((end - start).total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours} soat {minutes} daqiqa"
    return f"{minutes} daqiqa {seconds} soniya"


def _register_user_routes(
    router: Router,
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    scheduler: AsyncIOScheduler,
) -> None:
    async def find_user(session: AsyncSession, user_id: int) -> User | None:
        return await session.scalar(select(User).where(User.telegram_id == user_id))

    async def is_director(session: AsyncSession, user_id: int) -> bool:
        user = await find_user(session, user_id)
        return bool(user and user.rol == "direktor")

    async def get_worker(session: AsyncSession, user_id: int) -> Worker | None:
        return await session.scalar(select(Worker).where(Worker.user_id == user_id))

    def worker_offer_text(order: Order) -> str:
        return (
            f"<b>Yangi buyurtma #{order.id}</b>\n\n"
            f"<b>Mashina:</b> {_safe(order.car_model)}\n"
            f"<b>Narx:</b> {_safe(format_price(int(order.car_price)))}\n\n"
            "Buyurtmani qabul qilasizmi?"
        )

    async def activate_queued_order(
        order_id: int,
        worker_id: int,
        bot,
    ) -> bool:
        async with sessions() as session:
            order = await session.get(Order, order_id)
            worker = await session.get(Worker, worker_id)
            if (
                not order
                or not worker
                or order.status != "navbatda"
                or worker.status != "bo'sh"
                or order.worker_id not in {None, worker_id}
            ):
                return False
            assigned_at = now_tashkent()
            order.worker_id = worker_id
            order.queue_offer_worker_id = None
            order.queue_prompted_at = None
            order.status = "ishchiga_yuborildi"
            order.queued_offer = True
            order.assigned_at = assigned_at
            worker.status = "band"
            customer = await session.get(User, order.customer_id)
            text = (
                f"<b>Navbatdagi buyurtma #{order.id}</b>\n\n"
                f"<b>Mijoz:</b> {_safe(customer.name if customer else '—')}\n"
                f"<b>Mashina:</b> {_safe(order.car_model)}\n"
                f"<b>Davlat raqami:</b> {_safe(order.plate_number)}\n"
                f"<b>To'lov:</b> {_safe(order.payment_method)}\n"
                f"<b>Narx:</b> {_safe(format_price(int(order.car_price)))}\n"
                f"<b>Izoh:</b> {_safe(order.comment or '—')}\n\n"
                "Buyurtmani qabul qilasizmi?"
            )
            latitude = float(order.latitude)
            longitude = float(order.longitude)
            await session.commit()
        schedule_offer_timeout(scheduler, order_id, assigned_at)
        await bot.send_message(
            worker_id,
            text,
            reply_markup=worker_order_decision_keyboard(order_id),
        )
        await bot.send_location(
            worker_id,
            latitude=latitude,
            longitude=longitude,
        )
        return True

    async def offer_next_queued_order(worker_id: int, bot) -> None:
        async with sessions() as session:
            worker = await session.get(Worker, worker_id)
            if not worker or worker.status != "bo'sh":
                return
            assigned_order = await session.scalar(
                select(Order)
                .where(
                    Order.status == "navbatda",
                    Order.worker_id == worker_id,
                )
                .order_by(Order.created_at, Order.id)
                .limit(1)
            )
            if assigned_order:
                assigned_order_id = assigned_order.id
            else:
                assigned_order_id = None

        if assigned_order_id is not None:
            await activate_queued_order(assigned_order_id, worker_id, bot)
            return

        async with sessions() as session:
            worker = await session.get(Worker, worker_id)
            if not worker or worker.status != "bo'sh":
                return
            queued_order = await session.scalar(
                select(Order)
                .where(
                    Order.status == "navbatda",
                    Order.worker_id.is_(None),
                    Order.queue_offer_worker_id.is_(None),
                )
                .order_by(Order.created_at, Order.id)
                .limit(1)
            )
            if not queued_order:
                return
            customer = await session.get(User, queued_order.customer_id)
            queued_order.queue_offer_worker_id = worker_id
            queued_order.queue_prompted_at = now_tashkent()
            order_id = queued_order.id
            customer_name = customer.name if customer else "—"
            car_model = queued_order.car_model
            worker_name = worker.name
            await session.commit()

        await bot.send_message(
            settings.director_id,
            f"{_safe(worker_name)} endi bo'sh, navbatdagi buyurtmani "
            f"({_safe(customer_name)}, {_safe(car_model)}) beramizmi?",
            reply_markup=queue_offer_decision_keyboard(order_id, worker_id),
        )

    configure_worker_available_handler(offer_next_queued_order)

    @router.message(F.text == "Ishchi qo'shish")
    async def start_worker_registration(
        message: Message, state: FSMContext
    ) -> None:
        if not message.from_user:
            return
        async with sessions() as session:
            if not await is_director(session, message.from_user.id):
                await message.answer("Bu amal faqat direktor uchun.")
                return
        await state.clear()
        await state.set_state(WorkerRegistrationStates.waiting_user_id)
        await message.answer(
            "Ishchining Telegram ID raqamini yuboring:",
            reply_markup=ReplyKeyboardRemove(),
        )

    @router.message(WorkerRegistrationStates.waiting_user_id, F.text)
    async def receive_worker_id(message: Message, state: FSMContext) -> None:
        try:
            worker_id = int(message.text.strip())
        except ValueError:
            await message.answer("Telegram ID faqat raqam bo'lishi kerak.")
            return
        if worker_id <= 0 or worker_id == settings.director_id:
            await message.answer("Iltimos, to'g'ri ishchi Telegram ID'sini yuboring.")
            return
        await state.update_data(worker_id=worker_id)
        await state.set_state(WorkerRegistrationStates.waiting_name)
        await message.answer("Ishchining ism-familyasini yuboring:")

    @router.message(WorkerRegistrationStates.waiting_name, F.text)
    async def receive_worker_name(message: Message, state: FSMContext) -> None:
        name = message.text.strip()
        if len(name) < 2 or len(name) > 150:
            await message.answer("Ism-familya 2–150 belgi bo'lishi kerak.")
            return
        await state.update_data(name=name)
        await state.set_state(WorkerRegistrationStates.waiting_phone)
        await message.answer("Ishchining telefon raqamini yuboring:")

    @router.message(WorkerRegistrationStates.waiting_phone, F.text)
    async def receive_worker_phone(message: Message, state: FSMContext) -> None:
        phone = message.text.strip()
        if len(phone) < 5 or len(phone) > 40:
            await message.answer("Telefon raqamini to'g'ri kiriting:")
            return
        await state.update_data(phone=phone)
        await state.set_state(WorkerRegistrationStates.waiting_percent)
        await message.answer("Ishchining foiz ulushini kiriting (masalan: 30%):")

    @router.message(WorkerRegistrationStates.waiting_percent, F.text)
    async def receive_worker_percent(message: Message, state: FSMContext) -> None:
        raw_percent = message.text.strip().replace("%", "").replace(",", ".")
        try:
            percent = Decimal(raw_percent)
        except InvalidOperation:
            await message.answer("Foizni raqam ko'rinishida kiriting, masalan 30%.")
            return
        if percent <= 0 or percent > 100:
            await message.answer("Foiz 0 dan katta va 100 dan kichik yoki teng bo'lsin.")
            return

        data = await state.get_data()
        async with sessions() as session:
            director = await find_user(session, message.from_user.id)
            if not director or director.rol != "direktor":
                await message.answer("Bu amal faqat direktor uchun.")
                return
            user = await find_user(session, data["worker_id"])
            if user is None:
                user = User(
                    telegram_id=data["worker_id"],
                    rol="ishchi",
                    name=data["name"],
                    phone=data["phone"],
                )
                session.add(user)
            else:
                user.rol = "ishchi"
                user.name = data["name"]
                user.phone = data["phone"]

            worker = await get_worker(session, data["worker_id"])
            if worker is None:
                session.add(
                    Worker(
                        user_id=data["worker_id"],
                        name=data["name"],
                        phone=data["phone"],
                        share_percent=percent,
                        status="smenada_emas",
                    )
                )
            else:
                worker.name = data["name"]
                worker.phone = data["phone"]
                worker.share_percent = percent
            await session.commit()

        await state.clear()
        await message.answer(
            f"{_safe(data['name'])} ishchi sifatida qo'shildi. "
            "Boshlang'ich holati: smenada emas.",
            reply_markup=director_menu_keyboard(),
        )

    @router.message(F.text == "Ishga keldim")
    async def start_shift(message: Message) -> None:
        if not message.from_user:
            return
        async with sessions() as session:
            worker = await get_worker(session, message.from_user.id)
            if worker is None:
                await message.answer("Siz ishchi sifatida ro'yxatdan o'tmagansiz.")
                return
            if worker.status == "band":
                await message.answer("Siz hozir buyurtma bilan bandsiz.")
                return
            worker.status = "bo'sh"
            worker.shift_started_at = now_tashkent()
            worker.shift_ended_at = None
            user = await find_user(session, message.from_user.id)
            if user:
                user.rol = "ishchi"
            await session.commit()
        await message.answer(
            "Smena boshlandi. Siz hozir bo'shsiz.",
            reply_markup=worker_menu_keyboard(),
        )

    @router.message(F.text == "Ishdan ketdim")
    async def end_shift(message: Message) -> None:
        if not message.from_user:
            return
        async with sessions() as session:
            worker = await get_worker(session, message.from_user.id)
            if worker is None:
                await message.answer("Siz ishchi sifatida ro'yxatdan o'tmagansiz.")
                return
            if worker.status == "band":
                await message.answer(
                    "Buyurtma yakunlanmaguncha smenani tugatib bo'lmaydi."
                )
                return
            worker.status = "smenada_emas"
            worker.shift_ended_at = now_tashkent()
            await session.commit()
        await message.answer(
            "Smena tugadi.",
            reply_markup=worker_menu_keyboard(),
        )

    @router.callback_query(F.data.startswith("assign_workers:"))
    async def show_available_workers(
        callback: CallbackQuery,
    ) -> None:
        if not callback.from_user:
            return
        order_id = int(callback.data.split(":", 1)[1])
        async with sessions() as session:
            if not await is_director(session, callback.from_user.id):
                await callback.answer("Bu amal faqat direktor uchun.", show_alert=True)
                return
            order = await session.get(Order, order_id)
            if (
                not order
                or order.status not in {"yangi", "navbatda"}
                or order.worker_id is not None
            ):
                await callback.answer(
                    "Buyurtma hozir ishchiga yuborish uchun tayyor emas.",
                    show_alert=True,
                )
                return
            workers = list(
                (
                    await session.scalars(
                        select(Worker)
                        .where(Worker.status == "bo'sh")
                        .order_by(Worker.name)
                    )
                ).all()
            )
        if not workers:
            await callback.answer()
            await callback.message.answer(
                "Hozir barcha ishchilar band yoki smenada emas. "
                "Buyurtma bilan nima qilamiz?",
                reply_markup=no_available_workers_keyboard(order_id),
            )
            return
        await callback.answer()
        await callback.message.answer(
            f"Buyurtma #{order_id} uchun bo'sh ishchini tanlang:",
            reply_markup=available_workers_keyboard(order_id, workers),
        )

    @router.callback_query(F.data.startswith("queue_order:"))
    async def queue_order(callback: CallbackQuery) -> None:
        order_id = int(callback.data.split(":", 1)[1])
        async with sessions() as session:
            if not await is_director(session, callback.from_user.id):
                await callback.answer("Bu amal faqat direktor uchun.", show_alert=True)
                return
            order = await session.get(Order, order_id)
            if (
                not order
                or order.status not in {"yangi", "navbatda"}
                or order.worker_id is not None
            ):
                await callback.answer(
                    "Bu buyurtmani navbatga qo'yib bo'lmaydi.", show_alert=True
                )
                return
            order.status = "navbatda"
            order.queued_offer = False
            order.queue_offer_worker_id = None
            order.queue_prompted_at = None
            await session.commit()
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Buyurtma navbatga qo'yildi.")
        await callback.message.answer(f"Buyurtma #{order_id} navbatga qo'yildi.")

    @router.callback_query(F.data.startswith("busy_workers:"))
    async def show_busy_workers(callback: CallbackQuery) -> None:
        order_id = int(callback.data.split(":", 1)[1])
        async with sessions() as session:
            if not await is_director(session, callback.from_user.id):
                await callback.answer("Bu amal faqat direktor uchun.", show_alert=True)
                return
            order = await session.get(Order, order_id)
            if (
                not order
                or order.status not in {"yangi", "navbatda"}
                or order.worker_id is not None
            ):
                await callback.answer(
                    "Bu buyurtmani band ishchiga biriktirib bo'lmaydi.",
                    show_alert=True,
                )
                return
            workers = list(
                (
                    await session.scalars(
                        select(Worker)
                        .join(Order, Order.worker_id == Worker.user_id)
                        .where(Worker.status == "band")
                        .where(Order.status.in_(ACTIVE_ACCEPTED_STATUSES))
                        .order_by(Worker.name)
                    )
                ).all()
            )
        if not workers:
            await callback.answer("Band ishchilar topilmadi.", show_alert=True)
            return
        await callback.answer()
        await callback.message.answer(
            f"Buyurtma #{order_id} uchun band ishchini tanlang:",
            reply_markup=busy_workers_keyboard(order_id, workers),
        )

    @router.callback_query(F.data.startswith("queue_worker:"))
    async def assign_to_busy_worker(callback: CallbackQuery) -> None:
        _, order_id_raw, worker_id_raw = callback.data.split(":")
        order_id, worker_id = int(order_id_raw), int(worker_id_raw)
        async with sessions() as session:
            if not await is_director(session, callback.from_user.id):
                await callback.answer("Bu amal faqat direktor uchun.", show_alert=True)
                return
            order = await session.get(Order, order_id)
            worker = await session.get(Worker, worker_id)
            if (
                not order
                or not worker
                or order.status not in {"yangi", "navbatda"}
                or order.worker_id is not None
                or worker.status != "band"
            ):
                await callback.answer(
                    "Buyurtma yoki ishchi holati o'zgargan.", show_alert=True
                )
                return
            has_active_order = await session.scalar(
                select(func.count(Order.id)).where(
                    Order.worker_id == worker_id,
                    Order.status.in_(ACTIVE_ACCEPTED_STATUSES),
                )
            )
            if not has_active_order:
                await callback.answer(
                    "Bu ishchida qabul qilingan faol buyurtma yo'q.",
                    show_alert=True,
                )
                return
            order.status = "navbatda"
            order.worker_id = worker_id
            order.queued_offer = False
            order.queue_offer_worker_id = None
            order.queue_prompted_at = None
            await session.flush()
            queue_count = await session.scalar(
                select(func.count(Order.id)).where(
                    Order.worker_id == worker_id,
                    Order.status == "navbatda",
                )
            )
            worker_name = worker.name
            await session.commit()
        await callback.bot.send_message(
            worker_id,
            f"Sizda navbatda yana {queue_count} ta buyurtma bor.",
            reply_markup=cancel_only_keyboard(order_id),
        )
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer()
        await callback.message.answer(
            f"Buyurtma #{order_id} {worker_name} uchun navbatga biriktirildi."
        )

    @router.callback_query(F.data.startswith("queue_offer:"))
    async def decide_global_queue_offer(callback: CallbackQuery) -> None:
        _, order_id_raw, worker_id_raw, decision = callback.data.split(":")
        order_id, worker_id = int(order_id_raw), int(worker_id_raw)
        async with sessions() as session:
            if not await is_director(session, callback.from_user.id):
                await callback.answer("Bu amal faqat direktor uchun.", show_alert=True)
                return
            order = await session.get(Order, order_id)
            worker = await session.get(Worker, worker_id)
            if (
                not order
                or not worker
                or order.status != "navbatda"
                or order.worker_id is not None
                or order.queue_offer_worker_id != worker_id
            ):
                await callback.answer(
                    "Bu navbat taklifi endi amalda emas.", show_alert=True
                )
                return
            if decision == "no":
                order.queue_offer_worker_id = None
                order.queue_prompted_at = None
                await session.commit()
                await callback.message.edit_reply_markup(reply_markup=None)
                await callback.answer("Buyurtma navbatda qoldi.")
                return
            if decision != "yes":
                await callback.answer("Noto'g'ri tanlov.", show_alert=True)
                return
            if worker.status != "bo'sh":
                order.queue_offer_worker_id = None
                order.queue_prompted_at = None
                await session.commit()
                await callback.message.edit_reply_markup(reply_markup=None)
                await callback.answer(
                    "Ishchi hozir bo'sh emas.", show_alert=True
                )
                return
        activated = await activate_queued_order(order_id, worker_id, callback.bot)
        if not activated:
            await callback.answer(
                "Buyurtmani ishchiga yuborib bo'lmadi.", show_alert=True
            )
            return
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Navbatdagi buyurtma ishchiga yuborildi.")

    @router.callback_query(F.data.startswith("assign_worker:"))
    async def assign_order(
        callback: CallbackQuery,
    ) -> None:
        if not callback.from_user:
            return
        _, order_id_raw, worker_id_raw = callback.data.split(":")
        order_id, worker_id = int(order_id_raw), int(worker_id_raw)
        async with sessions() as session:
            if not await is_director(session, callback.from_user.id):
                await callback.answer("Bu amal faqat direktor uchun.", show_alert=True)
                return
            order = await session.get(Order, order_id)
            worker = await session.get(Worker, worker_id)
            if not order or not worker:
                await callback.answer("Buyurtma yoki ishchi topilmadi.", show_alert=True)
                return
            if (
                order.worker_id is not None
                or order.status not in {"yangi", "navbatda"}
            ):
                await callback.answer(
                    "Bu buyurtma allaqachon ishchiga yuborilgan.", show_alert=True
                )
                return
            if worker.status != "bo'sh":
                await callback.answer("Bu ishchi endi bo'sh emas.", show_alert=True)
                return
            was_queued = order.status == "navbatda"
            order.worker_id = worker.user_id
            order.queue_offer_worker_id = None
            order.queue_prompted_at = None
            order.assigned_at = now_tashkent()
            order.status = "ishchiga_yuborildi"
            order.queued_offer = was_queued
            worker.status = "band"
            await session.commit()
            assigned_at = order.assigned_at
            worker_name = worker.name
            worker_text = worker_offer_text(order)
        schedule_offer_timeout(scheduler, order_id, assigned_at)
        await callback.bot.send_message(
            worker_id,
            worker_text,
            reply_markup=worker_order_decision_keyboard(order_id),
        )
        await callback.answer()
        await callback.message.edit_reply_markup(
            reply_markup=cancel_only_keyboard(order_id)
        )
        await callback.message.answer(
            f"Buyurtma #{order_id} {worker_name} ishchiga yuborildi."
        )

    async def worker_order_and_worker(
        session: AsyncSession, order_id: int, worker_id: int
    ) -> tuple[Order | None, Worker | None]:
        order = await session.get(Order, order_id)
        worker = await session.get(Worker, worker_id)
        if not order or not worker or order.worker_id != worker_id:
            return None, None
        return order, worker

    @router.callback_query(F.data.startswith("worker_accept:"))
    async def accept_order(callback: CallbackQuery) -> None:
        if not callback.from_user:
            return
        order_id = int(callback.data.split(":", 1)[1])
        async with sessions() as session:
            order, worker = await worker_order_and_worker(
                session, order_id, callback.from_user.id
            )
            if not order or not worker:
                await callback.answer("Bu buyurtma sizga biriktirilmagan.", show_alert=True)
                return
            if order.status != "ishchiga_yuborildi":
                await callback.answer(
                    "Bu buyurtmaga avval javob berilgan.", show_alert=True
                )
                return
            order.status = "ishchi_qabul_qildi"
            order.accepted_at = now_tashkent()
            order.queued_offer = False
            customer = await session.get(User, order.customer_id)
            await session.commit()
            customer_name = customer.name if customer else "—"
            customer_phone = customer.phone if customer else "—"

            full_text = (
                f"<b>Buyurtma #{order.id} qabul qilindi</b>\n\n"
                f"<b>Mijoz:</b> {_safe(customer_name)}\n"
                f"<b>Mashina:</b> {_safe(order.car_model)}\n"
                f"<b>Davlat raqami:</b> {_safe(order.plate_number)}\n"
                f"<b>To'lov:</b> {_safe(order.payment_method)}\n"
                f"<b>Narx:</b> {_safe(format_price(int(order.car_price)))}\n"
                f"<b>Izoh:</b> {_safe(order.comment or '—')}"
            )
            latitude, longitude = float(order.latitude), float(order.longitude)
        remove_offer_timeout(scheduler, order_id)
        await callback.bot.send_message(
            order.customer_id,
            "Buyurtmangiz ishchiga biriktirildi. Xizmat jarayoni boshlanganda xabar beramiz.",
        )
        await callback.message.edit_text(full_text)
        await callback.message.answer(
            "Lokatsiya:",
            reply_markup=worker_status_keyboard(order_id, "route"),
        )
        await callback.bot.send_location(
            callback.from_user.id, latitude=latitude, longitude=longitude
        )
        await callback.bot.send_message(
            settings.director_id,
            f"{_safe(worker.name)} buyurtma #{order_id} ni qabul qildi.",
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("worker_reject:"))
    async def reject_order(callback: CallbackQuery) -> None:
        if not callback.from_user:
            return
        order_id = int(callback.data.split(":", 1)[1])
        async with sessions() as session:
            order, worker = await worker_order_and_worker(
                session, order_id, callback.from_user.id
            )
            if not order or not worker:
                await callback.answer("Bu buyurtma sizga biriktirilmagan.", show_alert=True)
                return
            if order.status != "ishchiga_yuborildi":
                await callback.answer(
                    "Bu buyurtmaga avval javob berilgan.", show_alert=True
                )
                return
            worker.status = "bo'sh"
            order.worker_id = None
            order.assigned_at = None
            order.status = "navbatda" if order.queued_offer else "yangi"
            order.queued_offer = False
            order.queue_offer_worker_id = None
            order.queue_prompted_at = None
            worker_name = worker.name
            await session.commit()
        remove_offer_timeout(scheduler, order_id)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.bot.send_message(
            settings.director_id,
            f"{_safe(worker_name)} rad etdi (buyurtma #{order_id}).",
        )
        await callback.answer("Buyurtma rad etildi.")
        await offer_next_queued_order(callback.from_user.id, callback.bot)

    async def can_cancel(
        session: AsyncSession,
        order: Order,
        actor_id: int,
    ) -> bool:
        actor = await find_user(session, actor_id)
        if actor and actor.rol == "direktor":
            return True
        return bool(actor and actor.rol == "ishchi" and order.worker_id == actor_id)

    @router.callback_query(F.data.startswith("cancel_order:"))
    async def request_cancellation(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        order_id = int(callback.data.split(":", 1)[1])
        async with sessions() as session:
            order = await session.get(Order, order_id)
            if not order:
                await callback.answer("Buyurtma topilmadi.", show_alert=True)
                return
            if order.status in {"yakunlandi", "bekor_qilindi"}:
                await callback.answer(
                    "Bu buyurtmani bekor qilib bo'lmaydi.", show_alert=True
                )
                return
            if not await can_cancel(session, order, callback.from_user.id):
                await callback.answer(
                    "Bu buyurtmani bekor qilish huquqingiz yo'q.", show_alert=True
                )
                return
        await state.set_state(CancellationStates.waiting_reason)
        await state.update_data(cancel_order_id=order_id)
        await callback.answer()
        await callback.message.answer(
            f"Buyurtma #{order_id} ni bekor qilish sababini tanlang:",
            reply_markup=cancellation_reasons_keyboard(order_id),
        )

    async def finalize_cancellation(
        actor_id: int,
        order_id: int,
        reason: str,
        bot,
    ) -> bool:
        reason = reason.strip()
        if not reason:
            return False
        async with sessions() as session:
            order = await session.get(Order, order_id)
            if (
                not order
                or order.status in {"yakunlandi", "bekor_qilindi"}
                or not await can_cancel(session, order, actor_id)
            ):
                return False
            worker_id = order.worker_id
            was_waiting_in_queue = order.status == "navbatda"
            if worker_id:
                worker = await session.get(Worker, worker_id)
                if worker and not was_waiting_in_queue:
                    worker.status = "bo'sh"
            order.status = "bekor_qilindi"
            order.queued_offer = False
            order.queue_offer_worker_id = None
            order.queue_prompted_at = None
            session.add(
                Cancellation(
                    order_id=order.id,
                    reason=reason,
                    cancelled_by=actor_id,
                    cancelled_at=now_tashkent(),
                )
            )
            await session.commit()
        remove_offer_timeout(scheduler, order_id)
        await bot.send_message(
            settings.director_id,
            f"Buyurtma #{order_id} bekor qilindi. Sabab: {_safe(reason)}",
        )
        if worker_id and worker_id != actor_id:
            await bot.send_message(
                worker_id,
                f"Buyurtma #{order_id} bekor qilindi. Sabab: {_safe(reason)}",
            )
        return True

    @router.callback_query(
        CancellationStates.waiting_reason,
        F.data.startswith("cancel_reason:"),
    )
    async def choose_cancellation_reason(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        _, order_id_raw, reason_code = callback.data.split(":")
        order_id = int(order_id_raw)
        data = await state.get_data()
        if data.get("cancel_order_id") != order_id:
            await callback.answer("Bekor qilish ma'lumoti eskirgan.", show_alert=True)
            return
        if reason_code == "other":
            await state.set_state(CancellationStates.waiting_custom_reason)
            await callback.answer()
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer(
                "Bekor qilish sababini matn ko'rinishida yozing:"
            )
            return
        reasons = {
            "customer": "Mijoz voz kechdi",
            "location": "Manzil noto'g'ri/topilmadi",
            "worker": "Ishchi yetib bora olmadi",
        }
        reason = reasons.get(reason_code)
        if not reason:
            await callback.answer("Noto'g'ri sabab.", show_alert=True)
            return
        completed = await finalize_cancellation(
            callback.from_user.id, order_id, reason, callback.bot
        )
        await state.clear()
        await callback.message.edit_reply_markup(reply_markup=None)
        if completed:
            await callback.answer("Buyurtma bekor qilindi.")
        else:
            await callback.answer(
                "Buyurtmani bekor qilib bo'lmadi.", show_alert=True
            )

    @router.message(CancellationStates.waiting_custom_reason, F.text)
    async def custom_cancellation_reason(
        message: Message, state: FSMContext
    ) -> None:
        reason = message.text.strip()
        if not reason:
            await message.answer("Sabab majburiy. Iltimos, sababni yozing:")
            return
        if len(reason) > 2000:
            await message.answer("Sabab 2000 belgidan oshmasin.")
            return
        data = await state.get_data()
        order_id = data.get("cancel_order_id")
        if not isinstance(order_id, int):
            await state.clear()
            await message.answer("Bekor qilish ma'lumoti topilmadi.")
            return
        completed = await finalize_cancellation(
            message.from_user.id, order_id, reason, message.bot
        )
        await state.clear()
        if completed:
            await message.answer(f"Buyurtma #{order_id} bekor qilindi.")
        else:
            await message.answer("Buyurtmani bekor qilib bo'lmadi.")

    @router.message(CancellationStates.waiting_custom_reason)
    async def require_custom_cancellation_reason(message: Message) -> None:
        await message.answer("Sabab majburiy. Uni matn ko'rinishida yozing:")

    @router.callback_query(F.data.startswith("worker_status:"))
    async def update_worker_status(callback: CallbackQuery, state: FSMContext) -> None:
        if not callback.from_user:
            return
        _, stage, order_id_raw = callback.data.split(":")
        order_id = int(order_id_raw)
        async with sessions() as session:
            order, worker = await worker_order_and_worker(
                session, order_id, callback.from_user.id
            )
            if not order or not worker:
                await callback.answer("Bu buyurtma sizga biriktirilmagan.", show_alert=True)
                return
            timestamp = now_tashkent()
            stage_data = {
                "route": (
                    "ishchi_qabul_qildi",
                    "yo'lda",
                    "Yo'lga chiqdi",
                    "arrived",
                ),
                "arrived": (
                    "yo'lda",
                    "yetib_keldi",
                    "Manzilga yetib keldi",
                    "washing",
                ),
                "washing": (
                    "yetib_keldi",
                    "yuvish_boshlandi",
                    "Yuvish boshlandi",
                    "complete",
                ),
            }
            if stage not in stage_data:
                if stage == "complete":
                    if order.status != "yuvish_boshlandi":
                        await callback.answer(
                            "Statuslarni ketma-ket yangilang.", show_alert=True
                        )
                        return
                    order.status = "yakunlanmoqda"
                    await session.commit()
                    await state.set_state(WorkerCompletionStates.waiting_before_photo)
                    await state.update_data(order_id=order_id)
                    await callback.message.edit_reply_markup(reply_markup=None)
                    await callback.message.answer(
                        "Ish tugadi. Birinchi rasmni yuboring (Oldin):",
                        reply_markup=ReplyKeyboardRemove(),
                    )
                    await callback.answer()
                    return
                await callback.answer("Noto'g'ri status.", show_alert=True)
                return

            expected_status, new_status, notice, next_stage = stage_data[stage]
            if order.status != expected_status:
                await callback.answer(
                    "Statuslarni ketma-ket yangilang.", show_alert=True
                )
                return
            order.status = new_status
            if stage == "route":
                order.route_started_at = timestamp
            elif stage == "arrived":
                order.arrived_at = timestamp
            elif stage == "washing":
                order.washing_started_at = timestamp
            await session.commit()
            model = order.car_model
            plate = order.plate_number
        await callback.bot.send_message(
            settings.director_id,
            f"<b>{_safe(worker.name)}</b> | {_safe(model)} | "
            f"{_safe(plate)}\n{notice}",
        )
        await callback.message.edit_reply_markup(
            reply_markup=worker_status_keyboard(order_id, next_stage)
        )
        if stage == "arrived":
            async with sessions() as session:
                order = await session.get(Order, order_id)
                customer = await session.get(User, order.customer_id) if order else None
            await callback.message.answer(
                f"Mijoz telefoni: <b>{_safe(customer.phone if customer else '—')}</b>"
            )
        await callback.answer()

    @router.message(WorkerCompletionStates.waiting_before_photo, F.photo)
    async def receive_before_photo(message: Message, state: FSMContext) -> None:
        await state.update_data(before_photo_id=message.photo[-1].file_id)
        await state.set_state(WorkerCompletionStates.waiting_after_photo)
        await message.answer("Endi ikkinchi rasmni yuboring (Keyin):")

    @router.message(WorkerCompletionStates.waiting_after_photo, F.photo)
    async def receive_after_photo(message: Message, state: FSMContext) -> None:
        await state.update_data(after_photo_id=message.photo[-1].file_id)
        await state.set_state(WorkerCompletionStates.waiting_comment)
        await message.answer("Ish bo'yicha qisqa izoh yuboring:")

    @router.message(WorkerCompletionStates.waiting_before_photo)
    async def require_before_photo(message: Message) -> None:
        await message.answer("Iltimos, avval «Oldin» rasmini yuboring.")

    @router.message(WorkerCompletionStates.waiting_after_photo)
    async def require_after_photo(message: Message) -> None:
        await message.answer("Iltimos, «Keyin» rasmini yuboring.")

    @router.message(WorkerCompletionStates.waiting_comment, F.text)
    async def complete_order(message: Message, state: FSMContext) -> None:
        if not message.from_user:
            return
        data = await state.get_data()
        comment = message.text.strip()
        if not comment or len(comment) > 2000:
            await message.answer("Qisqa izoh 1–2000 belgi bo'lishi kerak.")
            return
        async with sessions() as session:
            order = await session.get(Order, data["order_id"])
            worker = await get_worker(session, message.from_user.id)
            if (
                not order
                or not worker
                or order.worker_id != message.from_user.id
                or order.status != "yakunlanmoqda"
            ):
                await state.clear()
                await message.answer("Buyurtma topilmadi yoki sizga tegishli emas.")
                return
            customer = await session.get(User, order.customer_id)
            completed_at = now_tashkent()
            order.status = "yakunlandi"
            order.completed_at = completed_at
            order.before_photo_id = data["before_photo_id"]
            order.after_photo_id = data["after_photo_id"]
            order.worker_comment = comment
            worker.status = "bo'sh"
            duration = _duration_text(order.washing_started_at, completed_at)
            report = (
                f"<b>Yakuniy hisobot | Buyurtma #{order.id}</b>\n\n"
                f"<b>Ishchi:</b> {_safe(worker.name)}\n"
                f"<b>Mijoz:</b> {_safe(customer.name if customer else '—')} "
                f"({_safe(customer.phone if customer else '—')})\n"
                f"<b>Mashina:</b> {_safe(order.car_model)}\n"
                f"<b>Davlat raqami:</b> {_safe(order.plate_number)}\n"
                f"<b>Narx:</b> {_safe(format_price(int(order.car_price)))}\n"
                f"<b>To'lov:</b> {_safe(order.payment_method)}\n"
                f"<b>Ish davomiyligi:</b> {_safe(duration)}\n"
                f"<b>Izoh:</b> {_safe(comment)}"
            )
            await session.commit()
            before_photo_id = order.before_photo_id
            after_photo_id = order.after_photo_id
            customer_id = order.customer_id
        await message.answer(
            f"Buyurtma #{data['order_id']} yakunlandi.",
            reply_markup=worker_menu_keyboard(),
        )
        await message.bot.send_message(settings.director_id, report)
        await message.bot.send_photo(
            settings.director_id,
            before_photo_id,
            caption="Oldin",
        )
        await message.bot.send_photo(
            settings.director_id,
            after_photo_id,
            caption="Keyin",
        )
        await message.bot.send_message(
            customer_id,
            "Buyurtmangiz yakunlandi. RYX Wash xizmatidan foydalanganingiz uchun rahmat!",
        )
        await offer_next_queued_order(message.from_user.id, message.bot)
        await state.clear()

    @router.message(Command("worker_id"))
    async def worker_id(message: Message) -> None:
        if message.from_user:
            await message.answer(f"Sizning Telegram ID: <code>{message.from_user.id}</code>")

    @router.message(WorkerCompletionStates.waiting_comment)
    async def require_completion_comment(message: Message) -> None:
        await message.answer("Iltimos, ish bo'yicha qisqa izohni matn shaklida yuboring.")


def register_worker_routes(
    router: Router,
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    scheduler: AsyncIOScheduler,
) -> None:
    _register_user_routes(router, sessions, settings, scheduler)