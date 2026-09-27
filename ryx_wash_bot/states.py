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
    waiting_owner = State()
    waiting_amount = State()
    waiting_description = State()
    waiting_edit_amount = State()
    waiting_edit_description = State()


class ReportStates(StatesGroup):
    waiting_custom_range = State()
    waiting_worker = State()


class CustomerHistoryStates(StatesGroup):
    waiting_custom_range = State()


class CrmStates(StatesGroup):
    waiting_search = State()


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
    waiting_next_car = State()
    waiting_visit_time = State()
    waiting_location = State()
    waiting_address = State()
    waiting_comment = State()


class WorkerOrderStates(StatesGroup):
    waiting_plate = State()
    waiting_payment = State()
    waiting_arrival_eta = State()


class DirectorAssignmentStates(StatesGroup):
    waiting_custom_wash_duration = State()
    waiting_share_value = State()


class AdditionalIncomeStates(StatesGroup):
    waiting_description = State()
    waiting_amount = State()
    waiting_share_value = State()


class OperatorStates(StatesGroup):
    waiting_user_id = State()


class WorkerRegistrationStates(StatesGroup):
    waiting_user_id = State()
    waiting_name = State()
    waiting_phone = State()
    waiting_percent = State()


class WorkerCompletionStates(StatesGroup):
    waiting_before_photo = State()
    waiting_after_photo = State()
    waiting_comment = State()


class WorkerFinanceStates(StatesGroup):
    waiting_amount = State()
    waiting_description = State()


class CancellationStates(StatesGroup):
    waiting_reason = State()
    waiting_custom_reason = State()
