import html
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .catalog import format_price
from .config import Settings
from .models import Cancellation, Expense, Order, Worker

TASHKENT = ZoneInfo("Asia/Tashkent")


def _safe(value: object) -> str:
    return html.escape(str(value))


def day_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    current = now.astimezone(TASHKENT) if now else datetime.now(TASHKENT)
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def period_bounds(code: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    current = now.astimezone(TASHKENT) if now else datetime.now(TASHKENT)
    today, tomorrow = day_bounds(current)
    if code == "today":
        return today, tomorrow
    if code == "week":
        start = today - timedelta(days=today.weekday())
        return start, tomorrow
    if code == "month":
        return today.replace(day=1), tomorrow
    if code in {"3months", "6months"}:
        months = 3 if code == "3months" else 6
        month_index = current.year * 12 + current.month - 1 - (months - 1)
        year, zero_based_month = divmod(month_index, 12)
        return datetime(year, zero_based_month + 1, 1, tzinfo=TASHKENT), tomorrow
    raise ValueError(f"Unknown report period: {code}")


async def build_financial_report(
    session: AsyncSession,
    start: datetime,
    end: datetime,
    worker_id: int | None = None,
) -> list[str]:
    order_query = select(Order).where(
        Order.status == "yakunlandi",
        Order.completed_at >= start,
        Order.completed_at < end,
    )
    cancellation_query = (
        select(Cancellation, Order)
        .join(Order, Cancellation.order_id == Order.id)
        .where(
            Cancellation.cancelled_at >= start,
            Cancellation.cancelled_at < end,
        )
    )
    worker = None
    if worker_id is not None:
        order_query = order_query.where(Order.worker_id == worker_id)
        cancellation_query = cancellation_query.where(Order.worker_id == worker_id)
        worker = await session.get(Worker, worker_id)

    orders = list((await session.scalars(order_query.order_by(Order.completed_at))).all())
    cancellations = (await session.execute(cancellation_query)).all()
    expenses = list(
        (
            await session.scalars(
                select(Expense)
                .where(Expense.spent_at >= start, Expense.spent_at < end)
                .order_by(Expense.spent_at, Expense.id)
            )
        ).all()
    )

    revenue = sum((Decimal(order.car_price) for order in orders), Decimal("0"))
    cash = sum(
        (
            Decimal(order.car_price)
            for order in orders
            if order.payment_method == "Naqd"
        ),
        Decimal("0"),
    )
    card = sum(
        (
            Decimal(order.car_price)
            for order in orders
            if order.payment_method == "Karta"
        ),
        Decimal("0"),
    )
    expense_total = sum(
        (Decimal(expense.amount) for expense in expenses),
        Decimal("0"),
    )
    net_profit = revenue - expense_total

    period_end = end - timedelta(microseconds=1)
    filter_label = worker.name if worker else "Barcha ishchilar"
    header = (
        "<b>📊 Moliyaviy hisobot</b>\n\n"
        f"<b>Davr:</b> {start.astimezone(TASHKENT):%d.%m.%Y} — "
        f"{period_end.astimezone(TASHKENT):%d.%m.%Y}\n"
        f"<b>Filtr:</b> {_safe(filter_label)}\n\n"
        f"<b>💰 Umumiy kirim:</b> {_safe(format_price(int(revenue)))}\n"
        f"• 💵 Naqd: {_safe(format_price(int(cash)))}\n"
        f"• 💳 Karta: {_safe(format_price(int(card)))}\n"
        f"<b>📉 Umumiy chiqim:</b> {_safe(format_price(int(expense_total)))}\n"
        f"<b>💰 Sof foyda:</b> {_safe(format_price(int(net_profit)))}\n"
        f"<b>🧼 Yuvilgan mashinalar:</b> {len(orders)} ta\n"
        f"<b>🚫 Bekor qilingan buyurtmalar:</b> {len(cancellations)} ta"
    )
    if worker is not None:
        worker_share = revenue * Decimal(worker.share_percent) / Decimal("100")
        header += (
            f"\n<b>👷 {_safe(worker.name)} ulushi "
            f"({worker.share_percent:g}%):</b> "
            f"{_safe(format_price(int(worker_share)))}"
        )
    if worker_id is not None:
        header += "\n<i>📉 Chiqimlar davr bo'yicha umumiy ko'rsatildi.</i>"

    detail_lines: list[str] = []
    if expenses:
        detail_lines.append("\n<b>📉 Chiqimlar:</b>")
        detail_lines.extend(
            f"• {_safe(expense.description)} — "
            f"{_safe(format_price(int(expense.amount)))}"
            for expense in expenses
        )
    if cancellations:
        detail_lines.append("\n<b>🚫 Bekor qilish sabablari:</b>")
        for cancellation, order in cancellations:
            reason = _safe(cancellation.reason)
            if len(reason) > 1000:
                reason = reason[:997] + "..."
            detail_lines.append(f"• Buyurtma #{order.id}: {reason}")

    chunks: list[str] = []
    current = header
    for line in detail_lines:
        if len(current) + len(line) + 1 > 3800:
            chunks.append(current)
            current = "<b>📊 Hisobot davomi</b>\n" + line
        else:
            current += "\n" + line
    chunks.append(current)
    return chunks


async def send_daily_report(
    sessions: async_sessionmaker[AsyncSession],
    bot,
    settings: Settings,
) -> None:
    start, end = day_bounds()
    async with sessions() as session:
        chunks = await build_financial_report(session, start, end)
    for chunk in chunks:
        await bot.send_message(settings.director_id, chunk)