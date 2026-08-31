from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from .catalog import categories, format_price, models_for_category


def contact_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.add(
        KeyboardButton(text="📞 Telefon raqamimni yuborish", request_contact=True)
    )
    return builder.as_markup(resize_keyboard=True, one_time_keyboard=True)


def customer_menu_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="🚗➕ Yangi buyurtma"))
    builder.add(KeyboardButton(text="📋 Buyurtmalar tarixi"))
    return builder.as_markup(resize_keyboard=True)


def director_menu_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="➕👷 Ishchi qo'shish"))
    builder.add(KeyboardButton(text="👷 Ishchilarni boshqarish"))
    builder.add(KeyboardButton(text="📝 Qo'lda buyurtma qo'shish"))
    builder.add(KeyboardButton(text="🏷️ Narxlarni boshqarish"))
    builder.add(KeyboardButton(text="📉 Xarajat qo'shish"))
    builder.add(KeyboardButton(text="🧾 Xarajatlarni boshqarish"))
    builder.add(KeyboardButton(text="📊 Hisobot"))
    builder.add(KeyboardButton(text="🗂️ Mijozlar bazasi"))
    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)


def price_management_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🏷️ Narxlar ro'yxati", callback_data="price_list")
    builder.button(text="➕ Yangi model qo'shish", callback_data="price_add")
    builder.button(text="✏️ Narxni o'zgartirish", callback_data="price_edit")
    builder.button(text="🗑️ Modelni o'chirish", callback_data="price_delete")
    builder.adjust(1)
    return builder.as_markup()


def price_models_keyboard(models: list, action: str):
    builder = InlineKeyboardBuilder()
    for model in models:
        label = f"🏷️ {model.category} | {model.name} — {format_price(int(model.price))}"
        if len(label) > 60:
            label = label[:57] + "..."
        builder.button(
            text=label,
            callback_data=f"price_{action}_model:{model.id}",
        )
    builder.adjust(1)
    return builder.as_markup()


def expense_items_keyboard(expenses: list):
    builder = InlineKeyboardBuilder()
    for expense in expenses:
        date_text = expense.spent_at.strftime("%d.%m.%Y")
        label = (
            f"{date_text} | {format_price(int(expense.amount))} | "
            f"{expense.description}"
        )
        builder.button(
            text=f"✏️ {label}"[:60],
            callback_data=f"expense_edit:{expense.id}",
        )
        builder.button(
            text="🗑️",
            callback_data=f"expense_delete:{expense.id}",
        )
    builder.adjust(2)
    return builder.as_markup()


def expense_delete_confirm_keyboard(expense_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✅ Ha, o'chirish",
        callback_data=f"expense_delete_confirm:{expense_id}",
    )
    builder.button(
        text="↩️ Bekor qilish",
        callback_data="expense_delete_cancel",
    )
    builder.adjust(1)
    return builder.as_markup()


def report_period_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="📅 Bugun", callback_data="report_period:today")
    builder.button(text="📅 Shu hafta", callback_data="report_period:week")
    builder.button(text="📅 Shu oy", callback_data="report_period:month")
    builder.button(text="📅 3 oy", callback_data="report_period:3months")
    builder.button(text="📅 6 oy", callback_data="report_period:6months")
    builder.button(text="🗓️ Sana oralig'i", callback_data="report_period:custom")
    builder.adjust(2)
    return builder.as_markup()


def report_workers_keyboard(workers: list):
    builder = InlineKeyboardBuilder()
    builder.button(text="👥 Barcha ishchilar", callback_data="report_worker:all")
    for worker in workers:
        builder.button(
            text=f"👷 {worker.name}"[:60],
            callback_data=f"report_worker:{worker.user_id}",
        )
    builder.adjust(1)
    return builder.as_markup()


def crm_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="⭐ Eng faol mijozlar", callback_data="crm_top")
    builder.button(text="🔎 Mijozni qidirish", callback_data="crm_search")
    builder.adjust(1)
    return builder.as_markup()


def crm_customers_keyboard(customers: list):
    builder = InlineKeyboardBuilder()
    for customer in customers:
        name = customer.name or "Nomsiz mijoz"
        phone = customer.phone or "telefon yo'q"
        builder.button(
            text=f"👤 {name} | {phone}"[:60],
            callback_data=f"crm_customer:{customer.telegram_id}",
        )
    builder.button(text="🗂️ Mijozlar bazasi menyusi", callback_data="crm_menu")
    builder.adjust(1)
    return builder.as_markup()


def crm_card_keyboard(phone: str | None):
    builder = InlineKeyboardBuilder()
    builder.button(text="🔎 Mijozni qidirish", callback_data="crm_search")
    builder.button(text="⭐ Eng faol mijozlar", callback_data="crm_top")
    builder.adjust(1)
    return builder.as_markup()


def worker_menu_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="👤 Mening kabinetim"))
    builder.row(
        KeyboardButton(text="🟢 Ishga keldim"),
        KeyboardButton(text="🔴 Ishdan ketdim"),
    )
    return builder.as_markup(resize_keyboard=True)


def worker_cabinet_period_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="📅 Bugun", callback_data="cabinet_period:today")
    builder.button(text="📅 Shu hafta", callback_data="cabinet_period:week")
    builder.button(text="📅 Shu oy", callback_data="cabinet_period:month")
    builder.adjust(3)
    return builder.as_markup()


def worker_management_keyboard(workers: list):
    builder = InlineKeyboardBuilder()
    for worker in workers:
        action = "deactivate" if worker.active else "activate"
        state = "faol" if worker.active else "faol emas"
        builder.button(
            text=f"{'🟢' if worker.active else '⚪'} {worker.name} ({state})"[:60],
            callback_data=f"worker_manage:{action}:{worker.user_id}",
        )
    builder.adjust(1)
    return builder.as_markup()


def worker_deactivate_confirm_keyboard(worker_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✅ Ha, faolsizlantirish",
        callback_data=f"worker_deactivate_confirm:{worker_id}",
    )
    builder.button(
        text="↩️ Bekor qilish",
        callback_data="worker_manage_cancel",
    )
    builder.adjust(1)
    return builder.as_markup()


def category_keyboard():
    builder = InlineKeyboardBuilder()
    for category in categories():
        builder.button(text=f"🚗 {category}", callback_data=f"category:{category}")
    builder.adjust(2)
    return builder.as_markup()


def manual_category_keyboard():
    builder = InlineKeyboardBuilder()
    for category in categories():
        builder.button(text=f"🚗 {category}", callback_data=f"manual_category:{category}")
    builder.adjust(2)
    return builder.as_markup()


def model_keyboard(category: str):
    builder = InlineKeyboardBuilder()
    for model in models_for_category(category):
        builder.button(
            text=f"🚗 {model.name} — {format_price(model.price)}",
            callback_data=f"model:{model.id}",
        )
    builder.adjust(1)
    return builder.as_markup()


def manual_model_keyboard(category: str):
    builder = InlineKeyboardBuilder()
    for model in models_for_category(category):
        builder.button(
            text=f"🚗 {model.name} — {format_price(model.price)}",
            callback_data=f"manual_model:{model.id}",
        )
    builder.adjust(1)
    return builder.as_markup()


def saved_cars_keyboard(
    cars: list,
    prefix: str,
    page: int = 0,
    page_size: int = 8,
):
    builder = InlineKeyboardBuilder()
    total_pages = max(1, (len(cars) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    for car in cars[start : start + page_size]:
        label = f"{car.model} | {car.plate_number}"
        if len(label) > 60:
            label = label[:57] + "..."
        builder.button(text=f"🚗 {label}", callback_data=f"{prefix}_saved_car:{car.id}")
    if page > 0:
        builder.button(
            text="⬅️ Oldingi",
            callback_data=f"{prefix}_cars_page:{page - 1}",
        )
    if page + 1 < total_pages:
        builder.button(
            text="Keyingi ➡️",
            callback_data=f"{prefix}_cars_page:{page + 1}",
        )
    builder.button(
        text="🚗➕ Yangi mashina qo'shish",
        callback_data=f"{prefix}_new_car",
    )
    builder.adjust(1)
    return builder.as_markup()


def next_car_keyboard(prefix: str):
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🚗➕ Yana mashina qo'shish",
        callback_data=f"{prefix}_add_car",
    )
    builder.button(
        text="➡️ Davom etish",
        callback_data=f"{prefix}_finish_cars",
    )
    builder.adjust(1)
    return builder.as_markup()


def group_mode_keyboard(group_id: str, lead_order_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(
        text="👷 Barchasini bitta ishchiga berish",
        callback_data=f"group_single:{group_id}:{lead_order_id}",
    )
    builder.button(
        text="👥 Mashinalarni turli ishchilarga bo'lib berish",
        callback_data=f"group_split:{group_id}:{lead_order_id}",
    )
    builder.adjust(1)
    return builder.as_markup()


def group_workers_keyboard(lead_order_id: int, workers: list):
    builder = InlineKeyboardBuilder()
    for worker in workers:
        state = "bo'sh" if worker.status == "bo'sh" else "band"
        builder.button(
            text=f"👷 {worker.name} ({state})",
            callback_data=f"group_worker:{lead_order_id}:{worker.user_id}",
        )
    builder.adjust(1)
    return builder.as_markup()


def wash_duration_keyboard(prefix: str, *parts: int):
    builder = InlineKeyboardBuilder()
    for minutes in (30, 45, 60, 90, 120):
        suffix = ":".join(str(part) for part in parts)
        callback_data = f"{prefix}:{suffix}:{minutes}"
        builder.button(text=f"🧼 {minutes} daqiqa", callback_data=callback_data)
    custom_suffix = ":".join(str(part) for part in parts)
    builder.button(
        text="✍️ Boshqa vaqtni kiritish",
        callback_data=f"{prefix}_custom:{custom_suffix}",
    )
    builder.adjust(2, 1)
    return builder.as_markup()


def payment_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="💵 Naqd", callback_data="payment:Naqd")
    builder.button(text="💳 Karta", callback_data="payment:Karta")
    builder.adjust(2)
    return builder.as_markup()


def worker_payment_keyboard(order_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(
        text="💵 Naqd",
        callback_data=f"worker_payment:Naqd:{order_id}",
    )
    builder.button(
        text="💳 Karta",
        callback_data=f"worker_payment:Karta:{order_id}",
    )
    builder.adjust(2)
    return builder.as_markup()


def location_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.add(
        KeyboardButton(text="📍 Lokatsiyamni yuborish", request_location=True)
    )
    return builder.as_markup(resize_keyboard=True, one_time_keyboard=True)


def manual_location_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="📍 Lokatsiyani yuborish", request_location=True))
    builder.add(KeyboardButton(text="📝 Manzilni matn qilib yozish"))
    return builder.as_markup(resize_keyboard=True, one_time_keyboard=True)


def skip_comment_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="⏭️ O'tkazib yuborish"))
    return builder.as_markup(resize_keyboard=True, one_time_keyboard=True)


def new_order_assignment_keyboard(order_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(
        text="👷 Ishchilarga yuborish",
        callback_data=f"assign_workers:{order_id}",
    )
    builder.button(
        text="🚫 Bekor qilish",
        callback_data=f"cancel_order:{order_id}",
    )
    builder.adjust(1)
    return builder.as_markup()


def available_workers_keyboard(order_id: int, workers: list):
    builder = InlineKeyboardBuilder()
    for worker in workers:
        builder.button(
            text=f"👷 {worker.name} ({worker.share_percent:g}%)",
            callback_data=f"assign_worker:{order_id}:{worker.user_id}",
        )
    builder.adjust(1)
    return builder.as_markup()


def no_available_workers_keyboard(order_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(
        text="📋 Navbatga qo'yish",
        callback_data=f"queue_order:{order_id}",
    )
    builder.button(
        text="👷 Band ishchiga biriktirish",
        callback_data=f"busy_workers:{order_id}",
    )
    builder.adjust(1)
    return builder.as_markup()


def busy_workers_keyboard(order_id: int, workers: list):
    builder = InlineKeyboardBuilder()
    for worker in workers:
        builder.button(
            text=f"👷 {worker.name} ({worker.share_percent:g}%)",
            callback_data=f"queue_worker:{order_id}:{worker.user_id}",
        )
    builder.adjust(1)
    return builder.as_markup()


def queue_offer_decision_keyboard(order_id: int, worker_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✅ Ha",
        callback_data=f"queue_offer:{order_id}:{worker_id}:yes",
    )
    builder.button(
        text="❌ Yo'q",
        callback_data=f"queue_offer:{order_id}:{worker_id}:no",
    )
    builder.adjust(2)
    return builder.as_markup()


def worker_order_decision_keyboard(order_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Qabul qilaman", callback_data=f"worker_accept:{order_id}")
    builder.button(text="❌ Rad etaman", callback_data=f"worker_reject:{order_id}")
    builder.button(text="🚫 Bekor qilish", callback_data=f"cancel_order:{order_id}")
    builder.adjust(2, 1)
    return builder.as_markup()


def worker_status_keyboard(order_id: int, next_stage: str):
    labels = {
        "route": "🚗💨 Yo'lga chiqdim",
        "arrived": "📍 Manzilga yetib keldim",
        "washing": "🧼 Yuvish boshlandi",
        "complete": "🏁 Ish yakunlandi",
    }
    builder = InlineKeyboardBuilder()
    builder.button(
        text=labels[next_stage],
        callback_data=f"worker_status:{next_stage}:{order_id}",
    )
    builder.button(
        text="🚫 Bekor qilish",
        callback_data=f"cancel_order:{order_id}",
    )
    builder.adjust(1)
    return builder.as_markup()


def cancellation_reasons_keyboard(order_id: int):
    builder = InlineKeyboardBuilder()
    reasons = (
        ("🚫 Mijoz voz kechdi", "customer"),
        ("📍 Manzil noto'g'ri/topilmadi", "location"),
        ("⚠️ Ishchi yetib bora olmadi", "worker"),
        ("📝 Boshqa (yozib kiriting)", "other"),
    )
    for label, code in reasons:
        builder.button(
            text=label,
            callback_data=f"cancel_reason:{order_id}:{code}",
        )
    builder.adjust(1)
    return builder.as_markup()


def cancel_only_keyboard(order_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🚫 Bekor qilish",
        callback_data=f"cancel_order:{order_id}",
    )
    return builder.as_markup()
