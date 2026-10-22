from aiogram.fsm.state import State, StatesGroup


class Edit(StatesGroup):
    welcome = State()
    ad_text = State()
    ad_button = State()
