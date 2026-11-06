from aiogram import Router

from bot.handlers import common, create, tasks


def get_routers() -> list[Router]:
    # Порядок важен: сначала меню и команды, потом список/редактирование,
    # в конце — создание задач из любого текста/голосового.
    return [common.router, tasks.router, create.router]
