import json
import os
import re
import argparse
import time
import random
from datetime import datetime
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
import requests
from dotenv import load_dotenv

load_dotenv()

# ==== НАЛАШТУВАННЯ ====
# Посилання на видачу OLX з потрібними фільтрами (місто, ціна, сортування за датою).
# Задається через змінну OLX_SEARCH_URL; нижче лише приклад.
DEFAULT_SEARCH_URL = "https://www.olx.ua/uk/nedvizhimost/kvartiry/dolgosrochnaya-arenda-kvartir/kiev/?currency=UAH&search%5Border%5D=created_at:desc&search%5Bfilter_float_price:to%5D=20000"
SEARCH_URL = os.environ.get("OLX_SEARCH_URL") or DEFAULT_SEARCH_URL

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
_chat_ids_raw = os.environ.get("TELEGRAM_CHAT_IDS", "")
TELEGRAM_CHAT_IDS = [c.strip() for c in _chat_ids_raw.split(",") if c.strip()]

CHECK_INTERVAL_MIN_SECONDS = 60   # мінімальний інтервал між перевірками (1 хв)
CHECK_INTERVAL_MAX_SECONDS = 180  # максимальний інтервал між перевірками (3 хв)

# Робочі години (за київським часом) - поза цим вікном скрипт не перевіряє
# оголошення і нічого не шле. Автоматично враховує перехід на літній/зимовий час.
ACTIVE_HOURS_TZ = ZoneInfo("Europe/Kyiv")
ACTIVE_HOUR_START = 8   # 08:00 включно
ACTIVE_HOUR_END = 22    # до 22:00 (не включно)


def is_within_active_hours():
    now = datetime.now(ACTIVE_HOURS_TZ)
    return ACTIVE_HOUR_START <= now.hour < ACTIVE_HOUR_END

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SEEN_FILE = os.path.join(SCRIPT_DIR, "seen_ids.json")
ERROR_LOG = os.path.join(SCRIPT_DIR, "olx_monitor_error.log")
LOCK_FILE = os.path.join(SCRIPT_DIR, "olx_monitor.lock")


def log_error(message):
    print(message)
    with open(ERROR_LOG, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")


def check_config():
    """Перевіряє, що обов'язкові налаштування (токен, chat_id) задані."""
    if not TELEGRAM_TOKEN:
        raise SystemExit(
            "Помилка: не задано TELEGRAM_TOKEN. Створіть файл .env на основі .env.example "
            "або задайте змінну середовища TELEGRAM_TOKEN."
        )
    if not TELEGRAM_CHAT_IDS:
        raise SystemExit(
            "Помилка: не задано TELEGRAM_CHAT_IDS. Створіть файл .env на основі .env.example "
            "або задайте змінну середовища TELEGRAM_CHAT_IDS (через кому)."
        )


def load_seen_ids():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen_ids(seen_ids):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen_ids), f)


def send_telegram_message(text, chat_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        response = requests.post(url, data=payload, timeout=15)
        if not response.ok:
            log_error(f"Помилка Telegram API (chat_id={chat_id}): {response.status_code} {response.text}")
        else:
            print(f"Надіслано в Telegram (chat_id={chat_id}): {text[:50]}...")
    except Exception as e:
        log_error(f"Не вдалося надіслати повідомлення в Telegram (chat_id={chat_id}): {e}")


def extract_listing_id(url):
    match = re.search(r"ID([a-zA-Z0-9]+)\.html", url)
    return match.group(1) if match else url


def fetch_listings():
    """Завантажує і парсить сторінку пошуку OLX."""
    listings = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            locale="uk-UA",
            viewport={"width": 1366, "height": 768},
            extra_http_headers={
                "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        )
        page = context.new_page()

        try:
            page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)
        except Exception as e:
            log_error(f"Помилка завантаження сторінки OLX: {e}")
            browser.close()
            return listings

        # networkidle часто взагалі не настає через фонову рекламу/аналітику -
        # не вважаємо це фатальною помилкою, просто йдемо далі з тим HTML,
        # що вже є на сторінці
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            print("networkidle не настав за 8с - продовжую з поточним станом сторінки (це нормально)")

        html = page.content()
        browser.close()

    soup = BeautifulSoup(html, "html.parser")

    cards = soup.find_all("div", attrs={"data-testid": "l-card"})
    if not cards:
        log_error("Не знайдено жодної картки оголошення - можливо, змінилась розмітка сайту або показалась капча/блокування")
        snapshot_path = os.path.join(SCRIPT_DIR, "last_failed_page.html")
        try:
            with open(snapshot_path, "w", encoding="utf-8") as f:
                f.write(html)
        except Exception as e:
            log_error(f"Не вдалося зберегти діагностичний HTML: {e}")
        return listings

    for idx, card in enumerate(cards):
        if os.environ.get("OLX_DEBUG") and idx < 3:
            debug_path = os.path.join(SCRIPT_DIR, f"debug_card_{idx}.html")
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write(card.prettify())
            print(f"[DEBUG] Збережено картку {idx} у {debug_path}")

        link_tag = card.find("a", href=True)
        if not link_tag:
            continue
        href = link_tag["href"]

        # Пропускаємо промо-оголошення (позначені міткою "ТОП") -
        # вони штучно піднімаються нагору списку незалежно від сортування
        # за датою і не є справді новими. OLX позначає їх параметром
        # search_reason=search|promoted у посиланні картки
        if "promoted" in href:
            continue
        full_url = href if href.startswith("http") else "https://www.olx.ua" + href

        title_tag = card.find("h4") or card.find("h6")
        title = title_tag.get_text(strip=True) if title_tag else "Без назви"

        price_tag = card.find("p", attrs={"data-testid": "ad-price"})
        price = price_tag.get_text(strip=True) if price_tag else "Ціна не вказана"

        listing_id = extract_listing_id(full_url)

        listings.append({
            "id": listing_id,
            "title": title,
            "price": price,
            "url": full_url,
        })

    return listings


def send_notification(item, test_mode=False):
    prefix = "🧪 [ТЕСТ] " if test_mode else ""
    text = (
        f"{prefix}🏠 <b>{item['title']}</b>\n"
        f"💰 {item['price']}\n"
        f"🔗 {item['url']}"
    )
    for chat_id in TELEGRAM_CHAT_IDS:
        send_telegram_message(text, chat_id)


def run_once(test_mode=False):
    start_time = datetime.now()
    print(f"=== Початок перевірки: {start_time.strftime('%Y-%m-%d %H:%M:%S')} ===")

    if not test_mode and not is_within_active_hours():
        kyiv_now = datetime.now(ACTIVE_HOURS_TZ)
        print(f"Зараз {kyiv_now.strftime('%H:%M')} за Києвом - поза робочими годинами "
              f"({ACTIVE_HOUR_START}:00-{ACTIVE_HOUR_END}:00), перевірку пропускаю.")
        print("=== Кінець перевірки (пропущено, нічний час) ===\n")
        return

    try:
        _run_once_inner(test_mode=test_mode)
    finally:
        finish_time = datetime.now()
        duration = (finish_time - start_time).total_seconds()
        print(f"=== Кінець перевірки: {finish_time.strftime('%Y-%m-%d %H:%M:%S')} (тривалість {duration:.1f} сек) ===\n")


def _run_once_inner(test_mode=False):
    seen_ids = load_seen_ids()
    listings = fetch_listings()

    if not listings:
        print("Оголошень не знайдено (або помилка) - перевірте olx_monitor_error.log")
        return

    print(f"Знайдено оголошень на сторінці: {len(listings)}")

    if test_mode:
        # Штучно "забуваємо" останні 2 оголошення зі списку знайдених,
        # щоб перевірити, чи справді приходять сповіщення
        test_targets = listings[:2]
        for item in test_targets:
            seen_ids.discard(item["id"])
        print(f"[ТЕСТ] Штучно забуто {len(test_targets)} оголошень, зараз перевірю чи прийдуть сповіщення")

        for item in listings:
            seen_ids.add(item["id"])

        new_listings = test_targets

        if new_listings:
            for item in new_listings:
                send_notification(item, test_mode=True)
            print(f"[ТЕСТ] Надіслано {len(new_listings)} тестових сповіщень. Перевірте Telegram.")
        else:
            print("[ТЕСТ] Немає оголошень для тесту (список порожній).")

        # у тестовому режимі НЕ зберігаємо зміни в файл - щоб не зламати
        # реальну базу для наступного звичайного запуску
        return

    # ==== ЗВИЧАЙНИЙ РЕЖИМ ====
    is_first_run = len(seen_ids) == 0
    new_listings = [item for item in listings if item["id"] not in seen_ids]

    for item in listings:
        seen_ids.add(item["id"])
    save_seen_ids(seen_ids)

    if is_first_run:
        print(f"Перший запуск: збережено {len(seen_ids)} оголошень як базу, повідомлення не надсилаю.")
        return

    if new_listings:
        print(f"Знайдено нових оголошень: {len(new_listings)}")
        for item in new_listings:
            send_notification(item, test_mode=False)
    else:
        print("Нових оголошень немає.")


def main(test_mode=False, loop=False):
    if test_mode:
        # тестовий режим завжди одноразовий, цикл тут не потрібен
        run_once(test_mode=True)
        return

    if not loop:
        run_once(test_mode=False)
        return

    print(f"Запускаю моніторинг у режимі циклу: перевірка кожні {CHECK_INTERVAL_MIN_SECONDS}-{CHECK_INTERVAL_MAX_SECONDS} сек (випадково). Зупинити - Ctrl+C.")
    while True:
        start = time.time()
        try:
            run_once(test_mode=False)
        except Exception as e:
            log_error(f"Неочікувана помилка під час перевірки: {e}")

        elapsed = time.time() - start
        target_interval = random.uniform(CHECK_INTERVAL_MIN_SECONDS, CHECK_INTERVAL_MAX_SECONDS)
        sleep_time = max(0, target_interval - elapsed)
        print(f"Перевірку завершено за {elapsed:.1f} сек. Наступна перевірка через {sleep_time:.1f} сек "
              f"(випадковий інтервал: {target_interval:.1f} сек).\n")
        time.sleep(sleep_time)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OLX моніторинг нових оголошень")
    parser.add_argument("--test", action="store_true", help="Тестовий режим: штучно забуває 2 оголошення і шле про них сповіщення, не чіпаючи реальну базу")
    parser.add_argument("--loop", action="store_true", help="Запустити в нескінченному циклі з перевіркою кожні CHECK_INTERVAL_SECONDS секунд (для локального/VPS запуску; для GitHub Actions НЕ використовувати - там одноразовий запуск за розкладом)")
    args = parser.parse_args()

    check_config()

    # Захист від накопичення запусків: якщо попередній запуск (наприклад,
    # через cron) ще не завершився - новий одразу виходить, а не чекає
    # і не запускається паралельно
    if not args.loop:
        if os.path.exists(LOCK_FILE):
            lock_age = time.time() - os.path.getmtime(LOCK_FILE)
            if lock_age < 300:  # lock-файл свіжіший за 5 хв - вважаємо, що попередній запуск ще триває
                print(f"Попередній запуск ще триває (lock-файл віком {lock_age:.0f} сек) - пропускаю цей запуск.")
                exit(0)
            else:
                print(f"Знайдено застарілий lock-файл (віком {lock_age:.0f} сек) - ймовірно, попередній запуск завис. Видаляю і продовжую.")

        with open(LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))

        try:
            main(test_mode=args.test, loop=args.loop)
        finally:
            if os.path.exists(LOCK_FILE):
                os.remove(LOCK_FILE)
    else:
        main(test_mode=args.test, loop=args.loop)
