import os
import logging
import tempfile
import yt_dlp
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

    status_msg = await update.message.reply_text("Качаю видео...")

    with tempfile.TemporaryDirectory() as tmp_dir:
        out_template = os.path.join(tmp_dir, "%(id)s.%(ext)s")
        ydl_opts = {
            "outtmpl": out_template,
            "format": "best",
            "format_sort": ["res", "fps", "vcodec:h264", "quality"],
            "quiet": True,
            "no_warnings": True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(text, download=True)
                filepath = ydl.prepare_filename(info)
        except Exception as e:
            logger.exception("Ошибка скачивания")
            await status_msg.edit_text(f"Не получилось скачать видео 😔\n{e}")
            return

        try:
            file_size = os.path.getsize(filepath)
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
