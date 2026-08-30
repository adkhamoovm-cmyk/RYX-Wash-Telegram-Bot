import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from .config import Settings
from .db import create_tables, make_engine, session_factory
from .handlers import _new_router
from .models import User
from .scheduler import configure_timeout_runtime


async def run() -> None:
    settings = Settings()
    engine = make_engine(settings.async_database_url)
    await create_tables(engine)
    sessions = session_factory(engine)

    # Keep the director's role in the database, while DIRECTOR_ID remains the
    # notification destination and environment-level configuration.
    async with sessions() as session:
        director = await session.scalar(
            select(User).where(User.telegram_id == settings.director_id)
        )
        if director is None:
            session.add(
                User(
                    telegram_id=settings.director_id,
                    rol="direktor",
                )
            )
            await session.commit()

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
    configure_timeout_runtime(sessions, bot, settings)
    dispatcher = Dispatcher()
    dispatcher.include_router(_new_router(sessions, settings, scheduler))

    try:
        scheduler.start()
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
