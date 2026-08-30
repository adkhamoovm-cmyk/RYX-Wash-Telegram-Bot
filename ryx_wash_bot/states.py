from aiogram.fsm.state import State, StatesGroup


class RegistrationStates(StatesGroup):
    waiting_name = State()
    waiting_phone = State()


class OrderStates(StatesGroup):
    waiting_plate = State()
    waiting_payment = State()
    waiting_location = State()
    waiting_comment = State()
