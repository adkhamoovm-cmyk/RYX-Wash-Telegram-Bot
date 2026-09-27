import logging
import html
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from apscheduler.jobstores.base import JobLookupError
from apscheduler.triggers.cron import CronTrigger
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .config import Settings
from .models import Order, User, Worker
from .keyboards import new_order_assignment_keyboard

logger = logging.getLogger(__name__)
TASHKENT = ZoneInfo("Asia/Tashkent")
DEFAULT_WASH_DURATION_MINUTES = 60
DAILY_REPORT_JOB_ID = "daily-financial-report"
OPERATOR_REVIEW_MINUTES = 30

_sessions: async_sessionmaker[AsyncSession] | None = None
_bot: Bot | None = None
_settings: Settings | None = None
_worker_available_handler: Callable[[int, Bot], Awaitable[None]] | None = None


def configure_wash_timer_runtime(
    sessions: async_sessionmaker[AsyncSession],
    bot: Bot,
    settings: Settings,
) -> None:
    global _sessions, _bot, _settings
    _sessions = sessions
    _bot = bot
    _settings = settings


def configure_worker_available_handler(
    handler: Callable[[int, Bot], Awaitable[None]],
) -> None:
    global _worker_available_handler
    _worker_available_handler = handler


def wash_timeout_job_id(order_id: int) -> str:
    return f"wash-timeout:{order_id}"


def schedule_wash_timeout(
    scheduler: AsyncIOScheduler,
    order_id: int,
    washing_started_at: datetime,
    duration_minutes: int,
) -> None:
    scheduler.add_job(
        expire_wash_timeout,
        trigger="date",
        run_date=washing_started_at + timedelta(minutes=duration_minutes),
        args=[order_id],
        id=wash_timeout_job_id(order_id),
        replace_existing=True,
        misfire_grace_time=None,
    )


def remove_wash_timeout(scheduler: AsyncIOScheduler, order_id: int) -> None:
    try:
        scheduler.remove_job(wash_timeout_job_id(order_id))
    except JobLookupError:
        pass


def schedule_daily_report(scheduler: AsyncIOScheduler) -> None:
    scheduler.add_job(
        send_daily_financial_report,
        trigger=CronTrigger(hour=21, minute=0, timezone=TASHKENT),
        id=DAILY_REPORT_JOB_ID,
        replace_existing=True,
        misfire_grace_time=3600,
    )


def schedule_operator_review(
    scheduler: AsyncIOScheduler, order_id: int, created_at: datetime
) -> None:
    scheduler.add_job(
        escalate_unassigned_order,
        trigger="date",
        run_date=created_at + timedelta(minutes=OPERATOR_REVIEW_MINUTES),
        args=[order_id],
        id=f"operator-review:{order_id}",
        replace_existing=True,
        misfire_grace_time=None,
    )


async def escalate_unassigned_order(order_id: int) -> None:
    """Escalate a customer order only if no worker has been assigned."""
    if _sessions is None or _bot is None or _settings is None:
        logger.error("Operator-review runtime is not configured for order %s", order_id)
        return
    async with _sessions() as session:
        order = await session.get(Order, order_id)
        if not order or order.worker_id is not None or order.status not in {"yangi", "navbatda"}:
            return
        customer = await session.get(User, order.customer_id)
        text = (
            f"<b>⏰ Operator 30 daqiqada biriktirmadi | Buyurtma #{order.id}</b>\n\n"
            f"<b>👤 Mijoz:</b> {_safe_worker_name(customer.name if customer else '—')}\n"
            f"<b>📞 Telefon:</b> {_safe_worker_name(customer.phone if customer else '—')}\n"
            f"<b>🚗 Mashina:</b> {_safe_worker_name(order.car_model)}\n"
            f"<b>🪪 Davlat raqami:</b> {_safe_worker_name(order.plate_number or 'Ko‘rsatilmagan')}\n"
            f"<b>🎨 Rang:</b> {_safe_worker_name(order.car_color or '—')}\n"
            f"<b>💰 Narx:</b> {order.car_price:,.0f} so‘m\n"
            f"<b>💳 To‘lov:</b> {_safe_worker_name(order.payment_method or 'Ko‘rsatilmagan')}\n"
            f"<b>📝 Izoh:</b> {_safe_worker_name(order.comment or '—')}\n"
            f"<b>📍 Manzil:</b> {_safe_worker_name(order.address or 'Lokatsiya yuborilgan')}"
        )
        location = (float(order.latitude), float(order.longitude)) if (
            order.latitude is not None and order.longitude is not None
        ) else None
    try:
        await _bot.send_message(
            _settings.director_id, text,
            reply_markup=new_order_assignment_keyboard(order_id),
        )
        if location:
            await _bot.send_location(
                _settings.director_id, latitude=location[0], longitude=location[1],
            )
    except Exception:
        logger.exception("Could not escalate unassigned order %s", order_id)


async def send_daily_financial_report() -> None:
    if _sessions is None or _bot is None or _settings is None:
        logger.error("Daily report runtime is not configured")
        return
    try:
        from .reports import send_daily_report

        await send_daily_report(_sessions, _bot, _settings)
    except Exception:
        logger.exception("Could not send the daily financial report")


async def expire_wash_timeout(order_id: int) -> None:
    """Notify when the allocated washing time has elapsed."""
    if _sessions is None or _bot is None or _settings is None:
        logger.error("Timeout runtime is not configured for order %s", order_id)
        return

    async with _sessions() as session:
        order = await session.get(Order, order_id, with_for_update=True)
        if (
            order is None
            or order.status != "yuvish_boshlandi"
            or order.worker_id is None
            or order.washing_started_at is None
        ):
            return

        worker = await session.get(Worker, order.worker_id, with_for_update=True)
        worker_id = order.worker_id
        worker_name = worker.name if worker else str(worker_id)
        duration_minutes = order.wash_duration_minutes or DEFAULT_WASH_DURATION_MINUTES
        await session.commit()

    await _bot.send_message(
        worker_id,
        f"⏰ Buyurtma #{order_id} uchun ajratilgan "
        f"{duration_minutes} daqiqalik yuvish vaqti tugadi.",
    )
    await _bot.send_message(
        _settings.director_id,
        f"⏰ {_safe_worker_name(worker_name)} uchun buyurtma #{order_id} "
        f"bo'yicha {duration_minutes} daqiqalik yuvish vaqti tugadi.",
    )


def _safe_worker_name(name: str) -> str:
    return html.escape(name)


async def expire_worker_offer(order_id: int) -> None:
    """Compatibility target for offer jobs persisted by older deployments."""
    logger.info("Ignoring legacy worker-offer timeout job for order %s", order_id)