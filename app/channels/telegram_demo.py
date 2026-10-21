"""Public demo bot: anyone can talk to the demo studio (sales demo, CV link)."""

from aiogram import Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from app.channels.telegram_inbound import on_demo_message, on_demo_start
from app.context import AppContext


async def start(message: Message, app: AppContext) -> None:
    await on_demo_start(app, message)


async def any_message(message: Message, app: AppContext) -> None:
    await on_demo_message(app, message)


def build_demo_dispatcher() -> Dispatcher:
    router = Router(name="demo")
    router.message.filter(F.chat.type == "private")
    router.message.register(start, CommandStart())
    router.message.register(any_message)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    return dispatcher
