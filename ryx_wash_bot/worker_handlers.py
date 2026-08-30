import html
import logging
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .catalog import format_price
from .config import Settings
from .keyboards import (
    available_workers_keyboard,
    busy_workers_keyboard,
    cancellation_reasons_keyboard,
    cancel_only_keyboard,
    director_menu_keyboard,
    group_workers_keyboard,
    new_order_assignment_keyboard,
    no_available_workers_keyboard,
    wash_duration_keyboard,
    queue_offer_decision_keyboard,
    worker_cabinet_period_keyboard,
    worker_deactivate_confirm_keyboard,
    worker_management_keyboard,
    worker_menu_keyboard,
    worker_order_decision_keyboard,
    worker_payment_keyboard,
    worker_status_keyboard,
)
from .models import Cancellation, Order, User, Worker
from .reports import period_bounds
from .scheduler import (
    configure_worker_available_handler,
    remove_wash_timeout,
    schedule_wash_timeout,
)
from .states import (
    CancellationStates,
    DirectorAssignmentStates,
    WorkerOrderStates,
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


def _plate_display(plate_number: str | None) -> str:
    return plate_number or "Ishchi manzilda kiritadi"


def _payment_display(payment_method: str | None) -> str:
    return payment_method or "Mijoz oldida aniqlanadi"


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

    async def render_worker_cabinet(
        worker_id: int,
        period_code: str = "today",
    ) -> str | None:
        try:
            start, end = period_bounds(period_code)
        except ValueError:
            return None
        period_labels = {
            "today": "Bugun",
            "week": "Shu hafta",
            "month": "Shu oy",
        }
        async with sessions() as session:
            worker = await get_worker(session, worker_id)
            if worker is None:
                return None
            order_filter = (
                Order.worker_id == worker_id,
                Order.status == "yakunlandi",
                Order.completed_at >= start,
                Order.completed_at < end,
            )
            washed_count = await session.scalar(
                select(func.count(Order.id)).where(*order_filter)
            )
            total_revenue = await session.scalar(
                select(func.coalesce(func.sum(Order.car_price), 0)).where(
                    *order_filter
                )
            )
            earnings = Decimal(str(total_revenue or 0)) * Decimal(
                worker.share_percent
            ) / Decimal("100")
            recent_orders = list(
                (
                    await session.scalars(
                        select(Order)
                        .where(Order.worker_id == worker_id)
                        .order_by(Order.created_at.desc(), Order.id.desc())
                        .limit(10)
                    )
                ).all()
            )

            now = now_tashkent()
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            tomorrow = today_start + timedelta(days=1)
            shift_start = worker.shift_started_at
            shift_end = worker.shift_ended_at
            is_at_work = worker.status in {"bo'sh", "band"} or (
                shift_start is not None and shift_end is None
            )
            if is_at_work:
                work_start = (
                    max(shift_start, today_start)
                    if shift_start is not None
                    else today_start
                )
                work_end = now
            elif shift_end is not None and shift_end >= today_start:
                work_start = max(
                    shift_start or today_start,
                    today_start,
                )
                work_end = min(shift_end, tomorrow)
            else:
                work_start = work_end = today_start
            worked_today = _duration_text(work_start, work_end)

        lines = [
            "<b>👤 Mening kabinetim</b>",
            "",
            f"<b>👤 Ism:</b> {_safe(worker.name)}",
            f"<b>📞 Telefon:</b> {_safe(worker.phone)}",
            f"<b>📈 Foiz ulushi:</b> {_safe(worker.share_percent)}%",
            f"<b>🗓️ Davr:</b> {_safe(period_labels[period_code])}",
            f"<b>🧼 Yuvilgan mashinalar:</b> {washed_count or 0} ta",
            f"<b>💰 Ishlab topgan summa:</b> "
            f"{_safe(format_price(int(earnings)))}",
            "",
            f"<b>🕒 Joriy smena:</b> "
            f"{'ishda' if is_at_work else 'ishda emas'}",
            f"<b>⏱️ Bugungi ish vaqti:</b> {_safe(worked_today)}",
            "",
            "<b>📋 Oxirgi 10 ta buyurtma:</b>",
        ]
        if not recent_orders:
            lines.append("📭 Buyurtmalar hali yo'q.")
        else:
            for order in recent_orders:
                order_date = (order.completed_at or order.created_at).astimezone(
                    TASHKENT
                )
                lines.append(
                    f"• {order_date:%d.%m.%Y} | "
                    f"{_safe(order.car_model)} | "
                    f"{_safe(_plate_display(order.plate_number))} | "
                    f"{_safe(format_price(int(order.car_price)))}"
                )
        return "\n".join(lines)

    @router.message(F.text.in_({"👤 Mening kabinetim", "Mening kabinetim"}))
    async def show_worker_cabinet(message: Message) -> None:
        if not message.from_user:
            return
        text = await render_worker_cabinet(message.from_user.id)
        if text is None:
            await message.answer("❌ Siz ishchi sifatida ro'yxatdan o'tmagansiz.")
            return
        await message.answer(text, reply_markup=worker_cabinet_period_keyboard())

    @router.callback_query(F.data.startswith("cabinet_period:"))
    async def change_worker_cabinet_period(callback: CallbackQuery) -> None:
        period_code = callback.data.split(":", 1)[1]
        if not callback.from_user:
            return
        text = await render_worker_cabinet(callback.from_user.id, period_code)
        if text is None:
            await callback.answer("❌ Ishchi kabineti topilmadi.", show_alert=True)
            return
        await callback.message.edit_text(
            text,
            reply_markup=worker_cabinet_period_keyboard(),
        )
        await callback.answer()

    async def notify_customer(bot, customer_id: int, text: str) -> None:
        if customer_id <= 0:
            return
        try:
            await bot.send_message(customer_id, text)
        except Exception:
            logger.exception("Could not notify customer %s", customer_id)

    def worker_offer_text(order: Order) -> str:
        wash_duration = order.wash_duration_minutes or 60
        return (
            f"<b>🆕 Yangi buyurtma #{order.id}</b>\n\n"
            f"<b>🚗 Mashina:</b> {_safe(order.car_model)}\n"
            f"<b>💰 Narx:</b> {_safe(format_price(int(order.car_price)))}\n\n"
            f"🧼 Yuvish uchun vaqt: <b>{wash_duration} daqiqa</b>\n\n"
            "📥 Buyurtmani qabul qilasizmi?"
        )

    async def activate_queued_order(
        order_id: int,
        worker_id: int,
        bot,
    ) -> bool:
        async with sessions() as session:
            order_snapshot = await session.get(Order, order_id)
            if order_snapshot and order_snapshot.order_group_id:
                group_orders = list(
                    (
                        await session.scalars(
                            select(Order)
                            .where(
                                Order.order_group_id
                                == order_snapshot.order_group_id
                            )
                            .order_by(Order.id)
                            .with_for_update()
                        )
                    ).all()
                )
                order = next(
                    (item for item in group_orders if item.id == order_id),
                    None,
                )
            else:
                order = await session.get(Order, order_id, with_for_update=True)
                group_orders = [order] if order else []
            worker = await session.get(Worker, worker_id, with_for_update=True)
            if (
                not order
                or not worker
                or not worker.active
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
            sibling_count = 0
            if order.order_group_id and order.group_mode == "single":
                siblings = [
                    sibling
                    for sibling in group_orders
                    if sibling.id != order.id
                    and sibling.status in {"yangi", "navbatda"}
                ]
                for sibling in siblings:
                    sibling.status = "navbatda"
                    sibling.worker_id = worker_id
                    sibling.group_mode = "single"
                    sibling_count += 1
            customer = await session.get(User, order.customer_id)
            wash_duration = order.wash_duration_minutes or 60
            text = (
            f"<b>📋 Navbatdagi buyurtma #{order.id}</b>\n\n"
            f"<b>👤 Mijoz:</b> {_safe(customer.name if customer else '—')}\n"
            f"<b>🚗 Mashina:</b> {_safe(order.car_model)}\n"
            f"<b>🪪 Davlat raqami:</b> {_safe(_plate_display(order.plate_number))}\n"
            f"<b>🎨 Rang:</b> {_safe(order.car_color or '—')}\n"
            f"<b>💳 To'lov:</b> {_safe(_payment_display(order.payment_method))}\n"
            f"<b>💰 Narx:</b> {_safe(format_price(int(order.car_price)))}\n"
            f"🧼 Yuvish uchun vaqt: <b>{wash_duration} daqiqa</b>\n"
            f"<b>📍 Manzil:</b> {_safe(order.address or 'Telegram lokatsiyasi')}\n"
            f"<b>📝 Izoh:</b> {_safe(order.comment or '—')}\n\n"
                "Buyurtmani qabul qilasizmi?"
            )
            latitude = float(order.latitude) if order.latitude is not None else None
            longitude = float(order.longitude) if order.longitude is not None else None
            await session.commit()
        await bot.send_message(
            worker_id,
            text,
            reply_markup=worker_order_decision_keyboard(order_id),
        )
        if latitude is not None and longitude is not None:
            await bot.send_location(
                worker_id,
                latitude=latitude,
                longitude=longitude,
            )
        if sibling_count:
            await bot.send_message(
                worker_id,
                f"Sizda navbatda yana {sibling_count} ta guruh buyurtmasi bor.",
            )
        return True

    async def offer_next_queued_order(worker_id: int, bot) -> None:
        async with sessions() as session:
            worker = await session.get(Worker, worker_id)
            if not worker or not worker.active or worker.status != "bo'sh":
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
            queued_order = await session.scalar(
                select(Order)
                .where(
                    Order.status == "navbatda",
                    Order.worker_id.is_(None),
                    Order.queue_offer_worker_id.is_(None),
                )
                .order_by(Order.created_at, Order.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if not queued_order:
                return
            worker = await session.get(Worker, worker_id, with_for_update=True)
            if not worker or not worker.active or worker.status != "bo'sh":
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
            f"👷 {_safe(worker_name)} endi bo'sh, navbatdagi buyurtmani "
            f"({_safe(customer_name)}, {_safe(car_model)}) beramizmi?",
            reply_markup=queue_offer_decision_keyboard(order_id, worker_id),
        )

    configure_worker_available_handler(offer_next_queued_order)

    @router.callback_query(F.data.startswith("group_single:"))
    async def choose_single_worker_for_group(callback: CallbackQuery) -> None:
        _, group_id, _lead_id = callback.data.split(":")
        async with sessions() as session:
            if not await is_director(session, callback.from_user.id):
                await callback.answer("❌ Bu amal faqat direktor uchun.", show_alert=True)
                return
            orders = list(
                (
                    await session.scalars(
                        select(Order)
                        .where(Order.order_group_id == group_id)
                        .order_by(Order.id)
                        .with_for_update()
                    )
                ).all()
            )
            if not orders or any(
                order.status not in {"yangi", "navbatda"}
                or order.worker_id is not None
                for order in orders
            ):
                await callback.answer(
                    "⚠️ Guruh buyurtmasining holati o'zgargan.", show_alert=True
                )
                return
            workers = list(
                (
                    await session.scalars(
                        select(Worker)
                        .where(Worker.active.is_(True))
                        .where(Worker.status.in_({"bo'sh", "band"}))
                        .order_by(Worker.name)
                    )
                ).all()
            )
            for order in orders:
                order.group_mode = "single"
            if not workers:
                for order in orders:
                    order.status = "navbatda"
                await session.commit()
                await callback.message.edit_reply_markup(reply_markup=None)
                await callback.answer("✅ Guruh navbatga qo'yildi.")
                await callback.message.answer(
                    "⚠️ Hozir smenadagi ishchi yo'q. Guruh global navbatga qo'yildi."
                )
                return
            await session.commit()
        await callback.answer()
        await callback.message.answer(
            "👷 Guruhdagi barcha mashinalar uchun bitta ishchini tanlang:",
            reply_markup=group_workers_keyboard(int(_lead_id), workers),
        )

    @router.callback_query(F.data.startswith("group_worker:"))
    async def choose_group_wash_duration(callback: CallbackQuery) -> None:
        _, lead_order_id_raw, worker_id_raw = callback.data.split(":")
        lead_order_id, worker_id = int(lead_order_id_raw), int(worker_id_raw)
        async with sessions() as session:
            if not await is_director(session, callback.from_user.id):
                await callback.answer("❌ Bu amal faqat direktor uchun.", show_alert=True)
                return
            lead_order = await session.get(Order, lead_order_id)
            group_id = lead_order.order_group_id if lead_order else None
            orders = list(
                (
                    await session.scalars(
                        select(Order)
                        .where(Order.order_group_id == group_id)
                        .order_by(Order.id)
                    )
                ).all()
            )
            worker = await session.get(Worker, worker_id)
            if (
                not lead_order
                or not group_id
                or not worker
                or not worker.active
                or worker.status not in {"bo'sh", "band"}
                or not orders
                or any(
                    order.status not in {"yangi", "navbatda"}
                    or order.worker_id is not None
                    for order in orders
                )
            ):
                await callback.answer(
                    "⚠️ Ishchi yoki guruh holati o'zgargan.",
                    show_alert=True,
                )
                return
        await callback.answer()
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            "🧼 Mashinani yuvish uchun vaqtni tanlang:",
            reply_markup=wash_duration_keyboard(
                "group_worker_wash_duration", lead_order_id, worker_id
            ),
        )

    @router.callback_query(F.data.startswith("group_worker_wash_duration:"))
    async def assign_group_to_worker_with_wash_duration(
        callback: CallbackQuery,
    ) -> None:
        _, lead_order_id_raw, worker_id_raw, duration_raw = callback.data.split(":")
        lead_order_id, worker_id, wash_duration = (
            int(lead_order_id_raw),
            int(worker_id_raw),
            int(duration_raw),
        )
        if not 30 <= wash_duration <= 120:
            await callback.answer("❌ Yuvish vaqti noto'g'ri.", show_alert=True)
            return
        async with sessions() as session:
            if not await is_director(session, callback.from_user.id):
                await callback.answer("❌ Bu amal faqat direktor uchun.", show_alert=True)
                return
            lead_order_snapshot = await session.get(Order, lead_order_id)
            group_id = (
                lead_order_snapshot.order_group_id
                if lead_order_snapshot
                else None
            )
            orders = list(
                (
                    await session.scalars(
                        select(Order)
                        .where(Order.order_group_id == group_id)
                        .order_by(Order.id)
                        .with_for_update()
                    )
                ).all()
            )
            lead_order = next(
                (order for order in orders if order.id == lead_order_id),
                None,
            )
            worker = await session.get(Worker, worker_id, with_for_update=True)
            if (
                not worker
                or not worker.active
                or not lead_order
                or not group_id
                or worker.status not in {"bo'sh", "band"}
                or not orders
                or any(
                    order.status not in {"yangi", "navbatda"}
                    or order.worker_id is not None
                    for order in orders
                )
            ):
                await callback.answer(
                    "⚠️ Ishchi yoki guruh holati o'zgargan.", show_alert=True
                )
                return
            for order in orders:
                order.status = "navbatda"
                order.worker_id = worker_id
                order.group_mode = "single"
                order.wash_duration_minutes = wash_duration
            lead_order_id = orders[0].id
            worker_was_free = worker.status == "bo'sh"
            worker_name = worker.name
            await session.commit()

        if worker_was_free:
            activated = await activate_queued_order(
                lead_order_id, worker_id, callback.bot
            )
            if not activated:
                await callback.answer(
                    "Guruhni ishchiga yuborib bo'lmadi.", show_alert=True
                )
                return
        else:
            await callback.bot.send_message(
                worker_id,
                f"📋 Sizda navbatda yana {len(orders)} ta buyurtma bor.",
            )
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer()
        await callback.message.answer(
            f"✅ Guruhdagi {len(orders)} ta mashina {worker_name} ga biriktirildi.\n"
            f"🧼 Yuvish uchun vaqt: {wash_duration} daqiqa."
        )

    @router.callback_query(F.data.startswith("group_split:"))
    async def split_group_orders(callback: CallbackQuery) -> None:
        _, group_id, _lead_id = callback.data.split(":")
        async with sessions() as session:
            if not await is_director(session, callback.from_user.id):
                await callback.answer("Bu amal faqat direktor uchun.", show_alert=True)
                return
            orders = list(
                (
                    await session.scalars(
                        select(Order)
                        .where(Order.order_group_id == group_id)
                        .order_by(Order.id)
                        .with_for_update()
                    )
                ).all()
            )
            if not orders or any(
                order.status not in {"yangi", "navbatda"}
                or order.worker_id is not None
                for order in orders
            ):
                await callback.answer(
                    "Guruh buyurtmasining holati o'zgargan.", show_alert=True
                )
                return
            for order in orders:
                order.group_mode = "split"
            await session.commit()
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer()
        await callback.message.answer(
            "👷 Har bir mashina uchun ishchini alohida tanlang:"
        )
        for order in orders:
            await callback.bot.send_message(
                settings.director_id,
            f"<b>📋 Buyurtma #{order.id}</b>\n"
            f"<b>🚗 Mashina:</b> {_safe(order.car_model)}\n"
            f"<b>🪪 Davlat raqami:</b> {_safe(_plate_display(order.plate_number))}\n"
            f"<b>🎨 Rang:</b> {_safe(order.car_color or '—')}\n"
            f"<b>💰 Narx:</b> {_safe(format_price(int(order.car_price)))}",
                reply_markup=new_order_assignment_keyboard(order.id),
            )

    @router.message(F.text.in_({"➕👷 Ishchi qo'shish", "Ishchi qo'shish"}))
    async def start_worker_registration(
        message: Message, state: FSMContext
    ) -> None:
        if not message.from_user:
            return
        async with sessions() as session:
            if not await is_director(session, message.from_user.id):
                await message.answer("❌ Bu amal faqat direktor uchun.")
                return
        await state.clear()
        await state.set_state(WorkerRegistrationStates.waiting_user_id)
        await message.answer(
            "🪪 Ishchining Telegram ID raqamini yuboring:",
            reply_markup=ReplyKeyboardRemove(),
        )

    @router.message(F.text == "👷 Ishchilarni boshqarish")
    async def manage_workers(message: Message) -> None:
        if not message.from_user:
            return
        async with sessions() as session:
            if not await is_director(session, message.from_user.id):
                await message.answer("❌ Bu bo'lim faqat direktor uchun.")
                return
            workers = list(
                (
                    await session.scalars(
                        select(Worker).order_by(Worker.active.desc(), Worker.name)
                    )
                ).all()
            )
        if not workers:
            await message.answer("📭 Hali ishchilar ro'yxatdan o'tmagan.")
            return
        await message.answer(
            "👷 Ishchilar boshqaruvi:\n"
            "Faol ishchini faolsizlantirish yoki oldingi ishchini qayta "
            "faollashtirish uchun tanlang.",
            reply_markup=worker_management_keyboard(workers),
        )

    @router.callback_query(F.data.startswith("worker_manage:"))
    async def choose_worker_management_action(callback: CallbackQuery) -> None:
        if not callback.from_user:
            return
        _, action, worker_id_raw = callback.data.split(":")
        worker_id = int(worker_id_raw)
        async with sessions() as session:
            if not await is_director(session, callback.from_user.id):
                await callback.answer("❌ Bu amal faqat direktor uchun.", show_alert=True)
                return
            worker = await session.get(Worker, worker_id)
            if not worker:
                await callback.answer("❌ Ishchi topilmadi.", show_alert=True)
                return
            worker_name = worker.name
            is_active = worker.active
        if action == "deactivate":
            if not is_active:
                await callback.answer("Ishchi allaqachon faol emas.", show_alert=True)
                return
            await callback.answer()
            await callback.message.answer(
                f"⚠️ «{_safe(worker_name)}» ishchisini faolsizlantirishni "
                "tasdiqlaysizmi?",
                reply_markup=worker_deactivate_confirm_keyboard(worker_id),
            )
            return
        if action == "activate":
            async with sessions() as session:
                worker = await session.get(Worker, worker_id)
                if not worker:
                    await callback.answer("❌ Ishchi topilmadi.", show_alert=True)
                    return
                worker.active = True
                worker.status = "smenada_emas"
                await session.commit()
            await callback.answer("✅ Ishchi qayta faollashtirildi.")
            await callback.message.answer(
                f"✅ {_safe(worker_name)} qayta faol qilindi."
            )
            return
        await callback.answer("❌ Noto'g'ri amal.", show_alert=True)

    @router.callback_query(F.data.startswith("worker_deactivate_confirm:"))
    async def deactivate_worker(callback: CallbackQuery) -> None:
        if not callback.from_user:
            return
        worker_id = int(callback.data.split(":", 1)[1])
        async with sessions() as session:
            if not await is_director(session, callback.from_user.id):
                await callback.answer("❌ Bu amal faqat direktor uchun.", show_alert=True)
                return
            worker = await session.get(Worker, worker_id, with_for_update=True)
            if not worker:
                await callback.answer("❌ Ishchi topilmadi.", show_alert=True)
                return
            active_order = await session.scalar(
                select(Order.id).where(
                    Order.worker_id == worker_id,
                    Order.status.not_in({"yakunlandi", "bekor_qilindi"}),
                ).limit(1)
            )
            pending_offer = await session.scalar(
                select(Order.id).where(
                    Order.queue_offer_worker_id == worker_id,
                    Order.status == "navbatda",
                ).limit(1)
            )
            if active_order or pending_offer:
                await callback.answer(
                    "⚠️ Bu ishchida faol yoki navbatdagi buyurtma bor. "
                    "Avval buyurtmani yakunlang yoki boshqa ishchiga bering.",
                    show_alert=True,
                )
                return
            worker.active = False
            worker.status = "smenada_emas"
            worker_name = worker.name
            await session.commit()
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("✅ Ishchi faolsizlantirildi.")
        await callback.message.answer(
            f"✅ {_safe(worker_name)} faol ishchilar ro'yxatidan olib tashlandi."
        )

    @router.callback_query(F.data == "worker_manage_cancel")
    async def cancel_worker_management(callback: CallbackQuery) -> None:
        await callback.answer("Bekor qilindi.")
        await callback.message.edit_reply_markup(reply_markup=None)

    @router.message(WorkerRegistrationStates.waiting_user_id, F.text)
    async def receive_worker_id(message: Message, state: FSMContext) -> None:
        try:
            worker_id = int(message.text.strip())
        except ValueError:
            await message.answer("❌ Telegram ID faqat raqam bo'lishi kerak.")
            return
        if worker_id <= 0 or worker_id == settings.director_id:
            await message.answer("❌ Iltimos, to'g'ri ishchi Telegram ID'sini yuboring.")
            return
        await state.update_data(worker_id=worker_id)
        await state.set_state(WorkerRegistrationStates.waiting_name)
        await message.answer("👤 Ishchining ism-familyasini yuboring:")

    @router.message(WorkerRegistrationStates.waiting_name, F.text)
    async def receive_worker_name(message: Message, state: FSMContext) -> None:
        name = message.text.strip()
        if len(name) < 2 or len(name) > 150:
            await message.answer("❌ Ism-familya 2–150 belgi bo'lishi kerak.")
            return
        await state.update_data(name=name)
        await state.set_state(WorkerRegistrationStates.waiting_phone)
        await message.answer("📞 Ishchining telefon raqamini yuboring:")

    @router.message(WorkerRegistrationStates.waiting_phone, F.text)
    async def receive_worker_phone(message: Message, state: FSMContext) -> None:
        phone = message.text.strip()
        if len(phone) < 5 or len(phone) > 40:
            await message.answer("❌ Telefon raqamini to'g'ri kiriting:")
            return
        await state.update_data(phone=phone)
        await state.set_state(WorkerRegistrationStates.waiting_percent)
        await message.answer("📈 Ishchining foiz ulushini kiriting (masalan: 30%):")

    @router.message(WorkerRegistrationStates.waiting_percent, F.text)
    async def receive_worker_percent(message: Message, state: FSMContext) -> None:
        raw_percent = message.text.strip().replace("%", "").replace(",", ".")
        try:
            percent = Decimal(raw_percent)
        except InvalidOperation:
            await message.answer("❌ Foizni raqam ko'rinishida kiriting, masalan 30%.")
            return
        if percent <= 0 or percent > 100:
            await message.answer("❌ Foiz 0 dan katta va 100 dan kichik yoki teng bo'lsin.")
            return

        data = await state.get_data()
        async with sessions() as session:
            director = await find_user(session, message.from_user.id)
            if not director or director.rol != "direktor":
                await message.answer("❌ Bu amal faqat direktor uchun.")
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
                worker.active = True
                worker.status = "smenada_emas"
            await session.commit()

        await state.clear()
        await message.answer(
            f"✅ {_safe(data['name'])} ishchi sifatida qo'shildi. "
            "Boshlang'ich holati: smenada emas.",
            reply_markup=director_menu_keyboard(),
        )

    @router.message(F.text.in_({"🟢 Ishga keldim", "Ishga keldim"}))
    async def start_shift(message: Message) -> None:
        if not message.from_user:
            return
        async with sessions() as session:
            worker = await get_worker(session, message.from_user.id)
            if worker is None:
                await message.answer("❌ Siz ishchi sifatida ro'yxatdan o'tmagansiz.")
                return
            if not worker.active:
                await message.answer("⚠️ Sizning ishchi profilingiz faol emas.")
                return
            if worker.status == "band":
                await message.answer("⚠️ Siz hozir buyurtma bilan bandsiz.")
                return
            worker.status = "bo'sh"
            worker.shift_started_at = now_tashkent()
            worker.shift_ended_at = None
            user = await find_user(session, message.from_user.id)
            if user:
                user.rol = "ishchi"
            await session.commit()
        await message.answer(
            "✅ Smena boshlandi. Siz hozir bo'shsiz.",
            reply_markup=worker_menu_keyboard(),
        )

    @router.message(F.text.in_({"🔴 Ishdan ketdim", "Ishdan ketdim"}))
    async def end_shift(message: Message) -> None:
        if not message.from_user:
            return
        async with sessions() as session:
            worker = await get_worker(session, message.from_user.id)
            if worker is None:
                await message.answer("❌ Siz ishchi sifatida ro'yxatdan o'tmagansiz.")
                return
            if not worker.active:
                await message.answer("⚠️ Sizning ishchi profilingiz faol emas.")
                return
            if worker.status == "band":
                await message.answer(
                    "⚠️ Buyurtma yakunlanmaguncha smenani tugatib bo'lmaydi."
                )
                return
            worker.status = "smenada_emas"
            worker.shift_ended_at = now_tashkent()
            await session.commit()
        await message.answer(
            "✅ Smena tugadi.",
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
                await callback.answer("❌ Bu amal faqat direktor uchun.", show_alert=True)
                return
            order = await session.get(Order, order_id, with_for_update=True)
            if (
                not order
                or order.status not in {"yangi", "navbatda"}
                or order.worker_id is not None
            ):
                await callback.answer(
                    "⚠️ Buyurtma hozir ishchiga yuborish uchun tayyor emas.",
                    show_alert=True,
                )
                return
            workers = list(
                (
                    await session.scalars(
                        select(Worker)
                        .where(Worker.active.is_(True))
                        .where(Worker.status == "bo'sh")
                        .order_by(Worker.name)
                    )
                ).all()
            )
        if not workers:
            await callback.answer()
            await callback.message.answer(
                "⚠️ Hozir barcha ishchilar band yoki smenada emas. "
                "Buyurtma bilan nima qilamiz?",
                reply_markup=no_available_workers_keyboard(order_id),
            )
            return
        await callback.answer()
        await callback.message.answer(
            f"👷 Buyurtma #{order_id} uchun bo'sh ishchini tanlang:",
            reply_markup=available_workers_keyboard(order_id, workers),
        )

    @router.callback_query(F.data.startswith("queue_order:"))
    async def queue_order(callback: CallbackQuery) -> None:
        order_id = int(callback.data.split(":", 1)[1])
        async with sessions() as session:
            if not await is_director(session, callback.from_user.id):
                await callback.answer("❌ Bu amal faqat direktor uchun.", show_alert=True)
                return
            order = await session.get(Order, order_id, with_for_update=True)
            if (
                not order
                or order.status not in {"yangi", "navbatda"}
                or order.worker_id is not None
            ):
                await callback.answer(
                    "⚠️ Bu buyurtmani navbatga qo'yib bo'lmaydi.", show_alert=True
                )
                return
            order.status = "navbatda"
            order.queued_offer = False
            order.queue_offer_worker_id = None
            order.queue_prompted_at = None
            await session.commit()
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("✅ Buyurtma navbatga qo'yildi.")
        await callback.message.answer(f"✅ Buyurtma #{order_id} navbatga qo'yildi.")

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
                        .where(Worker.active.is_(True))
                        .where(Worker.status == "band")
                        .where(Order.status.in_(ACTIVE_ACCEPTED_STATUSES))
                        .order_by(Worker.name)
                    )
                ).all()
            )
        if not workers:
            await callback.answer("📭 Band ishchilar topilmadi.", show_alert=True)
            return
        await callback.answer()
        await callback.message.answer(
            f"Buyurtma #{order_id} uchun band ishchini tanlang:",
            reply_markup=busy_workers_keyboard(order_id, workers),
        )

    @router.callback_query(F.data.startswith("queue_worker:"))
    async def choose_queued_wash_duration(callback: CallbackQuery) -> None:
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
                or not worker.active
                or order.status not in {"yangi", "navbatda"}
                or order.worker_id is not None
                or worker.status != "band"
            ):
                await callback.answer(
                    "Buyurtma yoki ishchi holati o'zgargan.",
                    show_alert=True,
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
        await callback.answer()
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            "🧼 Navbatdagi mashinani yuvish uchun vaqtni tanlang:",
            reply_markup=wash_duration_keyboard(
                "queue_worker_wash_duration", order_id, worker_id
            ),
        )

    @router.callback_query(F.data.startswith("queue_worker_wash_duration:"))
    async def assign_to_busy_worker(callback: CallbackQuery) -> None:
        _, order_id_raw, worker_id_raw, duration_raw = callback.data.split(":")
        order_id, worker_id, wash_duration = (
            int(order_id_raw),
            int(worker_id_raw),
            int(duration_raw),
        )
        if not 30 <= wash_duration <= 120:
            await callback.answer("❌ Yuvish vaqti noto'g'ri.", show_alert=True)
            return
        async with sessions() as session:
            if not await is_director(session, callback.from_user.id):
                await callback.answer("Bu amal faqat direktor uchun.", show_alert=True)
                return
            order = await session.get(Order, order_id, with_for_update=True)
            worker = await session.get(Worker, worker_id, with_for_update=True)
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
            order.wash_duration_minutes = wash_duration
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
            f"Buyurtma #{order_id} {worker_name} uchun navbatga biriktirildi.\n"
            f"🧼 Yuvish uchun vaqt: {wash_duration} daqiqa."
        )

    @router.callback_query(F.data.startswith("queue_offer:"))
    async def decide_global_queue_offer(callback: CallbackQuery) -> None:
        _, order_id_raw, worker_id_raw, decision = callback.data.split(":")
        order_id, worker_id = int(order_id_raw), int(worker_id_raw)
        async with sessions() as session:
            if not await is_director(session, callback.from_user.id):
                await callback.answer("Bu amal faqat direktor uchun.", show_alert=True)
                return
            order = await session.get(Order, order_id, with_for_update=True)
            worker = await session.get(Worker, worker_id, with_for_update=True)
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
                await callback.answer("📋 Buyurtma navbatda qoldi.")
                return
            if decision != "yes":
                await callback.answer("❌ Noto'g'ri tanlov.", show_alert=True)
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
        await callback.answer("✅ Navbatdagi buyurtma ishchiga yuborildi.")

    @router.callback_query(F.data.startswith("assign_worker:"))
    async def choose_wash_duration(
        callback: CallbackQuery,
    ) -> None:
        if not callback.from_user:
            return
        _, order_id_raw, worker_id_raw = callback.data.split(":")
        order_id, worker_id = int(order_id_raw), int(worker_id_raw)
        async with sessions() as session:
            if not await is_director(session, callback.from_user.id):
                await callback.answer("❌ Bu amal faqat direktor uchun.", show_alert=True)
                return
            order = await session.get(Order, order_id)
            worker = await session.get(Worker, worker_id)
            if (
                not order
                or not worker
                or not worker.active
                or order.status not in {"yangi", "navbatda"}
                or order.worker_id is not None
                or worker.status != "bo'sh"
            ):
                await callback.answer(
                    "⚠️ Buyurtma yoki ishchi holati o'zgargan.",
                    show_alert=True,
                )
                return
        await callback.answer()
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            "🧼 Mashinani yuvish uchun vaqtni tanlang:",
            reply_markup=wash_duration_keyboard(
                "assign_worker_wash_duration", order_id, worker_id
            ),
        )

    @router.callback_query(F.data.startswith("assign_worker_wash_duration:"))
    async def assign_order(
        callback: CallbackQuery,
    ) -> None:
        if not callback.from_user:
            return
        _, order_id_raw, worker_id_raw, duration_raw = callback.data.split(":")
        order_id, worker_id, wash_duration = (
            int(order_id_raw),
            int(worker_id_raw),
            int(duration_raw),
        )
        if not 30 <= wash_duration <= 120:
            await callback.answer("❌ Yuvish vaqti noto'g'ri.", show_alert=True)
            return
        async with sessions() as session:
            if not await is_director(session, callback.from_user.id):
                await callback.answer("❌ Bu amal faqat direktor uchun.", show_alert=True)
                return
            # End the authorization read transaction before the compare-and-set
            # below. This also lets the SQLite regression test use independent
            # writer transactions like PostgreSQL production does.
            await session.commit()
            assigned_at = now_tashkent()
            order_claim = await session.execute(
                update(Order)
                .where(
                    Order.id == order_id,
                    Order.worker_id.is_(None),
                    Order.status.in_({"yangi", "navbatda"}),
                )
                .values(
                    worker_id=worker_id,
                    queue_offer_worker_id=None,
                    queue_prompted_at=None,
                    assigned_at=assigned_at,
                    status="ishchiga_yuborildi",
                    wash_duration_minutes=wash_duration,
                    queued_offer=case(
                        (Order.status == "navbatda", True),
                        else_=False,
                    ),
                )
                .execution_options(synchronize_session=False)
            )
            if order_claim.rowcount != 1:
                await session.rollback()
                order = await session.get(Order, order_id)
                worker = await session.get(Worker, worker_id)
                if not order or not worker:
                    await callback.answer(
                        "Buyurtma yoki ishchi topilmadi.",
                        show_alert=True,
                    )
                    return
                await callback.answer(
                    "Bu buyurtma allaqachon ishchiga yuborilgan.",
                    show_alert=True,
                )
                return
            worker_claim = await session.execute(
                update(Worker)
                .where(
                    Worker.user_id == worker_id,
                    Worker.active.is_(True),
                    Worker.status == "bo'sh",
                )
                .values(status="band")
                .execution_options(synchronize_session=False)
            )
            if worker_claim.rowcount != 1:
                await session.rollback()
                worker = await session.get(Worker, worker_id)
                if not worker:
                    await callback.answer(
                        "Buyurtma yoki ishchi topilmadi.",
                        show_alert=True,
                    )
                    return
                await callback.answer("⚠️ Bu ishchi endi bo'sh emas.", show_alert=True)
                return
            order = await session.get(Order, order_id)
            worker = await session.get(Worker, worker_id)
            await session.commit()
            if order is None or worker is None:
                raise RuntimeError("Claimed assignment rows could not be reloaded")
            worker_name = worker.name
            worker_text = worker_offer_text(order)
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
            f"Buyurtma #{order_id} {worker_name} ishchiga yuborildi.\n"
            f"🧼 Yuvish uchun vaqt: {wash_duration} daqiqa."
        )

    @router.callback_query(
        F.data.startswith("assign_worker_wash_duration_custom:")
    )
    @router.callback_query(
        F.data.startswith("group_worker_wash_duration_custom:")
    )
    @router.callback_query(
        F.data.startswith("queue_worker_wash_duration_custom:")
    )
    async def request_custom_wash_duration(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        if not callback.from_user:
            return
        prefix, order_id_raw, worker_id_raw = callback.data.split(":")
        async with sessions() as session:
            if not await is_director(session, callback.from_user.id):
                await callback.answer(
                    "❌ Bu amal faqat direktor uchun.", show_alert=True
                )
                return
        if prefix.startswith("assign_worker"):
            assignment_kind = "direct"
        elif prefix.startswith("group_worker"):
            assignment_kind = "group"
        else:
            assignment_kind = "busy"
        await state.set_state(DirectorAssignmentStates.waiting_custom_wash_duration)
        await state.update_data(
            assignment_kind=assignment_kind,
            order_id=int(order_id_raw),
            worker_id=int(worker_id_raw),
        )
        await callback.answer()
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            "✍️ Yuvish vaqtini daqiqada kiriting (30 dan 120 gacha):"
        )

    class _CustomCallbackMessage:
        def __init__(self, message: Message) -> None:
            self._message = message

        async def answer(self, *args, **kwargs):
            return await self._message.answer(*args, **kwargs)

        async def edit_reply_markup(self, *args, **kwargs):
            return None

    class _CustomCallback:
        def __init__(self, message: Message, data: str) -> None:
            self.from_user = message.from_user
            self.bot = message.bot
            self.message = _CustomCallbackMessage(message)
            self.data = data

        async def answer(self, *args, **kwargs):
            return None

    @router.message(DirectorAssignmentStates.waiting_custom_wash_duration, F.text)
    async def receive_custom_wash_duration(
        message: Message, state: FSMContext
    ) -> None:
        value = message.text.strip()
        if not value.isdigit() or not 30 <= int(value) <= 120:
            await message.answer("❌ Vaqt 30 dan 120 gacha butun daqiqa bo‘lsin.")
            return
        data = await state.get_data()
        assignment_kind = data.get("assignment_kind")
        order_id = data.get("order_id")
        worker_id = data.get("worker_id")
        if (
            assignment_kind not in {"direct", "group", "busy"}
            or not isinstance(order_id, int)
            or not isinstance(worker_id, int)
        ):
            await state.clear()
            await message.answer("❌ Yuvish vaqti ma'lumoti topilmadi.")
            return
        await state.clear()
        duration = int(value)
        callback_prefix = {
            "direct": "assign_worker_wash_duration",
            "group": "group_worker_wash_duration",
            "busy": "queue_worker_wash_duration",
        }[assignment_kind]
        callback = _CustomCallback(
            message,
            f"{callback_prefix}:{order_id}:{worker_id}:{duration}",
        )
        if assignment_kind == "direct":
            await assign_order(
                callback,
            )
        elif assignment_kind == "group":
            await assign_group_to_worker_with_wash_duration(callback)
        else:
            await assign_to_busy_worker(callback)

    async def worker_order_and_worker(
        session: AsyncSession, order_id: int, worker_id: int
    ) -> tuple[Order | None, Worker | None]:
        order = await session.get(Order, order_id, with_for_update=True)
        worker = await session.get(Worker, worker_id, with_for_update=True)
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
                await callback.answer("❌ Bu buyurtma sizga biriktirilmagan.", show_alert=True)
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
            f"<b>✅ Buyurtma #{order.id} qabul qilindi</b>\n\n"
            f"<b>👤 Mijoz:</b> {_safe(customer_name)}\n"
            f"<b>🚗 Mashina:</b> {_safe(order.car_model)}\n"
            f"<b>🪪 Davlat raqami:</b> {_safe(_plate_display(order.plate_number))}\n"
            f"<b>💳 To'lov:</b> {_safe(_payment_display(order.payment_method))}\n"
            f"<b>💰 Narx:</b> {_safe(format_price(int(order.car_price)))}\n"
            f"🧼 <b>Yuvish vaqti:</b> "
            f"{_safe(order.wash_duration_minutes or 60)} daqiqa\n"
            f"<b>📍 Manzil:</b> {_safe(order.address or 'Telegram lokatsiyasi')}\n"
            f"<b>📝 Izoh:</b> {_safe(order.comment or '—')}"
            )
            latitude = float(order.latitude) if order.latitude is not None else None
            longitude = float(order.longitude) if order.longitude is not None else None
            address = order.address
            car_photo_id = order.car_photo_id
            customer_id = order.customer_id
        await notify_customer(
            callback.bot,
            customer_id,
            "Buyurtmangiz ishchiga biriktirildi. "
            "Xizmat jarayoni boshlanganda xabar beramiz.",
        )
        await callback.message.edit_text(full_text)
        await callback.message.answer(
            (
                "📍 Lokatsiya:"
                if latitude is not None and longitude is not None
                else f"Manzil: {_safe(address or '—')}"
            ),
            reply_markup=worker_status_keyboard(order_id, "route"),
        )
        if latitude is not None and longitude is not None:
            await callback.bot.send_location(
                callback.from_user.id,
                latitude=latitude,
                longitude=longitude,
            )
        if car_photo_id:
            await callback.bot.send_photo(
                callback.from_user.id,
                car_photo_id,
                 caption=f"📷 Buyurtma #{order_id} boshlang'ich mashina rasmi",
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
                await callback.answer("❌ Bu buyurtma sizga biriktirilmagan.", show_alert=True)
                return
            if order.status != "ishchiga_yuborildi":
                await callback.answer(
                    "Bu buyurtmaga avval javob berilgan.", show_alert=True
                )
                return
            worker.status = "bo'sh"
            single_group_id = (
                order.order_group_id
                if order.group_mode == "single" and order.order_group_id
                else None
            )
            order.worker_id = None
            order.assigned_at = None
            order.status = "navbatda" if order.queued_offer else "yangi"
            order.queued_offer = False
            order.queue_offer_worker_id = None
            order.queue_prompted_at = None
            if single_group_id:
                siblings = list(
                    (
                        await session.scalars(
                            select(Order).where(
                                Order.order_group_id == single_group_id,
                                Order.id != order.id,
                                Order.status == "navbatda",
                            )
                        )
                    ).all()
                )
                for sibling in siblings:
                    sibling.worker_id = None
                    sibling.queue_offer_worker_id = None
                    sibling.queue_prompted_at = None
            worker_name = worker.name
            await session.commit()
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.bot.send_message(
            settings.director_id,
            f"❌ {_safe(worker_name)} rad etdi (buyurtma #{order_id}).",
        )
        await callback.answer("❌ Buyurtma rad etildi.")
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
                await callback.answer("❌ Buyurtma topilmadi.", show_alert=True)
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
        remove_wash_timeout(scheduler, order_id)
        await bot.send_message(
            settings.director_id,
            f"🚫 Buyurtma #{order_id} bekor qilindi. Sabab: {_safe(reason)}",
        )
        if worker_id and worker_id != actor_id:
            await bot.send_message(
                worker_id,
                f"🚫 Buyurtma #{order_id} bekor qilindi. Sabab: {_safe(reason)}",
            )
        if worker_id and not was_waiting_in_queue:
            await offer_next_queued_order(worker_id, bot)
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
            await callback.answer("⚠️ Bekor qilish ma'lumoti eskirgan.", show_alert=True)
            return
        if reason_code == "other":
            await state.set_state(CancellationStates.waiting_custom_reason)
            await callback.answer()
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer(
                "📝 Bekor qilish sababini matn ko'rinishida yozing:"
            )
            return
        reasons = {
            "customer": "Mijoz voz kechdi",
            "location": "Manzil noto'g'ri/topilmadi",
            "worker": "Ishchi yetib bora olmadi",
        }
        reason = reasons.get(reason_code)
        if not reason:
            await callback.answer("❌ Noto'g'ri sabab.", show_alert=True)
            return
        completed = await finalize_cancellation(
            callback.from_user.id, order_id, reason, callback.bot
        )
        await state.clear()
        await callback.message.edit_reply_markup(reply_markup=None)
        if completed:
            await callback.answer("✅ Buyurtma bekor qilindi.")
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
            await message.answer("⚠️ Sabab majburiy. Iltimos, sababni yozing:")
            return
        if len(reason) > 2000:
            await message.answer("❌ Sabab 2000 belgidan oshmasin.")
            return
        data = await state.get_data()
        order_id = data.get("cancel_order_id")
        if not isinstance(order_id, int):
            await state.clear()
            await message.answer("❌ Bekor qilish ma'lumoti topilmadi.")
            return
        completed = await finalize_cancellation(
            message.from_user.id, order_id, reason, message.bot
        )
        await state.clear()
        if completed:
            await message.answer(f"✅ Buyurtma #{order_id} bekor qilindi.")
        else:
            await message.answer("❌ Buyurtmani bekor qilib bo'lmadi.")

    @router.message(CancellationStates.waiting_custom_reason)
    async def require_custom_cancellation_reason(message: Message) -> None:
        await message.answer("⚠️ Sabab majburiy. Uni matn ko'rinishida yozing:")

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
                await callback.answer("❌ Bu buyurtma sizga biriktirilmagan.", show_alert=True)
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
                    remove_wash_timeout(scheduler, order_id)
                    await state.set_state(WorkerCompletionStates.waiting_before_photo)
                    await state.update_data(order_id=order_id)
                    await callback.message.edit_reply_markup(reply_markup=None)
                    if not order.plate_number:
                        await state.set_state(WorkerOrderStates.waiting_plate)
                        await callback.message.answer(
                            "✅ Yuvish tugadi. Endi mashinaning davlat raqamini "
                            "kiriting:",
                            reply_markup=ReplyKeyboardRemove(),
                        )
                    elif not order.payment_method:
                        await state.set_state(WorkerOrderStates.waiting_payment)
                        await callback.message.answer(
                            "✅ Yuvish tugadi. Mijoz oldidagi to‘lov turini "
                            "tanlang:",
                            reply_markup=worker_payment_keyboard(order_id),
                        )
                    else:
                        await callback.message.answer(
                            "✅ Ish tugadi. Birinchi rasmni yuboring (Oldin):",
                            reply_markup=ReplyKeyboardRemove(),
                        )
                    await callback.answer()
                    return
                await callback.answer("❌ Noto'g'ri status.", show_alert=True)
                return

            expected_status, new_status, notice, next_stage = stage_data[stage]
            if order.status != expected_status:
                await callback.answer(
                    "Statuslarni ketma-ket yangilang.", show_alert=True
                )
                return
            if stage == "route":
                order.status = new_status
                order.route_started_at = timestamp
                worker_name = worker.name
                model = order.car_model
                plate = order.plate_number
                await session.commit()
                await state.set_state(WorkerOrderStates.waiting_arrival_eta)
                await state.update_data(order_id=order_id)
                await callback.message.edit_reply_markup(reply_markup=None)
                await callback.message.answer(
                    "🚗 Yo'lga chiqqaningiz belgilandi.\n"
                    "⏱ Mijozga ko'rsatish uchun taxminiy yetib borish "
                    "vaqtini butun daqiqalarda kiriting (1–1440):"
                )
                await callback.answer()
                return
            order.status = new_status
            wash_duration = None
            if stage == "route":
                order.route_started_at = timestamp
            elif stage == "arrived":
                order.arrived_at = timestamp
            elif stage == "washing":
                order.washing_started_at = timestamp
                wash_duration = order.wash_duration_minutes or 60
            await session.commit()
            model = order.car_model
            plate = order.plate_number
        await callback.bot.send_message(
            settings.director_id,
            f"<b>{_safe(worker.name)}</b> | {_safe(model)} | "
            f"{_safe(_plate_display(plate))}\n{notice}",
        )
        await callback.message.edit_reply_markup(
            reply_markup=worker_status_keyboard(order_id, next_stage)
        )
        if stage == "washing":
            schedule_wash_timeout(
                scheduler,
                order_id,
                timestamp,
                duration_minutes=wash_duration or 60,
            )
            await callback.message.answer(
                f"🧼 Yuvish boshlandi. Ajratilgan vaqt: "
                f"<b>{wash_duration or 60} daqiqa</b>."
            )
        if stage == "arrived":
            async with sessions() as session:
                order = await session.get(Order, order_id)
                customer = await session.get(User, order.customer_id) if order else None
            await callback.message.answer(
                f"Mijoz telefoni: <b>{_safe(customer.phone if customer else '—')}</b>"
            )
        await callback.answer()

    @router.message(WorkerOrderStates.waiting_arrival_eta, F.text)
    async def receive_arrival_eta(message: Message, state: FSMContext) -> None:
        if not message.from_user:
            return
        raw_eta = message.text.strip()
        if not raw_eta.isdigit() or not 1 <= int(raw_eta) <= 1440:
            await message.answer(
                "❌ Yetib borish vaqti 1 dan 1440 gacha bo'lgan butun "
                "daqiqalarda bo'lsin."
            )
            return
        eta_minutes = int(raw_eta)
        data = await state.get_data()
        order_id = data.get("order_id")
        if not isinstance(order_id, int):
            await state.clear()
            await message.answer("❌ Buyurtma ma'lumoti topilmadi.")
            return
        async with sessions() as session:
            order, worker = await worker_order_and_worker(
                session, order_id, message.from_user.id
            )
            if (
                not order
                or not worker
                or order.status != "yo'lda"
            ):
                await state.clear()
                await message.answer(
                    "❌ Buyurtma topilmadi yoki yo'lga chiqish bosqichi yopilgan."
                )
                return
            order.arrival_eta_minutes = eta_minutes
            customer_id = order.customer_id
            model = order.car_model
            plate = order.plate_number
            worker_name = worker.name
            await session.commit()
        await state.clear()
        eta_text = f"⏱ Taxminiy yetib kelish: <b>{eta_minutes} daqiqa</b>."
        await notify_customer(
            message.bot,
            customer_id,
            f"🚗 Ishchi yo'lga chiqdi.\n{eta_text}",
        )
        await message.bot.send_message(
            settings.director_id,
            f"<b>{_safe(worker_name)}</b> | {_safe(model)} | "
            f"{_safe(_plate_display(plate))}\n"
            f"🚗 Yo'lga chiqdi.\n{eta_text}",
        )
        await message.answer(
            f"✅ ETA saqlandi.\n{eta_text}\n"
            "Manzilga yetganingizda quyidagi tugmani bosing:",
            reply_markup=worker_status_keyboard(order_id, "arrived"),
        )

    @router.message(WorkerOrderStates.waiting_plate, F.text)
    async def receive_order_plate(message: Message, state: FSMContext) -> None:
        if not message.from_user:
            return
        plate = " ".join(message.text.split()).upper()
        if not plate or len(plate) > 30:
            await message.answer(
                "❌ Davlat raqami bo‘sh bo‘lmasin va 30 belgidan oshmasin."
            )
            return
        data = await state.get_data()
        order_id = data.get("order_id")
        if not isinstance(order_id, int):
            await state.clear()
            await message.answer("❌ Buyurtma ma'lumoti topilmadi.")
            return
        async with sessions() as session:
            order, worker = await worker_order_and_worker(
                session, order_id, message.from_user.id
            )
            if (
                not order
                or not worker
                or order.status != "yakunlanmoqda"
            ):
                await state.clear()
                await message.answer(
                    "❌ Buyurtma topilmadi yoki bu bosqich endi faol emas."
                )
                return
            order.plate_number = plate
            await session.commit()
            payment_method = order.payment_method
        if not payment_method:
            await state.set_state(WorkerOrderStates.waiting_payment)
            await message.answer(
                f"✅ Davlat raqami saqlandi: <b>{_safe(plate)}</b>\n"
                "Endi mijoz oldidagi to‘lov turini tanlang:",
                reply_markup=worker_payment_keyboard(order_id),
            )
        else:
            await state.set_state(WorkerCompletionStates.waiting_before_photo)
            await message.answer(
                f"✅ Davlat raqami saqlandi: <b>{_safe(plate)}</b>\n"
                "Birinchi rasmni yuboring (Oldin):",
                reply_markup=ReplyKeyboardRemove(),
            )

    @router.callback_query(
        WorkerOrderStates.waiting_payment,
        F.data.startswith("worker_payment:"),
    )
    async def receive_worker_payment(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        if not callback.from_user:
            return
        try:
            _, payment_method, order_id_raw = callback.data.split(":")
            order_id = int(order_id_raw)
        except (TypeError, ValueError):
            await callback.answer("❌ To‘lov ma'lumoti noto‘g‘ri.", show_alert=True)
            return
        if payment_method not in {"Naqd", "Karta"}:
            await callback.answer("❌ To‘lov turi topilmadi.", show_alert=True)
            return
        async with sessions() as session:
            order, worker = await worker_order_and_worker(
                session, order_id, callback.from_user.id
            )
            if (
                not order
                or not worker
                or order.status not in {"yo‘lda", "yo'lda", "yakunlanmoqda"}
                or (
                    order.status == "yakunlanmoqda"
                    and not order.plate_number
                )
            ):
                await state.clear()
                await callback.answer(
                    "❌ Avval davlat raqamini kiriting yoki bu bosqich "
                    "endi faol emas.",
                    show_alert=True,
                )
                return
            order.payment_method = payment_method
            order_status = order.status
            if order.status in {"yo‘lda", "yo'lda"}:
                order.status = "yetib_keldi"
                order.arrived_at = now_tashkent()
            customer = await session.get(User, order.customer_id)
            customer_phone = customer.phone if customer else "—"
            worker_name = worker.name
            model = order.car_model
            plate = order.plate_number
            await session.commit()
        await callback.bot.send_message(
            settings.director_id,
            f"<b>{_safe(worker_name)}</b> | {_safe(model)} | "
            f"{_safe(plate)} | {_safe(payment_method)}\n"
            + (
                "Manzilga yetib keldi."
                if order_status in {"yo‘lda", "yo'lda"}
                else "Yuvish tugadi, to‘lov turi tanlandi."
            ),
        )
        await callback.message.edit_reply_markup(reply_markup=None)
        if order_status in {"yo‘lda", "yo'lda"}:
            await state.clear()
            await callback.message.answer(
                "✅ To‘lov turi saqlandi. "
                "Buyurtma manzilga yetib keldi deb belgilandi."
            )
            await callback.message.answer(
                f"Mijoz telefoni: <b>{_safe(customer_phone)}</b>"
            )
        else:
            await state.set_state(WorkerCompletionStates.waiting_before_photo)
            await state.update_data(order_id=order_id)
            await callback.message.answer(
                f"✅ To‘lov turi saqlandi: <b>{_safe(payment_method)}</b>\n"
                "Endi «Oldin» rasmini yuboring.",
                reply_markup=ReplyKeyboardRemove(),
            )
        await callback.answer()

    @router.message(WorkerCompletionStates.waiting_before_photo, F.photo)
    async def receive_before_photo(message: Message, state: FSMContext) -> None:
        await state.update_data(before_photo_id=message.photo[-1].file_id)
        await state.set_state(WorkerCompletionStates.waiting_after_photo)
        await message.answer("📷 Endi ikkinchi rasmni yuboring (Keyin):")

    @router.message(WorkerCompletionStates.waiting_after_photo, F.photo)
    async def receive_after_photo(message: Message, state: FSMContext) -> None:
        await state.update_data(after_photo_id=message.photo[-1].file_id)
        await state.set_state(WorkerCompletionStates.waiting_comment)
        await message.answer("📝 Ish bo'yicha qisqa izoh yuboring:")

    @router.message(WorkerCompletionStates.waiting_before_photo)
    async def require_before_photo(message: Message) -> None:
        await message.answer("⚠️ Iltimos, avval «Oldin» rasmini yuboring.")

    @router.message(WorkerCompletionStates.waiting_after_photo)
    async def require_after_photo(message: Message) -> None:
        await message.answer("⚠️ Iltimos, «Keyin» rasmini yuboring.")

    @router.message(WorkerCompletionStates.waiting_comment, F.text)
    async def complete_order(message: Message, state: FSMContext) -> None:
        if not message.from_user:
            return
        data = await state.get_data()
        comment = message.text.strip()
        if not comment or len(comment) > 2000:
            await message.answer("❌ Qisqa izoh 1–2000 belgi bo'lishi kerak.")
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
                await message.answer("❌ Buyurtma topilmadi yoki sizga tegishli emas.")
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
                 f"<b>📊 Yakuniy hisobot | Buyurtma #{order.id}</b>\n\n"
                 f"<b>👷 Ishchi:</b> {_safe(worker.name)}\n"
                 f"<b>👤 Mijoz:</b> {_safe(customer.name if customer else '—')} "
                f"({_safe(customer.phone if customer else '—')})\n"
                 f"<b>🚗 Mashina:</b> {_safe(order.car_model)}\n"
                 f"<b>🪪 Davlat raqami:</b> {_safe(_plate_display(order.plate_number))}\n"
                 f"<b>🎨 Rang:</b> {_safe(order.car_color or '—')}\n"
                 f"<b>💰 Narx:</b> {_safe(format_price(int(order.car_price)))}\n"
            f"<b>💳 To'lov:</b> {_safe(_payment_display(order.payment_method))}\n"
                 f"<b>⏱️ Ish davomiyligi:</b> {_safe(duration)}\n"
                 f"<b>📝 Izoh:</b> {_safe(comment)}"
            )
            await session.commit()
            before_photo_id = order.before_photo_id
            after_photo_id = order.after_photo_id
            customer_id = order.customer_id
        await message.answer(
            f"✅ Buyurtma #{data['order_id']} yakunlandi.",
            reply_markup=worker_menu_keyboard(),
        )
        await message.bot.send_message(settings.director_id, report)
        await message.bot.send_photo(
            settings.director_id,
            before_photo_id,
            caption="📷 Oldin",
        )
        await message.bot.send_photo(
            settings.director_id,
            after_photo_id,
            caption="📷 Keyin",
        )
        await notify_customer(
            message.bot,
            customer_id,
             "✅ Buyurtmangiz yakunlandi. "
            "RYX Wash xizmatidan foydalanganingiz uchun rahmat!",
        )
        await offer_next_queued_order(message.from_user.id, message.bot)
        await state.clear()

    @router.message(Command("worker_id"))
    async def worker_id(message: Message) -> None:
        if message.from_user:
            await message.answer(f"Sizning Telegram ID: <code>{message.from_user.id}</code>")

    @router.message(WorkerCompletionStates.waiting_comment)
    async def require_completion_comment(message: Message) -> None:
        await message.answer("📝 Iltimos, ish bo'yicha qisqa izohni matn shaklida yuboring.")


def register_worker_routes(
    router: Router,
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    scheduler: AsyncIOScheduler,
) -> None:
    _register_user_routes(router, sessions, settings, scheduler)