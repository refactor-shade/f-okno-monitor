from __future__ import annotations

import os
import time
import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import List, Dict, Optional
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote

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

# Telegram (Secrets preferred, Variables fallback)
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN_VAR") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID_VAR") or "").strip()

# f-okno creds
F_OKNO_EMAIL = os.getenv("F_OKNO_EMAIL", "").strip()
F_OKNO_PASSWORD = os.getenv("F_OKNO_PASSWORD", "").strip()

# Target
TARGET_URL = os.getenv(
    "TARGET_URL",
    "https://f-okno.ru/base/moscovskaya_oblast/sizo11noginsk",
).strip()

# Optional override, but normally auto-computed from TARGET_URL
LOGIN_URL = os.getenv("LOGIN_URL", "").strip()

# Human label fallback if h1 not found
SIZO_LABEL_FALLBACK = os.getenv("SIZO_LABEL", "СИЗО").strip()

# State
STATE_FILE = os.getenv("STATE_FILE", "state.json").strip()

# Behavior
ONLY_NOTIFY_WHEN_FREE = os.getenv("ONLY_NOTIFY_WHEN_FREE", "1") == "1"
TRY_REQUESTS_FIRST = os.getenv("TRY_REQUESTS_FIRST", "0") == "1"  # по умолчанию выключено, т.к. календарь после логина

# Burst mode (optional, default: single check per run)
BURST_MINUTES = int(os.getenv("BURST_MINUTES", "0") or "0")  # 0 = нет цикла
BURST_SLEEP_MIN = float(os.getenv("BURST_SLEEP_MIN", "75") or "75")
BURST_SLEEP_MAX = float(os.getenv("BURST_SLEEP_MAX", "105") or "105")
MAX_LOGINS_PER_RUN = int(os.getenv("MAX_LOGINS_PER_RUN", "2") or "2")

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# ---------- regex/markers ----------
_MONTHS = "января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря"
_WEEKDAYS = "понедельник|вторник|среда|четверг|пятница|суббота|воскресенье"
_DATE_RE = re.compile(rf"\b(\d{{1,2}})\s+({_MONTHS})(?:\s+({_WEEKDAYS}))?\b", re.IGNORECASE)

FREE_RE = re.compile(r"есть\s*мест", re.IGNORECASE)
NO_RE = re.compile(r"(свободных\s*(мест|дат)\s*нет|нет\s*мест)", re.IGNORECASE)

def _extract_human_date_from_text(text: str) -> str:
    t = norm_text(text)
    m = _DATE_RE.search(t)
    if m:
        return m.group(0).strip()

    # запасной вариант без дня недели
    m2 = re.search(rf"\b(\d{{1,2}})\s+({_MONTHS})\b", t, re.IGNORECASE)
    if m2:
        return m2.group(0).strip()

    return ""

# ---------- utils ----------
def norm_text(s: str) -> str:
    s = (s or "").replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def build_login_url_from_target(target_url: str) -> str:
    """
    f-okno логин URL: /login?request_uri=%2Fbase%2F...
    """
    try:
        u = urlparse(target_url)
        path = u.path or ""
        if not path.startswith("/"):
            path = "/" + path
        # request_uri expects encoded path where / -> %2F
        request_uri = quote(path, safe="")  # encodes '/' too
        return f"{u.scheme}://{u.netloc}/login?request_uri={request_uri}"
    except Exception:
        # fallback to old default if somehow failed
        return "https://f-okno.ru/login"


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


def load_last_free_dates() -> List[str]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        dates = data.get("free_dates", [])
        cleaned = sorted({str(x).strip() for x in dates if str(x).strip()})
        return cleaned
    except FileNotFoundError:
        return []
    except Exception:
        logging.exception("load_last_free_dates failed")
        return []


def save_free_dates(free_dates: List[str]) -> None:
    try:
        payload = {"free_dates": sorted({d.strip() for d in free_dates if d and d.strip()})}
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, sort_keys=True)
    except Exception:
        logging.exception("save_free_dates failed")


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

    try:
        (out / f"{reason}.summary.txt").write_text(
            "\n".join(
                [
                    f"reason={reason}",
                    f"target={TARGET_URL}",
                    f"current_url={driver.current_url}",
                    f"time_msk={datetime.now(MOSCOW_TZ).strftime('%Y-%m-%d %H:%M:%S')}",
                ]
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def _make_soup(html: str) -> BeautifulSoup:
    for parser in ("lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return BeautifulSoup(html, "html.parser")


def extract_label_from_h1(driver: webdriver.Chrome) -> str:
    """
    Берём красивый SIZO_LABEL из <h1> типа:
    <h1>СИЗО-12 Зеленоград(<a ...>Москва</a>)</h1>
    """
    try:
        h1 = driver.find_element(By.CSS_SELECTOR, "h1")
        txt = norm_text(h1.text)
        if not txt:
            return SIZO_LABEL_FALLBACK
        # привести "(Москва)" к " (Москва)" — чуть красивее
        txt = re.sub(r"\(\s*", " (", txt)
        txt = re.sub(r"\s*\)", ")", txt)
        return txt
    except Exception:
        return SIZO_LABEL_FALLBACK


def extract_free_dates_from_dom(driver: webdriver.Chrome) -> List[str]:
    """
    Устойчивый детектор свободных дат.

    Основной путь:
      - свободно = элемент .graphic_item.free
      - дата берётся из href родительского <a> как date=YYYY-MM-DD

    Fallback:
      - если классы сломались, но текст "есть места" остался
      - если href без date — берём дату из текста
    """
    iso_dates: List[str] = []
    human_dates: List[str] = []

    seen_iso = set()
    seen_human = set()

    cards = driver.find_elements(By.CSS_SELECTOR, ".graphic_item")

    for card in cards:
        try:
            cls = (card.get_attribute("class") or "").lower()
            txt = norm_text(card.get_attribute("innerText") or card.text or "")
        except Exception:
            continue

        is_free_by_class = "free" in cls.split()
        is_free_by_text = bool(FREE_RE.search(txt)) if txt else False

        if not (is_free_by_class or is_free_by_text):
            continue

        # 1) ISO дата из href
        iso_d = ""
        try:
            a = card.find_element(By.XPATH, "./ancestor::a[1]")
            href = a.get_attribute("href") or ""
            if href:
                q = parse_qs(urlparse(href).query)
                iso_d = (q.get("date", [""])[0] or "").strip()
        except Exception:
            iso_d = ""

        if iso_d:
            if iso_d not in seen_iso:
                seen_iso.add(iso_d)
                iso_dates.append(iso_d)
            continue

        # 2) fallback: дата из текста
        hd = _extract_human_date_from_text(txt)
        if hd and hd not in seen_human:
            seen_human.add(hd)
            human_dates.append(hd)

    if iso_dates:
        iso_dates.sort()
        return iso_dates

    return human_dates
    

def format_free_dates(dates: List[str]) -> str:
    if not dates:
        return "Свободных дат нет."
    return "\n".join([f"✅ <b>{d}</b>" for d in dates])


# ---------- requests first (optional) ----------
def try_requests_fetch_free_dates() -> Optional[List[str]]:
    """
    Иногда страница может отдать готовый HTML.
    Мы не шлём "нет мест", поэтому если free не нашли — возвращаем None и идём в Selenium.
    """
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        }
        r = requests.get(TARGET_URL, headers=headers, timeout=25, allow_redirects=True)
        if "/login" in (r.url or ""):
            return None

        html = r.text or ""
        # быстрый признак календаря
        if "graphic_item" not in html:
            return None

        soup = _make_soup(html)
        free_dates = []
        seen = set()

        # ищем a внутри которых есть .graphic_item.free
        for a in soup.select("a:has(.graphic_item.free)"):
            href = a.get("href") or ""
            if not href:
                continue
            q = parse_qs(urlparse(href).query)
            d = (q.get("date", [""])[0] or "").strip()
            if d and d not in seen:
                seen.add(d)
                free_dates.append(d)

        free_dates.sort()
        # если список пуст — лучше перейти в Selenium, вдруг HTML неполный
        return free_dates if free_dates else None
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
    sel = "form[action*='login'], input[type='password'], input[name='email'], input[name='login'], #login_form"
    try:
        return len(driver.find_elements(By.CSS_SELECTOR, sel)) > 0
    except Exception:
        return False


def _has_antibot(driver: webdriver.Chrome) -> bool:
    try:
        txt = norm_text(driver.page_source)
        return any(x.lower() in txt.lower() for x in ["доступ ограничен", "cloudflare", "access denied", "robot"])
    except Exception:
        return False


def wait_for_calendar_or_login(driver: webdriver.Chrome, timeout: int = 60) -> str:
    """
    Возвращает: SLOTS | LOGIN | ANTIBOT
    """
    end = time.time() + timeout

    while time.time() < end:
        url = (driver.current_url or "").lower()

        if _has_antibot(driver):
            return "ANTIBOT"

        if "/login" in url and _has_login_form(driver):
            return "LOGIN"

        ps = driver.page_source or ""
        if ("id=\"graphic_wrapper\"" in ps) or ("graphic_item" in ps):
            return "SLOTS"

        # чуть мягче: если DOM уже содержит плитки календаря
        try:
            if driver.find_elements(By.CSS_SELECTOR, ".graphic_item"):
                return "SLOTS"
        except Exception:
            pass

        time.sleep(0.5)

    raise TimeoutException(f"Timeout waiting for calendar/login. URL={driver.current_url}")


def _find_first(driver: webdriver.Chrome, css_list: List[str]):
    for css in css_list:
        els = driver.find_elements(By.CSS_SELECTOR, css)
        if els:
            return els[0]
    return None


def perform_login(driver: webdriver.Chrome, login_url: str) -> None:
    if not login_url:
        raise RuntimeError("LOGIN_URL пустой.")
    if not F_OKNO_EMAIL or not F_OKNO_PASSWORD:
        raise RuntimeError("Нужны Secrets: F_OKNO_EMAIL и F_OKNO_PASSWORD (иначе не залогиниться).")

    driver.get(login_url)
    wait_dom_complete(driver, timeout=30)

    if _has_antibot(driver):
        dump_debug_artifacts(driver, reason="login_antibot")
        raise RuntimeError("Ограничение доступа на странице логина.")

    # Поля на их форме обычно name="login" и name="pass"
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

    # Сабмит: пытаемся кликнуть по кнопке/ссылке формы
    submit_link = _find_first(driver, [
        "#login_form a.pre_button",
        "#login_form a[onclick*='doForm']",
        "a.pre_button.blue.large",
        "a[onclick*='doForm']",
    ])

    if submit_link:
        driver.execute_script("arguments[0].click();", submit_link)
    else:
        driver.execute_script(
            "if (typeof doForm === 'function') { doForm('login_form'); } "
            "else { document.getElementById('login_form').submit(); }"
        )

    def _logged_in(d):
        url = (d.current_url or "")
        if "/login" not in url:
            return True
        ps = norm_text(d.page_source).lower()
        return ("выход" in ps) or ("logout" in ps)

    try:
        WebDriverWait(driver, 35).until(_logged_in)
    except Exception:
        dump_debug_artifacts(driver, reason="login_no_redirect")
        raise RuntimeError("После отправки формы не ушли со страницы логина (пароль/блок/верстка).")

    logging.info("Login OK (seems). URL now: %s", driver.current_url)


def open_target_with_login_if_needed(driver: webdriver.Chrome, login_url: str, login_counter: Dict[str, int]) -> None:
    driver.get(TARGET_URL)
    wait_dom_complete(driver, timeout=30)

    state = wait_for_calendar_or_login(driver, timeout=45)
    logging.info("State: %s | URL: %s", state, driver.current_url)

    if state == "ANTIBOT":
        dump_debug_artifacts(driver, reason="target_antibot")
        raise RuntimeError("Ограничение доступа на TARGET_URL.")

    if state == "SLOTS":
        return

    # LOGIN
    if login_counter["count"] >= MAX_LOGINS_PER_RUN:
        dump_debug_artifacts(driver, reason="too_many_logins")
        raise RuntimeError("Слишком много редиректов на логин за один запуск.")

    dump_debug_artifacts(driver, reason="redirected_to_login")
    login_counter["count"] += 1
    perform_login(driver, login_url)

    driver.get(TARGET_URL)
    wait_dom_complete(driver, timeout=30)

    state2 = wait_for_calendar_or_login(driver, timeout=45)
    logging.info("State after login: %s | URL: %s", state2, driver.current_url)

    if state2 != "SLOTS":
        dump_debug_artifacts(driver, reason="still_not_slots_after_login")
        raise RuntimeError("После логина не увидели календарь (блок/не та учётка/верстка).")


def single_check_and_notify(driver: webdriver.Chrome, login_url: str, login_counter: Dict[str, int]) -> bool:
    """
    Возвращает True если отправили уведомление (нашли НОВЫЕ свободные даты).
    """
    open_target_with_login_if_needed(driver, login_url, login_counter)

    # на всякий — дождаться появления элементов календаря (вместо sleep)
    try:
        WebDriverWait(driver, 10).until(lambda d: len(d.find_elements(By.CSS_SELECTOR, ".graphic_item")) > 0)
    except Exception:
        pass

    # артефакты страницы (полезно при спорных кейсах)
    Path("page.html").write_text(driver.page_source or "", encoding="utf-8")
    try:
        driver.save_screenshot("page.png")
    except Exception:
        pass

    label = extract_label_from_h1(driver)
    free_dates = extract_free_dates_from_dom(driver)

    logging.info("Label: %s", label)
    logging.info("Free dates: %s", free_dates)

    last_free = load_last_free_dates()

    # Режим: уведомляем только когда есть свободные и они отличаются от предыдущих
    if free_dates and free_dates != last_free:
        ts = datetime.now(MOSCOW_TZ).strftime("%Y-%m-%d %H:%M")
        text = (
            f"🚨 Появились свободные слоты в <b>{label}</b>! [{ts}]\n\n"
            f"{format_free_dates(free_dates)}\n\n"
            f"Открыть календарь: <a href='{TARGET_URL}'>страница записи</a>"
        )
        send_tg(text)
        save_free_dates(free_dates)
        logging.info("Sent notification + saved free_dates.")
        return True

    # Ничего не отправляем (как ты просила)
    if not free_dates:
        logging.info("No free dates — silent.")
    else:
        logging.info("Free dates unchanged — silent.")
    return False


def one_check_run() -> None:
    # login_url: explicit override OR computed from TARGET_URL
    login_url = LOGIN_URL or build_login_url_from_target(TARGET_URL)
    login_counter = {"count": 0}

    # 0) Быстрый путь через requests (опционально)
    if TRY_REQUESTS_FIRST:
        free_dates = try_requests_fetch_free_dates()
        if free_dates is not None:
            last_free = load_last_free_dates()
            if free_dates and free_dates != last_free:
                ts = datetime.now(MOSCOW_TZ).strftime("%Y-%m-%d %H:%M")
                text = (
                    f"🚨 Появились свободные слоты в <b>{SIZO_LABEL_FALLBACK}</b>! [{ts}]\n\n"
                    f"{format_free_dates(free_dates)}\n\n"
                    f"Открыть календарь: <a href='{TARGET_URL}'>страница записи</a>"
                )
                send_tg(text)
                save_free_dates(free_dates)
                logging.info("Sent + saved free_dates (requests).")
            else:
                logging.info("Requests: silent (no free or unchanged).")
            return

    # 1) Selenium путь
    driver = make_driver()
    try:
        if BURST_MINUTES <= 0:
            single_check_and_notify(driver, login_url, login_counter)
            return

        end = time.time() + BURST_MINUTES * 60
        sent_any = False

        while time.time() < end:
            try:
                sent = single_check_and_notify(driver, login_url, login_counter)
                sent_any = sent_any or sent
            except Exception:
                # при ошибке — сохраняем артефакты, но уведомление "ошибка" НЕ шлём (ты этого не просила)
                logging.exception("Check failed inside burst loop")
                try:
                    dump_debug_artifacts(driver, reason="burst_error")
                except Exception:
                    pass
                # при ошибке можно попробовать продолжить, но без фанатизма
            # пауза с небольшим разбросом (чтобы не быть "ровным роботом")
            # без random, чтобы не тащить импорт — делаем простую псевдо-джиттер логику
            # (зависит от текущих секунд)
            now = int(time.time())
            span = max(1.0, BURST_SLEEP_MAX - BURST_SLEEP_MIN)
            jitter = (now % int(span + 1))  # 0..span
            sleep_s = BURST_SLEEP_MIN + float(jitter)
            time.sleep(sleep_s)

        logging.info("Burst finished. sent_any=%s", sent_any)

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
