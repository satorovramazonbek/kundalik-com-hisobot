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
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery, KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove
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
    captcha = State()


# Ochiq brauzer seanslari: user_id -> (client, sinf, chorak, urinishlar)
SESSIONS: dict[int, dict] = {}
SESSION_TTL = 300  # captcha kutish vaqti (soniya)


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
    await _drop_session(msg.from_user.id)
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
    await _drop_session(msg.from_user.id)
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


REFRESH_KB = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Boshqa rasm", callback_data="captcha_refresh")]])


async def _drop_session(uid: int) -> None:
    sess = SESSIONS.pop(uid, None)
    if sess:
        task = sess.get("timer")
        if task and task is not asyncio.current_task():
            task.cancel()
        await sess["client"].close()
        SEM.release()


async def _expire(uid: int, bot: Bot, chat_id: int, state: FSMContext) -> None:
    await asyncio.sleep(SESSION_TTL)
    if uid in SESSIONS:
        await _drop_session(uid)
        await state.clear()
        await bot.send_message(chat_id, "⌛ Captcha kutish vaqti tugadi. Qayta boshlash: /start")


async def _ask_captcha(msg: Message, img: bytes, retry: bool = False) -> None:
    text = ("❌ Captcha noto'g'ri. Yangi rasmdagi belgilarni yozing:" if retry
            else "🔐 Sayt captcha so'rayapti. Rasmdagi belgilarni yozib yuboring:")
    await msg.answer_photo(BufferedInputFile(img, "captcha.png"), caption=text, reply_markup=REFRESH_KB)


async def _finish(msg: Message, state: FSMContext, uid: int) -> None:
    sess = SESSIONS[uid]
    wait = await msg.answer("⏳ Hisobot tayyorlanmoqda... (1-2 daqiqa)")
    try:
        report = await asyncio.wait_for(sess["client"].get_quarter_report(sess["class_name"], sess["quarter"]), 240)
        filename, content = build_excel(report)
        await msg.answer_document(BufferedInputFile(content, filename=filename),
                                  caption=f"✅ {report.title}\n\nYana hisobot: /start")
    except KundalikError as e:
        await msg.answer(f"⚠️ {e}\n\nQayta urinish: /start")
    except asyncio.TimeoutError:
        await msg.answer("⌛ Sayt juda sekin javob berdi. Keyinroq urinib ko'ring: /start")
    except Exception:
        log.exception("Kutilmagan xato")
        await msg.answer("❗ Kutilmagan xatolik yuz berdi. /start")
    finally:
        await state.clear()
        await _drop_session(uid)
        try:
            await wait.delete()
        except Exception:
            pass


async def _try_login(msg: Message, state: FSMContext, uid: int, captcha: str | None) -> None:
    sess = SESSIONS[uid]
    try:
        img = await sess["client"].login(captcha)
    except KundalikError as e:
        await state.clear()
        await _drop_session(uid)
        await msg.answer(f"⚠️ {e}\n\nQayta urinish: /start")
        return
    except Exception:
        log.exception("Login xato")
        await state.clear()
        await _drop_session(uid)
        await msg.answer("❗ Saytga ulanishda xatolik. /start")
        return
    if img:
        sess["tries"] += 1
        if sess["tries"] > 5:
            await state.clear()
            await _drop_session(uid)
            await msg.answer("Captcha juda ko'p marta noto'g'ri kiritildi. /start")
            return
        await state.set_state(Form.captcha)
        await _ask_captcha(msg, img, retry=captcha is not None)
        return
    await _finish(msg, state, uid)


@router.message(Form.quarter, F.text.regexp(r"^\s*([1-4])"))
async def got_quarter(msg: Message, state: FSMContext, bot: Bot):
    uid = msg.from_user.id
    quarter = int(msg.text.strip()[0])
    data = await state.get_data()
    await state.set_data({})  # parol FSM da saqlanib qolmasin
    await _drop_session(uid)

    wait = await msg.answer("⏳ Kundalik.com ga ulanilmoqda...", reply_markup=ReplyKeyboardRemove())
    await SEM.acquire()
    client = KundalikClient(data["login"], data["password"])
    try:
        await client.open()
    except Exception:
        SEM.release()
        log.exception("Brauzer ochilmadi")
        await msg.answer("❗ Brauzer ishga tushmadi. /start")
        await state.clear()
        return
    SESSIONS[uid] = {"client": client, "class_name": data["class_name"], "quarter": quarter, "tries": 0,
                     "timer": asyncio.create_task(_expire(uid, bot, msg.chat.id, state))}
    try:
        await wait.delete()
    except Exception:
        pass
    await _try_login(msg, state, uid, None)


@router.message(Form.captcha, F.text)
async def got_captcha(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    if uid not in SESSIONS:
        await state.clear()
        await msg.answer("Seans tugagan. /start")
        return
    await _try_login(msg, state, uid, msg.text)


@router.callback_query(F.data == "captcha_refresh")
async def refresh_captcha(cb: CallbackQuery):
    sess = SESSIONS.get(cb.from_user.id)
    if not sess:
        await cb.answer("Seans tugagan. /start", show_alert=True)
        return
    page = sess["client"].page
    try:
        await page.click("button.captcha__button")
        await page.wait_for_timeout(1000)
        img = await sess["client"]._captcha_png(page)
        await cb.message.answer_photo(BufferedInputFile(img, "captcha.png"),
                                      caption="Yangi rasm. Belgilarni yozing:", reply_markup=REFRESH_KB)
    except Exception:
        log.exception("captcha refresh")
    await cb.answer()


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
