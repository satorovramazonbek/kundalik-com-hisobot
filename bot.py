"""
Telegram bot: o'qituvchi login/paroli bilan kundalik.com (emaktab.uz) ga kiradi,
sinflar va choraklarni tugma qilib ko'rsatadi, tanlangan hisobotni Excel qilib yuboradi.

Oqim: /start -> login -> parol -> (captcha) -> sinf tugmasi -> chorak tugmasi -> Excel
"""
from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           Message, ReplyKeyboardRemove)
from dotenv import load_dotenv

load_dotenv()

from excel import build_excel  # noqa: E402
from scraper import KundalikClient, KundalikError  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

TOKEN = os.getenv("BOT_TOKEN")
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USERS", "").replace(" ", "").split(",") if x}
SEM = asyncio.Semaphore(int(os.getenv("MAX_PARALLEL", "2")))
SESSION_TTL = int(os.getenv("SESSION_TTL", "600"))  # ochiq seans necha soniya kutadi

router = Router()


class Form(StatesGroup):
    login = State()
    password = State()
    captcha = State()
    choose = State()  # sinf / chorak tugmalari


# user_id -> {"client", "classes", "periods", "class", "timer", "tries", "busy"}
SESSIONS: dict[int, dict] = {}


# ---------------- seans boshqaruvi ----------------
async def drop_session(uid: int) -> None:
    sess = SESSIONS.pop(uid, None)
    if not sess:
        return
    t = sess.get("timer")
    if t and t is not asyncio.current_task():
        t.cancel()
    try:
        await sess["client"].close()
    finally:
        SEM.release()


def touch(uid: int, bot: Bot, chat_id: int, state: FSMContext) -> None:
    """Seans taymerini yangilaydi."""
    sess = SESSIONS.get(uid)
    if not sess:
        return
    if sess.get("timer"):
        sess["timer"].cancel()

    async def expire():
        await asyncio.sleep(SESSION_TTL)
        if uid in SESSIONS:
            await drop_session(uid)
            await state.clear()
            await bot.send_message(chat_id, "⌛ Seans vaqti tugadi. Qayta boshlash: /start")

    sess["timer"] = asyncio.create_task(expire())


def kb(items: list[str], prefix: str, cols: int = 4) -> InlineKeyboardMarkup:
    rows, row = [], []
    for i, name in enumerate(items):
        row.append(InlineKeyboardButton(text=name[:30], callback_data=f"{prefix}:{i}"))
        if len(row) == cols:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


REFRESH_KB = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Boshqa rasm", callback_data="cap_refresh")]])


async def fail(msg: Message, state: FSMContext, uid: int, text: str) -> None:
    await state.clear()
    await drop_session(uid)
    await msg.answer(f"⚠️ {text}\n\nQayta urinish: /start")


# ---------------- handlerlar ----------------
@router.message(F.from_user.func(lambda u: bool(ALLOWED) and u.id not in ALLOWED))
async def not_allowed(msg: Message):
    await msg.answer(f"⛔ Sizga ruxsat yo'q. ID: {msg.from_user.id}")


@router.message(CommandStart())
@router.message(Command("hisobot"))
async def start(msg: Message, state: FSMContext):
    await state.clear()
    await drop_session(msg.from_user.id)
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
    await drop_session(msg.from_user.id)
    await msg.answer("❌ Bekor qilindi. Qayta boshlash: /start", reply_markup=ReplyKeyboardRemove())


@router.message(Form.login, F.text)
async def got_login(msg: Message, state: FSMContext):
    await state.update_data(login=msg.text.strip())
    await msg.answer("2️⃣ <b>Parolni</b> yuboring (xabaringiz xavfsizlik uchun darhol o'chiriladi):")
    await state.set_state(Form.password)


@router.message(Form.password, F.text)
async def got_password(msg: Message, state: FSMContext, bot: Bot):
    uid = msg.from_user.id
    password = msg.text
    try:
        await msg.delete()
    except Exception:
        pass
    data = await state.get_data()
    await state.set_data({})  # parol FSM xotirasida qolmasin
    await drop_session(uid)

    wait = await msg.answer("⏳ Kundalik.com ga ulanilmoqda...")
    await SEM.acquire()
    client = KundalikClient(data["login"], password)
    try:
        await client.open()
    except Exception:
        SEM.release()
        log.exception("Brauzer ochilmadi")
        await state.clear()
        await msg.answer("❗ Brauzer ishga tushmadi. /start")
        return
    SESSIONS[uid] = {"client": client, "tries": 0, "busy": False}
    touch(uid, bot, msg.chat.id, state)
    try:
        await wait.delete()
    except Exception:
        pass
    await try_login(msg, state, uid, None)


async def try_login(msg: Message, state: FSMContext, uid: int, captcha: str | None) -> None:
    sess = SESSIONS[uid]
    try:
        img = await sess["client"].login(captcha)
    except KundalikError as e:
        return await fail(msg, state, uid, str(e))
    except Exception:
        log.exception("Login xato")
        return await fail(msg, state, uid, "Saytga ulanishda xatolik.")

    if img:
        sess["tries"] += 1
        if sess["tries"] > 6:
            return await fail(msg, state, uid, "Captcha juda ko'p marta noto'g'ri kiritildi.")
        await state.set_state(Form.captcha)
        caption = ("❌ Captcha noto'g'ri. Yangi rasmdagi belgilarni yozing:" if captcha
                   else "🔐 Sayt captcha so'rayapti. Rasmdagi belgilarni yozib yuboring:")
        await msg.answer_photo(BufferedInputFile(img, "captcha.png"), caption=caption, reply_markup=REFRESH_KB)
        return

    await show_classes(msg, state, uid)


@router.message(Form.captcha, F.text)
async def got_captcha(msg: Message, state: FSMContext, bot: Bot):
    uid = msg.from_user.id
    if uid not in SESSIONS:
        await state.clear()
        return await msg.answer("Seans tugagan. /start")
    touch(uid, bot, msg.chat.id, state)
    wait = await msg.answer("⏳ Tekshirilmoqda...")
    try:
        await try_login(msg, state, uid, msg.text)
    finally:
        try:
            await wait.delete()
        except Exception:
            pass


@router.callback_query(F.data == "cap_refresh")
async def refresh_captcha(cb: CallbackQuery):
    sess = SESSIONS.get(cb.from_user.id)
    if not sess:
        return await cb.answer("Seans tugagan. /start", show_alert=True)
    client = sess["client"]
    try:
        await client.page.click("button.captcha__button")
        await client.page.wait_for_timeout(1000)
        img = await client._captcha_png(client.page)
        await cb.message.answer_photo(BufferedInputFile(img, "captcha.png"),
                                      caption="Yangi rasm. Belgilarni yozing:", reply_markup=REFRESH_KB)
    except Exception:
        log.exception("captcha refresh")
    await cb.answer()


async def show_classes(msg: Message, state: FSMContext, uid: int) -> None:
    sess = SESSIONS[uid]
    wait = await msg.answer("✅ Tizimga kirildi. Sinflar ro'yxati olinmoqda...")
    try:
        classes = await sess["client"].list_classes()
    except KundalikError as e:
        return await fail(msg, state, uid, str(e))
    except Exception:
        log.exception("Sinflar olinmadi")
        return await fail(msg, state, uid, "Sinflar ro'yxatini olishda xatolik.")
    finally:
        try:
            await wait.delete()
        except Exception:
            pass
    sess["classes"] = classes
    await state.set_state(Form.choose)
    await msg.answer("3️⃣ <b>Sinfni</b> tanlang:", reply_markup=kb([c[0] for c in classes], "cls", cols=4))


@router.callback_query(F.data.startswith("cls:"))
async def chose_class(cb: CallbackQuery, state: FSMContext, bot: Bot):
    uid = cb.from_user.id
    sess = SESSIONS.get(uid)
    if not sess or "classes" not in sess:
        return await cb.answer("Seans tugagan. /start", show_alert=True)
    if sess["busy"]:
        return await cb.answer("Iltimos kuting...")
    await cb.answer()
    touch(uid, bot, cb.message.chat.id, state)
    name, gid = sess["classes"][int(cb.data.split(":")[1])]
    sess["class"] = name
    await cb.message.edit_text(f"Sinf: <b>{name}</b>\n⏳ Choraklar olinmoqda...")
    sess["busy"] = True
    try:
        periods = await sess["client"].list_periods(gid)
    except KundalikError as e:
        return await fail(cb.message, state, uid, str(e))
    except Exception:
        log.exception("Davrlar olinmadi")
        return await fail(cb.message, state, uid, "Choraklarni olishda xatolik.")
    finally:
        sess["busy"] = False

    if not periods:  # davr tanlovi yo'q — sayt standart davr bilan quradi
        periods = [("Joriy davr", None)]
    sess["periods"] = periods
    await cb.message.edit_text(f"Sinf: <b>{name}</b>\n\n4️⃣ <b>Chorakni</b> tanlang:",
                               reply_markup=kb([p[0] for p in periods], "per", cols=2))


@router.callback_query(F.data.startswith("per:"))
async def chose_period(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    sess = SESSIONS.get(uid)
    if not sess or "periods" not in sess:
        return await cb.answer("Seans tugagan. /start", show_alert=True)
    if sess["busy"]:
        return await cb.answer("Iltimos kuting...")
    await cb.answer()
    sess["busy"] = True
    pname, pval = sess["periods"][int(cb.data.split(":")[1])]
    await cb.message.edit_text(f"Sinf: <b>{sess['class']}</b>, {pname}\n⏳ Hisobot tuzilmoqda... (1-3 daqiqa)")
    try:
        report = await asyncio.wait_for(sess["client"].build_report(sess["class"], pname, pval), 300)
        filename, content = build_excel(report)
        await cb.message.answer_document(BufferedInputFile(content, filename=filename),
                                         caption=f"✅ {report.title}\n\nYana hisobot: /start")
        await cb.message.delete()
    except KundalikError as e:
        await cb.message.answer(f"⚠️ {e}\n\nQayta urinish: /start")
    except asyncio.TimeoutError:
        await cb.message.answer("⌛ Sayt juda sekin javob berdi. Keyinroq urinib ko'ring: /start")
    except Exception:
        log.exception("Hisobot xato")
        await cb.message.answer("❗ Kutilmagan xatolik yuz berdi. /start")
    finally:
        await state.clear()
        await drop_session(uid)


@router.message()
async def fallback(msg: Message):
    await msg.answer("Hisobot olish uchun /start bosing.")


async def main():
    if not TOKEN:
        raise SystemExit("BOT_TOKEN topilmadi. .env fayliga BOT_TOKEN=... yozing.")
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)
    log.info("Bot ishga tushdi")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
