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
TRY_REQUESTS_FIRST = os.getenv("TRY_REQUESTS_FIRST", "1") == "1"

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# ---------- regex/markers ----------
_MONTHS = "января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря"
_WEEKDAYS = "понедельник|вторник|среда|четверг|пятница|суббота|воскресенье"
_DATE_RE = re.compile(rf"\b(\d{{1,2}})\s+({_MONTHS})(?:\s+({_WEEKDAYS}))?\b", re.IGNORECASE)

FREE_RE = re.compile(r"есть\s*мест", re.IGNORECASE)
NO_RE = re.compile(r"(свободных\s*(мест|дат)\s*нет|нет\s*мест)", re.IGNORECASE)


# ---------- utils ----------
def norm_text(s: str) -> str:
    s = (s or "").replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def send_tg(text: str) -> None:
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


def _make_soup(html: str) -> BeautifulSoup:
    for parser in ("lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return BeautifulSoup(html, "html.parser")


def _extract_date_from_text(text: str) -> str:
    t = norm_text(text)
    m = _DATE_RE.search(t)
    if m:
        return m.group(0).strip()
    m2 = re.search(rf"\b(\d{{1,2}})\s+({_MONTHS})\b", t, re.IGNORECASE)
    if m2:
        return m2.group(0).strip()
    return ""


# ---------- parsing (HTML fallback) ----------
def parse_slots_from_html(html: str) -> List[Dict]:
    """
    Устойчиво к NBSP/переносам и вложенным элементам.
    """
    soup = _make_soup(html)
    slots: List[Dict] = []

    def climb_for_card_text(node) -> str:
        cur = node
        for _ in range(7):
            if not cur:
                break
            try:
                txt = norm_text(cur.get_text(" ", strip=True))
            except Exception:
                txt = ""
            if txt and (re.search(rf"\b(\d{{1,2}})\s+({_MONTHS})\b", txt, re.IGNORECASE) or _DATE_RE.search(txt)):
                return txt
            cur = getattr(cur, "parent", None)
        try:
            return norm_text(node.parent.get_text(" ", strip=True)) if node and node.parent else ""
        except Exception:
            return ""

    # 1) Свободные
    for s in soup.find_all(string=FREE_RE):
        card_text = climb_for_card_text(getattr(s, "parent", None))
        d = _extract_date_from_text(card_text)
        slots.append({"date": d or "Есть места (даты не распознаны)", "status": "Свободно"})

    if slots:
        uniq = []
        seen = set()
        for it in slots:
            if it["date"] in seen:
                continue
            seen.add(it["date"])
            uniq.append(it)
        return uniq

    # 2) Нет мест
    for s in soup.find_all(string=NO_RE):
        card_text = climb_for_card_text(getattr(s, "parent", None))
        d = _extract_date_from_text(card_text)
        if d:
            slots.append({"date": d, "status": "Нет мест"})

    return slots


# ---------- requests first (optional) ----------
def try_requests_fetch() -> Optional[List[Dict]]:
    """
    Иногда страница отдаёт готовый HTML и без Selenium.
    Если получилось распарсить — возвращаем список слотов.
    Если редирект/логин/пусто — вернём None и пойдём в Selenium.
    """
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        }
        r = requests.get(TARGET_URL, headers=headers, timeout=25, allow_redirects=True)
        html = r.text or ""
        # если нас перекинуло на логин — смысла парсить нет
        if "/login" in (r.url or ""):
            return None

        # быстрый “сигнал”, что на странице реально есть "Есть места"
        if not FREE_RE.search(norm_text(html)):
            return None

        slots = parse_slots_from_html(html)
        if slots:
            return slots
        return None
    except Exception:
        return None


# ---------- Selenium helpers ----------
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


def _has_login_form(driver: webdriver.Chrome) -> bool:
    sel = "form[action*='login'], input[type='password'], input[name='email'], input[name='login']"
    try:
        return len(driver.find_elements(By.CSS_SELECTOR, sel)) > 0
    except Exception:
        return False


def _has_antibot(driver: webdriver.Chrome) -> bool:
    try:
        txt = norm_text(driver.page_source)
        return any(x.lower() in txt.lower() for x in ["доступ ограничен", "капча", "cloudflare", "access denied", "robot"])
    except Exception:
        return False


def _candidate_tile_elements(driver: webdriver.Chrome):
    css = (
        "#graphic_wrapper a, #graphic_container a, "
        ".graphic_item, .graphic_item.free, .graphic_item.red, .graphic_item.disabled, "
        ".talon, .talon_item, .ticket, .ticket-item, .calendar-item, "
        ".calendar .day, .calendar .item, .day-item, .day, "
        "[class*='talon'], [class*='ticket'], [class*='calendar'], [class*='day'], [class*='graphic_item']"
    )
    try:
        return driver.find_elements(By.CSS_SELECTOR, css)
    except Exception:
        return []

def wait_for_calendar_or_login(driver: webdriver.Chrome, timeout: int = 60) -> str:
    """
    Возвращает: SLOTS | LOGIN | ANTIBOT

    Фикс под f-okno:
    - календарь на /base/... рисуется как #graphic_wrapper с элементами .graphic_item
    - свободные дни обычно имеют класс .graphic_item.free
    """
    end = time.time() + timeout

    while time.time() < end:
        url = (driver.current_url or "").lower()

        # 1) антибот/капча
        if _has_antibot(driver):
            return "ANTIBOT"

        # 2) признак логина
        if "/login" in url and _has_login_form(driver):
            return "LOGIN"

        # 3) быстрый признак календаря по HTML (f-okno)
        ps = driver.page_source or ""
        if ("id=\"graphic_wrapper\"" in ps) or ("graphic_item" in ps):
            # даже если нет текста, но есть график — считаем, что календарь есть
            return "SLOTS"

        # 4) универсальный признак календаря по DOM (на будущее)
        tiles = _candidate_tile_elements(driver)
        if len(tiles) >= 6:
            return "SLOTS"

        time.sleep(0.5)

    raise TimeoutException(f"Timeout waiting for calendar/login. URL={driver.current_url}")

def _find_first(driver: webdriver.Chrome, css_list: List[str]):
    for css in css_list:
        els = driver.find_elements(By.CSS_SELECTOR, css)
        if els:
            return els[0]
    return None

def perform_login(driver: webdriver.Chrome) -> None:
    if not LOGIN_URL:
        raise RuntimeError("LOGIN_URL не задан.")
    if not F_OKNO_EMAIL or not F_OKNO_PASSWORD:
        raise RuntimeError("Нужны Secrets: F_OKNO_EMAIL и F_OKNO_PASSWORD (иначе не залогиниться).")

    driver.get(LOGIN_URL)
    wait_dom_complete(driver, timeout=30)

    if _has_antibot(driver):
        dump_debug_artifacts(driver, reason="login_antibot")
        raise RuntimeError("Антибот/капча на странице логина.")

    # Поля именно такие на их форме: name="login" и name="pass"
    email_el = _find_first(driver, ["#login_form input[name='login']", "input[name='login']"])
    pass_el = _find_first(driver, ["#login_form input[name='pass']", "input[name='pass']", "input[type='password']"])

    if not email_el or not pass_el:
        dump_debug_artifacts(driver, reason="login_fields_not_found")
        raise RuntimeError("Не нашёл поля login/pass на странице логина (верстка поменялась?).")

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

    # Ждём, пока reCAPTCHA v3 положит токен в hidden input (иногда успевает не сразу)
    try:
        WebDriverWait(driver, 25).until(
            lambda d: ((d.find_element(By.ID, "g-recaptcha-response").get_attribute("value") or "").strip() not in ("", "0"))
        )
    except Exception:
        logging.warning("reCAPTCHA token not ready (or not found). Trying to submit anyway.")

    # Сабмитим "как на сайте": кликом по <a onclick="doForm('login_form')"> или вызовом doForm
    submit_link = _find_first(driver, [
        "#login_form a.pre_button",
        "#login_form a[onclick*='doForm']",
        "a.pre_button.blue.large",
        "a[onclick*=\"doForm('login_form'\"]",
        "a[onclick*='doForm']",
    ])

    if submit_link:
        driver.execute_script("arguments[0].click();", submit_link)
    else:
        # запасной вариант — вызвать их JS напрямую
        driver.execute_script(
            "if (typeof doForm === 'function') { doForm('login_form'); } "
            "else { document.getElementById('login_form').submit(); }"
        )

    # Ждём, что либо уйдём с /login, либо появится признак авторизации
    def _logged_in(d):
        url = (d.current_url or "")
        if "/login" not in url:
            return True
        ps = norm_text(d.page_source).lower()
        # иногда остаются на /login но меняется меню — ловим по словам "выход"
        return ("выход" in ps) or ("logout" in ps)

    try:
        WebDriverWait(driver, 35).until(_logged_in)
    except Exception:
        # вытащим текст ошибки (обычно красным)
        err = ""
        try:
            err = (driver.find_element(By.CSS_SELECTOR, ".pre_form_compact p[style*='color:red']").text or "").strip()
        except Exception:
            pass
        dump_debug_artifacts(driver, reason="login_no_redirect")
        if err:
            raise RuntimeError(f"Логин не прошёл: {err}")
        raise RuntimeError("После отправки формы не ушли со страницы логина (капча/блок/неверный пароль).")

    logging.info("Login OK (seems). URL now: %s", driver.current_url)


def extract_slots_from_dom(driver: webdriver.Chrome) -> List[Dict]:
    """
    Главный парсер слотов прямо из DOM.

    Фикс под f-okno:
    - карточки календаря: .graphic_item
    - свободные дни: .graphic_item.free
    - внутри текст 'Есть места'
    """
    slots: List[Dict] = []

    # 1) Самый надёжный путь: f-okno конкретно
    try:
        free_cards = driver.find_elements(By.CSS_SELECTOR, ".graphic_item.free")
    except Exception:
        free_cards = []

    for card in free_cards:
        try:
            txt = norm_text(card.text)
        except Exception:
            continue
        if not txt:
            continue

        # На всякий случай проверим маркер "Есть места"
        if not FREE_RE.search(txt):
            # иногда текст внутри может быть в дочернем, попробуем innerText
            try:
                inner = norm_text(card.get_attribute("innerText") or "")
            except Exception:
                inner = ""
            if not FREE_RE.search(inner):
                continue
            txt = inner

        d = _extract_date_from_text(txt)
        slots.append({"date": d or "Есть места (даты не распознаны)", "status": "Свободно"})

    if slots:
        # уникализация по дате
        uniq: List[Dict] = []
        seen = set()
        for it in slots:
            if it["date"] in seen:
                continue
            seen.add(it["date"])
            uniq.append(it)
        return uniq

    # 2) Если свободных не нашли — соберём "Нет мест" (полезно для snapshot)
    try:
        no_cards = driver.find_elements(By.CSS_SELECTOR, ".graphic_item.red, .graphic_item.disabled")
    except Exception:
        no_cards = []

    for card in no_cards:
        try:
            txt = norm_text(card.text)
        except Exception:
            continue
        if not txt:
            continue

        if not NO_RE.search(txt):
            try:
                inner = norm_text(card.get_attribute("innerText") or "")
            except Exception:
                inner = ""
            if not NO_RE.search(inner):
                continue
            txt = inner

        d = _extract_date_from_text(txt)
        if d:
            slots.append({"date": d, "status": "Нет мест"})

    if slots:
        # уникализация по (date,status)
        uniq: List[Dict] = []
        seen = set()
        for it in slots:
            key = (it["date"], it["status"])
            if key in seen:
                continue
            seen.add(key)
            uniq.append(it)
        return uniq

    # 3) Fallback: парсинг из HTML (если DOM-структура не совпала)
    return parse_slots_from_html(driver.page_source or "")


def open_target_with_login_if_needed(driver: webdriver.Chrome) -> None:
    driver.get(TARGET_URL)
    wait_dom_complete(driver, timeout=30)

    state = wait_for_calendar_or_login(driver, timeout=45)
    logging.info("State: %s | URL: %s", state, driver.current_url)

    if state == "ANTIBOT":
        dump_debug_artifacts(driver, reason="target_antibot")
        raise RuntimeError("Антибот/ограничение доступа на TARGET_URL.")

    if state == "SLOTS":
        return

    # LOGIN
    dump_debug_artifacts(driver, reason="redirected_to_login")
    perform_login(driver)

    driver.get(TARGET_URL)
    wait_dom_complete(driver, timeout=30)

    state2 = wait_for_calendar_or_login(driver, timeout=45)
    logging.info("State after login: %s | URL: %s", state2, driver.current_url)

    if state2 != "SLOTS":
        dump_debug_artifacts(driver, reason="still_not_slots_after_login")
        raise RuntimeError("После логина не увидели календарь (капча/блок/не та учётка/верстка).")


# ---------- main ----------
def one_check_run() -> None:
    # 0) Быстрый путь через requests (если возможно)
    if TRY_REQUESTS_FIRST:
        slots = try_requests_fetch()
        if slots is not None:
            has_free = any(s.get("status") == "Свободно" for s in slots)
            logging.info("Requests: got %d slots, free=%s", len(slots), has_free)

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
                logging.info("Snapshot изменился — сохранено (requests).")
            else:
                logging.info("Без изменений (requests).")
            return  # всё, Selenium не нужен

    # 1) Selenium путь
    driver = make_driver()
    try:
        open_target_with_login_if_needed(driver)

        # дать JS дорисовать календарь (иногда реально нужно)
        time.sleep(2.0)

        # артефакты всегда полезны
        Path("page.html").write_text(driver.page_source or "", encoding="utf-8")
        try:
            driver.save_screenshot("page.png")
        except Exception:
            pass

        slots = extract_slots_from_dom(driver)
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
