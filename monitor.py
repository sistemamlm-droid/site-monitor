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
import difflib
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

# Страница считается "карточкой товара", если её адрес содержит один из этих кусков.
# Только на таких страницах проверяются цена и текст. На остальных (каталог,
# листинги, разделы) отслеживается только факт появления новой страницы.
PRODUCT_URL_MARKERS = ["/product/"]

# Блоки с этими словами в class/id считаются "шумом" и вырезаются перед сравнением
# текста: рекомендации, слайдеры, отзывы, счётчики просмотров и т.п. — то, что
# меняется само по себе, без реальной правки карточки.
NOISE_KEYWORDS = [
    "nav", "footer", "header", "menu", "cookie", "banner", "slider", "carousel",
    "recommend", "viewed", "related", "similar", "popular", "social", "share",
    "chat", "widget", "subscribe", "newsletter", "breadcrumb", "review", "rating",
    "comment", "compare", "wishlist", "favorite", "cart", "counter",
]

# Насколько текст карточки должен отличаться, чтобы считаться реальным изменением.
# 1.0 = тексты идентичны. 0.95 означает "разрешаем" до ~5% технических отличий.
CONTENT_SIMILARITY_THRESHOLD = 0.95

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
    """Полный текст страницы (используется только для обхода по ссылкам)."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text)


def is_product_url(url):
    return any(marker in url for marker in PRODUCT_URL_MARKERS)


def get_core_text(html):
    """Текст страницы БЕЗ шумных блоков (рекомендации, шапка/подвал, отзывы и т.п.) —
    используется для сравнения карточек товаров, чтобы не ловить ложные срабатывания."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()
    for el in soup.find_all(True):
        classes = " ".join(el.get("class", []) or [])
        el_id = el.get("id", "") or ""
        attrs = (classes + " " + el_id).lower()
        if any(keyword in attrs for keyword in NOISE_KEYWORDS):
            el.decompose()
    main = soup.find("main") or soup.body or soup
    text = main.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text)


def text_similarity(old_text, new_text):
    if not old_text or not new_text:
        return 0.0
    return difflib.SequenceMatcher(None, old_text, new_text).quick_ratio()


def diff_snippet(old_text, new_text, max_len=200):
    """Возвращает короткий фрагмент 'было / стало' — первое найденное отличие."""
    sm = difflib.SequenceMatcher(None, old_text, new_text)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        old_part = old_text[i1:i2].strip()
        new_part = new_text[j1:j2].strip()
        if not old_part and not new_part:
            continue
        parts = []
        if old_part:
            parts.append(f"было: «{old_part[:max_len]}»")
        if new_part:
            parts.append(f"стало: «{new_part[:max_len]}»")
        return " / ".join(parts)
    return ""


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

            product = is_product_url(url)
            page_state_path = os.path.join(STATE_DIR, f"page_{url_key(url)}.json")
            prev = load_json(page_state_path, None)

            if not product:
                # Каталог/листинг/разделы: контент не сравниваем (слишком шумно —
                # порядок и набор товаров на витрине меняется сам по себе).
                # Только фиксируем факт посещения, чтобы находить новые страницы.
                save_json(page_state_path, {"visited": True})
                time.sleep(REQUEST_DELAY)
                continue

            core_text = get_core_text(html)
            prices = extract_prices(core_text)

            if prev is not None:
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
                elif "text" in prev:
                    # Цена не менялась — проверяем сам текст карточки,
                    # но только если отличия существенные (не техническая мелочь).
                    similarity = text_similarity(prev["text"], core_text)
                    if similarity < CONTENT_SIMILARITY_THRESHOLD:
                        changes_report.append(f"✏️ Изменение текста карточки: {url}")
                        snippet = diff_snippet(prev["text"], core_text)
                        if snippet:
                            changes_report.append(f"   {snippet}")

            save_json(page_state_path, {"text": core_text, "prices": prices})
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
