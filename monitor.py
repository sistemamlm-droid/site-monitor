"""
Мониторинг сайта: новые страницы + изменения контента/цен.
Присылает уведомления в Telegram только когда есть реальные изменения.

Требует переменные окружения:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
"""

import os
import re
import json
import time
import hashlib
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ===================== НАСТРОЙКИ (можно менять) =====================

BASE_URL = "https://ru.siberianhealth.com/ru/"
DOMAIN = urlparse(BASE_URL).netloc

# Стандартные адреса, где обычно лежит карта сайта
SITEMAP_CANDIDATES = [
    "https://ru.siberianhealth.com/sitemap.xml",
    "https://ru.siberianhealth.com/sitemap_index.xml",
]

STATE_DIR = "state"           # папка, где хранится "память" о прошлом состоянии
MAX_PAGES = 150                # лимит страниц за один прогон (чтобы не превышать лимиты)
REQUEST_DELAY = 1.0            # пауза между запросами, сек (вежливость к серверу)
PAGE_TIMEOUT_MS = 30000        # таймаут загрузки одной страницы

# Регулярка для цен в рублях, например: "1 990 руб", "2490руб", "999 ₽"
PRICE_RE = re.compile(r"\d[\d\s]{1,9},?\d*\s?(?:руб|₽|RUB)", re.IGNORECASE)

# =======================================================================

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PersonalPriceMonitor/1.0)"}

os.makedirs(STATE_DIR, exist_ok=True)


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for i in range(0, len(text), 3900):
        chunk = text[i:i + 3900]
        try:
            requests.post(
                url,
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": chunk,
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
        except Exception as e:
            print("Ошибка отправки в Telegram:", e)


def _parse_sitemap(sitemap_url, seen):
    if sitemap_url in seen:
        return []
    seen.add(sitemap_url)
    try:
        r = requests.get(sitemap_url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception:
        return []
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls = []
    for sm in root.findall("sm:sitemap/sm:loc", ns):
        urls.extend(_parse_sitemap(sm.text.strip(), seen))
    for loc in root.findall("sm:url/sm:loc", ns):
        urls.append(loc.text.strip())
    return urls


def try_sitemap_urls():
    urls = []
    seen = set()
    for sm in SITEMAP_CANDIDATES:
        urls.extend(_parse_sitemap(sm, seen))
    return urls


def fetch_rendered(url, browser):
    """Открывает страницу как настоящий браузер (важно для сайтов на JS)."""
    page = browser.new_page(user_agent=HEADERS["User-Agent"])
    html = ""
    try:
        page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)  # даём время догрузиться JS-контенту
        html = page.content()
    except Exception as e:
        print(f"Не удалось загрузить {url}: {e}")
    finally:
        page.close()
    return html


def crawl_links(start_url, browser, limit):
    """Резервный способ поиска страниц, если sitemap.xml не найден:
    обходим сайт по внутренним ссылкам."""
    to_visit = [start_url]
    visited = set()
    found = []
    while to_visit and len(found) < limit:
        url = to_visit.pop(0)
        if url in visited:
            continue
        visited.add(url)
        html = fetch_rendered(url, browser)
        if not html:
            continue
        found.append(url)
        soup = BeautifulSoup(html, "lxml")
        for a in soup.find_all("a", href=True):
            link = urljoin(url, a["href"]).split("#")[0]
            if urlparse(link).netloc == DOMAIN and link not in visited:
                to_visit.append(link)
        time.sleep(REQUEST_DELAY)
    return found


def clean_text(html):
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text)


def extract_prices(text):
    return sorted(set(PRICE_RE.findall(text)))


def url_key(url):
    return hashlib.md5(url.encode("utf-8")).hexdigest()


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main():
    known_urls_path = os.path.join(STATE_DIR, "known_urls.json")
    known_urls = set(load_json(known_urls_path, []))
    is_first_run = len(known_urls) == 0

    with sync_playwright() as p:
        browser = p.chromium.launch()

        current_urls = try_sitemap_urls()
        current_urls = [u for u in current_urls if urlparse(u).netloc == DOMAIN]

        if not current_urls:
            print("sitemap.xml не найден, обхожу сайт по ссылкам...")
            current_urls = crawl_links(BASE_URL, browser, MAX_PAGES)

        current_urls = current_urls[:MAX_PAGES]
        current_set = set(current_urls)

        changes_report = []

        new_pages = current_set - known_urls
        if not is_first_run and new_pages:
            changes_report.append("🆕 Новые страницы:")
            changes_report.extend(f"— {u}" for u in sorted(new_pages))

        for url in current_urls:
            html = fetch_rendered(url, browser)
            if not html:
                continue
            text = clean_text(html)
            text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            prices = extract_prices(text)

            page_state_path = os.path.join(STATE_DIR, f"page_{url_key(url)}.json")
            prev = load_json(page_state_path, None)

            if prev is not None and prev["hash"] != text_hash:
                old_prices = set(prev.get("prices", []))
                new_prices_set = set(prices)
                if old_prices != new_prices_set:
                    changes_report.append(f"💰 Изменение цены: {url}")
                    removed = old_prices - new_prices_set
                    added = new_prices_set - old_prices
                    if removed:
                        changes_report.append(f"   было: {', '.join(removed)}")
                    if added:
                        changes_report.append(f"   стало: {', '.join(added)}")
                else:
                    changes_report.append(f"✏️ Изменение контента: {url}")

            save_json(page_state_path, {"hash": text_hash, "prices": prices})
            time.sleep(REQUEST_DELAY)

        browser.close()

    save_json(known_urls_path, sorted(current_set))

    if is_first_run:
        print(f"Первый запуск: сохранена базовая версия ({len(current_set)} страниц). "
              f"Уведомления начнутся со следующей проверки.")
        return

    if changes_report:
        header = f"Изменения на {BASE_URL} ({time.strftime('%Y-%m-%d')}):\n\n"
        send_telegram(header + "\n".join(changes_report))
        print("Найдены изменения, отправлено в Telegram.")
    else:
        print("Изменений не найдено.")


if __name__ == "__main__":
    main()
