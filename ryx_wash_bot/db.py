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
                    ADD COLUMN IF NOT EXISTS queue_offer_worker_id BIGINT REFERENCES workers(user_id),
                    ADD COLUMN IF NOT EXISTS assigned_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS accepted_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS route_started_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS arrived_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS washing_started_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS before_photo_id VARCHAR(255),
                    ADD COLUMN IF NOT EXISTS after_photo_id VARCHAR(255),
                    ADD COLUMN IF NOT EXISTS worker_comment TEXT
                    ,
                    ADD COLUMN IF NOT EXISTS queued_offer BOOLEAN NOT NULL DEFAULT FALSE,
                    ADD COLUMN IF NOT EXISTS queue_prompted_at TIMESTAMPTZ
                """
            )
        )
        await connection.execute(
            text(
                """
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'workers'
                          AND column_name = 'name'
                    ) AND NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'workers'
                          AND column_name = 'ism'
                    ) THEN
                        ALTER TABLE workers RENAME COLUMN name TO ism;
                    END IF;

                    IF EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'workers'
                          AND column_name = 'phone'
                    ) AND NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'workers'
                          AND column_name = 'telefon'
                    ) THEN
                        ALTER TABLE workers RENAME COLUMN phone TO telefon;
                    END IF;

                    IF EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'workers'
                          AND column_name = 'share_percent'
                    ) AND NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'workers'
                          AND column_name = 'foiz'
                    ) THEN
                        ALTER TABLE workers RENAME COLUMN share_percent TO foiz;
                    END IF;

                    IF EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'workers'
                          AND column_name = 'status'
                    ) AND NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'workers'
                          AND column_name = 'holat'
                    ) THEN
                        ALTER TABLE workers RENAME COLUMN status TO holat;
                    END IF;

                    IF EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'cancellations'
                          AND column_name = 'reason'
                    ) AND NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'cancellations'
                          AND column_name = 'sabab'
                    ) THEN
                        ALTER TABLE cancellations RENAME COLUMN reason TO sabab;
                    END IF;

                    IF EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'cancellations'
                          AND column_name = 'cancelled_by'
                    ) AND NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'cancellations'
                          AND column_name = 'kim_bekor_qildi'
                    ) THEN
                        ALTER TABLE cancellations
                            RENAME COLUMN cancelled_by TO kim_bekor_qildi;
                    END IF;

                    IF EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'cancellations'
                          AND column_name = 'cancelled_at'
                    ) AND NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'cancellations'
                          AND column_name = 'vaqt'
                    ) THEN
                        ALTER TABLE cancellations RENAME COLUMN cancelled_at TO vaqt;
                    END IF;
                END $$;
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
