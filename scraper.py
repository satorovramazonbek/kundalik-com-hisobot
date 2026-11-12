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
# Botda tanlanadigan hisobotlar: kalit -> (sayt report=..., tugma nomi)
REPORTS = {
    "cr": ("progress-classteacher", "👩‍🏫 Fan bo‘yicha — o‘rtacha baholar (Sinf rahbariga)"),
    "pg": ("progress-groups", "📘 O‘zlashtirish — fanlar kesimida (chorak baholari)"),
    "ca": ("class-attendance", "🗓 Davomat — Sinflar"),
}
# Fanlar kesimida (fan tugmalari bilan) chiqadigan hisobotlar
SUBJECT_REPORTS = {"cr", "pg"}
STUDY_YEAR = os.getenv("STUDY_YEAR", time.strftime("%Y") if int(time.strftime("%m")) >= 9 else str(int(time.strftime("%Y")) - 1))


class KundalikError(Exception):
    """Foydalanuvchiga ko'rsatiladigan xatolik."""


class TwoFactorRequired(Exception):
    """Sayt yangi qurilmadan kirishni Telegram orqali tasdiqlashni so'rayapti."""

    def __init__(self, link: str | None, qr_png: bytes | None):
        super().__init__("2FA")
        self.link = link
        self.qr_png = qr_png


@dataclass
class Report:
    title: str
    rows: list[list[str]] = field(default_factory=list)  # 1-qator sarlavha
    excel_bytes: bytes | None = None  # sayt o'zi Excel bergan bo'lsa
    excel_name: str | None = None


@dataclass
class TableReport:
    """Umumiy jadval ko'rinishidagi hisobot (Sinflar, Davomat va h.k.)."""
    report_name: str
    class_name: str
    period: str
    tables: list[dict] = field(default_factory=list)  # {"head": n, "rows": [[...]]}
    site_excel: bytes | None = None


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


def quarter_range(quarter: int) -> tuple[str, str]:
    """Chorakning boshi va oxiri (dd/mm/yyyy). Oxiri kelajakda bo'lsa — bugun."""
    import datetime as dt
    starts = os.getenv("QUARTER_STARTS", "09-02,11-04,01-08,03-28").split(",")
    ends = os.getenv("QUARTER_ENDS", "").split(",") if os.getenv("QUARTER_ENDS") else None

    def day(mmdd: str) -> dt.date:
        m, d = map(int, mmdd.split("-"))
        return dt.date(int(STUDY_YEAR) if m >= 8 else int(STUDY_YEAR) + 1, m, d)

    start = day(starts[quarter - 1])
    if ends:
        end = day(ends[quarter - 1])
    elif quarter < len(starts):
        end = day(starts[quarter]) - dt.timedelta(days=1)
    else:
        end = day("05-25")
    end = min(end, dt.date.today())
    return start.strftime("%d/%m/%Y"), end.strftime("%d/%m/%Y")


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
            if "login." not in page.url:  # saqlangan seans bilan allaqachon kirilgan
                self.logged_in = True
                return None
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

        if "2fa" in page.url.lower():
            await self._raise_2fa(page)

        if "login." in page.url and await page.locator("input[name='password']").count():
            if await self._captcha_visible(page):
                return await self._captcha_png(page)
            err = page.locator(".login__body__hint_error-message:not(.login__body__hint_hidden)")
            msg = (await err.first.inner_text()).strip() if await err.count() else ""
            raise KundalikError("Login yoki parol noto'g'ri." + (f"\nSayt javobi: {msg}" if msg else ""))
        if "login." in page.url:
            raise KundalikError("Tizimga kirish yakunlanmadi (sayt kutilmagan sahifa ochdi).")
        self.logged_in = True
        return None

    async def _raise_2fa(self, page: Page) -> None:
        link = await page.evaluate(
            "() => { const a = [...document.querySelectorAll('a[href]')].find(a => /t\\.me\\/.+_bot\\?start=/i.test(a.href));"
            " return a ? a.href : null; }")
        if not link:
            m = re.search(r"https://t\.me/[A-Za-z0-9_]+_bot\?start=[A-Za-z0-9_-]+", await page.content())
            link = m.group(0) if m else None
        qr = None
        for sel in ("canvas", "svg[class*='qr' i]", "img[src*='qr' i]", "[class*='qr' i]"):
            loc = page.locator(sel).first
            try:
                if await loc.count() and await loc.is_visible():
                    qr = await loc.screenshot()
                    break
            except Exception:
                pass
        raise TwoFactorRequired(link, qr)

    async def wait_2fa(self, timeout_s: int = 300) -> bool:
        """Foydalanuvchi Telegram'da tasdiqlaguncha kutadi."""
        try:
            await self.page.wait_for_url(lambda u: "login." not in u, timeout=timeout_s * 1000)
        except PWTimeout:
            return False
        try:
            await self.page.wait_for_load_state("networkidle", timeout=30000)
        except PWTimeout:
            pass
        await self._dump(self.page, "after_2fa")
        self.logged_in = True
        return True

    # ---------------- hisobot ----------------
    async def _report_url(self, extra: str = "", report: str = REPORT_KEY) -> str:
        if not self.school_id:
            html = await self.page.content()
            m = re.search(r"school=(\d{6,})", html) or re.search(r"/journal/school/(\d{6,})", html)
            if not m:
                raise KundalikError("Maktab ID si topilmadi.")
            self.school_id = m.group(1)
        return (f"{SCHOOLS_URL}/v2/reports/default?school={self.school_id}"
                f"&report={report}&year={STUDY_YEAR}{extra}")

    async def list_classes(self, report: str = REPORT_KEY) -> list[tuple[str, str]]:
        """Hisobot sahifasidagi sinflar ro'yxati: [(nomi, group_id), ...]"""
        page = self.page
        await page.goto(await self._report_url(report=report), wait_until="networkidle")
        await self._dump(page, f"classes_{report}")
        items = await page.evaluate(
            """() => {
                const out = [...document.querySelectorAll('a[href*="group="]')]
                    .filter(a => a.href.includes('report='))
                    .map(a => [(a.title || a.innerText).trim(), new URL(a.href).searchParams.get('group')]);
                for (const s of document.querySelectorAll('select[name*="group" i], select[id*="group" i], select[id*="class" i]'))
                    for (const o of s.options) if (o.value && !/^0?$/.test(o.value)) out.push([o.text.trim(), o.value]);
                return out;
            }"""
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

    async def build_class_report(self, class_name: str, group_id: str, quarter: int,
                                 report: str = REPORT_KEY) -> ClassReport:
        """O'quvchilar x fanlar jadvali (Sinf rahbariga / O'zlashtirish - Sinflar)."""
        page = self.page
        await page.goto(await self._report_url(f"&group={group_id}", report=report), wait_until="networkidle")
        if report == "progress-classteacher":
            date = quarter_date(quarter)
            await page.evaluate("d => { const el = document.querySelector('#date'); if (el) { el.value = d;"
                                " el.dispatchEvent(new Event('change', {bubbles: true})); } }", date)
        else:
            await self._set_period(page, quarter)
        await self._dump(page, f"params_{report}")

        btn = page.locator("#build_report_button")
        await btn.wait_for(state="visible")
        await btn.click()
        try:
            await page.wait_for_selector("#reportPlaceHolder table#resultsTable, #reportPlaceHolder table#marks",
                                         timeout=180000)
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

    async def _set_period(self, page: Page, quarter: int) -> None:
        """Hisobot davrini tanlaydi: chorak havolasi / ro'yxati, yoki sana (bitta yoki oraliq)."""
        rx = rf"(^|\D){quarter}\s*[-‑]?\s*(chorak|чорак|четверт)|(chorak|четверть)\s*{quarter}(\D|$)"
        # 1) davr havolalari (ul.switch ichidagi "1 chorak" kabi)
        href = await page.evaluate(
            """rx => { const r = new RegExp(rx, 'i');
                const a = [...document.querySelectorAll('#report_wrapper a, form a')].find(a => r.test(a.innerText.trim()));
                return a ? a.href : null; }""", rx)
        if href and not href.endswith("#"):
            await page.goto(href, wait_until="networkidle")
        start, end = quarter_range(quarter)
        done = await page.evaluate(
            """([rx, d, d1, d2]) => {
                const r = new RegExp(rx, 'i'), log = [];
                const fire = el => { el.dispatchEvent(new Event('input', {bubbles: true}));
                                     el.dispatchEvent(new Event('change', {bubbles: true})); };
                for (const s of document.querySelectorAll('select')) {
                    const o = [...s.options].find(o => r.test(o.text.trim()));
                    if (o) { s.value = o.value; fire(s); log.push('select:' + (s.name || s.id) + '=' + o.text.trim()); }
                }
                const inputs = [...document.querySelectorAll('input[type=text], input[type=date], input:not([type])')]
                    .filter(i => /date|from|to|start|end|begin|sana/i.test((i.id || '') + ' ' + (i.name || '')));
                const isFrom = i => /from|start|begin|d1|_1|min/i.test((i.id || '') + ' ' + (i.name || ''));
                const isTo = i => /(^|[^a-z])to|end|d2|_2|max|till/i.test((i.id || '') + ' ' + (i.name || ''));
                for (const i of inputs) {
                    const v = isFrom(i) ? d1 : isTo(i) ? d2 : d;
                    if (i.type === 'date') { const [dd, mm, yy] = v.split('/'); i.value = `${yy}-${mm}-${dd}`; }
                    else i.value = v;
                    fire(i); log.push('input:' + (i.id || i.name) + '=' + i.value);
                }
                return log;
            }""", [rx, quarter_date(quarter), start, end])
        log.info("Hisobot davri (%s-chorak): havola=%s, %s", quarter, href, done)
        await page.wait_for_timeout(500)

    async def build_table_report(self, report: str, report_name: str, class_name: str, group_id: str,
                                 quarter: int) -> TableReport:
        """Istalgan hisobot (O'zlashtirish/Davomat -> Sinflar): sinf + chorak tanlab, jadvalni oladi."""
        page = self.page
        await page.goto(await self._report_url(f"&group={group_id}", report=report), wait_until="networkidle")
        await self._set_period(page, quarter)
        await self._dump(page, f"params_{report}")

        btn = page.locator("#build_report_button, input[type=submit][value*='qurish' i], "
                           "input[type=button][value*='qurish' i], button:has-text('qurish')").first
        if await btn.count():
            await btn.click()
        try:
            await page.wait_for_selector("#reportPlaceHolder table, #report_wrapper table.grid", timeout=180000)
        except PWTimeout:
            msg = await page.evaluate("() => (document.querySelector('#errors')?.innerText || "
                                      "document.querySelector('#reportPlaceHolder')?.innerText || '').trim()")
            await self._dump(page, f"failed_{report}")
            raise KundalikError("Hisobot tuzilmadi." + (f"\nSayt javobi: {msg[:300]}" if msg else ""))
        await page.wait_for_timeout(1500)
        await self._dump(page, f"result_{report}")

        data = await page.evaluate(
            """() => {
                const root = document.querySelector('#reportPlaceHolder') || document.querySelector('#report_wrapper');
                const clean = el => el.innerText.replace(/\\s+/g, ' ').trim();
                const tables = [];
                for (const t of root.querySelectorAll('table')) {
                    if (t.parentElement.closest('table')) continue;           // ichma-ich jadvallarni o'tkazamiz
                    const grid = [], spans = {};                               // rowspan/colspan ni yoyish
                    const trs = [...t.querySelectorAll(':scope > thead > tr, :scope > tbody > tr, :scope > tfoot > tr, :scope > tr')];
                    trs.forEach((tr, ri) => {
                        const row = []; let c = 0;
                        const take = () => { while (spans[ri + ',' + c] !== undefined) { row[c] = spans[ri + ',' + c]; c++; } };
                        for (const cell of tr.children) {
                            take();
                            const v = clean(cell), cs = +cell.getAttribute('colspan') || 1, rs = +cell.getAttribute('rowspan') || 1;
                            for (let k = 0; k < cs; k++) {
                                row[c + k] = k ? '' : v;
                                for (let j = 1; j < rs; j++) spans[(ri + j) + ',' + (c + k)] = '';
                            }
                            c += cs;
                        }
                        take();
                        grid.push([...row].map(x => x ?? ''));
                    });
                    const head = t.querySelectorAll(':scope > thead > tr').length;
                    if (grid.some(r => r.some(x => x))) tables.push({head, rows: grid});
                }
                const title = [...root.children].filter(e => e.tagName !== 'TABLE').map(clean).find(x => x) || '';
                return {tables, title};
            }"""
        )
        if not data["tables"]:
            raise KundalikError("Hisobotda jadval topilmadi.")
        m = re.search(r":\s*(.+)$", data["title"])
        period = m.group(1).strip() if m else f"{quarter} chorak"
        log.info("Hisobot %s: %s, %s, jadvallar=%d, qatorlar=%s", report, class_name, period, len(data["tables"]),
                 [len(x["rows"]) for x in data["tables"]])
        rep = TableReport(report_name, class_name, period, data["tables"])

        href = await page.evaluate("() => { const a = document.querySelector('#excelButton, a[href*=\"excel\" i], a[href*=\"xls\" i]');"
                                   " return a && a.offsetParent !== null ? a.href : null; }")
        if href and not href.endswith("#"):
            try:
                resp = await page.context.request.get(href, timeout=120000)
                body = await resp.body()
                if resp.ok and body[:2] == b"PK":
                    rep.site_excel = body
            except Exception as e:
                log.warning("Sayt Excel faylini olishda xato: %s", e)
        return rep

    async def _parse_marks(self, page: Page, class_name: str, quarter: int) -> ClassReport:
        data = await page.evaluate(
            """() => {
                const clean = el => el.innerText.replace(/\\s+/g, ' ').trim();
                const ph = document.querySelector('#reportPlaceHolder');
                const t = ph.querySelector('table#resultsTable') || ph.querySelector('table#marks');
                const head = [...t.querySelectorAll('thead tr:first-child > *')].map(clean);
                const all = [...t.querySelectorAll(':scope > tbody > tr, :scope > tfoot > tr')]
                    .map(tr => [...tr.children].map(clean));
                const body = all.filter(r => /^\\d+$/.test(r[0] || ''));
                const foot = all.filter(r => r[0] && !/^\\d+$/.test(r[0]));
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
        m = re.search(r"(\d)\s*-?\s*(chorak|чорак|четверт)", data["title"], re.I)
        period = f"{m.group(1)} chorak" if m else f"{quarter} chorak"
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

    async def open(self, storage_state: dict | None = None, profile_dir: str | Path | None = None) -> None:
        """profile_dir berilsa — doimiy brauzer profili (oddiy brauzerdek qurilmani eslab qoladi)."""
        self._pw = await async_playwright().start()
        opts = dict(locale="uz-UZ", accept_downloads=True, viewport={"width": 1600, "height": 1000})
        self._browser = None
        if profile_dir:
            try:
                Path(profile_dir).mkdir(parents=True, exist_ok=True)
                os.chmod(profile_dir, 0o700)
                self._ctx = await self._pw.chromium.launch_persistent_context(str(profile_dir), headless=HEADLESS, **opts)
                if storage_state and storage_state.get("cookies"):
                    await self._ctx.add_cookies(storage_state["cookies"])
            except Exception:
                log.exception("Doimiy profil ochilmadi, oddiy brauzer bilan davom etiladi")
                self._ctx = None
        if not getattr(self, "_ctx", None):
            self._browser = await self._pw.chromium.launch(headless=HEADLESS)
            self._ctx = await self._browser.new_context(storage_state=storage_state, **opts)
        self.page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        self.page.set_default_timeout(30000)

    async def profile_name(self) -> str:
        """Kirgan o'qituvchining F.I.Sh (sahifadagi foydalanuvchi ma'lumotidan)."""
        js = """() => { const u = (window.__M__INITIAL__STATE__ || {}).user || {};
                 return [u.lastName, u.firstName, u.middleName].filter(Boolean).join(' '); }"""
        try:
            name = await self.page.evaluate(js)
            if not name:
                await self.page.goto("https://emaktab.uz/teachers", wait_until="domcontentloaded")
                name = await self.page.evaluate(js)
            return name or ""
        except Exception:
            return ""

    async def storage_state(self) -> dict | None:
        try:
            return await self._ctx.storage_state()
        except Exception:
            return None

    async def resume(self) -> bool:
        """Saqlangan cookie bilan kirilganmi — tekshiradi."""
        try:
            await self.page.goto("https://emaktab.uz/teachers", wait_until="domcontentloaded")
        except Exception:
            return False
        if "login." in self.page.url:
            return False
        self.logged_in = True
        return True

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
