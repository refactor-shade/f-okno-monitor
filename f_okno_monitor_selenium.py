from __future__ import annotations

import os
import time
import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import List, Dict, Optional, Tuple
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait


# ---------- настройки / окружение ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

F_OKNO_EMAIL = os.getenv("F_OKNO_EMAIL", "").strip()
F_OKNO_PASSWORD = os.getenv("F_OKNO_PASSWORD", "").strip()

LOGIN_URL = os.getenv(
    "LOGIN_URL",
    "https://f-okno.ru/login?request_uri=%2Fbase%2Fmoscovskaya_oblast%2Fsizo11noginsk",
).strip()

TARGET_URL = os.getenv(
    "TARGET_URL",
    "https://f-okno.ru/base/moscovskaya_oblast/sizo11noginsk",
).strip()

SIZO_LABEL = os.getenv("SIZO_LABEL", "СИЗО").strip()

STATE_FILE = os.getenv("STATE_FILE", "state.json").strip()
ONLY_NOTIFY_WHEN_FREE = os.getenv("ONLY_NOTIFY_WHEN_FREE", "1") == "1"

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


# ---------- Telegram ----------
def send_tg(text: str) -> None:
    """Отправка сообщения в Telegram (HTML)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы — сообщение не отправлено.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code != 200:
            logging.warning("Telegram send failed: %s %s", r.status_code, r.text[:300])
    except Exception:
        logging.exception("Telegram send exception")


# ---------- state ----------
def load_last_snapshot() -> str:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""
    except Exception:
        logging.exception("load_last_snapshot failed")
        return ""


def save_snapshot(snapshot: str) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(snapshot)
    except Exception:
        logging.exception("save_snapshot failed")


def format_slots(slots: List[Dict], only_available: bool = True) -> str:
    if not slots:
        return "Свободных дат нет."

    filtered = [s for s in slots if s.get("status") == "Свободно"] if only_available else slots
    if not filtered:
        return "Свободных дат нет."

    lines = []
    for s in filtered:
        d = (s.get("date") or "").strip()
        if d:
            lines.append(f"✅ <b>{d}</b>")
    return "\n".join(lines) if lines else "Свободных дат нет."


# ---------- debug artifacts ----------
def dump_debug_artifacts(driver: webdriver.Chrome, reason: str = "debug") -> None:
    out = Path("debug")
    out.mkdir(parents=True, exist_ok=True)

    try:
        (out / f"{reason}.url.txt").write_text(driver.current_url or "", encoding="utf-8")
    except Exception:
        pass

    try:
        (out / f"{reason}.page.html").write_text(driver.page_source or "", encoding="utf-8")
    except Exception:
        pass

    try:
        driver.save_screenshot(str(out / f"{reason}.page.png"))
    except Exception:
        pass


# ---------- Selenium ----------
def make_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1280,2000")
    opts.add_argument("--lang=ru-RU")
    return webdriver.Chrome(service=Service(), options=opts)


def wait_dom_complete(driver: webdriver.Chrome, timeout: int = 30) -> None:
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )


def wait_for_any_marker(driver: webdriver.Chrome, timeout: int = 45) -> str:
    """
    Маркеры состояния страницы:
    - SLOTS_TEXT: есть слоты/нет мест/в целом “расписание”
    - LOGIN_FORM: форма логина
    - ANTIBOT: капча/ограничение
    """
    end = time.time() + timeout
    checks: List[Tuple[str, Tuple[str, str]]] = [
        ("SLOTS_TEXT", (By.XPATH,
            "//*[contains(., 'Есть места') "
            "or contains(., 'Свободных мест нет') "
            "or contains(., 'Свободных дат нет') "
            "or contains(., 'Нет мест') "
            "or contains(., 'Запись') or contains(., 'передач')]"
        )),
        ("LOGIN_FORM", (By.CSS_SELECTOR,
            "form[action*='login'], input[type='password'], input[name='email'], input[name='login']"
        )),
        ("ANTIBOT", (By.XPATH,
            "//*[contains(., 'Доступ ограничен') "
            "or contains(., 'капча') "
            "or contains(., 'Cloudflare') "
            "or contains(., 'Access denied') "
            "or contains(., 'robot')]"
        )),
    ]

    last_url = ""
    while time.time() < end:
        try:
            last_url = driver.current_url
        except Exception:
            pass

        for name, (by, sel) in checks:
            try:
                if driver.find_elements(by, sel):
                    return name
            except Exception:
                continue

        time.sleep(0.5)

    raise TimeoutException(f"Timeout waiting for markers. Last URL: {last_url}")


def _find_first(driver: webdriver.Chrome, css_list: List[str]) -> Optional[object]:
    for css in css_list:
        els = driver.find_elements(By.CSS_SELECTOR, css)
        if els:
            return els[0]
    return None


def perform_login(driver: webdriver.Chrome) -> None:
    """
    Реальный логин с F_OKNO_EMAIL / F_OKNO_PASSWORD.
    Если капча/антибот — это будет видно по debug артефактам.
    """
    if not LOGIN_URL:
        raise RuntimeError("LOGIN_URL не задан.")

    if not F_OKNO_EMAIL or not F_OKNO_PASSWORD:
        raise RuntimeError("Нужны Secrets: F_OKNO_EMAIL и F_OKNO_PASSWORD (иначе не залогиниться).")

    driver.get(LOGIN_URL)
    wait_dom_complete(driver, timeout=30)

    marker = wait_for_any_marker(driver, timeout=30)
    logging.info("Login page marker: %s | URL: %s", marker, driver.current_url)

    if marker == "ANTIBOT":
        dump_debug_artifacts(driver, reason="login_antibot")
        raise RuntimeError("На странице логина антибот/капча — Selenium в GitHub Actions не проходит.")

    email_el = _find_first(driver, [
        "input[type='email']",
        "input[name='email']",
        "input[name='login']",
        "input[name*='mail']",
        "input[autocomplete='username']",
    ])
    pass_el = _find_first(driver, [
        "input[type='password']",
        "input[name='password']",
        "input[autocomplete='current-password']",
    ])

    if not email_el or not pass_el:
        dump_debug_artifacts(driver, reason="login_fields_not_found")
        raise RuntimeError("Не нашёл поля email/пароль на странице логина (верстка поменялась?).")

    try:
        email_el.clear()
    except Exception:
        pass
    email_el.send_keys(F_OKNO_EMAIL)

    try:
        pass_el.clear()
    except Exception:
        pass
    pass_el.send_keys(F_OKNO_PASSWORD)

    submit = _find_first(driver, [
        "button[type='submit']",
        "input[type='submit']",
        "button[name='login']",
    ])

    if submit:
        submit.click()
    else:
        pass_el.send_keys(Keys.ENTER)

    try:
        WebDriverWait(driver, 25).until(lambda d: "/login" not in (d.current_url or ""))
    except Exception:
        dump_debug_artifacts(driver, reason="login_no_redirect")
        raise RuntimeError("После отправки формы не ушли со страницы логина (возможна капча/неверный пароль).")

    wait_dom_complete(driver, timeout=30)
    logging.info("Login OK (seems). URL now: %s", driver.current_url)


# ---------- парсинг HTML ----------
_MONTHS = "января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря"
_WEEKDAYS = "понедельник|вторник|среда|четверг|пятница|суббота|воскресенье"
_DATE_RE = re.compile(rf"\b(\d{{1,2}})\s+({_MONTHS})(?:\s+({_WEEKDAYS}))?\b", re.IGNORECASE)

# ВАЖНО: более “широкие” маркеры (учитываем NBSP и разные формулировки)
def norm_text(s: str) -> str:
    s = (s or "").replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _make_soup(html: str) -> BeautifulSoup:
    for parser in ("lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return BeautifulSoup(html, "html.parser")


def parse_slots_from_html(html: str) -> List[Dict]:
    """
    Возвращает список:
    [{"date": "23 декабря вторник", "status": "Свободно"|"Нет мест"}, ...]
    Устойчиво к NBSP/переносам и вложенным элементам.
    """
    soup = _make_soup(html)
    slots: List[Dict] = []

    free_re = re.compile(r"есть\s*мест", re.IGNORECASE)
    no_re = re.compile(r"(свободных\s*мест\s*нет|свободных\s*дат\s*нет|нет\s*мест)", re.IGNORECASE)

    def extract_date(text: str) -> str:
        t = norm_text(text)
        m = _DATE_RE.search(t)
        if m:
            return m.group(0).strip()
        m2 = re.search(rf"\b(\d{{1,2}})\s+({_MONTHS})\b", t, re.IGNORECASE)
        if m2:
            return m2.group(0).strip()
        return ""

    def climb_for_card_text(node) -> str:
        cur = node
        for _ in range(6):
            if not cur:
                break
            try:
                txt = norm_text(cur.get_text(" ", strip=True))
            except Exception:
                txt = ""
            if txt:
                if _DATE_RE.search(txt) or re.search(rf"\b(\d{{1,2}})\s+({_MONTHS})\b", txt, re.IGNORECASE):
                    return txt
            cur = getattr(cur, "parent", None)
        try:
            return norm_text(node.parent.get_text(" ", strip=True)) if node and node.parent else ""
        except Exception:
            return ""

    # 1) Свободные слоты: ищем все вхождения “Есть места”
    for s in soup.find_all(string=free_re):
        card_text = climb_for_card_text(getattr(s, "parent", None))
        d = extract_date(card_text)
        if d:
            slots.append({"date": d, "status": "Свободно"})
        else:
            slots.append({"date": "Есть места (даты не распознаны)", "status": "Свободно"})

    if slots:
        uniq = []
        seen = set()
        for it in slots:
            key = it["date"]
            if key in seen:
                continue
            seen.add(key)
            uniq.append(it)
        return uniq

    # 2) Если свободных не нашли — соберём “нет мест” (полезно для snapshot)
    for s in soup.find_all(string=no_re):
        card_text = climb_for_card_text(getattr(s, "parent", None))
        d = extract_date(card_text)
        if d:
            slots.append({"date": d, "status": "Нет мест"})

    return slots


# ---------- основной прогон ----------
def open_target_with_login_if_needed(driver: webdriver.Chrome) -> None:
    """
    Пытаемся открыть TARGET_URL.
    Если редирект на логин — логинимся и повторяем.
    """
    driver.get(TARGET_URL)
    wait_dom_complete(driver, timeout=30)
    marker = wait_for_any_marker(driver, timeout=45)
    logging.info("Marker: %s | URL: %s", marker, driver.current_url)

    if marker == "ANTIBOT":
        dump_debug_artifacts(driver, reason="target_antibot")
        raise RuntimeError("На TARGET_URL антибот/ограничение доступа.")

    if marker != "LOGIN_FORM":
        return

    logging.info("Redirected to login. Trying to login...")
    dump_debug_artifacts(driver, reason="redirected_to_login")

    perform_login(driver)

    driver.get(TARGET_URL)
    wait_dom_complete(driver, timeout=30)
    marker2 = wait_for_any_marker(driver, timeout=45)
    logging.info("Marker after login: %s | URL: %s", marker2, driver.current_url)

    if marker2 == "LOGIN_FORM":
        dump_debug_artifacts(driver, reason="still_on_login_after_login")
        raise RuntimeError("После логина всё равно остаёмся на логине (возможна капча/блок/не та учётка).")

    if marker2 == "ANTIBOT":
        dump_debug_artifacts(driver, reason="antibot_after_login")
        raise RuntimeError("После логина попали на антибот/ограничение доступа.")


def one_check_run() -> None:
    driver = make_driver()
    try:
        open_target_with_login_if_needed(driver)

        # дать JS дорисовать календарь
        time.sleep(1.0)

        html = driver.page_source
        Path("page.html").write_text(html, encoding="utf-8")
        try:
            driver.save_screenshot("page.png")
        except Exception:
            pass

        slots = parse_slots_from_html(html)
        has_free = any(s.get("status") == "Свободно" for s in slots)

        free_dates = [(s.get("date") or "").strip() for s in slots if s.get("status") == "Свободно"]
        if free_dates:
            logging.info("===> Найдены свободные слоты: %d шт.", len(free_dates))
            for d in free_dates:
                logging.info("FREE_DATE: %s", d)
        else:
            logging.info("===> Свободных слотов нет.")

        snapshot = json.dumps(slots, ensure_ascii=False, sort_keys=True)
        last = load_last_snapshot()

        if snapshot != last:
            if has_free or (not ONLY_NOTIFY_WHEN_FREE):
                ts = datetime.now(MOSCOW_TZ).strftime("%Y-%m-%d %H:%M")
                text = (
                    f"🚨 Появились свободные слоты в {SIZO_LABEL}! [{ts}]\n\n"
                    f"{format_slots(slots, only_available=True)}\n\n"
                    f"Записаться тут: <a href='{TARGET_URL}'>страница записи</a>"
                )
                send_tg(text)

            save_snapshot(snapshot)
            logging.info("Snapshot изменился — сохранено.")
        else:
            logging.info("Без изменений (snapshot не менялся).")

    except Exception:
        logging.exception("FATAL")
        try:
            dump_debug_artifacts(driver, reason="fatal")
        except Exception:
            pass
        raise
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    one_check_run()

