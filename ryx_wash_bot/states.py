from aiogram.fsm.state import State, StatesGroup


class RegistrationStates(StatesGroup):
    waiting_name = State()
    waiting_phone = State()


class OrderStates(StatesGroup):
    waiting_plate = State()
    waiting_payment = State()
    waiting_location = State()
    waiting_comment = State()


class WorkerRegistrationStates(StatesGroup):
    waiting_user_id = State()
    waiting_name = State()
    waiting_phone = State()
    waiting_percent = State()


class WorkerCompletionStates(StatesGroup):
    waiting_before_photo = State()
    waiting_after_photo = State()
    waiting_comment = State()
