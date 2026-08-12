"""
Скрапер snaptik.net — версия по реальному запросу (подсмотрено через DevTools).

Реальный эндпоинт: POST https://snaptik.net/api/ajaxSearch
Тело: q=<ссылка на tiktok>&lang=en
Ответ: JSON с полем "data", в котором лежит HTML с кнопками скачивания,
одна из которых ведёт на https://dl.snapcdn.app/get?token=<JWT> — это уже
прямая ссылка на видео.

ВАЖНО: сайт защищён Cloudflare (виден cf_clearance в куках браузера).
Обычный requests, скорее всего, получит 403 без прохождения JS-проверки
Cloudflare. Поэтому используем cloudscraper — библиотеку, которая умеет
автоматически обходить эту защиту (эмулирует то, что делает браузер).

Установка: pip install cloudscraper beautifulsoup4
"""

import re
import json
import cloudscraper
from bs4 import BeautifulSoup

BASE_URL = "https://snaptik.net"

HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
    "origin": BASE_URL,
    "referer": f"{BASE_URL}/en",
    "x-requested-with": "XMLHttpRequest",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
}


def get_scraper():
    """
    Создаёт cloudscraper-сессию, которая сама пройдёт Cloudflare-проверку
    при первом обращении к сайту.
    """
    return cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "darwin", "mobile": False}
    )


def fetch_download_links(tiktok_url: str) -> list[dict]:
    """
    Возвращает список найденных ссылок на скачивание:
    [{"quality": "...", "url": "..."}]
    """
    scraper = get_scraper()

    # заходим на главную, чтобы получить куки (в т.ч. пройти Cloudflare)
    scraper.get(f"{BASE_URL}/en", headers=HEADERS, timeout=30)

    resp = scraper.post(
        f"{BASE_URL}/api/ajaxSearch",
        headers=HEADERS,
        data={"q": tiktok_url, "lang": "en"},
        timeout=30,
    )
    resp.raise_for_status()

    try:
        payload = resp.json()
    except json.JSONDecodeError:
        raise RuntimeError(
            f"Ответ не в JSON (возможно, заблокировал Cloudflare): {resp.text[:300]}"
        )

    html = payload.get("data")
    if not html:
        raise RuntimeError(f"В ответе нет поля 'data': {payload}")

    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        if "dl.snapcdn.app" in href or "download" in " ".join(a.get("class", [])):
            links.append({"quality": text or "video", "url": href})

    if not links:
        raise RuntimeError(
            "Ссылок на видео не нашлось в ответе — либо изменился формат "
            "ответа, либо видео недоступно."
        )
    return links


if __name__ == "__main__":
    test_url = "https://www.tiktok.com/@theeditsf_/video/7673107848942849302"
    for link in fetch_download_links(test_url):
        print(link["quality"], "->", link["url"])
