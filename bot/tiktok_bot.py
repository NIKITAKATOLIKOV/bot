import logging
import os
import re
import tempfile

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from snaptik_scraper import SnapTikClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
MAX_TELEGRAM_BYTES = 49 * 1024 * 1024

TIKTOK_RE = re.compile(r"https?://(?:www\.|m\.|vm\.|vt\.)?tiktok\.com/\S+", re.I)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Кинь ссылку на TikTok — попробую скачать через SnapTik в максимальном доступном качестве."
    )


def extract_tiktok_link(text: str) -> str | None:
    match = TIKTOK_RE.search(text or "")
    return match.group(0).rstrip(").,]}>\"'") if match else None


def download_candidate(client: SnapTikClient, url: str, filepath: str) -> int:
    headers = {
        "Accept": "video/mp4,application/octet-stream,*/*;q=0.8",
        "Range": "bytes=0-",
        "Referer": "https://snaptik.net/en",
    }

    with client.session.get(
        url,
        headers=headers,
        stream=True,
        allow_redirects=True,
        timeout=90,
    ) as response:
        response.raise_for_status()

        content_type = response.headers.get("content-type", "").lower()
        if "text/html" in content_type and "video" not in content_type:
            raise RuntimeError(f"вместо видео сервер вернул HTML ({content_type})")

        total = 0
        with open(filepath, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_TELEGRAM_BYTES:
                    raise RuntimeError("файл больше лимита Telegram-бота (~50 МБ)")
                f.write(chunk)

    if total < 1024:
        raise RuntimeError(f"слишком маленький файл: {total} байт")
    return total


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tiktok_url = extract_tiktok_link(update.message.text or "")
    if not tiktok_url:
        await update.message.reply_text("Это не похоже на ссылку TikTok 🤔")
        return

    status = await update.message.reply_text("Запрашиваю варианты качества у SnapTik…")
    client = SnapTikClient()

    try:
        links = client.resolve(tiktok_url)
    except Exception as e:
        logger.exception("SnapTik resolve failed")
        await status.edit_text(f"SnapTik не отдал ссылку 😔\n{e}")
        return

    with tempfile.TemporaryDirectory() as tmp_dir:
        filepath = os.path.join(tmp_dir, "tiktok.mp4")
        last_error = None

        for index, candidate in enumerate(links[:8], start=1):
            try:
                await status.edit_text(
                    f"Пробую вариант {index}/{min(len(links), 8)}: "
                    f"{candidate.label[:70] or 'video'}"
                )
                size = download_candidate(client, candidate.url, filepath)
                logger.info(
                    "Downloaded candidate %d: %.2f MiB, label=%s",
                    index,
                    size / 1024 / 1024,
                    candidate.label,
                )

                with open(filepath, "rb") as video_file:
                    await update.message.reply_document(
                        document=video_file,
                        filename="tiktok.mp4",
                        caption=f"SnapTik: {size / 1024 / 1024:.2f} MiB",
                    )
                await status.delete()
                return
            except Exception as e:
                last_error = e
                logger.warning("Candidate %d failed: %s", index, e, exc_info=True)
                try:
                    if os.path.exists(filepath):
                        os.remove(filepath)
                except OSError:
                    pass

        await status.edit_text(
            "SnapTik нашёл варианты, но ни один не удалось скачать 😔\n"
            f"Последняя ошибка: {last_error}"
        )


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задана переменная окружения TG_BOT_TOKEN")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()


if __name__ == "__main__":
    main()
