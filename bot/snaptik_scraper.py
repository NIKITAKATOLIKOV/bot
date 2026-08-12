"""
Скрапер snaptik.net.

ВАЖНО: этот код основан на типичной схеме работы клонов snaptik и может
перестать работать в любой момент, если они поменяют фронтенд/токены.
Проверь и при необходимости подправь селекторы/эндпоинты через DevTools
(вкладка Network -> Fetch/XHR) на реальном сайте, если что-то сломается.

Схема:
1. GET https://snaptik.net/en  -> достаём скрытый token из формы.
2. POST https://snaptik.net/abc2.php с url и token -> получаем ответ,
   который обычно завёрнут в eval-packed JS (Dean Edwards packer).
3. Распаковываем packer -> получаем HTML с ссылками на скачивание.
4. Парсим ссылки из HTML через BeautifulSoup.
"""

import re
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://snaptik.net"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    ),
    "Referer": f"{BASE_URL}/en",
}


def unpack_js(packed: str) -> str:
    """
    Минимальный распаковщик для Dean Edwards JS Packer
    (eval(function(p,a,c,k,e,d){...}('...',36,N,'a|b|c'.split('|'),0,{})))
    Многие "downloader" сайты используют именно этот паковщик.
    """
    match = re.search(
        r"eval\(function\(p,a,c,k,e,d\).*?\)\((.*)\)\s*$",
        packed,
        re.DOTALL,
    )
    if not match:
        # возможно ответ уже не запакован — вернём как есть
        return packed

    args = match.group(1)
    # args выглядит примерно как: 'p_string',36,c,'k|k|k'.split('|'),0,{}
    parts = re.match(
        r"'(?P<p>(?:[^'\\]|\\.)*)',\s*\d+,\s*(?P<c>\d+),\s*'(?P<k>(?:[^'\\]|\\.)*)'\.split\('\|'\)",
        args,
        re.DOTALL,
    )
    if not parts:
        raise ValueError("Не удалось распарсить packer — формат ответа изменился")

    p = parts.group("p").encode().decode("unicode_escape")
    c = int(parts.group("c"))
    k = parts.group("k").split("|")

    def base36(n: int) -> str:
        digits = "0123456789abcdefghijklmnopqrstuvwxyz"
        if n == 0:
            return "0"
        out = ""
        while n:
            n, r = divmod(n, 36)
            out = digits[r] + out
        return out

    for i in range(c - 1, -1, -1):
        if i < len(k) and k[i]:
            p = re.sub(r"\b" + base36(i) + r"\b", k[i], p)

    return p


def get_token(session: requests.Session) -> str:
    resp = session.get(f"{BASE_URL}/en", headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    token_input = soup.find("input", {"name": "token"})
    if not token_input or not token_input.get("value"):
        raise RuntimeError("Не нашёл token на странице — верстка сайта изменилась")
    return token_input["value"]


def fetch_download_links(tiktok_url: str) -> list[dict]:
    """
    Возвращает список найденных ссылок на скачивание:
    [{"quality": "...", "url": "..."}]
    """
    session = requests.Session()
    token = get_token(session)

    resp = session.post(
        f"{BASE_URL}/abc2.php",
        headers=HEADERS,
        data={"url": tiktok_url, "lang": "en", "token": token},
        timeout=20,
    )
    resp.raise_for_status()

    html = resp.text
    if "eval(function(p,a,c,k,e,d)" in html:
        html = unpack_js(html)

    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("http") and ("download" in a.get("class", []) or ".mp4" in href):
            links.append({"quality": a.get_text(strip=True) or "video", "url": href})

    if not links:
        raise RuntimeError(
            "Ссылок на видео не нашлось — либо сайт поменял разметку, "
            "либо сработала защита (капча/rate-limit)."
        )
    return links


if __name__ == "__main__":
    test_url = "https://www.tiktok.com/@example/video/1234567890"
    for link in fetch_download_links(test_url):
        print(link["quality"], "->", link["url"])
