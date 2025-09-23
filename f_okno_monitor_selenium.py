import os, time, json, logging
from datetime import datetime
from typing import List, Dict

from dotenv import load_dotenv
from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from telegram import Bot

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
EMAIL = os.getenv("F_OKNO_EMAIL")
PASSWORD = os.getenv("F_OKNO_PASSWORD")
LOGIN_URL = os.getenv("LOGIN_URL", "https://f-okno.ru/login?request_uri=%2Fbase%2Fmoscovskaya_oblast%2Fsizo11noginsk")
TARGET_URL = os.getenv("TARGET_URL", "https://f-okno.ru/base/moscovskaya_oblast/sizo11noginsk")
STATE_FILE = os.getenv("STATE_FILE", "state_sizo11.json")

# периодичность тут игнорируется (её задаёт GitHub Actions cron),
# но оставлю на случай локального запуска:
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_MIN", "3")) * 60

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("f-okno-selenium")
bot = Bot(token=TELEGRAM_TOKEN)

def make_driver() -> webdriver.Chrome:
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--window-size=1280,2000")
    chrome_options.add_argument(
        "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )

    # ВАЖНО: GitHub Actions даёт переменную CHROME_PATH
    chrome_binary = os.getenv("CHROME_PATH")
    if chrome_binary:
        chrome_options.binary_location = chrome_binary

    # webdriver-manager скачает совместимый chromedriver сам
    from webdriver_manager.chrome import ChromeDriverManager
    drv = webdriver.Chrome(ChromeDriverManager().install(), options=chrome_options)
    drv.set_page_load_timeout(60)
    return drv

def login(driver: webdriver.Chrome):
    log.info("Открываю страницу логина…")
    driver.get(LOGIN_URL)
    wait = WebDriverWait(driver, 20)

    # ПОДГОНИ при необходимости под реальные селекторы формы:
    email_candidates = [(By.NAME, "email"), (By.CSS_SELECTOR, "input[type='email']"), (By.ID, "email")]
    pwd_candidates   = [(By.NAME, "password"), (By.CSS_SELECTOR, "input[type='password']"), (By.ID, "password")]
    submit_candidates= [(By.CSS_SELECTOR, "form button[type='submit']"),
                        (By.XPATH, "//form//button[contains(.,'Войти') or contains(.,'Авторизоваться')]"),
                        (By.CSS_SELECTOR, "button[type='submit']")]

    def find_first(cands):
        for how, what in cands:
            try: return wait.until(EC.presence_of_element_located((how, what)))
            except Exception: pass
        raise RuntimeError(f"Элемент не найден, проверь селекторы: {cands}")

    email_input = find_first(email_candidates)
    pwd_input   = find_first(pwd_candidates)
    email_input.clear(); email_input.send_keys(EMAIL)
    pwd_input.clear();   pwd_input.send_keys(PASSWORD)
    find_first(submit_candidates).click()

    time.sleep(2)
    driver.get(TARGET_URL)
    wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    log.info("Логин завершён, целевая страница открыта.")

def parse_slots_from_html(html: str) -> List[Dict]:
    soup = BeautifulSoup(html, "lxml")
    slots = []
    # ПОДГОНИ селекторы под реальный DOM:
    slot_nodes = soup.select(".slots-list .slot")  # заменишь при необходимости
    if slot_nodes:
        for node in slot_nodes:
            date = (node.select_one(".date").get_text(strip=True) if node.select_one(".date") else "")
            time_ = (node.select_one(".time").get_text(strip=True) if node.select_one(".time") else "")
            status = (node.select_one(".status").get_text(strip=True) if node.select_one(".status") else "")
            if not status:
                btn = node.select_one("button, a")
                if btn and ("Записаться" in btn.get_text() or "Свобод" in btn.get_text()):
                    status = "Свободно"
            slots.append({"date": date, "time": time_, "status": status})
        return slots

    # fallback: хотя бы понять, встречается ли «Свободно»
    text = soup.get_text("\n", strip=True)
    status = "Свободно" if ("Свобод" in text or "Записаться" in text) else "Недоступно/не найдено"
    slots.append({"date": "", "time": "", "status": status})
    return slots

def load_last_snapshot() -> str:
    if not os.path.exists(STATE_FILE): return ""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f: return f.read()
    except Exception:
        return ""

def save_snapshot(s: str):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f: f.write(s)
    except Exception as e:
        log.warning("Не удалось сохранить снапшот: %s", e)

def format_slots(slots: List[Dict]) -> str:
    if not slots: return "Слотов не найдено."
    return "\n".join(f"- {(s.get('date') or '')} {(s.get('time') or '')} — {(s.get('status') or '')}".strip()
                     for s in slots)

def send_tg(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_* не настроены"); return
    bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)

def one_check_run():
    """Одна проверка (для GitHub Actions)."""
    driver = make_driver()
    try:
        login(driver)
        time.sleep(2)
        html = driver.page_source
        slots = parse_slots_from_html(html)

        snapshot = json.dumps(slots, ensure_ascii=False, sort_keys=True)
        last = load_last_snapshot()
        if snapshot != last:
            text = f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] Обновление слотов СИЗО-11:\n\n{format_slots(slots)}"
            send_tg(text)
            save_snapshot(snapshot)
        else:
            logging.info("Без изменений.")
    finally:
        driver.quit()

if __name__ == "__main__":
    # для локального запуска можно крутить цикл,
    # но на Actions выполняем один прогон:
    one_check_run()
