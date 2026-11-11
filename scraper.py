"""
Kundalik.com (hozir emaktab.uz) saytiga o'qituvchi sifatida kirib,
sinfning choraklik (yakuniy) baholar hisobotini olib beruvchi modul.

Sayt rasmiy veb-interfeysi orqali ishlaydi (Playwright, Chromium).
Har bir qadamda debug/ papkasiga sahifa HTML va skrinshot saqlanadi,
shunda sayt tuzilishi o'zgarsa, selektorlarni tez sozlash mumkin.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from playwright.async_api import Page, TimeoutError as PWTimeout, async_playwright

log = logging.getLogger(__name__)

LOGIN_URL = os.getenv("KUNDALIK_LOGIN_URL", "https://login.emaktab.uz/login")
SCHOOLS_URL = os.getenv("KUNDALIK_SCHOOLS_URL", "https://schools.emaktab.uz")
HEADLESS = os.getenv("HEADLESS", "1") != "0"
DEBUG_DIR = Path(os.getenv("DEBUG_DIR", "debug"))
SAVE_DEBUG = os.getenv("SAVE_DEBUG", "1") != "0"

# Menyu/tugma matnlari (o'zbek lotin, kirill va rus tilidagi variantlar)
RE_PERIOD = re.compile(r"chorak|чорак|четверт|yarim|полугод|semestr|триместр|yil|год", re.I)
# Hisobot turi: "O'zlashtirish -> Sinf rahbariga". Boshqasi kerak bo'lsa .env da o'zgartiring
# (masalan: digital-tabel, progress-groups, progress-students)
REPORT_KEY = os.getenv("REPORT_KEY", "progress-classteacher")
STUDY_YEAR = os.getenv("STUDY_YEAR", time.strftime("%Y") if int(time.strftime("%m")) >= 9 else str(int(time.strftime("%Y")) - 1))


class KundalikError(Exception):
    """Foydalanuvchiga ko'rsatiladigan xatolik."""


@dataclass
class Report:
    title: str
    rows: list[list[str]] = field(default_factory=list)  # 1-qator sarlavha
    excel_bytes: bytes | None = None  # sayt o'zi Excel bergan bo'lsa
    excel_name: str | None = None


@dataclass
class ClassReport:
    """'Sinf rahbariga' hisoboti: o'quvchilar x fanlar."""
    class_name: str
    period: str                       # masalan: "2 chorak" (saytning o'zi yozgani)
    subjects: list[str]
    students: list[tuple[str, str, list[str], str, str]]  # (№, FISH, [fan baholari], o'rtacha, reyting)
    footer: list[tuple[str, list[str]]]                    # (nomi, [fanlar bo'yicha qiymat])
    site_excel: bytes | None = None
    site_excel_name: str | None = None


def quarter_date(quarter: int) -> str:
    """Chorak ichidagi sana (dd/mm/yyyy). Kelajakdagi sana bo'lsa — bugun."""
    import datetime as dt
    mmdd = os.getenv("QUARTER_DATES", "10-15,12-15,02-20,04-25").split(",")[quarter - 1]
    m, d = map(int, mmdd.split("-"))
    y = int(STUDY_YEAR) if m >= 8 else int(STUDY_YEAR) + 1
    day = min(dt.date(y, m, d), dt.date.today())
    return day.strftime("%d/%m/%Y")


def available_quarters() -> list[int]:
    """Boshlangan choraklar (kelajakdagilarni ko'rsatmaymiz)."""
    import datetime as dt
    starts = os.getenv("QUARTER_STARTS", "09-02,11-04,01-08,03-28").split(",")
    today = dt.date.today()
    out = []
    for q, mmdd in enumerate(starts, 1):
        m, d = map(int, mmdd.split("-"))
        y = int(STUDY_YEAR) if m >= 8 else int(STUDY_YEAR) + 1
        if dt.date(y, m, d) <= today:
            out.append(q)
    return out or [1]


class KundalikClient:
    def __init__(self, login: str, password: str):
        self.login_name = login
        self.password = password
        self.logged_in = False
        self.page: Page | None = None
        self.school_id: str | None = None
        self._dbg_dir = DEBUG_DIR / time.strftime("%Y%m%d-%H%M%S")
        self._step = 0

    # ---------------- yordamchi ----------------
    async def _dump(self, page: Page, name: str) -> None:
        if not SAVE_DEBUG:
            return
        try:
            self._step += 1
            self._dbg_dir.mkdir(parents=True, exist_ok=True)
            base = self._dbg_dir / f"{self._step:02d}_{name}"
            base.with_suffix(".html").write_text(await page.content(), encoding="utf-8")
            await page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
            base.with_suffix(".url").write_text(page.url, encoding="utf-8")
        except Exception as e:  # debug hech qachon asosiy ishni buzmasin
            log.warning("debug dump xato: %s", e)

    @staticmethod
    async def _log_links(page: Page) -> None:
        """Sahifadagi havolalarni logga yozadi (sozlash uchun)."""
        links = await page.evaluate(
            "() => [...document.querySelectorAll('a')].map(a => (a.innerText||'').trim().replace(/\\s+/g,' ') + ' | ' + a.href)"
            ".filter(x => !x.startsWith(' |'))"
        )
        log.info("Sahifa %s havolalari:\n%s", page.url, "\n".join(links[:150]))

    @staticmethod
    async def _click_text(page: Page, pattern: re.Pattern, kinds=("a", "button", "input[type=submit]", "span")) -> bool:
        for kind in kinds:
            loc = page.locator(kind).filter(has_text=pattern) if not kind.startswith("input") else page.locator(kind)
            n = await loc.count()
            for i in range(n):
                el = loc.nth(i)
                if kind.startswith("input"):
                    val = (await el.get_attribute("value")) or ""
                    if not pattern.search(val):
                        continue
                if await el.is_visible():
                    await el.click()
                    return True
        return False

    @staticmethod
    async def _select_option(page: Page, wanted: str) -> bool:
        """Sahifadagi <select> lar orasidan matni `wanted` ga mos variantni tanlaydi."""
        norm = lambda s: re.sub(r"[\s\-«»\"']", "", s).lower()
        w = norm(wanted)
        selects = page.locator("select")
        for i in range(await selects.count()):
            sel = selects.nth(i)
            options = await sel.locator("option").all()
            for opt in options:
                text = (await opt.inner_text()).strip()
                if text and (norm(text) == w or (len(w) > 1 and w in norm(text))):
                    value = await opt.get_attribute("value")
                    await sel.select_option(value=value)
                    await page.wait_for_load_state("networkidle")
                    return True
        return False

    # ---------------- asosiy qadamlar ----------------
    async def _captcha_visible(self, page: Page) -> bool:
        img = page.locator("img.captcha__image")
        return bool(await img.count()) and await img.first.is_visible()

    async def _captcha_png(self, page: Page) -> bytes:
        img = page.locator("img.captcha__image").first
        await img.wait_for(state="visible")
        await page.wait_for_function("el => el.complete && el.naturalWidth > 0", arg=await img.element_handle())
        return await img.screenshot()

    async def login(self, captcha: str | None = None) -> bytes | None:
        """Kirishga urinadi. Muvaffaqiyatli bo'lsa None, captcha kerak bo'lsa uning PNG rasmini qaytaradi."""
        page = self.page
        if captcha is None:
            await page.goto(LOGIN_URL, wait_until="domcontentloaded")
            await self._dump(page, "login_page")
            if await self._captcha_visible(page):
                await page.fill("input[name='login']", self.login_name)
                await page.fill("input[name='password']", self.password)
                return await self._captcha_png(page)

        await page.fill("input[name='login']", self.login_name)
        await page.fill("input[name='password']", self.password)
        if captcha is not None:
            await page.fill("input[name='Captcha.Input']", captcha.strip())
        await page.click("input[type='submit'], [data-test-id='login-button']")
        try:
            await page.wait_for_load_state("networkidle", timeout=30000)
        except PWTimeout:
            pass
        await self._dump(page, "after_login")

        if "login." in page.url and await page.locator("input[name='password']").count():
            if await self._captcha_visible(page):
                return await self._captcha_png(page)
            err = page.locator(".login__body__hint_error-message:not(.login__body__hint_hidden)")
            msg = (await err.first.inner_text()).strip() if await err.count() else ""
            raise KundalikError("Login yoki parol noto'g'ri." + (f"\nSayt javobi: {msg}" if msg else ""))
        self.logged_in = True
        return None

    # ---------------- hisobot ----------------
    async def _report_url(self, extra: str = "") -> str:
        if not self.school_id:
            html = await self.page.content()
            m = re.search(r"school=(\d{6,})", html) or re.search(r"/journal/school/(\d{6,})", html)
            if not m:
                raise KundalikError("Maktab ID si topilmadi.")
            self.school_id = m.group(1)
        return (f"{SCHOOLS_URL}/v2/reports/default?school={self.school_id}"
                f"&report={REPORT_KEY}&year={STUDY_YEAR}{extra}")

    async def list_classes(self) -> list[tuple[str, str]]:
        """Hisobot sahifasidagi sinflar ro'yxati: [(nomi, group_id), ...]"""
        page = self.page
        await page.goto(await self._report_url(), wait_until="networkidle")
        await self._dump(page, "report_classes")
        items = await page.evaluate(
            """() => [...document.querySelectorAll('a[href*="group="]')]
                .filter(a => a.href.includes('report='))
                .map(a => [(a.title || a.innerText).trim(), new URL(a.href).searchParams.get('group')])"""
        )
        seen, out = set(), []
        for name, gid in items:
            if gid and gid not in seen and name:
                seen.add(gid)
                out.append((name, gid))
        log.info("Sinflar: %s", out)
        if not out:
            raise KundalikError("Hisobot sahifasida sinflar ro'yxati topilmadi.")
        return out

    async def build_class_report(self, class_name: str, group_id: str, quarter: int) -> ClassReport:
        page = self.page
        await page.goto(await self._report_url(f"&group={group_id}"), wait_until="networkidle")
        date = quarter_date(quarter)
        await page.evaluate("d => { const el = document.querySelector('#date'); if (el) { el.value = d;"
                            " el.dispatchEvent(new Event('change', {bubbles: true})); } }", date)
        await self._dump(page, "report_params")

        btn = page.locator("#build_report_button")
        await btn.wait_for(state="visible")
        await btn.click()
        try:
            await page.wait_for_selector("#reportPlaceHolder table#marks", timeout=180000)
        except PWTimeout:
            msg = await page.evaluate("() => (document.querySelector('#errors')?.innerText || '').trim()")
            raise KundalikError("Hisobot tuzilmadi." + (f"\nSayt javobi: {msg}" if msg else ""))
        await page.wait_for_timeout(1000)
        await self._dump(page, "report_result")

        rep = await self._parse_marks(page, class_name, quarter)

        # Saytning umumiy Excel fayli (ixtiyoriy)
        href = await page.evaluate("() => { const a = document.querySelector('#excelButton');"
                                   " return a && a.style.display !== 'none' ? a.href : null; }")
        if href and not href.endswith("#"):
            try:
                resp = await page.context.request.get(href, timeout=120000)
                body = await resp.body()
                if resp.ok and body[:2] == b"PK":
                    rep.site_excel = body
                    rep.site_excel_name = "umumiy.xlsx"
            except Exception as e:
                log.warning("Sayt Excel faylini olishda xato: %s", e)
        return rep

    async def _parse_marks(self, page: Page, class_name: str, quarter: int) -> ClassReport:
        data = await page.evaluate(
            """() => {
                const clean = el => el.innerText.replace(/\\s+/g, ' ').trim();
                const ph = document.querySelector('#reportPlaceHolder');
                const t = ph.querySelector('table#marks');
                const head = [...t.querySelectorAll('thead tr:first-child > *')].map(clean);
                const body = [...t.querySelectorAll('tbody tr')].map(tr => [...tr.children].map(clean));
                const foot = [...t.querySelectorAll('tfoot tr')].map(tr => [...tr.children].map(clean));
                const title = (ph.querySelector('div')?.innerText || '').trim();
                return {head, body, foot, title};
            }"""
        )
        head = data["head"]
        # head: №, FISH, fan1..fanN, O'rtacha ball, Reyting
        subjects = head[2:-2]
        n = len(subjects)
        students = [(r[0], r[1], r[2:2 + n], r[2 + n] if len(r) > 2 + n else "", r[3 + n] if len(r) > 3 + n else "")
                    for r in data["body"] if len(r) >= 2 + n]
        footer = [(r[0], r[1:1 + n]) for r in data["foot"] if r]
        m = re.search(r":\s*(.+)$", data["title"])
        period = m.group(1).strip() if m else f"{quarter} chorak"
        log.info("Hisobot: %s, %s, fanlar=%s, o'quvchilar=%d", class_name, period, subjects, len(students))
        if not subjects:
            raise KundalikError("Hisobotda fanlar topilmadi.")

        return ClassReport(class_name, period, subjects, students, footer)

    async def _extract_table(self, page: Page) -> list[list[str]]:
        """Sahifadagi eng katta jadvalni qatorlar ro'yxati ko'rinishida qaytaradi."""
        return await page.evaluate(
            """() => {
                let best = null, bestCells = 0;
                for (const t of document.querySelectorAll('table')) {
                    const c = t.querySelectorAll('td,th').length;
                    if (c > bestCells) { best = t; bestCells = c; }
                }
                if (!best) return [];
                const rows = [];
                for (const tr of best.querySelectorAll('tr')) {
                    const row = [];
                    for (const cell of tr.querySelectorAll('th,td')) {
                        const txt = cell.innerText.replace(/\\s+/g, ' ').trim();
                        const span = parseInt(cell.getAttribute('colspan') || '1');
                        row.push(txt);
                        for (let k = 1; k < span; k++) row.push('');
                    }
                    if (row.some(x => x !== '')) rows.push(row);
                }
                return rows;
            }"""
        )

    async def open(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=HEADLESS)
        self._ctx = await self._browser.new_context(locale="uz-UZ", accept_downloads=True,
                                                    viewport={"width": 1600, "height": 1000})
        self.page = await self._ctx.new_page()
        self.page.set_default_timeout(30000)

    async def close(self) -> None:
        for obj in (getattr(self, "_ctx", None), getattr(self, "_browser", None)):
            try:
                if obj:
                    await obj.close()
            except Exception:
                pass
        if getattr(self, "_pw", None):
            await self._pw.stop()
            self._pw = None


async def _cli() -> None:
    """Terminaldan sinash: python scraper.py LOGIN PAROL"""
    import sys
    from excel import build_subject_excel

    c = KundalikClient(sys.argv[1], sys.argv[2])
    await c.open()
    try:
        img = await c.login()
        while img:
            Path("captcha.png").write_bytes(img)
            img = await c.login(input("captcha.png dagi belgilarni kiriting: "))
        classes = await c.list_classes()
        for i, (n, _) in enumerate(classes):
            print(i, n)
        cname, gid = classes[int(input("Sinf raqami: "))]
        q = int(input(f"Chorak {available_quarters()}: "))
        rep = await c.build_class_report(cname, gid, q)
        for i, sub in enumerate(rep.subjects):
            print(i, sub)
        name, data = build_subject_excel(rep, int(input("Fan raqami: ")))
        Path(name).write_bytes(data)
        print("Saqlandi:", name)
    finally:
        await c.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_cli())
