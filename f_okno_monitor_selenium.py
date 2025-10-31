# -*- coding: utf-8 -*-
"""
Монитор слотов Ф-ОКНО (СИЗО-11 Ногинск).

Особенности:
- Без webdriver_manager: подбор chromedriver делает Selenium Manager.
- Время в сообщениях — по Москве (Europe/Moscow).
- Сообщение шлётся только при появлении новых свободных дат
  (или при отключении фильтра ONLY_NOTIFY_WHEN_FREE=0).
- Снапшот в STATE_FILE предотвращает дубли.
- Артефакты page.html/page.png/run.log сохраняются для диагностики.
"""

from __future__ import annotations

import os
import json
import time
import logging
from typing import List, Dict
from datetime import datetime

from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC  # noqa: F401
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException

import requests


# -------------------------- Конфигурация из ENV ---------------------------

LOGIN_URL = os.getenv(
    "LOGIN_URL",
    "https://f-okno.ru/login?request_uri=%2Fbase%2Fmoscovskaya_oblast%2Fsizo11noginsk",
)

TARGET_URL = os.getenv(
    "TARGET_URL",
    "https://f-okno.ru/base/moscovskaya_oblast/sizo11noginsk",
)

STATE_FILE = os.getenv("STATE_FILE", "state_sizo11.json")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")  # id чата/канала/пользователя

# По умолчанию шлём только если есть свободные даты
ONLY_NOTIFY_WHEN_FREE = os.getenv("ONLY_NOTIFY_WHEN_FREE", "1") == "1"


# Лог в файл — GitHub Actions поднимет артефактом
logging.basicConfig(
    filename="run.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


# --------------------------- Утилиты и I/O --------------------------------

def load_last_snapshot() -> str:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def save_snapshot(s: str) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(s)


def send_tg(text: str, parse_mode: str = "HTML") -> None:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        log.info("TELEGRAM creds not set — skipping send.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, data=data, timeout=20)
        if r.status_code != 200:
            log.warning("Telegram send failed: %s %s", r.status_code, r.text)
    except Exception:
        log.exception("Telegram send fatal")


# ------------------------------ WebDriver ---------------------------------

def make_driver() -> webdriver.Chrome:
    """
    Создаёт Chrome, полагаясь на Selenium Manager для подбора chromedriver.
    Если в ENV есть CHROME_PATH (прокинутый из workflow), используем его.
    """
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1280,2200")

    chrome_path = os.getenv("CHROME_PATH") or os.getenv("CHROME_BIN")
    if chrome_path:
        opts.binary_location = chrome_path

    # Service() без executable_path → Selenium Manager сам подберёт драйвер
    service = Service()
    drv = webdriver.Chrome(service=service, options=opts)
    drv.set_page_load_timeout(60)
    return drv


def safe_get(driver: webdriver.Chrome, url: str, wait_css: str | None = None, timeout: int = 30) -> None:
    driver.get(url)
    if wait_css:
        try:
            WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, wait_css))
            )
        except TimeoutException:
            log.warning("Timeout waiting for %s on %s", wait_css, url)


def login(driver: webdriver.Chrome) -> None:
    """
    Лёгкий «логин»: открываем LOGIN_URL (чтобы получить правильные куки),
    затем целевую страницу TARGET_URL.
    Если есть реальный логин/пароль — можно дописать ввод в поля.
    """
    safe_get(driver, LOGIN_URL)
    time.sleep(1.5)
    safe_get(driver, TARGET_URL)
    time.sleep(1.0)


# ------------------------------ Парсинг -----------------------------------

def parse_slots_from_html(html: str) -> List[Dict]:
    """
    Возвращает список dict: {"date": "...", "status": "Свободно"/"..."}.
    Парсинг терпим к вёрстке: ищем карточки дней и текстовые индикаторы.
    """
    soup = BeautifulSoup(html, "lxml")
    slots: List[Dict] = []

    # 1) Частый вариант — «плитки» дней/календарь
    day_nodes = soup.select(".day, .calendar-day, .slot, .slots-list .slot, .calendar .day")
    if day_nodes:
        for n in day_nodes:
            text = n.get_text(" ", strip=True)
            if not text:
                continue
            status = "Свободно" if ("Есть места" in text or "Записаться" in text or "Свободн" in text) else ""
            date_part = text
            for marker in ["Есть места", "Свободных мест нет", "мест нет", "Записаться"]:
                date_part = date_part.replace(marker, "").strip()
            if status:
                slots.append({"date": date_part, "status": status})
            else:
                slots.append({"date": date_part, "status": "Нет мест"})
        return slots

    # 2) Фоллбек — просто текстовая проверка всей страницы
    text = soup.get_text("\n", strip=True)
    if "Есть места" in text or "Записаться" in text:
        slots.append({"date": "", "status": "Свободно"})
    else:
        slots.append({"date": "", "status": "Нет мест"})

    return slots


# --------------------------- Форматирование -------------------------------

def format_slots(slots: List[Dict], only_available: bool = True) -> str:
    """
    Возвращает список строк вида:
      ✅ <b>16 октября четверг</b>
    Если свободных нет — «Свободных дат нет.»
    """
    if not slots:
        return "Свободных дат нет."

    filtered = [s for s in slots if s.get("status") == "Свободно"] if only_available else slots
    if not filtered:
        return "Свободных дат нет."

    lines = []
    for s in filtered:
        d = (s.get("date") or "").strip() or "Свободно"
        lines.append(f"✅ <b>{d}</b>")
    return "\n".join(lines)


# ------------------------------ Основной прогон ---------------------------

def one_check_run() -> None:
    """
    Один прогон мониторинга:
      - грузим страницу
      - парсим свободные даты
      - сравниваем со снапшотом
      - шлём Telegram (фильтр по ONLY_NOTIFY_WHEN_FREE)
    """
    ts_msk = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d %H:%M")

    driver = make_driver()
    try:
        login(driver)

        html = driver.page_source
        with open("page.html", "w", encoding="utf-8") as f:
            f.write(html)

        slots = parse_slots_from_html(html)
        has_free = any(s.get("status") == "Свободно" for s in slots)

        # лог: что нашли
        free_dates = [(s.get("date") or "").strip() for s in slots if s.get("status") == "Свободно"]
        if free_dates:
            log.info("===> Найдены свободные слоты: %d", len(free_dates))
            for d in free_dates:
                log.info("FREE_DATE: %s", d)
        else:
            log.info("===> Свободных слотов нет.")

        snapshot = json.dumps(slots, ensure_ascii=False, sort_keys=True)
        last = load_last_snapshot()

        if snapshot != last:
            if has_free or not ONLY_NOTIFY_WHEN_FREE:
                text = (
                    f"🚨 Появились свободные слоты в СИЗО-11! "
                    f"[{ts_msk}]\n\n"
                    f"{format_slots(slots, only_available=True)}\n\n"
                    f"Записаться тут: <a href='{TARGET_URL}'>страница записи</a>"
                )
                send_tg(text)
            # снапшот сохраняем всегда при изменении
            save_snapshot(snapshot)
        else:
            log.info("Без изменений (snapshot match).")

    except Exception:
        log.exception("FATAL")
        try:
            driver.save_screenshot("page.png")
            with open("page.html", "w", encoding="utf-8") as f:
                f.write(driver.page_source)
        except Exception:
            pass
        raise
    finally:
        driver.quit()


# --------------------------------- main -----------------------------------

if __name__ == "__main__":
    log.info("=== RUN START (MSK %s) ===", datetime.now(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d %H:%M:%S"))
    fail_on_error = os.getenv("FAIL_ON_ERROR", "1") == "1"
    try:
        one_check_run()
    except Exception:
        if fail_on_error:
            raise
        else:
            log.exception("Suppressed failure (FAIL_ON_ERROR=0)")
    finally:
        log.info("=== RUN END ===")
