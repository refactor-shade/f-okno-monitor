import os
import time
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import List, Dict

import requests
from bs4 import BeautifulSoup

# Selenium 4+ с Selenium Manager (без ручного chromedriver)
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
)
TARGET_URL = os.getenv(
    "TARGET_URL",
    "https://f-okno.ru/base/moscovskaya_oblast/sizo11noginsk",
)
STATE_FILE = os.getenv("STATE_FILE", "state_sizo11.json")
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
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code != 200:
            logging.warning("Telegram send failed: %s %s", r.status_code, r.text[:200])
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
        # галочка и жирный
        lines.append(f"✅ <b>{d}</b>")
    return "\n".join(lines) if lines else "Свободных дат нет."


# ---------- Selenium ----------
def make_driver() -> webdriver.Chrome:
    """Запускаем Chrome; Selenium Manager сам подберёт chromedriver."""
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    # НИЧЕГО не передаём про путь к chromedriver
    return webdriver.Chrome(service=Service(), options=opts)


def login(driver: webdriver.Chrome) -> None:
    """Открываем страницу логина/целевую, ждём загрузку основной формы."""
    driver.get(LOGIN_URL)
    # Дадим странице стабильно прогрузиться
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "form"))
        )
    except Exception:
        # даже если формы нет, сохраним HTML для дебага
        logging.warning("Login page wait timeout")


# ---------- парсинг HTML ----------
def parse_slots_from_html(html: str) -> List[Dict]:
    import re
from typing import List, Dict
from bs4 import BeautifulSoup

_MONTHS = (
    "января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря"
)
_WEEKDAYS = "понедельник|вторник|среда|четверг|пятница|суббота|воскресенье"
_DATE_RE = re.compile(rf"\b(\d{{1,2}})\s+({_MONTHS})(?:\s+({_WEEKDAYS}))?\b", re.IGNORECASE)

def parse_slots_from_html(html: str) -> List[Dict]:
    """
    Возвращает список:
    [{"date": "23 декабря вторник", "status": "Свободно"|"Нет мест"}, ...]
    """
    soup = BeautifulSoup(html, "lxml")
    slots: List[Dict] = []

    # Карточки календаря на f-okno чаще всего похожи на .talon / .ticket / .day / .calendar-item
    candidate_nodes = soup.select(
        ".talon, .talon_item, .ticket, .ticket-item, .calendar-item, "
        ".calendar .day, .calendar .item, .day-item, .day"
    )

    def _status_from_text(t: str) -> str:
        return "Свободно" if any(x in t for x in ("Есть места", "Доступно", "Свобод")) else "Нет мест"

    def _date_from_text(t: str) -> str:
        # Пытаемся вытащить "23 декабря вторник"
        m = _DATE_RE.search(t)
        if m:
            return m.group(0).strip()

        # Если regex не сработал — попробуем взять первую “разумную” часть
        # (часто это "23 декабря вторник" или "23 декабря")
        t = re.sub(r"\s+", " ", t).strip()
        for junk in ("Есть места", "Свободных мест нет", "Свободных дат нет", "Нет мест"):
            t = t.replace(junk, "").strip()
        return t

    if candidate_nodes:
        for node in candidate_nodes:
            t = node.get_text(" ", strip=True)
            if not t:
                continue

            status = _status_from_text(t)
            date = _date_from_text(t)

            # Фильтр: чтобы не тащить пустые/мусорные строки
            if date and len(date) >= 3:
                slots.append({"date": date, "status": status})

        # Иногда на странице есть служебные блоки — если совсем ничего не распознали, fallback ниже
        if slots:
            return slots

    # --- fallback: если карточки не нашлись или не распарсились ---
    full_text = soup.get_text(" ", strip=True)
    if any(x in full_text for x in ("Есть места", "Доступно", "Свобод")):
        return [{"date": "Есть места (даты не распознаны)", "status": "Свободно"}]

    return []

# ---------- основной прогон ----------
def one_check_run() -> None:
    driver = make_driver()
    try:
        login(driver)
        time.sleep(2)

        html = driver.page_source
        with open("page.html", "w", encoding="utf-8") as f:
            f.write(html)

        slots = parse_slots_from_html(html)
        has_free = any(s.get("status") == "Свободно" for s in slots)

        # для логов покажем, что нашли
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
                    f"🚨 Появились свободные слоты в СИЗО-11! [{ts}]\n\n"
                    f"{format_slots(slots, only_available=True)}\n\n"
                    f"Записаться тут: <a href='{TARGET_URL}'>страница записи</a>"
                )
                send_tg(text)

            save_snapshot(snapshot)  # снимок сохраняем всегда, если изменился
        else:
            logging.info("Без изменений (snapshot не менялся).")

    except Exception:
        logging.exception("FATAL")
        # Сохраним скрин и html на случай разборов в артефактах
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
