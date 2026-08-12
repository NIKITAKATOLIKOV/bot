import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://snaptik.net"
LANDING_URL = f"{BASE_URL}/en"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": LANDING_URL,
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
    """Best-effort decoder for the common Dean Edwards JS packer."""
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
    base = int(match.group("a"))
    count = int(match.group("c"))
    keys = match.group("k").split("|")

    # Snaptik-like packers normally use base36. Keep a graceful fallback.
    if base != 36:
        logger.warning("Unexpected JS packer base=%s; trying base36 replacement", base)

    for i in range(count - 1, -1, -1):
        if i < len(keys) and keys[i]:
            payload = re.sub(r"\b" + re.escape(_base36(i)) + r"\b", keys[i], payload)
    return payload


def _extract_htmlish_text(response: requests.Response) -> str:
    text = response.text

    # Some downloader sites return JSON containing the HTML in result/data/html.
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
                    # Favor common payload keys first.
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
    # Prefer explicit resolution labels.
    resolutions = [int(x) for x in re.findall(r"(?<!\d)(2160|1440|1080|720|540|480|360)(?:p)?(?!\d)", blob)]
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

    # Prefer MP4 video over audio and generic buttons.
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
        # Exclude obvious navigation/assets while allowing signed download endpoints.
        if any(x in blob for x in ("privacy", "terms", "about-us", ".css", ".js", ".png", ".jpg", ".svg")):
            return
        if "mp3" in blob and "mp4" not in blob:
            return

        if url in seen:
            return
        seen.add(url)
        found.append(DownloadLink(url=url, label=label or "video", quality_score=_score_quality(label, attrs)))

    for a in soup.find_all("a"):
        attrs = {k: " ".join(v) if isinstance(v, list) else v for k, v in a.attrs.items()}
        label = a.get_text(" ", strip=True)
        for key in ("href", "data-url", "data-href", "data-download"):
            add(a.get(key), label, attrs)

    # Also inspect common data attributes/buttons generated by JS.
    for tag in soup.find_all(["button", "source", "video"]):
        attrs = {k: " ".join(v) if isinstance(v, list) else v for k, v in tag.attrs.items()}
        label = tag.get_text(" ", strip=True)
        for key in ("src", "data-url", "data-href", "data-download"):
            add(tag.get(key), label, attrs)

    # Last-resort extraction for direct URLs embedded in unpacked JS/JSON.
    for match in re.findall(r"https?://[^\s'\"<>\\]+", html):
        clean = match.rstrip(");,]")
        low = clean.lower()
        if any(token in low for token in (".mp4", "download", "video")):
            add(clean, "embedded video", {})

    found.sort(key=lambda x: x.quality_score, reverse=True)
    return found


class SnapTikClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def _load_form(self):
        response = self.session.get(LANDING_URL, timeout=20)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        # Find the form that actually accepts a TikTok URL.
        form = None
        for candidate in soup.find_all("form"):
            if candidate.find("input", {"name": "url"}) or candidate.find("input", {"type": "url"}):
                form = candidate
                break

        if form is None:
            raise RuntimeError("SnapTik: не нашёл форму загрузки на странице /en")

        action = form.get("action") or LANDING_URL
        endpoint = urljoin(response.url, action)
        method = (form.get("method") or "post").lower()

        fields: dict[str, str] = {}
        for inp in form.find_all("input"):
            name = inp.get("name")
            if not name:
                continue
            input_type = (inp.get("type") or "text").lower()
            if input_type in {"submit", "button", "file"}:
                continue
            fields[name] = inp.get("value") or ""

        return response.url, endpoint, method, fields

    def resolve(self, tiktok_url: str) -> list[DownloadLink]:
        page_url, endpoint, method, fields = self._load_form()

        # Preserve all hidden fields/tokens exactly as emitted by SnapTik.
        fields["url"] = tiktok_url
        fields.setdefault("lang", "en")

        headers = {
            "Referer": page_url,
            "Origin": f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}",
            "X-Requested-With": "XMLHttpRequest",
        }

        logger.info("SnapTik submit: %s %s fields=%s", method.upper(), endpoint, list(fields.keys()))

        if method == "get":
            response = self.session.get(endpoint, params=fields, headers=headers, timeout=30)
        else:
            response = self.session.post(endpoint, data=fields, headers=headers, timeout=30)
        response.raise_for_status()

        html = _extract_htmlish_text(response)
        links = _find_download_links(html, response.url)

        if not links:
            preview = re.sub(r"\s+", " ", html)[:300]
            raise RuntimeError(
                "SnapTik ответил, но ссылки на видео не найдены. "
                f"Возможно, изменился frontend или сработала защита. Ответ: {preview!r}"
            )

        logger.info("SnapTik links: %s", [(x.label, x.quality_score, x.url[:100]) for x in links[:8]])
        return links
