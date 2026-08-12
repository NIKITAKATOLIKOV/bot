import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://snaptik.net"
LANDING_URL = f"{BASE_URL}/en"
AJAX_URL = f"{BASE_URL}/api/ajaxSearch"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass
class DownloadLink:
    url: str
    label: str = "video"
    quality_score: int = 0


def _base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if n == 0:
        return "0"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out


def unpack_js(packed: str) -> str:
    """Best-effort decoder for the Dean Edwards JS packer."""
    if "eval(function(p,a,c,k,e" not in packed:
        return packed

    match = re.search(
        r"eval\(function\(p,a,c,k,e,(?:d|r)\).*?\)\(\s*'(?P<p>(?:[^'\\]|\\.)*)'\s*,\s*(?P<a>\d+)\s*,\s*(?P<c>\d+)\s*,\s*'(?P<k>(?:[^'\\]|\\.)*)'\.split\('\|'\)",
        packed,
        re.DOTALL,
    )
    if not match:
        return packed

    payload = bytes(match.group("p"), "utf-8").decode("unicode_escape")
    count = int(match.group("c"))
    keys = match.group("k").split("|")

    for i in range(count - 1, -1, -1):
        if i < len(keys) and keys[i]:
            payload = re.sub(r"\b" + re.escape(_base36(i)) + r"\b", keys[i], payload)
    return payload


def _extract_htmlish_text(response: requests.Response) -> str:
    text = response.text
    content_type = response.headers.get("content-type", "").lower()

    if "json" in content_type or text.lstrip().startswith(("{", "[")):
        try:
            obj = response.json()
        except ValueError:
            obj = None

        if obj is not None:
            candidates: list[str] = []

            def walk(value):
                if isinstance(value, str):
                    candidates.append(value)
                elif isinstance(value, dict):
                    for key in ("result", "data", "html", "content"):
                        if key in value:
                            walk(value[key])
                    for key, child in value.items():
                        if key not in {"result", "data", "html", "content"}:
                            walk(child)
                elif isinstance(value, list):
                    for child in value:
                        walk(child)

            walk(obj)
            if candidates:
                text = max(candidates, key=len)

    return unpack_js(text)


def _score_quality(label: str, attrs: dict | None = None) -> int:
    attrs = attrs or {}
    blob = " ".join([label] + [str(v) for v in attrs.values()]).lower()

    score = 0
    resolutions = [
        int(x)
        for x in re.findall(
            r"(?<!\d)(2160|1440|1080|720|540|480|360)(?:p)?(?!\d)", blob
        )
    ]
    if resolutions:
        score += max(resolutions) * 100

    if "4k" in blob:
        score += 2160 * 100
    if "2k" in blob:
        score += 1440 * 100
    if "full hd" in blob or "fullhd" in blob or "fhd" in blob:
        score += 1080 * 100
    if re.search(r"\bhd\b", blob):
        score += 720 * 100

    if "mp4" in blob or "video" in blob:
        score += 10_000
    if "mp3" in blob or "audio" in blob or "music" in blob:
        score -= 100_000
    if "watermark" in blob and "no watermark" not in blob and "without watermark" not in blob:
        score -= 5_000

    return score


def _find_download_links(html: str, base_url: str) -> list[DownloadLink]:
    soup = BeautifulSoup(html, "html.parser")
    found: list[DownloadLink] = []
    seen: set[str] = set()

    def add(raw_url: str | None, label: str, attrs: dict | None = None):
        if not raw_url:
            return
        raw_url = raw_url.strip().replace("&amp;", "&")
        if raw_url.startswith(("javascript:", "#", "data:")):
            return

        url = urljoin(base_url, raw_url)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return

        blob = f"{label} {raw_url}".lower()
        if any(x in blob for x in ("privacy", "terms", "about-us", ".css", ".js", ".png", ".jpg", ".svg")):
            return
        if "mp3" in blob and "mp4" not in blob:
            return

        if url in seen:
            return
        seen.add(url)
        found.append(
            DownloadLink(
                url=url,
                label=label or "video",
                quality_score=_score_quality(label, attrs),
            )
        )

    for a in soup.find_all("a"):
        attrs = {k: " ".join(v) if isinstance(v, list) else v for k, v in a.attrs.items()}
        label = a.get_text(" ", strip=True)
        for key in ("href", "data-url", "data-href", "data-download"):
            add(a.get(key), label, attrs)

    for tag in soup.find_all(["button", "source", "video"]):
        attrs = {k: " ".join(v) if isinstance(v, list) else v for k, v in tag.attrs.items()}
        label = tag.get_text(" ", strip=True)
        for key in ("src", "data-url", "data-href", "data-download"):
            add(tag.get(key), label, attrs)

    # SnapTik responses sometimes contain direct URLs inside JS/JSON rather than anchors.
    for match in re.findall(r"https?://[^\s'\"<>\\]+", html):
        clean = match.rstrip(");,]")
        low = clean.lower()
        if any(token in low for token in (".mp4", "download", "video", "tiktokcdn", "byteoversea")):
            add(clean, "embedded video", {})

    found.sort(key=lambda x: x.quality_score, reverse=True)
    return found


class SnapTikClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def resolve(self, tiktok_url: str) -> list[DownloadLink]:
        # First visit mirrors a normal browser session and lets the server set
        # ordinary cookies. We do NOT hard-code cf_clearance or copied browser cookies.
        try:
            landing = self.session.get(
                LANDING_URL,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
                timeout=20,
            )
            logger.info("SnapTik landing: HTTP %s", landing.status_code)
        except requests.RequestException as exc:
            logger.warning("SnapTik landing request failed: %s", exc)

        headers = {
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": BASE_URL,
            "Referer": LANDING_URL,
            "X-Requested-With": "XMLHttpRequest",
        }
        payload = {"q": tiktok_url, "lang": "en"}

        logger.info("SnapTik submit: POST %s fields=%s", AJAX_URL, list(payload.keys()))
        response = self.session.post(
            AJAX_URL,
            headers=headers,
            data=payload,
            timeout=35,
        )

        if response.status_code in {403, 429, 503}:
            preview = re.sub(r"\s+", " ", response.text)[:180]
            raise RuntimeError(
                f"SnapTik/Cloudflare заблокировал серверный запрос (HTTP {response.status_code}). "
                f"Ответ: {preview!r}"
            )

        response.raise_for_status()
        logger.info(
            "SnapTik ajax: HTTP %s content-type=%s bytes=%s",
            response.status_code,
            response.headers.get("content-type", ""),
            len(response.content),
        )

        html = _extract_htmlish_text(response)
        links = _find_download_links(html, response.url)

        if not links:
            preview = re.sub(r"\s+", " ", html)[:500]
            raise RuntimeError(
                "SnapTik ответил, но ссылки на видео не найдены. "
                f"Фрагмент ответа: {preview!r}"
            )

        logger.info(
            "SnapTik links: %s",
            [(x.label, x.quality_score, x.url[:120]) for x in links[:8]],
        )
        return links
