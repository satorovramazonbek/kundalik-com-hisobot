"""
Kirishlar jurnali va diskdagi seanslar.

- data/jurnal.jsonl  — kim, qachon, qaysi eMaktab profiliga kirgani va qanday hisobot olgani.
                       Parol HECH QACHON yozilmaydi.
- data/sessions/<telegram_id>.json — saqlangan seans (cookie). Fayl huquqi 600 (faqat egasi o'qiy oladi).
"""
from __future__ import annotations

import io
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
