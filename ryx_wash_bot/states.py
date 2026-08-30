from aiogram.fsm.state import State, StatesGroup


class RegistrationStates(StatesGroup):
    waiting_name = State()
    waiting_phone = State()


class PriceManagementStates(StatesGroup):
    waiting_new_category = State()
    waiting_new_name = State()
    waiting_new_price = State()
    waiting_updated_price = State()


class ExpenseStates(StatesGroup):
    waiting_amount = State()
    waiting_description = State()


class ReportStates(StatesGroup):
    waiting_custom_range = State()
    waiting_worker = State()


class OrderStates(StatesGroup):
    waiting_car_choice = State()
    waiting_car_category = State()
    waiting_car_model = State()
    waiting_plate = State()
    waiting_car_color = State()
    waiting_next_car = State()
    waiting_payment = State()
    waiting_location = State()
    waiting_comment = State()


class ManualOrderStates(StatesGroup):
    waiting_customer_name = State()
    waiting_customer_phone = State()
    waiting_car_choice = State()
    waiting_new_category = State()
    waiting_new_model = State()
    waiting_new_color = State()
    waiting_next_car = State()
    waiting_car_category = State()
    waiting_car_model = State()
    waiting_plate = State()
    waiting_car_photo = State()
    waiting_payment = State()
    waiting_location = State()
    waiting_address = State()
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


class CancellationStates(StatesGroup):
    waiting_reason = State()
    waiting_custom_reason = State()
