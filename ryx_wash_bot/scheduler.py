import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .config import Settings
from .models import Order, Worker

logger = logging.getLogger(__name__)
TASHKENT = ZoneInfo("Asia/Tashkent")
OFFER_TIMEOUT_MINUTES = 3

_sessions: async_sessionmaker[AsyncSession] | None = None
_bot: Bot | None = None
_settings: Settings | None = None
_worker_available_handler: Callable[[int, Bot], Awaitable[None]] | None = None


def configure_timeout_runtime(
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


def offer_timeout_job_id(order_id: int) -> str:
    return f"worker-offer-timeout:{order_id}"


def schedule_offer_timeout(
    scheduler: AsyncIOScheduler,
    order_id: int,
    assigned_at: datetime,
) -> None:
    scheduler.add_job(
        expire_worker_offer,
        trigger="date",
        run_date=assigned_at + timedelta(minutes=OFFER_TIMEOUT_MINUTES),
        args=[order_id],
        id=offer_timeout_job_id(order_id),
        replace_existing=True,
        misfire_grace_time=None,
    )


def remove_offer_timeout(scheduler: AsyncIOScheduler, order_id: int) -> None:
    try:
        scheduler.remove_job(offer_timeout_job_id(order_id))
    except JobLookupError:
        pass


async def expire_worker_offer(order_id: int) -> None:
    """Expire an unanswered offer; callable path is persisted by APScheduler."""
    if _sessions is None or _bot is None or _settings is None:
        logger.error("Timeout runtime is not configured for order %s", order_id)
        return

    async with _sessions() as session:
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
        if (
            order is None
            or order.status != "ishchiga_yuborildi"
            or order.worker_id is None
        ):
            return

        worker = await session.get(Worker, order.worker_id, with_for_update=True)
        worker_id = order.worker_id
        worker_name = worker.name if worker else str(worker_id)
        if worker and worker.status == "band":
            worker.status = "bo'sh"

        order.worker_id = None
        order.assigned_at = None
        order.status = "navbatda" if order.queued_offer else "yangi"
        order.queued_offer = False
        order.queue_offer_worker_id = None
        order.queue_prompted_at = None
        if order.group_mode == "single" and order.order_group_id:
            siblings = [
                sibling
                for sibling in group_orders
                if sibling.id != order.id and sibling.status == "navbatda"
            ]
            for sibling in siblings:
                sibling.worker_id = None
                sibling.queue_offer_worker_id = None
                sibling.queue_prompted_at = None
        await session.commit()

    await _bot.send_message(
        worker_id,
        f"Buyurtma #{order_id} bo'yicha taklif muddati tugadi.",
    )
    await _bot.send_message(
        _settings.director_id,
        f"{worker_name} so'rovga javob bermadi (buyurtma #{order_id}).",
    )
    if _worker_available_handler is not None:
        await _worker_available_handler(worker_id, _bot)