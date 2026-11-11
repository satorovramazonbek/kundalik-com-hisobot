"""
Telegram bot: o'qituvchi login/paroli, sinf va chorakni so'raydi,
kundalik.com (emaktab.uz) dan choraklik hisobotni olib Excel qilib yuboradi.
"""
from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove
from dotenv import load_dotenv

load_dotenv()

from excel import build_excel  # noqa: E402
from scraper import KundalikClient, KundalikError  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

TOKEN = os.getenv("BOT_TOKEN")
# Ixtiyoriy: faqat shu Telegram ID lar foydalana oladi (vergul bilan). Bo'sh bo'lsa — hamma.
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USERS", "").replace(" ", "").split(",") if x}
# Bir vaqtda nechta brauzer ishlashi mumkin
SEM = asyncio.Semaphore(int(os.getenv("MAX_PARALLEL", "2")))

router = Router()


class Form(StatesGroup):
    login = State()
    password = State()
    class_name = State()
    quarter = State()


QUARTER_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=f"{i}-chorak") for i in range(1, 5)]],
    resize_keyboard=True,
    one_time_keyboard=True,
)


@router.message(F.from_user.func(lambda u: bool(ALLOWED) and u.id not in ALLOWED))
async def not_allowed(msg: Message):
    await msg.answer(f"⛔ Sizga ruxsat yo'q. ID: {msg.from_user.id}")


@router.message(CommandStart())
@router.message(Command("hisobot"))
async def start(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer(
        "👋 Assalomu alaykum!\n"
        "Men kundalik.com (emaktab.uz) dan sinfning <b>choraklik hisobotini</b> Excel qilib beraman.\n\n"
        "1️⃣ O'qituvchi <b>loginini</b> yuboring:",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(Form.login)


@router.message(Command("bekor"))
async def cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("❌ Bekor qilindi. Qayta boshlash: /start", reply_markup=ReplyKeyboardRemove())


@router.message(Form.login, F.text)
async def got_login(msg: Message, state: FSMContext):
    await state.update_data(login=msg.text.strip())
    await msg.answer("2️⃣ <b>Parolni</b> yuboring (xabaringiz xavfsizlik uchun darhol o'chiriladi):")
    await state.set_state(Form.password)


@router.message(Form.password, F.text)
async def got_password(msg: Message, state: FSMContext):
    await state.update_data(password=msg.text)
    try:
        await msg.delete()  # parol chatda qolmasin
    except Exception:
        pass
    await msg.answer("3️⃣ <b>Sinf</b> nomini saytdagidek yozing (masalan: <code>5-A</code>):")
    await state.set_state(Form.class_name)


@router.message(Form.class_name, F.text)
async def got_class(msg: Message, state: FSMContext):
    await state.update_data(class_name=msg.text.strip())
    await msg.answer("4️⃣ <b>Chorakni</b> tanlang:", reply_markup=QUARTER_KB)
    await state.set_state(Form.quarter)


@router.message(Form.quarter, F.text.regexp(r"^\s*([1-4])"))
async def got_quarter(msg: Message, state: FSMContext):
    quarter = int(msg.text.strip()[0])
    data = await state.get_data()
    await state.clear()  # parol xotirada saqlanib qolmasin

    wait = await msg.answer("⏳ Kundalik.com ga kirilmoqda va hisobot tayyorlanmoqda... (1-2 daqiqa)",
                            reply_markup=ReplyKeyboardRemove())
    try:
        async with SEM:
            client = KundalikClient(data["login"], data["password"])
            report = await asyncio.wait_for(client.get_quarter_report(data["class_name"], quarter), timeout=240)
        filename, content = build_excel(report)
        await msg.answer_document(BufferedInputFile(content, filename=filename),
                                  caption=f"✅ {report.title}\n\nYana hisobot: /start")
    except KundalikError as e:
        await msg.answer(f"⚠️ {e}\n\nQayta urinish: /start")
    except asyncio.TimeoutError:
        await msg.answer("⌛ Sayt juda sekin javob berdi. Keyinroq qayta urinib ko'ring: /start")
    except Exception:
        log.exception("Kutilmagan xato")
        await msg.answer("❗ Kutilmagan xatolik yuz berdi. Administratorga murojaat qiling. /start")
    finally:
        try:
            await wait.delete()
        except Exception:
            pass


@router.message(Form.quarter)
async def bad_quarter(msg: Message):
    await msg.answer("Iltimos, 1 dan 4 gacha chorakni tanlang.", reply_markup=QUARTER_KB)


@router.message()
async def fallback(msg: Message):
    await msg.answer("Hisobot olish uchun /start bosing.")


async def main():
    if not TOKEN:
        raise SystemExit("BOT_TOKEN topilmadi. .env fayliga BOT_TOKEN=... yozing.")
    from aiogram.client.default import DefaultBotProperties
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)
    log.info("Bot ishga tushdi")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
