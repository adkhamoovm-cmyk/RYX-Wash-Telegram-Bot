import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from .config import Settings
from .db import create_tables, initialize_catalog, make_engine, session_factory
from .handlers import _new_router
from .models import User
from .scheduler import schedule_daily_report


async def reconcile_staff_roles(sessions, director_id: int) -> None:
    """Make the configured director authoritative without deleting staff data."""
    async with sessions() as session:
        director = await session.scalar(
            select(User).where(User.telegram_id == director_id)
        )
        if director is None:
            session.add(User(telegram_id=director_id, rol="direktor"))
        else:
            director.rol = "direktor"
        await session.execute(
            User.__table__.update()
            .where(
                User.rol == "direktor",
                User.telegram_id != director_id,
            )
            .values(rol="operator")
        )
        await session.commit()


async def run() -> None:
    settings = Settings()
    engine = make_engine(settings.async_database_url)
    await create_tables(engine)
    sessions = session_factory(engine)
    await initialize_catalog(sessions)

    await reconcile_staff_roles(sessions, settings.director_id)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    scheduler = AsyncIOScheduler(
        jobstores={
            "default": SQLAlchemyJobStore(
                url=settings.sync_database_url,
                tablename="apscheduler_jobs",
            )
        },
        timezone="Asia/Tashkent",
    )
    schedule_daily_report(scheduler)
    dispatcher = Dispatcher()
    dispatcher.include_router(_new_router(sessions, settings, scheduler))

    try:
        scheduler.start(paused=True)
        for job in scheduler.get_jobs():
            if job.id.startswith("wash-timeout:"):
                scheduler.remove_job(job.id)
        scheduler.resume()
        await bot.delete_webhook(drop_pending_updates=True)
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await bot.session.close()
        await engine.dispose()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    asyncio.run(run())
