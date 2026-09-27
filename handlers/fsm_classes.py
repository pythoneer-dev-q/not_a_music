from aiogram.fsm.state import State, StatesGroup


class NewPl(StatesGroup):
    title = State()


class Profile(StatesGroup):
    about = State()