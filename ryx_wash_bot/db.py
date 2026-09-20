from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .catalog import CAR_MODELS, CarModel, load_models
from .models import Base, ServiceModel


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
        # version of the bot, so add the assignment and catalog columns safely.
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
                    ADD COLUMN IF NOT EXISTS arrival_eta_minutes INTEGER,
                    ADD COLUMN IF NOT EXISTS arrival_eta_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS visit_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS washing_started_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS before_photo_id VARCHAR(255),
                    ADD COLUMN IF NOT EXISTS after_photo_id VARCHAR(255),
                    ADD COLUMN IF NOT EXISTS worker_comment TEXT,
                    ADD COLUMN IF NOT EXISTS queued_offer BOOLEAN NOT NULL DEFAULT FALSE,
                    ADD COLUMN IF NOT EXISTS queue_prompted_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS address TEXT,
                    ADD COLUMN IF NOT EXISTS car_photo_id VARCHAR(255),
                    ADD COLUMN IF NOT EXISTS car_color VARCHAR(50),
                    ADD COLUMN IF NOT EXISTS order_group_id VARCHAR(36),
                    ADD COLUMN IF NOT EXISTS group_mode VARCHAR(20),
                    ADD COLUMN IF NOT EXISTS wash_duration_minutes INTEGER NOT NULL DEFAULT 60
                    ,ADD COLUMN IF NOT EXISTS worker_share_type VARCHAR(10)
                    ,ADD COLUMN IF NOT EXISTS worker_share_value NUMERIC(12,2)
                    ,ADD COLUMN IF NOT EXISTS worker_share_amount NUMERIC(12,2)
                """
            )
        )
        await connection.execute(
            text(
                """
                ALTER TABLE expenses
                    ADD COLUMN IF NOT EXISTS worker_id BIGINT REFERENCES workers(user_id)
                """
            )
        )
        await connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS worker_additional_income (
                    id SERIAL PRIMARY KEY,
                    worker_id BIGINT NOT NULL REFERENCES workers(user_id),
                    description TEXT NOT NULL,
                    amount NUMERIC(12,2) NOT NULL,
                    share_type VARCHAR(10) NOT NULL,
                    share_value NUMERIC(12,2) NOT NULL,
                    worker_amount NUMERIC(12,2) NOT NULL,
                    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    created_by BIGINT NOT NULL REFERENCES users(telegram_id)
                )
                """
            )
        )
        await connection.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_worker_additional_income_worker
                    ON worker_additional_income(worker_id, occurred_at)
                """
            )
        )
        await connection.execute(
            text(
                """
                ALTER TABLE workers
                    ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE
                """
            )
        )
        await connection.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_orders_order_group_id
                ON orders (order_group_id)
                """
            )
        )
        await connection.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_orders_visit_at
                ON orders (visit_at)
                """
            )
        )
        await connection.execute(
            text(
                """
                ALTER TABLE orders
                    ALTER COLUMN latitude DROP NOT NULL,
                    ALTER COLUMN longitude DROP NOT NULL,
                    ALTER COLUMN plate_number DROP NOT NULL,
                    ALTER COLUMN payment_method DROP NOT NULL
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
                        ALTER TABLE cancellations
                            RENAME COLUMN cancelled_at TO vaqt;
                    END IF;
                END $$;
                """
            )
        )
        # Freeze the payout that legacy assigned orders previously derived from
        # the worker's mutable percentage. This is idempotent and deliberately
        # runs after the legacy worker-column compatibility block above.
        await connection.execute(
            text(
                """
                UPDATE orders AS orders_to_snapshot
                SET worker_share_type = 'percent',
                    worker_share_value = workers.foiz,
                    worker_share_amount = ROUND(
                        orders_to_snapshot.car_price * workers.foiz / 100,
                        2
                    )
                FROM workers
                WHERE orders_to_snapshot.worker_id = workers.user_id
                  AND orders_to_snapshot.worker_share_type IS NULL
                  AND orders_to_snapshot.worker_share_amount IS NULL
                """
            )
        )


async def initialize_catalog(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Seed the initial catalog once and load active models into the bot cache."""
    async with sessions() as session:
        existing_model_id = await session.scalar(select(ServiceModel.id).limit(1))
        if existing_model_id is None:
            session.add_all(
                [
                    ServiceModel(
                        id=model.id,
                        category=model.category,
                        name=model.name,
                        price=model.price,
                    )
                    for model in CAR_MODELS
                ]
            )
            await session.commit()
        models = list(
            (
                await session.scalars(
                    select(ServiceModel)
                    .where(ServiceModel.active.is_(True))
                    .order_by(
                        ServiceModel.category,
                        ServiceModel.created_at,
                        ServiceModel.id,
                    )
                )
            ).all()
        )
        load_models(
            CarModel(
                id=model.id,
                category=model.category,
                name=model.name,
                price=int(model.price),
            )
            for model in models
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