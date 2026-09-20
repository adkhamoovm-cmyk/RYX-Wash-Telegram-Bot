import html
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .catalog import format_price
from .config import Settings
from .models import Cancellation, Expense, Order, User, Worker, WorkerAdditionalIncome

TASHKENT = ZoneInfo("Asia/Tashkent")


def _safe(value: object) -> str:
    return html.escape(str(value))


def _percent(value: Decimal) -> str:
    return format(Decimal(value).normalize(), "f")


def _money(value: Decimal) -> str:
    rounded = Decimal(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return _safe(format_price(int(rounded)))


def _safe_summary(value: object, limit: int = 3000) -> str:
    text = str(value)
    escaped_parts: list[str] = []
    escaped_length = 0
    truncated = False
    for character in text:
        escaped_character = html.escape(character)
        if escaped_length + len(escaped_character) > limit - 3:
            truncated = True
            break
        escaped_parts.append(escaped_character)
        escaped_length += len(escaped_character)
    if truncated:
        escaped_parts.append("...")
    return "".join(escaped_parts)


def _duration_text(start: datetime | None, end: datetime | None) -> str:
    if not start or not end:
        return "—"
    seconds = max(0, int((end - start).total_seconds()))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours} soat {minutes} daqiqa" if hours else f"{minutes} daqiqa {seconds} soniya"


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
    if code in {"3months", "6months", "12months", "year"}:
        months = {"3months": 3, "6months": 6, "12months": 12, "year": 12}[code]
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
    workers = list((await session.scalars(select(Worker).order_by(Worker.name))).all())
    worker = None
    if worker_id is not None:
        order_query = order_query.where(Order.worker_id == worker_id)
        cancellation_query = cancellation_query.where(Order.worker_id == worker_id)
        worker = await session.get(Worker, worker_id)
        if worker is None:
            return ["❌ Ishchi topilmadi."]

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
    additional = list(
        (
            await session.scalars(
                select(WorkerAdditionalIncome).where(
                    WorkerAdditionalIncome.occurred_at >= start,
                    WorkerAdditionalIncome.occurred_at < end,
                )
            )
        ).all()
    )

    def order_earning(order: Order) -> Decimal:
        if order.worker_share_type == "none":
            return Decimal("0")
        if order.worker_share_amount is not None:
            return Decimal(order.worker_share_amount)
        if order.worker_share_type == "amount" and order.worker_share_value is not None:
            return Decimal(order.worker_share_value)
        if order.worker_share_type == "percent" and order.worker_share_value is not None:
            return Decimal(order.car_price) * Decimal(order.worker_share_value) / Decimal("100")
        return Decimal("0")

    revenue = sum((Decimal(order.car_price) for order in orders), Decimal("0"))
    additional_gross = sum((Decimal(item.amount) for item in additional), Decimal("0"))
    additional_earnings = sum((Decimal(item.worker_amount) for item in additional), Decimal("0"))
    worker_earnings = sum((order_earning(order) for order in orders), Decimal("0"))
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
    net_profit = (
        revenue + additional_gross - expense_total
        - worker_earnings - additional_earnings
    )
    display_additional_gross = (
        sum(
            (Decimal(item.amount) for item in additional if item.worker_id == worker_id),
            Decimal("0"),
        )
        if worker_id is not None
        else additional_gross
    )

    period_end = end - timedelta(microseconds=1)
    filter_label = worker.name if worker else "Barcha ishchilar"
    header = (
        "<b>📊 RYX WASH HISOBOTI</b>\n\n"
        f"<b>Davr:</b> {start.astimezone(TASHKENT):%d.%m.%Y} — "
        f"{period_end.astimezone(TASHKENT):%d.%m.%Y}\n"
        f"<b>Filtr:</b> {_safe(filter_label)}\n\n"
        f"<b>🧼 Yuvilgan mashinalar:</b> {len(orders)} ta\n"
        f"<b>💰 Umumiy tushum:</b> {_money(revenue)}\n"
        f"<b>➕ Qo‘shimcha daromad:</b> {_money(display_additional_gross)}\n"
        f"• 💵 Naqd: {_money(cash)}\n"
        f"• 💳 Karta: {_money(card)}\n"
        f"<b>🚫 Bekor qilingan:</b> {len(cancellations)} ta"
    )
    if worker is not None:
        worker_additional = [
            item for item in additional if item.worker_id == worker_id
        ]
        worker_share = sum(
            (order_earning(order) for order in orders), Decimal("0")
        )
        worker_additional_gross = sum(
            (Decimal(item.amount) for item in worker_additional), Decimal("0")
        )
        worker_additional_earnings = sum(
            (Decimal(item.worker_amount) for item in worker_additional), Decimal("0")
        )
        worker_expense = sum(
            (Decimal(item.amount) for item in expenses if item.worker_id == worker_id),
            Decimal("0"),
        )
        worker_profit = (
            revenue + worker_additional_gross - worker_share
            - worker_additional_earnings - worker_expense
        )
        average_check = revenue / len(orders) if orders else Decimal("0")
        header += (
            f"\n\n<b>👷 {_safe(worker.name)} hisoboti</b>\n"
            f"• 📈 O‘rtacha chek: {_money(average_check)}\n"
            f"• 💼 Worker payout "
            f"({_percent(worker.share_percent)}%): "
            f"{_money(worker_share)}\n"
            f"• ➕ Qo‘shimcha daromad: {_money(worker_additional_gross)}\n"
            f"• ➕ Qo‘shimcha ulush: {_money(worker_additional_earnings)}\n"
            f"• 📉 Worker xarajati: {_money(worker_expense)}\n"
            f"• 💰 Worker-attributable business profit: {_money(worker_profit)}"
        )
    else:
        header += (
            f"\n\n<b>📉 Umumiy chiqim:</b> "
            f"{_money(expense_total)}\n"
            f"<b>💰 Sof foyda:</b> {_money(net_profit)}"
        )

    detail_lines: list[str] = []
    if worker is None and expenses:
        detail_lines.append("\n<b>📉 Chiqimlar:</b>")
        detail_lines.extend(
            f"• {_safe(expense.description)} — "
            f"{_money(Decimal(expense.amount))} | "
            f"{'Umumiy' if expense.worker_id is None else f'Worker #{expense.worker_id}'}"
            for expense in expenses
        )
    if cancellations:
        detail_lines.append("\n<b>🚫 Bekor qilish sabablari:</b>")
        for cancellation, order in cancellations:
            reason = _safe(cancellation.reason)
            if len(reason) > 1000:
                reason = reason[:997] + "..."
            detail_lines.append(f"• Buyurtma #{order.id}: {reason}")
    if worker is not None:
        if not orders:
            detail_lines.append("\n<i>Bu davrda yakunlangan buyurtmalar topilmadi.</i>")
        else:
            detail_lines.append("\n<b>🧾 Buyurtmalar:</b>")
            for order in orders:
                completed_at = order.completed_at
                if completed_at is not None:
                    completed_at = completed_at.astimezone(TASHKENT)
                    date_label = completed_at.strftime("%d.%m %H:%M")
                else:
                    date_label = "—"
                payment = order.payment_method or "To‘lov ko‘rsatilmagan"
                plate = order.plate_number or "Raqam ko‘rsatilmagan"
                detail_lines.append(
                    f"• <b>#{order.id}</b> {date_label} — "
                    f"{_safe(order.car_model)} | {_safe(plate)} | "
                    f"{_money(Decimal(order.car_price))} | "
                    f"{_safe(payment)} | "
                    f"⏱ {_duration_text(order.arrived_at, order.completed_at)}"
                )
    else:
        orders_by_worker: dict[int | None, list[Order]] = {}
        for order in orders:
            orders_by_worker.setdefault(order.worker_id, []).append(order)
        cancellations_by_worker: dict[int | None, int] = {}
        for _cancellation, cancelled_order in cancellations:
            cancellations_by_worker[cancelled_order.worker_id] = (
                cancellations_by_worker.get(cancelled_order.worker_id, 0) + 1
            )
        for listed_worker in workers:
            worker_orders = orders_by_worker.get(listed_worker.user_id, [])
            worker_revenue = sum(
                (Decimal(order.car_price) for order in worker_orders),
                Decimal("0"),
            )
            worker_cash = sum(
                (
                    Decimal(order.car_price)
                    for order in worker_orders
                    if order.payment_method == "Naqd"
                ),
                Decimal("0"),
            )
            worker_card = sum(
                (
                    Decimal(order.car_price)
                    for order in worker_orders
                    if order.payment_method == "Karta"
                ),
                Decimal("0"),
            )
            worker_share = sum(
                (order_earning(order) for order in worker_orders), Decimal("0")
            )
            worker_additional = [
                item for item in additional
                if item.worker_id == listed_worker.user_id
            ]
            worker_additional_gross = sum(
                (Decimal(item.amount) for item in worker_additional), Decimal("0")
            )
            worker_additional_earnings = sum(
                (Decimal(item.worker_amount) for item in worker_additional), Decimal("0")
            )
            worker_expense = sum(
                (
                    Decimal(item.amount)
                    for item in expenses
                    if item.worker_id == listed_worker.user_id
                ),
                Decimal("0"),
            )
            worker_profit = (
                worker_revenue + worker_additional_gross - worker_share
                - worker_additional_earnings - worker_expense
            )
            average_check = (
                worker_revenue / len(worker_orders) if worker_orders else Decimal("0")
            )
            detail_lines.extend(
                [
                    f"\n<b>👷 {_safe(listed_worker.name)}</b>",
                    f"• 🧼 Yuvilgan: {len(worker_orders)} ta",
                    f"• 🚫 Bekor qilingan: "
                    f"{cancellations_by_worker.get(listed_worker.user_id, 0)} ta",
                    f"• 💰 Tushum: {_money(worker_revenue)}",
                    f"  ├ Naqd: {_money(worker_cash)}",
                    f"  ├ Karta: {_money(worker_card)}",
                    f"• 📈 O‘rtacha chek: {_money(average_check)}",
                    f"• 💼 Ulush ({_percent(listed_worker.share_percent)}%): "
                    f"{_money(worker_share)}",
                    f"• ➕ Qo‘shimcha daromad: {_money(worker_additional_gross)}",
                    f"• ➕ Qo‘shimcha ulush: {_money(worker_additional_earnings)}",
                    f"• 📉 Worker xarajati: {_money(worker_expense)}",
                    f"• 💰 Worker foydasi: {_money(worker_profit)}",
                ]
            )
            if worker_orders:
                for order in worker_orders:
                    completed_at = order.completed_at
                    date_label = (
                        completed_at.astimezone(TASHKENT).strftime("%d.%m %H:%M")
                        if completed_at is not None
                        else "—"
                    )
                    detail_lines.append(
                        f"  • #{order.id} {date_label} — "
                        f"{_safe(order.car_model)} | "
                        f"{_safe(order.plate_number or 'Raqam yo‘q')} | "
                        f"{_money(Decimal(order.car_price))} | "
                        f"{_safe(order.payment_method or 'To‘lov ko‘rsatilmagan')} | "
                        f"⏱ {_duration_text(order.arrived_at, order.completed_at)}"
                    )
            else:
                detail_lines.append("  <i>Davrda yakunlangan order yo‘q.</i>")

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


CUSTOMER_STATUS_LABELS = {
    "yangi": "🕐 Yangi",
    "navbatda": "⏳ Navbatda",
    "ishchiga_yuborildi": "📤 Ishchiga yuborildi",
    "ishchi_qabul_qildi": "✅ Ishchi qabul qildi",
    "yo'lda": "🚗 Yo‘lda",
    "yo‘lda": "🚗 Yo‘lda",
    "yetib_keldi": "📍 Yetib keldi",
    "yuvish_boshlandi": "🧼 Yuvish boshlandi",
    "yakunlanmoqda": "📸 Yakunlanmoqda",
    "yakunlandi": "🎉 Yakunlandi",
    "bekor_qilindi": "🚫 Bekor qilindi",
}


async def build_customer_history_report(
    session: AsyncSession,
    start: datetime,
    end: datetime,
) -> list[str]:
    orders = list(
        (
            await session.execute(
                select(Order, User)
                .join(User, User.telegram_id == Order.customer_id)
                .where(Order.created_at >= start, Order.created_at < end)
                .order_by(Order.created_at, Order.id)
            )
        ).all()
    )
    cancellations = list(
        (
            await session.execute(
                select(Cancellation, Order, User)
                .join(Order, Cancellation.order_id == Order.id)
                .join(User, User.telegram_id == Order.customer_id)
                .where(
                    Cancellation.cancelled_at >= start,
                    Cancellation.cancelled_at < end,
                )
                .order_by(Cancellation.cancelled_at, Cancellation.id)
            )
        ).all()
    )

    customer_orders: dict[int, list[tuple[Order, User]]] = {}
    customers: dict[int, User] = {}
    for order, customer in orders:
        customers[customer.telegram_id] = customer
        customer_orders.setdefault(customer.telegram_id, []).append((order, customer))
    cancellations_by_customer: dict[int, int] = {}
    for _cancellation, _order, customer in cancellations:
        customers[customer.telegram_id] = customer
        cancellations_by_customer[customer.telegram_id] = (
            cancellations_by_customer.get(customer.telegram_id, 0) + 1
        )
    unique_customers = len(customers)
    completed_count = sum(order.status == "yakunlandi" for order, _ in orders)
    cancelled_customer_ids = {customer.telegram_id for _, _, customer in cancellations}
    period_end = end - timedelta(microseconds=1)
    header = (
        "<b>👥 RYX WASH MIJOZLAR TARIXI</b>\n\n"
        f"<b>Davr:</b> {start.astimezone(TASHKENT):%d.%m.%Y} — "
        f"{period_end.astimezone(TASHKENT):%d.%m.%Y}\n\n"
        f"<b>👤 Unikal mijozlar:</b> {unique_customers} ta\n"
        f"<b>🧾 Jami buyurtmalar:</b> {len(orders)} ta\n"
        f"<b>🎉 Yakunlangan:</b> {completed_count} ta\n"
        f"<b>🚫 Bekor qilingan:</b> {len(cancellations)} ta\n"
        f"<b>⚠️ Bekor qilgan mijozlar:</b> "
        f"{len(cancelled_customer_ids)} ta"
    )
    detail_lines: list[str] = []
    if not orders and not cancellations:
        detail_lines.append(
            "\n<i>Bu davrda mijozlar yoki buyurtmalar topilmadi.</i>"
        )
    else:
        detail_lines.append("\n<b>📋 Mijozlar va buyurtmalar:</b>")
        for customer_id, customer in customers.items():
            customer_orders_list = customer_orders.get(customer_id, [])
            detail_lines.extend(
                [
                    "",
                    f"<b>👤 {_safe(customer.name or 'Nomsiz mijoz')}</b>",
                    f"📞 {_safe(customer.phone or 'Telefon ko‘rsatilmagan')} | "
                    f"Yangi buyurtmalar: {len(customer_orders_list)} ta | "
                    f"Bekor qilishlar: "
                    f"{cancellations_by_customer.get(customer_id, 0)} ta",
                ]
            )
            if not customer_orders_list:
                detail_lines.append(
                    "  <i>Davrda yangi order yo‘q; cancellation faolligi mavjud.</i>"
                )
            for order, _ in customer_orders_list:
                created_at = order.created_at
                date_label = (
                    created_at.astimezone(TASHKENT).strftime("%d.%m %H:%M")
                    if created_at
                    else "—"
                )
                detail_lines.append(
                    f"  • #{order.id} {date_label} — "
                    f"{_safe(order.car_model)} | "
                    f"{_safe(order.plate_number or 'Raqam kiritilmagan')} | "
                    f"{_money(Decimal(order.car_price))} | "
                    f"{_safe(CUSTOMER_STATUS_LABELS.get(order.status, order.status))}"
                )
    if cancellations:
        detail_lines.append("\n<b>🚫 Bekor qilingan buyurtmalar:</b>")
        for cancellation, order, customer in cancellations:
            cancelled_at = cancellation.cancelled_at.astimezone(TASHKENT)
            detail_lines.extend(
                [
                f"• {_safe(customer.name or 'Nomsiz mijoz')} | "
                f"{_safe(customer.phone or 'Telefon ko‘rsatilmagan')}",
                f"  {_safe(order.car_model)} | "
                f"{_safe(order.plate_number or 'Raqam kiritilmagan')} | "
                f"{cancelled_at:%d.%m.%Y %H:%M}",
                f"  Sabab: {_safe_summary(cancellation.reason)}",
                ]
            )

    chunks: list[str] = []
    current = header
    for line in detail_lines:
        if len(current) + len(line) + 1 > 3800:
            chunks.append(current)
            current = "<b>👥 Mijozlar tarixi davomi</b>\n" + line
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