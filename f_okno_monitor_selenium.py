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
from selenium.webdriver.support.ui import WebDriverWait


# ---------- настройки / окружение ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

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
    """Форматируем даты списком (только свободные — по умолчанию)."""
    if not slots:
        return "Свободных дат нет."

    filtered = [s for s in slots if s.get("status") == "Свободно"] if only_available else slots
    if not filtered:
        return "Свободных дат нет."

    lines = []
    for s in filtered:
        d = (s.get("date") or "").strip()
        if not d:
            continue
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
    """Запускаем Chrome; Selenium Manager сам подберёт chromedriver."""
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1280,2000")

    # Иногда помогает против странных редиректов/локалей
    opts.add_argument("--lang=ru-RU")
    # opts.add_argument("--disable-blink-features=AutomationControlled")  # спорно, можно включать/выключать

    return webdriver.Chrome(service=Service(), options=opts)


def wait_dom_complete(driver: webdriver.Chrome, timeout: int = 30) -> None:
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )


def login(driver: webdriver.Chrome) -> None:
    """
    Открываем LOGIN_URL (если задан).
    Этот проект может работать и без реального ввода логина/пароля,
    но некоторые страницы могут редиректить — поэтому после login()
    мы ВСЕГДА переходим на TARGET_URL в one_check_run().
    """
    if not LOGIN_URL:
        return

    driver.get(LOGIN_URL)
    try:
        wait_dom_complete(driver, timeout=20)
    except Exception:
        logging.warning("Login page wait timeout")


def wait_for_any_marker(driver: webdriver.Chrome, timeout: int = 45) -> str:
    """
    Ждём не один селектор, а любой признак того, что мы на одной из ожидаемых страниц:
    - страница слотов (есть места / нет мест)
    - форма логина
    - антибот/ограничение доступа
    - (fallback) основной контейнер контента
    """
    end = time.time() + timeout

    checks: List[Tuple[str, Tuple[str, str]]] = [
        ("SLOTS_TEXT", (By.XPATH, "//*[contains(., 'Есть места') or contains(., 'Свобод') or contains(., 'Нет мест') or contains(., 'Свободных дат нет') or contains(., 'Свободных мест нет')]")),
        ("LOGIN_FORM", (By.CSS_SELECTOR, "form[action*='login'], input[type='password'], input[name='email'], input[name='login']")),
        ("ANTIBOT", (By.XPATH, "//*[contains(., 'Доступ ограничен') or contains(., 'robot') or contains(., 'капча') or contains(., 'Cloudflare') or contains(., 'Access denied')]")),
        ("CONTENT", (By.CSS_SELECTOR, "main, .container, .content, body")),
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

    raise TimeoutException(f"Timeout waiting for page markers. Last URL: {last_url}")


# ---------- парсинг HTML ----------
_MONTHS = "января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря"
_WEEKDAYS = "понедельник|вторник|среда|четверг|пятница|суббота|воскресенье"
_DATE_RE = re.compile(rf"\b(\d{{1,2}})\s+({_MONTHS})(?:\s+({_WEEKDAYS}))?\b", re.IGNORECASE)

_FREE_MARKERS = ("Есть места", "Доступно", "Свобод")
_NO_MARKERS = ("Свободных мест нет", "Свободных дат нет", "Нет мест")


def _make_soup(html: str) -> BeautifulSoup:
    # если lxml не установлен — не падаем
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
    """
    soup = _make_soup(html)
    slots: List[Dict] = []

    candidate_nodes = soup.select(
        ".talon, .talon_item, .ticket, .ticket-item, .calendar-item, "
        ".calendar .day, .calendar .item, .day-item, .day, "
        "[class*='talon'], [class*='ticket'], [class*='calendar']"
    )

    def status_from_text(t: str) -> Optional[str]:
        if any(x.lower() in t.lower() for x in _FREE_MARKERS):
            return "Свободно"
        if any(x.lower() in t.lower() for x in _NO_MARKERS):
            return "Нет мест"
        return None

    def date_from_text(t: str) -> str:
        m = _DATE_RE.search(t)
        if m:
            return m.group(0).strip()

        t2 = re.sub(r"\s+", " ", t).strip()
        for junk in (*_FREE_MARKERS, *_NO_MARKERS):
            t2 = t2.replace(junk, "").strip()
        return t2

    # 1) Пытаемся вытащить из карточек
    if candidate_nodes:
        for node in candidate_nodes:
            t = node.get_text(" ", strip=True)
            if not t:
                continue

            st = status_from_text(t)
            if st is None:
                continue

            d = date_from_text(t)
            if d and len(d) >= 3:
                slots.append({"date": d, "status": st})

        if slots:
            # уникализация (иногда карточки дублируются)
            uniq = []
            seen = set()
            for s in slots:
                key = (s.get("date"), s.get("status"))
                if key in seen:
                    continue
                seen.add(key)
                uniq.append(s)
            return uniq

    # 2) Fallback по всему тексту страницы
    full_text = soup.get_text("\n", strip=True)
    lines = [ln.strip() for ln in full_text.splitlines() if ln.strip()]

    free_lines = [ln for ln in lines if any(x.lower() in ln.lower() for x in _FREE_MARKERS)]
    if free_lines:
        found_dates = []
        for ln in free_lines:
            m = _DATE_RE.search(ln)
            if m:
                found_dates.append(m.group(0).strip())

        if found_dates:
            uniq = []
            for d in found_dates:
                if d not in uniq:
                    uniq.append(d)
            return [{"date": d, "status": "Свободно"} for d in uniq]

        return [{"date": "Есть места (даты не распознаны)", "status": "Свободно"}]

    return []


# ---------- основной прогон ----------
def one_check_run() -> None:
    driver = make_driver()
    try:
        login(driver)

        # Всегда идём на целевой URL
        driver.get(TARGET_URL)
        wait_dom_complete(driver, timeout=30)

        marker = wait_for_any_marker(driver, timeout=45)
        logging.info("Marker: %s | URL: %s", marker, driver.current_url)

        # Если мы внезапно на логине/антиботе — сохраняем артефакты и падаем осмысленно
        if marker == "LOGIN_FORM":
            dump_debug_artifacts(driver, reason="still_on_login")
            raise RuntimeError("Похоже, нас редиректнуло на логин (или требуется авторизация/капча).")

        if marker == "ANTIBOT":
            dump_debug_artifacts(driver, reason="antibot")
            raise RuntimeError("Похоже, сработала защита/ограничение доступа (антибот/капча/403).")

        # Дать JS дорисовать календарь (часто это реально нужно)
        time.sleep(1.0)

        html = driver.page_source
        Path("page.html").write_text(html, encoding="utf-8")
        try:
            driver.save_screenshot("page.png")
        except Exception:
            pass

        slots = parse_slots_from_html(html)
        has_free = any(s.get("status") == "Свободно" for s in slots)

        # логи
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
