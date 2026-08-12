import os
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
from snaptik_scraper import fetch_download_links

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "ВСТАВЬ_СЮДА_ТОКЕН_ОТ_BOTFATHER")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Кинь мне ссылку на TikTok видео — скачаю и пришлю сюда."
    )


def is_tiktok_link(text: str) -> bool:
    text = text.lower()
    return "tiktok.com" in text or "vm.tiktok" in text


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if not is_tiktok_link(text):
        await update.message.reply_text("Это не похоже на ссылку TikTok 🤔")
        return

    status_msg = await update.message.reply_text("Ищу ссылку на видео через snaptik...")

    try:
        links = fetch_download_links(text)
        # берём первую найденную ссылку (обычно это самое высокое качество без вотемарки)
        video_url = links[0]["url"]
    except Exception as e:
        logger.exception("Ошибка получения ссылки со snaptik")
        await status_msg.edit_text(f"Не получилось получить ссылку со snaptik 😔\n{e}")
        return

    await status_msg.edit_text("Качаю видео...")

    with tempfile.TemporaryDirectory() as tmp_dir:
        filepath = os.path.join(tmp_dir, "video.mp4")

        try:
            with requests.get(video_url, headers={"User-Agent": "Mozilla/5.0"}, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(filepath, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        f.write(chunk)
        except Exception as e:
            logger.exception("Ошибка скачивания файла")
            await status_msg.edit_text(f"Не получилось скачать видео 😔\n{e}")
            return

        try:
            # Telegram бот-API ограничивает файлы 50 МБ на отправку через send_video
            file_size = os.path.getsize(filepath)
            if file_size > 49 * 1024 * 1024:
                await status_msg.edit_text(
                    "Видео слишком большое для отправки через бота (>50МБ)."
                )
                return

            with open(filepath, "rb") as video_file:
                await update.message.reply_video(video=video_file)
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
