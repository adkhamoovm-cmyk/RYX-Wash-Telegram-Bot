from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from .catalog import categories, format_price, models_for_category


def contact_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="Telefon raqamimni yuborish", request_contact=True))
    return builder.as_markup(resize_keyboard=True, one_time_keyboard=True)


def customer_menu_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="Yangi buyurtma"))
    return builder.as_markup(resize_keyboard=True)


def director_menu_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="Ishchi qo'shish"))
    return builder.as_markup(resize_keyboard=True)


def worker_menu_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.row(
        KeyboardButton(text="Ishga keldim"),
        KeyboardButton(text="Ishdan ketdim"),
    )
    return builder.as_markup(resize_keyboard=True)


def category_keyboard():
    builder = InlineKeyboardBuilder()
    for category in categories():
        builder.button(text=category, callback_data=f"category:{category}")
    builder.adjust(2)
    return builder.as_markup()


def model_keyboard(category: str):
    builder = InlineKeyboardBuilder()
    for model in models_for_category(category):
        builder.button(
            text=f"{model.name} — {format_price(model.price)}",
            callback_data=f"model:{model.id}",
        )
    builder.adjust(1)
    return builder.as_markup()


def payment_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Naqd", callback_data="payment:Naqd")
    builder.button(text="Karta", callback_data="payment:Karta")
    builder.adjust(2)
    return builder.as_markup()


def location_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="Lokatsiyamni yuborish", request_location=True))
    return builder.as_markup(resize_keyboard=True, one_time_keyboard=True)


def skip_comment_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="O'tkazib yuborish"))
    return builder.as_markup(resize_keyboard=True, one_time_keyboard=True)


def new_order_assignment_keyboard(order_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Ishchilarga yuborish",
        callback_data=f"assign_workers:{order_id}",
    )
    builder.button(
        text="Bekor qilish",
        callback_data=f"cancel_order:{order_id}",
    )
    builder.adjust(1)
    return builder.as_markup()


def available_workers_keyboard(order_id: int, workers: list):
    builder = InlineKeyboardBuilder()
    for worker in workers:
        builder.button(
            text=f"{worker.name} ({worker.share_percent:g}%)",
            callback_data=f"assign_worker:{order_id}:{worker.user_id}",
        )
    builder.adjust(1)
    return builder.as_markup()


def no_available_workers_keyboard(order_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Navbatga qo'yish",
        callback_data=f"queue_order:{order_id}",
    )
    builder.button(
        text="Band ishchiga biriktirish",
        callback_data=f"busy_workers:{order_id}",
    )
    builder.adjust(1)
    return builder.as_markup()


def busy_workers_keyboard(order_id: int, workers: list):
    builder = InlineKeyboardBuilder()
    for worker in workers:
        builder.button(
            text=f"{worker.name} ({worker.share_percent:g}%)",
            callback_data=f"queue_worker:{order_id}:{worker.user_id}",
        )
    builder.adjust(1)
    return builder.as_markup()


def queue_offer_decision_keyboard(order_id: int, worker_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Ha",
        callback_data=f"queue_offer:{order_id}:{worker_id}:yes",
    )
    builder.button(
        text="Yo'q",
        callback_data=f"queue_offer:{order_id}:{worker_id}:no",
    )
    builder.adjust(2)
    return builder.as_markup()


def worker_order_decision_keyboard(order_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="Qabul qilaman", callback_data=f"worker_accept:{order_id}")
    builder.button(text="Rad etaman", callback_data=f"worker_reject:{order_id}")
    builder.button(text="Bekor qilish", callback_data=f"cancel_order:{order_id}")
    builder.adjust(2, 1)
    return builder.as_markup()


def worker_status_keyboard(order_id: int, next_stage: str):
    labels = {
        "route": "Yo'lga chiqdim",
        "arrived": "Manzilga yetib keldim",
        "washing": "Yuvish boshlandi",
        "complete": "Ish yakunlandi",
    }
    builder = InlineKeyboardBuilder()
    builder.button(
        text=labels[next_stage],
        callback_data=f"worker_status:{next_stage}:{order_id}",
    )
    builder.button(
        text="Bekor qilish",
        callback_data=f"cancel_order:{order_id}",
    )
    builder.adjust(1)
    return builder.as_markup()


def cancellation_reasons_keyboard(order_id: int):
    builder = InlineKeyboardBuilder()
    reasons = (
        ("Mijoz voz kechdi", "customer"),
        ("Manzil noto'g'ri/topilmadi", "location"),
        ("Ishchi yetib bora olmadi", "worker"),
        ("Boshqa (yozib kiriting)", "other"),
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
        text="Bekor qilish",
        callback_data=f"cancel_order:{order_id}",
    )
    return builder.as_markup()
