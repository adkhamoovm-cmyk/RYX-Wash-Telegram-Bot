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


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id"), nullable=False, index=True
    )
    car_category: Mapped[str] = mapped_column(String(50), nullable=False)
    car_model: Mapped[str] = mapped_column(String(100), nullable=False)
    car_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    plate_number: Mapped[str] = mapped_column(String(30), nullable=False)
    payment_method: Mapped[str] = mapped_column(String(20), nullable=False)
    latitude: Mapped[Decimal] = mapped_column(Numeric(10, 7), nullable=False)
    longitude: Mapped[Decimal] = mapped_column(Numeric(10, 7), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="yangi")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    customer: Mapped[User] = relationship(back_populates="orders")
