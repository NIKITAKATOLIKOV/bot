import os
import re
import json
import logging
import tempfile
import requests
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "ВСТАВЬ_СЮДА_ТОКЕН_ОТ_BOTFATHER")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    ),
    "Referer": "https://www.tiktok.com/",
    "Accept-Language": "en-US,en;q=0.9",
}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Кинь мне ссылку на TikTok видео — скачаю и пришлю сюда."
    )


def is_tiktok_link(text: str) -> bool:
    text = text.lower()
    return "tiktok.com" in text or "vm.tiktok" in text


def resolve_full_url(session: requests.Session, tiktok_url: str) -> str:
    resp = session.get(
        tiktok_url, headers=HEADERS, allow_redirects=True, timeout=20
    )
    return resp.url


def extract_video_json(html: str) -> dict:
    """
    Достаёт JSON с данными видео из HTML-страницы TikTok.
    Пробуем оба варианта названия script-тега, т.к. TikTok их менял.
    """
    patterns = [
        r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
        r'<script id="SIGI_STATE"[^>]*>(.*?)</script>',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
    raise RuntimeError("Не нашёл JSON с данными видео на странице TikTok")


def find_video_node(data: dict) -> dict:
    """
    Ищет узел с полем 'bitrateInfo' в любом месте JSON-дерева —
    структура немного отличается между __UNIVERSAL_DATA__ и SIGI_STATE.
    """
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if "bitrateInfo" in node or ("playAddr" in node and "downloadAddr" in node):
                return node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    raise RuntimeError("Не нашёл информацию о видео (bitrateInfo/playAddr) в JSON")


def get_hd_video_url(session: requests.Session, tiktok_url: str) -> str:
    full_url = resolve_full_url(session, tiktok_url)
    logger.info("Полный URL: %s", full_url)

    resp = session.get(full_url, headers=HEADERS, timeout=20)
    resp.raise_for_status()

    data = extract_video_json(resp.text)
    video = find_video_node(data)

    bitrate_info = video.get("bitrateInfo") or []
    candidates = []
    for b in bitrate_info:
        play_addr = b.get("PlayAddr") or {}
        url_list = play_addr.get("UrlList") or []
        size = play_addr.get("DataSize") or 0
        if url_list:
            candidates.append((int(size), url_list[0]))

    # запасной вариант, если bitrateInfo пуст
    if not candidates:
        for key in ("downloadAddr", "playAddr"):
            if video.get(key):
                candidates.append((0, video[key]))

    if not candidates:
        raise RuntimeError("Не нашёл ни одной ссылки на видео")

    best_size, video_url = max(candidates, key=lambda c: c[0])
    logger.info(
        "Выбран вариант %.2f MiB из %d доступных: %s",
        best_size / 1024 / 1024, len(candidates), video_url,
    )
    return video_url.replace("&amp;", "&")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if not is_tiktok_link(text):
        await update.message.reply_text("Это не похоже на ссылку TikTok 🤔")
        return

    status_msg = await update.message.reply_text("Ищу лучшую версию видео...")

    session = requests.Session()

    try:
        video_url = get_hd_video_url(session, text)
    except Exception as e:
        logger.exception("Ошибка получения ссылки")
        await status_msg.edit_text(f"Не получилось получить ссылку 😔\n{e}")
        return

    await status_msg.edit_text("Качаю видео...")

    with tempfile.TemporaryDirectory() as tmp_dir:
        filepath = os.path.join(tmp_dir, "video.mp4")

        download_headers = dict(HEADERS)
        download_headers["Range"] = "bytes=0-"

        try:
            with session.get(
                video_url, headers=download_headers, stream=True, timeout=60
            ) as r:
                r.raise_for_status()
                with open(filepath, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        f.write(chunk)
        except Exception as e:
            logger.exception("Ошибка скачивания файла")
            await status_msg.edit_text(f"Не получилось скачать видео 😔\n{e}")
            return

        try:
            file_size = os.path.getsize(filepath)
            logger.info("Скачанный файл: %.2f MiB", file_size / 1024 / 1024)

            if file_size > 49 * 1024 * 1024:
                await status_msg.edit_text(
                    "Видео слишком большое для отправки через бота (>50МБ)."
                )
                return

            with open(filepath, "rb") as video_file:
                await update.message.reply_document(document=video_file)
            await status_msg.delete()
        except Exception as e:
            logger.exception("Ошибка отправки")
            await status_msg.edit_text(f"Скачал, но не смог отправить: {e}")


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()


if __name__ == "__main__":
    main()
