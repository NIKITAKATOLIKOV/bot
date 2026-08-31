import asyncio
import logging
import os
import re
import tempfile

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
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


def has_confirmed_subscription(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return bool(context.user_data.get("subscription_confirmed"))


async def ask_for_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Подписаться на канал ❤️‍🔥", url=REQUIRED_CHANNEL_URL)],
            [InlineKeyboardButton("Я подписался ✅", callback_data="subscription_confirmed")],
        ]
    )

    await update.effective_message.reply_text(
        "Чтобы пользоваться ботом, подпишись на @thefencemusic ❤️‍🔥",
        reply_markup=keyboard,
    )


async def require_subscription(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    if has_confirmed_subscription(context):
        return True

    await ask_for_subscription(update, context)
    return False


async def confirm_subscription(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    context.user_data["subscription_confirmed"] = True

    await query.edit_message_text(
        "Готово ❤️‍🔥 Теперь кидай ссылку на TikTok."
    )


# URL второго Railway-сервиса с Local Telegram Bot API.
# Пример: https://telegram-api-production-xxxx.up.railway.app
BOT_API_SERVER_URL = os.environ.get("TELEGRAM_BOT_API_URL", "").strip().rstrip("/")

# Railway-сервис bot ограничен 1 GB RAM (см. Replica Limits). python-telegram-bot
# по умолчанию читает файл целиком в память при отправке, поэтому реальный лимит
# файла должен быть в разы меньше объёма RAM, а не близко к максимуму Bot API.
MAX_TELEGRAM_BYTES = 520 * 1024 * 1024

# Не даём двум скачиваниям/отправкам идти одновременно — на 1 GB RAM параллельные
# запросы легко удваивают/утраивают пиковое потребление памяти и валят контейнер.
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(1)

# Один клиент на процесс: переиспользуем сессию requests вместо создания новой
# на каждое сообщение.
SNAPTIK_CLIENT = SnapTikClient()

TIKTOK_RE = re.compile(
    r"https?://(?:www\.|m\.|vm\.|vt\.)?tiktok\.com/\S+",
    re.I,
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_subscription(update, context):
        return

    limit_mib = MAX_TELEGRAM_BYTES / 1024 / 1024
    mode = f"до ~{limit_mib:.0f} МБ" if BOT_API_SERVER_URL else "до ~50 МБ (обычный Telegram API)"
    await update.message.reply_text(
        "Привет! Кинь ссылку на TikTok — скачаю его в HD и пришлю тебе "
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
                        f"(лимит этой конфигурации ~{MAX_TELEGRAM_BYTES / 1024 / 1024:.0f} MiB)"
                    )
            except ValueError:
                pass

        total = 0
        with open(filepath, "wb") as f:
            for chunk in response.iter_content(chunk_size=1 * 1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_TELEGRAM_BYTES:
                    raise RuntimeError(
                        f"файл превысил лимит этой конфигурации (~{MAX_TELEGRAM_BYTES / 1024 / 1024:.0f} MiB)"
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

    status = await update.message.reply_text("Запрашиваю варианты качества у найнмайса…")
    client = SNAPTIK_CLIENT

    async with DOWNLOAD_SEMAPHORE:
        await _process_tiktok_link(update, status, client, tiktok_url)


async def _process_tiktok_link(update, status, client, tiktok_url):
    try:
        links = client.resolve(tiktok_url)
    except Exception as e:
        logger.exception("Найнмайс resolve failed")
        await status.edit_text(f"Найнмайс не отдал ссылку 😔\n{e}")
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
                    # read_file_handle=False: файл читается потоково сетевым
                    # бэкендом при отправке, а не загружается целиком в RAM.
                    input_file = InputFile(
                        video_file,
                        filename="tiktok.mp4",
                        read_file_handle=False,
                    )
                    await update.message.reply_document(
                        document=input_file,
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
    app.add_handler(
        CallbackQueryHandler(confirm_subscription, pattern="^subscription_confirmed$")
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()


if __name__ == "__main__":
    main()
