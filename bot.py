"""
Telegram bot: o'qituvchi login/paroli bilan kundalik.com (emaktab.uz) ga kiradi,
sinflar va choraklarni tugma qilib ko'rsatadi, tanlangan hisobotni Excel qilib yuboradi.

Oqim: /start -> login -> parol -> (captcha) -> hisobot turi -> sinf -> chorak -> (fan) -> Excel
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           Message, ReplyKeyboardRemove)
from dotenv import load_dotenv

load_dotenv()

from excel import build_class_excel, build_subject_excel, build_table_excel  # noqa: E402
from scraper import REPORTS, SUBJECT_REPORTS, KundalikClient, KundalikError, TwoFactorRequired, available_quarters  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

TOKEN = os.getenv("BOT_TOKEN")
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USERS", "").replace(" ", "").split(",") if x}
SEM = asyncio.Semaphore(int(os.getenv("MAX_PARALLEL", "3")))
SESSION_TTL = int(os.getenv("SESSION_TTL", "1800"))  # ochiq brauzer necha soniya harakatsiz kutadi
COOKIE_TTL = int(os.getenv("COOKIE_TTL", str(8 * 3600)))  # qayta login so'ramaslik muddati (faqat xotirada)

router = Router()


class Form(StatesGroup):
    login = State()
    password = State()
    captcha = State()
    choose = State()  # sinf / chorak tugmalari


# user_id -> {"client", "classes", "periods", "class", "timer", "tries", "busy"}
SESSIONS: dict[int, dict] = {}
# user_id -> (cookie holati, tugash vaqti). Faqat operativ xotirada, diskka yozilmaydi.
COOKIES: dict[int, tuple[dict, float]] = {}


# ---------------- seans boshqaruvi ----------------
async def drop_session(uid: int, logout: bool = False) -> None:
    if logout:
        COOKIES.pop(uid, None)
    sess = SESSIONS.pop(uid, None)
    if not sess:
        return
    t = sess.get("timer")
    if t and t is not asyncio.current_task():
        t.cancel()
    try:
        if not logout and sess["client"].logged_in:
            st = await sess["client"].storage_state()
            if st:
                COOKIES[uid] = (st, time.time() + COOKIE_TTL)
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
            await bot.send_message(chat_id, "💤 Uzoq vaqt harakat bo'lmadi, brauzer yopildi.\n"
                                            "Davom etish uchun /start bosing (qayta login shart emas).")

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


async def status(msg: Message, uid: int, line: str, replace_last: bool = False) -> None:
    """Bitta xabarda kirish jarayonini bosqichma-bosqich ko'rsatadi."""
    sess = SESSIONS.get(uid)
    if not sess:
        return await msg.answer(line)
    lines = sess.setdefault("status_lines", [])
    if replace_last and lines:
        lines[-1] = line
    else:
        lines.append(line)
    text = "\n".join(lines)
    st = sess.get("status_msg")
    try:
        if st:
            await st.edit_text(text)
            return
    except Exception:
        pass
    sess["status_msg"] = await msg.answer(text)


def restart_status(uid: int) -> None:
    """Keyingi holat yangi xabarda chiqsin (masalan captcha rasmidan keyin)."""
    sess = SESSIONS.get(uid)
    if sess:
        sess["status_msg"] = None
        sess["status_lines"] = []


async def fail(msg: Message, state: FSMContext, uid: int, text: str) -> None:
    """Login xatosi: seans yopiladi."""
    await state.clear()
    await drop_session(uid, logout=True)
    await msg.answer(f"⚠️ {text}\n\nQayta urinish: /start")


async def soft_fail(msg: Message, state: FSMContext, uid: int, text: str) -> None:
    """Hisobot xatosi: seans saqlanadi, menyuga qaytamiz."""
    await msg.answer(f"⚠️ {text}")
    if uid in SESSIONS:
        await show_reports(msg, state, uid)
    else:
        await msg.answer("Qayta urinish: /start")


async def open_session(uid: int, client: KundalikClient, bot: Bot, chat_id: int, state: FSMContext,
                       storage_state: dict | None = None) -> bool:
    await SEM.acquire()
    try:
        await client.open(storage_state)
    except Exception:
        SEM.release()
        log.exception("Brauzer ochilmadi")
        return False
    SESSIONS[uid] = {"client": client, "tries": 0, "busy": False}
    touch(uid, bot, chat_id, state)
    return True


# ---------------- handlerlar ----------------
@router.message(F.from_user.func(lambda u: bool(ALLOWED) and u.id not in ALLOWED))
async def not_allowed(msg: Message):
    await msg.answer(f"⛔ Sizga ruxsat yo'q. ID: {msg.from_user.id}")


@router.message(CommandStart())
@router.message(Command("hisobot"))
async def start(msg: Message, state: FSMContext, bot: Bot):
    uid = msg.from_user.id
    await state.clear()
    sess = SESSIONS.get(uid)
    if sess and sess["client"].logged_in and not sess["busy"]:
        touch(uid, bot, msg.chat.id, state)
        return await show_reports(msg, state, uid)
    await drop_session(uid)

    saved = COOKIES.get(uid)
    if saved and saved[1] > time.time():
        wait = await msg.answer("⏳ Avvalgi seans tiklanmoqda...")
        client = KundalikClient("", "")
        ok = await open_session(uid, client, bot, msg.chat.id, state, saved[0])
        if ok and await client.resume():
            SESSIONS[uid]["profile"] = await client.profile_name()
            try:
                await wait.delete()
            except Exception:
                pass
            return await show_reports(msg, state, uid)
        await drop_session(uid, logout=True)
        try:
            await wait.edit_text("Seans muddati tugagan, qayta kirish kerak.")
        except Exception:
            pass

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
    await msg.answer("❌ Bekor qilindi. Davom etish: /start", reply_markup=ReplyKeyboardRemove())


@router.message(Command("chiqish"))
async def logout(msg: Message, state: FSMContext):
    await state.clear()
    await drop_session(msg.from_user.id, logout=True)
    await msg.answer("🚪 Tizimdan chiqildi, saqlangan seans o'chirildi. Qayta kirish: /start",
                     reply_markup=ReplyKeyboardRemove())


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
    saved = COOKIES.get(uid)  # oldingi seans cookie lari — sayt qurilmani "tanish" deb biladi, 2FA kamroq so'raladi

    client = KundalikClient(data["login"], password)
    if not await open_session(uid, client, bot, msg.chat.id, state, saved[0] if saved else None):
        await state.clear()
        await msg.answer("❗ Brauzer ishga tushmadi. /start")
        return
    await status(msg, uid, f"🔐 <b>{html.escape(data['login'])}</b> login orqali kirishga urinilmoqda...")
    await try_login(msg, state, uid, None)


async def try_login(msg: Message, state: FSMContext, uid: int, captcha: str | None) -> None:
    sess = SESSIONS[uid]
    try:
        img = await sess["client"].login(captcha)
    except TwoFactorRequired as tf:
        return await ask_2fa(msg, state, uid, tf)
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
        if captcha:
            await status(msg, uid, "❌ Captcha noto'g'ri, sayt yangi rasm berdi.", replace_last=True)
        else:
            await status(msg, uid, "🧩 Sayt captcha so'radi.")
        await msg.answer_photo(BufferedInputFile(img, "captcha.png"),
                               caption="Rasmdagi belgilarni yozib yuboring 👇", reply_markup=REFRESH_KB)
        restart_status(uid)
        return

    if captcha:
        await status(msg, uid, "✅ Captcha o'tdi.", replace_last=True)
    await logged_in(msg, state, uid)


async def logged_in(msg: Message, state: FSMContext, uid: int) -> None:
    sess = SESSIONS[uid]
    name = await sess["client"].profile_name()
    sess["profile"] = name
    await status(msg, uid, "✅ Profilga kirdim" + (f": <b>{html.escape(name)}</b>" if name else "."))
    await show_reports(msg, state, uid)


async def ask_2fa(msg: Message, state: FSMContext, uid: int, tf: TwoFactorRequired) -> None:
    """Sayt yangi qurilmadan kirishni Telegram orqali tasdiqlashni so'raydi — o'qituvchi o'zi tasdiqlaydi."""
    text = ("🛡 <b>eMaktab kirishni tasdiqlashni so'rayapti</b> (yangi qurilma).\n\n"
            "Pastdagi tugmani bosing, ochilgan rasmiy <b>@emaktab_auth_bot</b> da kirishni tasdiqlang. "
            "Tasdiqlashingiz bilan bot o'zi davom etadi (5 daqiqa kutadi).")
    buttons = []
    if tf.link:
        buttons.append([InlineKeyboardButton(text="✅ Telegram'da tasdiqlash", url=tf.link)])
    markup = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    if tf.qr_png:
        await msg.answer_photo(BufferedInputFile(tf.qr_png, "qr.png"), caption=text, reply_markup=markup)
    else:
        await msg.answer(text + ("" if tf.link else "\n\n(Havola topilmadi — eMaktab ilovasi orqali tasdiqlang.)"),
                         reply_markup=markup)
    restart_status(uid)
    sess = SESSIONS[uid]
    await status(msg, uid, "🛡 Sayt kirishni Telegram orqali tasdiqlashni so'radi. Tasdiqlashingiz kutilmoqda...")
    sess["busy"] = True
    try:
        ok = await sess["client"].wait_2fa(300)
    except Exception:
        log.exception("2FA kutishda xato")
        ok = False
    finally:
        if uid in SESSIONS:
            sess["busy"] = False
    if uid not in SESSIONS:
        return
    if not ok:
        return await fail(msg, state, uid, "Kirish 5 daqiqa ichida tasdiqlanmadi.")
    await status(msg, uid, "✅ Kirish tasdiqlandi.", replace_last=True)
    await logged_in(msg, state, uid)


def reports_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=name, callback_data=f"rep:{key}")]
                                                 for key, (_, name) in REPORTS.items()])


async def show_reports(msg: Message, state: FSMContext, uid: int, edit: bool = False) -> None:
    await state.set_state(Form.choose)
    prof = SESSIONS.get(uid, {}).get("profile")
    text = ("👤 Profil: <b>" + html.escape(prof) + "</b>\n\n" if prof else "") + "3️⃣ <b>Hisobot turini</b> tanlang:"
    if edit:
        await msg.edit_text(text, reply_markup=reports_kb())
    else:
        await msg.answer(text, reply_markup=reports_kb())


@router.callback_query(F.data.startswith("rep:"))
async def chose_report(cb: CallbackQuery, state: FSMContext, bot: Bot):
    uid = cb.from_user.id
    sess = SESSIONS.get(uid)
    key = cb.data.split(":")[1]
    if not sess or key not in REPORTS:
        return await cb.answer("Seans tugagan. /start", show_alert=True)
    if sess["busy"]:
        return await cb.answer("Iltimos kuting...")
    await cb.answer()
    touch(uid, bot, cb.message.chat.id, state)
    sess["rep_key"] = key
    sess.pop("report", None)
    try:
        await cb.message.delete()
    except Exception:
        pass
    await show_classes(cb.message, state, uid)


@router.callback_query(F.data == "again")
async def again(cb: CallbackQuery, state: FSMContext, bot: Bot):
    uid = cb.from_user.id
    if uid not in SESSIONS:
        return await cb.answer("Seans tugagan. /start", show_alert=True)
    await cb.answer()
    touch(uid, bot, cb.message.chat.id, state)
    await cb.message.edit_reply_markup(reply_markup=None)
    await show_reports(cb.message, state, uid)


@router.callback_query(F.data == "done")
async def done(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await drop_session(cb.from_user.id)  # cookie saqlanadi — /start da qayta login so'ralmaydi
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer("✅ Tayyor. Yana hisobot kerak bo'lsa /start bosing (qayta login shart emas).\n"
                            "Tizimdan butunlay chiqish: /chiqish")


NEXT_KB = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔁 Boshqa hisobot", callback_data="again"),
                                                 InlineKeyboardButton(text="✅ Tugatish", callback_data="done")]])


@router.message(Form.captcha, F.text)
async def got_captcha(msg: Message, state: FSMContext, bot: Bot):
    uid = msg.from_user.id
    if uid not in SESSIONS:
        await state.clear()
        return await msg.answer("Seans tugagan. /start")
    touch(uid, bot, msg.chat.id, state)
    await status(msg, uid, f"⏳ Captcha (<code>{html.escape(msg.text.strip()[:20])}</code>) kiritildi, tekshirilmoqda...")
    await try_login(msg, state, uid, msg.text)


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
    report, name = REPORTS[sess.get("rep_key", "cr")]
    wait = await msg.answer(f"{name}\n⏳ Sinflar ro'yxati olinmoqda...")
    sess["busy"] = True
    try:
        classes = await sess["client"].list_classes(report)
    except KundalikError as e:
        sess["busy"] = False
        return await soft_fail(msg, state, uid, str(e))
    except Exception:
        log.exception("Sinflar olinmadi")
        sess["busy"] = False
        return await soft_fail(msg, state, uid, "Sinflar ro'yxatini olishda xatolik.")
    finally:
        sess["busy"] = False
        try:
            await wait.delete()
        except Exception:
            pass
    sess["classes"] = classes
    await state.set_state(Form.choose)
    await msg.answer(f"{name}\n\n4️⃣ <b>Sinfni</b> tanlang:", reply_markup=kb([c[0] for c in classes], "cls", cols=4))


@router.callback_query(F.data.startswith("cls:"))
async def chose_class(cb: CallbackQuery, state: FSMContext, bot: Bot):
    uid = cb.from_user.id
    sess = SESSIONS.get(uid)
    if not sess or "classes" not in sess:
        return await cb.answer("Seans tugagan. /start", show_alert=True)
    await cb.answer()
    touch(uid, bot, cb.message.chat.id, state)
    sess["class"] = sess["classes"][int(cb.data.split(":")[1])]
    quarters = available_quarters()
    buttons = [[InlineKeyboardButton(text=f"{q}-chorak", callback_data=f"q:{q}") for q in quarters]]
    await cb.message.edit_text(f"{REPORTS[sess.get('rep_key', 'cr')][1]}\nSinf: <b>{sess['class'][0]}</b>\n\n"
                               "5️⃣ <b>Chorakni</b> tanlang:",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


def subjects_kb(subjects: list[str]) -> InlineKeyboardMarkup:
    m = kb(subjects, "sub", cols=2)
    m.inline_keyboard.append([InlineKeyboardButton(text="📊 Umumiy (barcha fanlar)", callback_data="sub:all")])
    m.inline_keyboard.append([InlineKeyboardButton(text="🔁 Boshqa hisobot", callback_data="again"),
                              InlineKeyboardButton(text="✅ Tugatish", callback_data="done")])
    return m


@router.callback_query(F.data.startswith("q:"))
async def chose_quarter(cb: CallbackQuery, state: FSMContext, bot: Bot):
    uid = cb.from_user.id
    sess = SESSIONS.get(uid)
    if not sess or "class" not in sess:
        return await cb.answer("Seans tugagan. /start", show_alert=True)
    if sess["busy"]:
        return await cb.answer("Iltimos kuting...")
    await cb.answer()
    touch(uid, bot, cb.message.chat.id, state)
    quarter = int(cb.data.split(":")[1])
    cname, gid = sess["class"]
    await cb.message.edit_text(f"Sinf: <b>{cname}</b>, {quarter}-chorak\n⏳ Hisobot tuzilmoqda... (1-3 daqiqa)")
    key = sess.get("rep_key", "cr")
    report, rname = REPORTS[key]
    sess["busy"] = True
    try:
        if key not in SUBJECT_REPORTS:
            sess["busy"] = False
            return await send_table_report(cb, state, uid, report, rname, cname, gid, quarter)
        rep = await asyncio.wait_for(sess["client"].build_class_report(cname, gid, quarter, report), 300)
    except KundalikError as e:
        sess["busy"] = False
        return await soft_fail(cb.message, state, uid, str(e))
    except asyncio.TimeoutError:
        sess["busy"] = False
        return await soft_fail(cb.message, state, uid, "Sayt juda sekin javob berdi. Keyinroq urinib ko'ring.")
    except Exception:
        log.exception("Hisobot xato")
        sess["busy"] = False
        return await soft_fail(cb.message, state, uid, "Kutilmagan xatolik yuz berdi.")
    finally:
        sess["busy"] = False

    sess["report"] = rep
    note = ""
    if not re.search(rf"\b{quarter}\b", rep.period):
        note = f"\n⚠️ Sayt <b>{rep.period}</b> uchun hisobot berdi (chorak sanalarini .env da tekshiring)."
    await cb.message.edit_text(
        f"{rname}\nSinf: <b>{cname}</b>, hisobot davri: <b>{rep.period}</b>{note}\n\n"
        f"6️⃣ <b>Fanni</b> tanlang (bir nechtasini ketma-ket olishingiz mumkin):",
        reply_markup=subjects_kb(rep.subjects),
    )


async def send_table_report(cb: CallbackQuery, state: FSMContext, uid: int, report: str, rname: str,
                            cname: str, gid: str, quarter: int) -> None:
    """O'zlashtirish/Davomat -> Sinflar: jadvalni Excel qilib yuboradi."""
    sess = SESSIONS[uid]
    sess["busy"] = True
    try:
        rep = await asyncio.wait_for(sess["client"].build_table_report(report, rname, cname, gid, quarter), 300)
    except KundalikError as e:
        return await soft_fail(cb.message, state, uid, str(e))
    except asyncio.TimeoutError:
        return await soft_fail(cb.message, state, uid, "Sayt juda sekin javob berdi. Keyinroq urinib ko'ring.")
    except Exception:
        log.exception("Hisobot xato")
        return await soft_fail(cb.message, state, uid, "Kutilmagan xatolik yuz berdi.")
    finally:
        sess["busy"] = False
    note = ""
    if not re.search(rf"\b{quarter}\b", rep.period):
        note = f"\n⚠️ Sayt <b>{rep.period}</b> uchun hisobot berdi."
    await cb.message.edit_text(f"{rname}\nSinf: <b>{cname}</b>, davr: <b>{rep.period}</b>{note}")
    filename, content = build_table_excel(rep)
    caption = f"{rname}\n{cname} sinf, {rep.period}"
    if rep.site_excel:
        await cb.message.answer_document(BufferedInputFile(rep.site_excel, filename="sayt_" + filename),
                                         caption=caption + "\n(saytning o'z Excel fayli)")
    await cb.message.answer_document(BufferedInputFile(content, filename=filename), caption=caption,
                                     reply_markup=NEXT_KB)


@router.callback_query(F.data.startswith("sub:"))
async def chose_subject(cb: CallbackQuery, state: FSMContext, bot: Bot):
    uid = cb.from_user.id
    sess = SESSIONS.get(uid)
    if not sess or "report" not in sess:
        return await cb.answer("Seans tugagan. /start", show_alert=True)
    what = cb.data.split(":")[1]
    touch(uid, bot, cb.message.chat.id, state)
    rep = sess["report"]
    try:
        if what == "all":
            filename, content = build_class_excel(rep)
            caption = f"📊 {rep.class_name} — umumiy, {rep.period}"
        else:
            idx = int(what)
            filename, content = build_subject_excel(rep, idx)
            caption = f"📘 {rep.class_name} — {rep.subjects[idx]}, {rep.period}"
        await cb.answer("Yuborilmoqda...")
        await cb.message.answer_document(BufferedInputFile(content, filename=filename),
                                         caption=caption + "\n\nYana fan tanlang, «🔁 Boshqa hisobot» yoki «✅ Tugatish» ni bosing.")
    except Exception:
        log.exception("Excel xato")
        await cb.answer("Xatolik yuz berdi", show_alert=True)


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
