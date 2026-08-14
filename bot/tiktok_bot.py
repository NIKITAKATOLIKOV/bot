import logging
import os
import re
import tempfile

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from snaptik_scraper import SnapTikClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")

REQUIRED_CHANNEL = "@thefencemusic"
REQUIRED_CHANNEL_URL = "https://t.me/thefencemusic"


async def is_subscribed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if update.effective_user is None:
        return False

    try:
        member = await context.bot.get_chat_member(
            chat_id=REQUIRED_CHANNEL,
            user_id=update.effective_user.id,
        )
        return member.status in {"member", "administrator", "creator"}
    except Exception:
        logger.exception(
            "Не удалось проверить подписку пользователя %s на %s",
            update.effective_user.id,
            REQUIRED_CHANNEL,
        )
        return False


async def require_subscription(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    if await is_subscribed(update, context):
        return True

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Подписаться на канал", url=REQUIRED_CHANNEL_URL)]]
    )
    await update.message.reply_text(
        "Чтобы пользоваться ботом, подпишись на @thefencemusic, "
        "а потом отправь ссылку ещё раз.",
        reply_markup=keyboard,
    )
    return False



# URL второго Railway-сервиса с Local Telegram Bot API.
# Пример: https://telegram-api-production-xxxx.up.railway.app
BOT_API_SERVER_URL = os.environ.get("TELEGRAM_BOT_API_URL", "").strip().rstrip("/")

# Local Bot API поддерживает загрузку до 2000 MB. Оставляем небольшой запас.
MAX_TELEGRAM_BYTES = 1950 * 1024 * 1024

TIKTOK_RE = re.compile(
    r"https?://(?:www\.|m\.|vm\.|vt\.)?tiktok\.com/\S+",
    re.I,
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_subscription(update, context):
        return

    mode = "до ~1.95 ГБ" if BOT_API_SERVER_URL else "до ~50 МБ (обычный Telegram API)"
    await update.message.reply_text(
        "Привет! Кинь ссылку на TikTok — скачаю через SnapTik "
        f"в максимальном доступном качестве.\nРежим отправки: {mode}."
    )


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Твой Telegram ID: {update.effective_user.id}"
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
        timeout=(20, 180),
    ) as response:
        response.raise_for_status()

        content_type = response.headers.get("content-type", "").lower()
        if "text/html" in content_type and "video" not in content_type:
            raise RuntimeError(f"вместо видео сервер вернул HTML ({content_type})")

        content_length = response.headers.get("content-length")
        if content_length:
            try:
                announced_size = int(content_length)
                if announced_size > MAX_TELEGRAM_BYTES:
                    raise RuntimeError(
                        f"файл слишком большой: {announced_size / 1024 / 1024:.1f} MiB "
                        "(лимит этой конфигурации ~1950 MiB)"
                    )
            except ValueError:
                pass

        total = 0
        with open(filepath, "wb") as f:
            for chunk in response.iter_content(chunk_size=4 * 1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_TELEGRAM_BYTES:
                    raise RuntimeError(
                        "файл превысил лимит этой конфигурации (~1950 MiB)"
                    )
                f.write(chunk)

    if total < 1024:
        raise RuntimeError(f"слишком маленький файл: {total} байт")

    return total


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_subscription(update, context):
        return

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
                size_mib = size / 1024 / 1024

                logger.info(
                    "Downloaded candidate %d: %.2f MiB, label=%s",
                    index,
                    size_mib,
                    candidate.label,
                )

                # Без Local Bot API оставляем понятную ошибку вместо долгой
                # попытки отправить огромный файл через api.telegram.org.
                if not BOT_API_SERVER_URL and size > 49 * 1024 * 1024:
                    await status.edit_text(
                        f"Видео скачано: {size_mib:.1f} MiB, но большой Telegram API "
                        "ещё не подключён. Добавь TELEGRAM_BOT_API_URL."
                    )
                    return

                await status.edit_text(
                    f"Видео скачано: {size_mib:.1f} MiB. Отправляю в Telegram…"
                )

                with open(filepath, "rb") as video_file:
                    await update.message.reply_document(
                        document=video_file,
                        filename="tiktok.mp4",
                        caption=f"SnapTik: {size_mib:.2f} MiB",
                        read_timeout=3600,
                        write_timeout=3600,
                        connect_timeout=60,
                        pool_timeout=60,
                    )

                await status.delete()
                return

            except Exception as e:
                last_error = e
                logger.warning(
                    "Candidate %d failed: %s",
                    index,
                    e,
                    exc_info=True,
                )
                try:
                    if os.path.exists(filepath):
                        os.remove(filepath)
                except OSError:
                    pass

        await status.edit_text(
            "SnapTik нашёл варианты, но ни один не удалось скачать/отправить 😔\n"
            f"Последняя ошибка: {last_error}"
        )


def build_app():
    if not BOT_TOKEN:
        raise RuntimeError("Не задана переменная окружения TG_BOT_TOKEN")

    builder = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .media_write_timeout(3600)
    )

    if BOT_API_SERVER_URL:
        # В Railway бот и Bot API работают в разных контейнерах.
        # Поэтому local_mode=False: файл передаётся по HTTP, а не как file:// путь.
        builder = (
            builder
            .base_url(f"{BOT_API_SERVER_URL}/bot")
            .base_file_url(f"{BOT_API_SERVER_URL}/file/bot")
            .local_mode(False)
        )
        logger.info("Using Local Telegram Bot API: %s", BOT_API_SERVER_URL)
    else:
        logger.warning(
            "TELEGRAM_BOT_API_URL is not set; using api.telegram.org "
            "and large uploads will not work."
        )

    return builder.build()


def main():
    app = build_app()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", show_id))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()


if __name__ == "__main__":
    main()
