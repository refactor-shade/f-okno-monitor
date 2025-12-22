import os
import time
import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import List, Dict

import requests
from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


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

# Чтобы текст уведомления был правильный при смене СИЗО
SIZO_LABEL = os.getenv("SIZO_LABEL", "СИЗО-11").strip()

STATE_FILE = os.getenv("STATE_FILE", "state.json").strip()
ONLY_NOTIFY_WHEN_FREE = os.getenv("ONLY_NOTIFY_WHEN_FREE", "1") == "1"

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


# ---------- утилиты ----------
def send_tg(text: str) -> None:
    """Отправка сообщения в Telegram (HTML)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("TELEGRAM_* не заданы — сообщение не отправлено.")
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


# ---------- Selenium ----------
def make_driver() -> webdriver.Chrome:
    """Запускаем Chrome; Selenium Manager сам подберёт chromedriver."""
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1280,2000")
    return webdriver.Chrome(service=Service(), options=opts)


def login(driver: webdriver.Chrome) -> None:
    """
    Открываем LOGIN_URL (если задан).
    ВАЖНО: этот проект может работать и без реального ввода логина/пароля,
    но некоторые страницы могут редиректить — поэтому после login() мы ВСЕГДА
    переходим на TARGET_URL в one_check_run().
    """
    if not LOGIN_URL:
        return

    driver.get(LOGIN_URL)
    try:
        WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.CSS_SELECTOR, "body")))
    except Exception:
        logging.warning("Login page wait timeout")


# ---------- парсинг HTML ----------
_MONTHS = "января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря"
_WEEKDAYS = "понедельник|вторник|среда|четверг|пятница|суббота|воскресенье"
_DATE_RE = re.compile(rf"\b(\d{{1,2}})\s+({_MONTHS})(?:\s+({_WEEKDAYS}))?\b", re.IGNORECASE)

_FREE_MARKERS = ("Есть места", "Доступно", "Свобод")
_NO_MARKERS = ("Свободных мест нет", "Свободных дат нет", "Нет мест")


def parse_slots_from_html(html: str) -> List[Dict]:
    """
    Возвращает список:
    [{"date": "23 декабря вторник", "status": "Свободно"|"Нет мест"}, ...]
    """
    soup = BeautifulSoup(html, "lxml")
    slots: List[Dict] = []

    # Самые частые “карточки” календаря на f-okno
    candidate_nodes = soup.select(
        ".talon, .talon_item, .ticket, .ticket-item, .calendar-item, "
        ".calendar .day, .calendar .item, .day-item, .day"
    )

    def status_from_text(t: str) -> str | None:
        if any(x in t for x in _FREE_MARKERS):
            return "Свободно"
        if any(x in t for x in _NO_MARKERS):
            return "Нет мест"
        # карточка может быть “пустая/серая” — не считаем её вообще
        return None

    def date_from_text(t: str) -> str:
        m = _DATE_RE.search(t)
        if m:
            return m.group(0).strip()

        # fallback: уберём мусор и вернём хоть что-то осмысленное
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
            return slots

    # 2) Fallback по всему тексту страницы
    full_text = soup.get_text("\n", strip=True)
    lines = [ln.strip() for ln in full_text.splitlines() if ln.strip()]

    free_lines = [ln for ln in lines if any(x in ln for x in _FREE_MARKERS)]
    if free_lines:
        # Попробуем вытащить даты из строк где “Есть места”
        found_dates = []
        for ln in free_lines:
            m = _DATE_RE.search(ln)
            if m:
                found_dates.append(m.group(0).strip())

        if found_dates:
            # уникализируем, сохраняя порядок
            uniq = []
            for d in found_dates:
                if d not in uniq:
                    uniq.append(d)
            return [{"date": d, "status": "Свободно"} for d in uniq]

        # если даты не извлеклись — хотя бы скажем “есть места”
        return [{"date": "Есть места (даты не распознаны)", "status": "Свободно"}]

    return []


# ---------- основной прогон ----------
def one_check_run() -> None:
    driver = make_driver()
    try:
        login(driver)

        # КЛЮЧЕВОЕ: всегда переходим на TARGET_URL перед парсингом
        driver.get(TARGET_URL)

        # Ждём, пока страница действительно прогрузится
        # (на некоторых СИЗО календарь подтягивается JS-ом)
        WebDriverWait(driver, 25).until(
            lambda d: ("Запись на передачу" in d.page_source)
                      or ("Есть места" in d.page_source)
                      or ("Свободных мест нет" in d.page_source)
                      or ("Свободных дат нет" in d.page_source)
        )

        # Небольшая страховка: дать дорисоваться плиткам
        time.sleep(1)

        html = driver.page_source
        with open("page.html", "w", encoding="utf-8") as f:
            f.write(html)

        slots = parse_slots_from_html(html)
        has_free = any(s.get("status") == "Свободно" for s in slots)

        # для логов покажем, что нашли
        free_dates = [(s.get("date") or "").strip() for s in slots if s.get("status") == "Свободно"]
        if free_dates:
            logging.info("URL: %s", driver.current_url)
            logging.info("===> Найдены свободные слоты: %d шт.", len(free_dates))
            for d in free_dates:
                logging.info("FREE_DATE: %s", d)
        else:
            logging.info("URL: %s", driver.current_url)
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
        else:
            logging.info("Без изменений (snapshot не менялся).")

    except Exception:
        logging.exception("FATAL")
        try:
            driver.save_screenshot("page.png")
            with open("page.html", "w", encoding="utf-8") as f:
                f.write(driver.page_source)
        except Exception:
            pass
        raise
    finally:
        driver.quit()


if __name__ == "__main__":
    one_check_run()
