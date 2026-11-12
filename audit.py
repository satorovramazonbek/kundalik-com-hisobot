"""
Kirishlar jurnali va diskdagi seanslar.

- data/jurnal.jsonl  — kim, qachon, qaysi eMaktab profiliga kirgani va qanday hisobot olgani.
                       Parol HECH QACHON yozilmaydi.
- data/sessions/<telegram_id>.json — saqlangan seans (cookie). Fayl huquqi 600 (faqat egasi o'qiy oladi).
"""
from __future__ import annotations

import hashlib
import io
import shutil
import json
import os
import time
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
JOURNAL = DATA_DIR / "jurnal.jsonl"
SESS_DIR = DATA_DIR / "sessions"
PROFILES_DIR = DATA_DIR / "profiles"

EVENTS = {
    "login_try": "Kirishga urinish",
    "login_ok": "Profilga kirdi",
    "login_resume": "Saqlangan seans bilan kirdi",
    "login_fail": "Kira olmadi",
    "report": "Hisobot oldi",
    "logout": "Tizimdan chiqdi",
}


def _private_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p, 0o700)
    except OSError:
        pass


# ---------------- jurnal ----------------
def log_event(user, event: str, login: str = "", profile: str = "", details: str = "") -> None:
    """user — aiogram User obyekti."""
    _private_dir(DATA_DIR)
    rec = {
        "vaqt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tg_id": user.id,
        "tg_username": f"@{user.username}" if user.username else "",
        "tg_ism": user.full_name,
        "hodisa": event,
        "login": login,
        "profil": profile,
        "tafsilot": details,
    }
    with JOURNAL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    try:
        os.chmod(JOURNAL, 0o600)
    except OSError:
        pass


def read_journal() -> list[dict]:
    if not JOURNAL.exists():
        return []
    out = []
    for line in JOURNAL.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def journal_text(limit: int = 20) -> str:
    rows = read_journal()[-limit:]
    if not rows:
        return "Jurnal hozircha bo'sh."
    lines = []
    for r in reversed(rows):
        who = r["tg_username"] or r["tg_ism"] or str(r["tg_id"])
        what = EVENTS.get(r["hodisa"], r["hodisa"])
        prof = f" → {r['profil']}" if r.get("profil") else (f" ({r['login']})" if r.get("login") else "")
        det = f" — {r['tafsilot']}" if r.get("tafsilot") else ""
        lines.append(f"<code>{r['vaqt'][5:16]}</code> {who}: {what}{prof}{det}")
    return "\n".join(lines)


def journal_excel() -> tuple[str, bytes]:
    rows = read_journal()
    wb = Workbook()
    ws = wb.active
    ws.title = "Jurnal"
    head = ["Vaqt", "Telegram ID", "Username", "Telegram ismi", "Hodisa", "eMaktab login", "Profil", "Tafsilot"]
    ws.append(head)
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="D9E1F2")
    for r in rows:
        ws.append([r["vaqt"], r["tg_id"], r["tg_username"], r["tg_ism"], EVENTS.get(r["hodisa"], r["hodisa"]),
                   r["login"], r["profil"], r["tafsilot"]])
    for i, w in enumerate([19, 14, 18, 22, 26, 18, 34, 40], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return f"jurnal_{time.strftime('%Y%m%d_%H%M')}.xlsx", buf.getvalue()


# ---------------- diskdagi seanslar ----------------
def save_session(uid: int, storage_state: dict, expires: float, login: str, profile: str) -> None:
    _private_dir(SESS_DIR)
    path = SESS_DIR / f"{uid}.json"
    tmp = path.with_suffix(".tmp")
    # Faylni darhol 600 huquq bilan yaratamiz
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"state": storage_state, "expires": expires, "login": login, "profile": profile}, f)
    os.replace(tmp, path)


def load_sessions() -> dict[int, dict]:
    out = {}
    if not SESS_DIR.exists():
        return out
    now = time.time()
    for p in SESS_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if d.get("expires", 0) > now:
                out[int(p.stem)] = d
            else:
                p.unlink(missing_ok=True)
        except (ValueError, OSError):
            pass
    return out


def delete_session(uid: int) -> None:
    (SESS_DIR / f"{uid}.json").unlink(missing_ok=True)


# ---------------- doimiy brauzer profillari ----------------
def profile_dir(uid: int, login: str) -> Path:
    """Har Telegram foydalanuvchisi + eMaktab login uchun alohida profil (boshqa foydalanuvchi ishlata olmaydi)."""
    _private_dir(PROFILES_DIR)
    _private_dir(PROFILES_DIR / str(uid))
    h = hashlib.sha256(login.strip().lower().encode()).hexdigest()[:16]
    return PROFILES_DIR / str(uid) / h


def delete_profiles(uid: int) -> None:
    shutil.rmtree(PROFILES_DIR / str(uid), ignore_errors=True)


# ---------------- saqlangan login/parol (shifrlangan) ----------------
# Kalit .env dagi CRED_KEY da (data/ dan alohida). Fayl: data/credentials/<uid>.bin, huquq 600.
CRED_DIR = DATA_DIR / "credentials"


def _fernet():
    from cryptography.fernet import Fernet
    key = os.getenv("CRED_KEY", "").strip()
    if not key:
        key = Fernet.generate_key().decode()
        env = Path(".env")
        with env.open("a", encoding="utf-8") as f:
            f.write(f"\nCRED_KEY={key}\n")
        os.environ["CRED_KEY"] = key
    return Fernet(key.encode())


def save_credentials(uid: int, login: str, password: str) -> None:
    _private_dir(CRED_DIR)
    token = _fernet().encrypt(json.dumps({"login": login, "password": password}).encode())
    path = CRED_DIR / f"{uid}.bin"
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(token)
    os.replace(tmp, path)


def load_credentials(uid: int) -> tuple[str, str] | None:
    path = CRED_DIR / f"{uid}.bin"
    if not path.exists():
        return None
    try:
        d = json.loads(_fernet().decrypt(path.read_bytes()))
        return d["login"], d["password"]
    except Exception:
        return None


def delete_credentials(uid: int) -> None:
    (CRED_DIR / f"{uid}.bin").unlink(missing_ok=True)
