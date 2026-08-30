from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy import text

from .models import Base


def make_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(
        database_url,
        pool_pre_ping=True,
        pool_recycle=1800,
    )


async def create_tables(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        # create_all does not alter tables that were created by an earlier
        # version of the bot, so add the assignment/completion columns safely.
        await connection.execute(
            text(
                """
                ALTER TABLE orders
                    ADD COLUMN IF NOT EXISTS worker_id BIGINT REFERENCES workers(user_id),
                    ADD COLUMN IF NOT EXISTS assigned_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS accepted_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS route_started_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS arrived_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS washing_started_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS before_photo_id VARCHAR(255),
                    ADD COLUMN IF NOT EXISTS after_photo_id VARCHAR(255),
                    ADD COLUMN IF NOT EXISTS worker_comment TEXT
                """
            )
        )


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
