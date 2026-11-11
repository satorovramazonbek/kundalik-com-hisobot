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
RE_REPORTS = re.compile(r"hisobot|ҳисобот|отч[её]т", re.I)
RE_FINAL = re.compile(r"yakuniy|якуний|итогов|chorak|чорак|четверт", re.I)
RE_SHOW = re.compile(r"ko.?rsat|кўрсат|показат|сформир|shakllant|qidir|найти", re.I)
RE_EXCEL = re.compile(r"excel|xls|эксел", re.I)


class KundalikError(Exception):
    """Foydalanuvchiga ko'rsatiladigan xatolik."""


@dataclass
class Report:
    title: str
    rows: list[list[str]] = field(default_factory=list)  # 1-qator sarlavha
    excel_bytes: bytes | None = None  # sayt o'zi Excel bergan bo'lsa
    excel_name: str | None = None


class KundalikClient:
    def __init__(self, login: str, password: str):
        self.login = login
        self.password = password
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
    async def _do_login(self, page: Page) -> None:
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        await self._dump(page, "login_page")

        captcha = page.locator("input[name='Captcha.Input']")
        if await captcha.count() and await captcha.first.is_visible():
            raise KundalikError(
                "Saytda captcha chiqyapti (ko'p marta noto'g'ri urinish bo'lgan). "
                "Bir oz kuting yoki brauzerda bir marta qo'lda kiring, so'ng qayta urinib ko'ring."
            )

        await page.fill("input[name='login']", self.login)
        await page.fill("input[name='password']", self.password)
        await page.click("input[type='submit'], [data-test-id='login-button']")
        try:
            await page.wait_for_load_state("networkidle", timeout=30000)
        except PWTimeout:
            pass
        await self._dump(page, "after_login")

        if "login." in page.url and await page.locator("input[name='password']").count():
            err = page.locator(".login__body__error, .error, .validation-summary-errors")
            msg = (await err.first.inner_text()).strip() if await err.count() else ""
            raise KundalikError("Login yoki parol noto'g'ri." + (f"\nSayt javobi: {msg}" if msg else ""))

    async def _open_final_marks_report(self, page: Page) -> None:
        """'Hisobotlar' -> 'Yakuniy baholar' sahifasini topib ochadi."""
        custom = os.getenv("KUNDALIK_REPORT_URL")  # kerak bo'lsa .env da aniq URL berish mumkin
        if custom:
            await page.goto(custom, wait_until="networkidle")
            await self._dump(page, "report_custom")
            return

        if "schools." not in page.url:
            await page.goto(SCHOOLS_URL + "/", wait_until="networkidle")
        await self._dump(page, "schools_home")

        if not await self._click_text(page, RE_REPORTS, kinds=("a",)):
            raise KundalikError("Saytda 'Hisobotlar' bo'limi topilmadi. (debug papkasini tekshiring)")
        await page.wait_for_load_state("networkidle")
        await self._dump(page, "reports")

        if not await self._click_text(page, RE_FINAL, kinds=("a",)):
            raise KundalikError("'Yakuniy / choraklik baholar' hisoboti topilmadi.")
        await page.wait_for_load_state("networkidle")
        await self._dump(page, "final_marks")

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

    async def get_quarter_report(self, class_name: str, quarter: int) -> Report:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=HEADLESS)
            ctx = await browser.new_context(locale="uz-UZ", accept_downloads=True, viewport={"width": 1600, "height": 1000})
            page = await ctx.new_page()
            page.set_default_timeout(30000)
            try:
                await self._do_login(page)
                await self._open_final_marks_report(page)

                if not await self._select_option(page, class_name):
                    raise KundalikError(f"'{class_name}' sinfi ro'yxatda topilmadi. Sinf nomini saytdagidek yozing (masalan: 5-A).")
                quarter_names = [f"{quarter}-chorak", f"{quarter} chorak", f"{quarter}-четверть", f"{quarter} четверть", f"{quarter}-чорак"]
                for qn in quarter_names:
                    if await self._select_option(page, qn):
                        break
                else:
                    raise KundalikError(f"{quarter}-chorak tanlovi topilmadi.")
                await self._dump(page, "selected")

                await self._click_text(page, RE_SHOW)
                try:
                    await page.wait_for_load_state("networkidle", timeout=30000)
                except PWTimeout:
                    pass
                await self._dump(page, "result")

                title = f"{class_name} sinf — {quarter}-chorak"

                # 1) Sayt o'zi Excel bera olsa — shuni yuklab olamiz
                excel_btn = page.locator("a, button").filter(has_text=RE_EXCEL)
                if await excel_btn.count():
                    try:
                        async with page.expect_download(timeout=30000) as dl_info:
                            await excel_btn.first.click()
                        dl = await dl_info.value
                        path = await dl.path()
                        if path:
                            data = Path(path).read_bytes()
                            return Report(title=title, excel_bytes=data, excel_name=dl.suggested_filename)
                    except PWTimeout:
                        log.info("Excel tugmasi yuklab bermadi, jadvalni o'zimiz o'qiymiz")

                # 2) Aks holda jadvalni o'qiymiz
                rows = await self._extract_table(page)
                if len(rows) < 2:
                    raise KundalikError("Hisobot jadvali bo'sh yoki topilmadi.")
                return Report(title=title, rows=rows)
            finally:
                await ctx.close()
                await browser.close()


async def _cli() -> None:
    """Terminaldan sinash: python scraper.py LOGIN PAROL 5-A 2"""
    import sys
    from excel import build_excel

    login, pwd, cls, q = sys.argv[1:5]
    rep = await KundalikClient(login, pwd).get_quarter_report(cls, int(q))
    name, data = build_excel(rep)
    Path(name).write_bytes(data)
    print("Saqlandi:", name)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_cli())
