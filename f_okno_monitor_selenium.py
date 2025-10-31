import os
import time
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import List, Dict

from dotenv import load_dotenv
from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

from telegram import Bot

# .env полезен для локального запуска; на GitHub Actions переменные идут из secrets
load_dotenv()

# ==== Конфиг из окружения ====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
EMAIL = os.getenv("F_OKNO_EMAIL")
PASSWORD = os.getenv("F_OKNO_PASSWORD")

LOGIN_URL = os.getenv(
    "LOGIN_URL",
    "https://f-okno.ru/login?request_uri=%2Fbase%2Fmoscovskaya_oblast%2Fsizo11noginsk",
)
TARGET_URL = os.getenv(
    "TARGET_URL",
    "https://f-okno.ru/base/moscovskaya_oblast/sizo11noginsk",
)
STATE_FILE = os.getenv("STATE_FILE", "state_sizo11.json")

# периодичность в Actions задаёт cron, но оставим дефолт для локального цикла
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_MIN", "3")) * 60

# ==== Логирование ====
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("f-okno-selenium")
# файл-лог для артефактов в Actions
try:
    fh = logging.FileHandler("run.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(fh)
except Exception:
    pass

bot = Bot(token=TELEGRAM_TOKEN)


from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager  # как и было


from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
import os

def make_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1280,2000")

    # Экшен setup-chrome дал путь к бинарнику Chromium
    chrome_path = os.getenv("CHROME_PATH") or os.getenv("CHROME_BIN")
    if chrome_path:
        opts.binary_location = chrome_path

    # КЛЮЧЕВОЕ: не указывать executable_path — Selenium Manager сам подберёт chromedriver
    service = Service()

    driver = webdriver.Chrome(service=service, options=opts)
    driver.set_page_load_timeout(60)
    return driver

def login(driver: webdriver.Chrome):
    log.info("Открываю страницу логина…")
    driver.get(LOGIN_URL)
    wait = WebDriverWait(driver, 20)

    # Правильные селекторы для f-okno (заменили email->login, password->pass)
    email_candidates = [
        (By.NAME, "login"),
        (By.CSS_SELECTOR, "input[name='login']"),
    ]
    pwd_candidates = [
        (By.NAME, "pass"),
        (By.CSS_SELECTOR, "input[name='pass']"),
    ]
    submit_candidates = [
        (By.CSS_SELECTOR, "#login_form .pre_button"),
        (By.XPATH, "//a[contains(.,'Авторизоваться')]"),
    ]

    def find_first(cands):
        for how, what in cands:
            try:
                return wait.until(EC.presence_of_element_located((how, what)))
            except Exception:
                continue
        raise RuntimeError(f"Элемент не найден. Проверь селекторы: {cands}")

    # Находим поля и вводим учётки
    email_input = find_first(email_candidates)
    pwd_input = find_first(pwd_candidates)
    email_input.clear(); email_input.send_keys(EMAIL)
    pwd_input.clear();   pwd_input.send_keys(PASSWORD)

    # Нажимаем кнопку авторизации (это <a ... onclick="doForm('login_form')">)
    find_first(submit_candidates).click()

    # Дадим время на редирект/подгрузку, затем явно перейдём на целевую страницу
    time.sleep(2)
    driver.get(TARGET_URL)
    wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    log.info("Логин завершён, целевая страница открыта.")

def parse_slots_from_html(html: str) -> List[Dict]:
    soup = BeautifulSoup(html, "lxml")
    out: List[Dict] = []

    items = soup.select("#graphic_container .graphic_item")
    if not items:
        # fallback: хотя бы понять — есть ли где-то «Есть места»
        text = soup.get_text(" ", strip=True)
        status = "Свободно" if ("Есть места" in text or "Записаться" in text) else "Нет мест"
        return [{"date": "", "time": "", "status": status}]

    for item in items:
        date_el = item.select_one(".graphic_item_date")
        date_txt = date_el.get_text(" ", strip=True) if date_el else ""

        classes = set(item.get("class", []))
        slots_el = item.select_one(".graphic_item_slots")
        slots_txt = slots_el.get_text(" ", strip=True) if slots_el else ""

        # «Свободно», если явно написано «Есть места», или день зелёный
        is_free = ("Есть места" in slots_txt) or any(c in {"green", "available", "free"} for c in classes)

        out.append({"date": date_txt, "time": "", "status": "Свободно" if is_free else "Нет мест"})

    return out


def load_last_snapshot() -> str:
    if not os.path.exists(STATE_FILE):
        return ""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def save_snapshot(s: str):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(s)
    except Exception as e:
        log.warning("Не удалось сохранить снапшот: %s", e)


def format_slots(slots: List[Dict], only_available: bool = True) -> str:
    if not slots:
        return "Свободных дат нет."

    # берём только свободные, если включено
    filtered = [s for s in slots if (s.get("status") == "Свободно")] if only_available else slots
    if not filtered:
        return "Свободных дат нет."

    lines = []
    for s in filtered:
        d = (s.get("date") or "").strip()
        # ✅ и жирным — заметный алерт
        lines.append(f"✅ <b>{d}</b>")
    return "\n".join(lines)


import asyncio  # убедись, что импорт есть вверху

def send_tg(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_* не настроены")
        return

    async def _go():
        chat = TELEGRAM_CHAT_ID.strip()
        chat_id = chat if chat.startswith("@") else int(chat)
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")

    asyncio.run(_go())


def one_check_run():
    """Один прогон. Шлём уведомление только если появились свободные даты
    (или если отключён фильтр ONLY_NOTIFY_WHEN_FREE)."""
    driver = make_driver()
    try:
        login(driver)
        time.sleep(2)

        html = driver.page_source
        with open("page.html", "w", encoding="utf-8") as f:
            f.write(html)

        slots = parse_slots_from_html(html)
        has_free = any(s.get("status") == "Свободно" for s in slots)
        # Печатаем понятный итог проверки в логи GitHub Actions и в run.log
        free_dates = [(s.get("date") or "").strip() for s in slots if s.get("status") == "Свободно"]
        if free_dates:
            log.info("===> Найдены свободные слоты: %d шт.", len(free_dates))
            for d in free_dates:
                log.info("FREE_DATE: %s", d)
        else:
            log.info("===> Свободных слотов нет.")

        snapshot = json.dumps(slots, ensure_ascii=False, sort_keys=True)
        last = load_last_snapshot()

        # включено по умолчанию: слать ТОЛЬКО при наличии свободных дат
        ONLY_NOTIFY_WHEN_FREE = os.getenv("ONLY_NOTIFY_WHEN_FREE", "1") == "1"

        if snapshot != last:
            if has_free or not ONLY_NOTIFY_WHEN_FREE:
                ts = datetime.now(ZoneInfo("Europe/Moscow")).strftime('%Y-%m-%d %H:%M')
                text = (
                    f"🚨 Появились свободные слоты в СИЗО-11! "
                    f"[{ts}]\n\n"
                    f"{format_slots(slots, only_available=True)}\n\n"
                    f"Записаться тут: <a href='{TARGET_URL}'>страница записи</a>"
                )
                send_tg(text)
            # Сохраняем снимок ВСЕГДА, если он изменился — чтобы не слать дубликаты
            save_snapshot(snapshot)
        else:
            log.info("Без изменений.")

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


if __name__ == "__main__":
    # В Actions нужен один прогон
    one_check_run()
    # Для локального кручения по кругу можно заменить на цикл:
    # while True: one_check_run(); time.sleep(CHECK_INTERVAL)
