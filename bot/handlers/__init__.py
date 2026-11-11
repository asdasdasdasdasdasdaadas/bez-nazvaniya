from aiogram import Dispatcher

from bot.handlers import antispam, members, moderation, owner, private, reactions


def setup(dp: Dispatcher) -> None:
    dp.include_router(owner.router)
    dp.include_router(private.router)
    dp.include_router(members.router)
    dp.include_router(moderation.router)
    dp.include_router(antispam.router)
    dp.include_router(reactions.router)
