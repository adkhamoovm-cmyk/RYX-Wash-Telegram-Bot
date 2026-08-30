from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, ForeignKey, Numeric, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    rol: Mapped[str] = mapped_column(String(20), nullable=False, default="mijoz")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    orders: Mapped[list["Order"]] = relationship(back_populates="customer")
    worker: Mapped["Worker | None"] = relationship(back_populates="user")
    cars: Mapped[list["CustomerCar"]] = relationship(
        back_populates="customer", cascade="all, delete-orphan"
    )


class CustomerCar(Base):
    __tablename__ = "customer_cars"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id"), nullable=False, index=True
    )
    car_category: Mapped[str] = mapped_column(
        "kategoriya", String(50), nullable=False
    )
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    plate_number: Mapped[str] = mapped_column(
        "davlat_raqami", String(30), nullable=False
    )
    color: Mapped[str | None] = mapped_column("rang", String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    customer: Mapped[User] = relationship(back_populates="cars")


class Worker(Base):
    __tablename__ = "workers"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id"), primary_key=True
    )
    name: Mapped[str] = mapped_column("ism", String(150), nullable=False)
    phone: Mapped[str] = mapped_column("telefon", String(40), nullable=False)
    share_percent: Mapped[Decimal] = mapped_column(
        "foiz", Numeric(5, 2), nullable=False
    )
    status: Mapped[str] = mapped_column(
        "holat", String(20), nullable=False, default="smenada_emas"
    )
    shift_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    shift_ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[User] = relationship(back_populates="worker")
    orders: Mapped[list["Order"]] = relationship(
        back_populates="worker", foreign_keys="Order.worker_id"
    )


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id"), nullable=False, index=True
    )
    worker_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("workers.user_id"), nullable=True, index=True
    )
    queue_offer_worker_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("workers.user_id"), nullable=True
    )
    car_category: Mapped[str] = mapped_column(String(50), nullable=False)
    car_model: Mapped[str] = mapped_column(String(100), nullable=False)
    car_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    plate_number: Mapped[str] = mapped_column(String(30), nullable=False)
    payment_method: Mapped[str] = mapped_column(String(20), nullable=False)
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7), nullable=True)
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    car_color: Mapped[str | None] = mapped_column(String(50), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    car_photo_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    order_group_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    group_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="yangi")
    queued_offer: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="false"
    )
    queue_prompted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    assigned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    route_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    arrived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    washing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    before_photo_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    after_photo_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    worker_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    customer: Mapped[User] = relationship(back_populates="orders")
    worker: Mapped[Worker | None] = relationship(
        back_populates="orders", foreign_keys=[worker_id]
    )
    cancellations: Mapped[list["Cancellation"]] = relationship(back_populates="order")


class Cancellation(Base):
    __tablename__ = "cancellations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id"), nullable=False, index=True
    )
    reason: Mapped[str] = mapped_column("sabab", Text, nullable=False)
    cancelled_by: Mapped[int] = mapped_column(
        "kim_bekor_qildi", BigInteger, nullable=False
    )
    cancelled_at: Mapped[datetime] = mapped_column(
        "vaqt", DateTime(timezone=True), nullable=False
    )

    order: Mapped[Order] = relationship(back_populates="cancellations")
