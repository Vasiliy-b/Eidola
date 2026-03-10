"""Telegram bot message handlers for content intake.

Handles: single photo, video, album (media group), text captions,
content type selection, and confirmation flow.
"""

import logging
import uuid
from datetime import datetime
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..config import settings
from ..content.models import (
    CONTENT_TYPE_TO_FLOW,
    ContentItem,
    ContentType,
    MediaFile,
    PostingFlow,
    UniqualizationStatus,
    UploadedBy,
)
from ..content.mongo_content import ContentStore
from .keyboards import cancel_keyboard, confirm_keyboard, content_type_keyboard
from .states import ContentEdit, ContentUpload

logger = logging.getLogger("eidola.bot.handlers")

router = Router()

ORIGINALS_DIR = Path(settings.content_dir).resolve() / "originals"

_store: ContentStore | None = None


def get_store() -> ContentStore:
    global _store
    if _store is None:
        _store = ContentStore()
    return _store


def _generate_content_id() -> str:
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    short = uuid.uuid4().hex[:6]
    return f"cnt_{ts}_{short}"


async def _download_file(bot: Bot, file_id: str, dest_path: str) -> None:
    """Download a file from Telegram to local storage."""
    file = await bot.get_file(file_id)
    if file.file_path:
        await bot.download_file(file.file_path, dest_path)


# --- /start and /help ---

@router.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я бот для загрузки контента.\n\n"
        "Отправь мне:\n"
        "- Фото — пост с одной фото\n"
        "- Несколько фото (альбом) — карусель\n"
        "- Видео — пост/Reel/Story\n\n"
        "Команды:\n"
        "/status — статус контента\n"
        "/queue — очередь на сегодня\n"
        "/list — последний контент\n"
        "/cancel <ID> — отменить контент\n"
        "/edit <ID> — изменить текст (до обработки)"
    )


@router.message(Command("status"))
async def cmd_status(message: Message):
    store = get_store()
    stats = store.get_content_stats()
    await message.answer(
        f"Контент:\n"
        f"  Всего: {stats['total_items']}\n"
        f"  Ожидает: {stats['pending']}\n"
        f"  Обработка: {stats['processing']}\n"
        f"  Готово: {stats['done']}\n"
        f"  Ошибки: {stats['failed']}\n"
        f"  Опубликовано вариантов: {stats['posted_variants']}"
    )


@router.message(Command("queue"))
async def cmd_queue(message: Message):
    store = get_store()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    schedule = store.get_schedule_for_date(today)
    if not schedule:
        await message.answer("На сегодня постов не запланировано.")
        return

    lines = [f"Расписание на {today}:"]
    for s in schedule[:20]:
        lines.append(f"  {s.account_id}: {s.content_id} [{s.posting_state.value}]")
    if len(schedule) > 20:
        lines.append(f"  ... и ещё {len(schedule) - 20}")
    await message.answer("\n".join(lines))


# --- /cancel <content_id> ---

@router.message(Command("cancel"))
async def cmd_cancel(message: Message):
    """Cancel a content item by ID. Removes pending schedules and marks cancelled."""
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        store = get_store()
        recent = store.get_recent_items(limit=5)
        if not recent:
            await message.answer("Использование: /cancel <content_id>\nНет контента для отмены.")
            return
        lines = ["Использование: /cancel <content_id>\n\nПоследний контент:"]
        for item in recent:
            status_emoji = {
                "pending": "⏳", "processing": "⚙️", "done": "✅",
                "failed": "❌", "cancelled": "🚫",
            }.get(item.uniqualization_status.value, "❓")
            lines.append(
                f"  {status_emoji} <code>{item.content_id}</code> "
                f"[{item.type.value}] {item.uniqualization_status.value}"
            )
        await message.answer("\n".join(lines), parse_mode="HTML")
        return

    content_id = parts[1].strip()
    store = get_store()
    item = store.get_content_item(content_id)
    if not item:
        await message.answer(f"Контент не найден: {content_id}")
        return

    if item.uniqualization_status == UniqualizationStatus.CANCELLED:
        await message.answer(f"Контент уже отменён: {content_id}")
        return

    success = store.cancel_content(content_id)
    if success:
        await message.answer(
            f"Контент отменён: <code>{content_id}</code>\n"
            f"Расписание удалено, варианты помечены.",
            parse_mode="HTML",
        )
    else:
        await message.answer(f"Не удалось отменить: {content_id}")


# --- /edit <content_id> ---

@router.message(Command("edit"))
async def cmd_edit(message: Message, state: FSMContext):
    """Edit caption for a pending content item."""
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        store = get_store()
        pending = [
            i for i in store.get_recent_items(limit=10)
            if i.uniqualization_status == UniqualizationStatus.PENDING
        ]
        if not pending:
            await message.answer(
                "Использование: /edit <content_id>\n"
                "Редактировать можно только контент со статусом 'pending'.\n"
                "Нет подходящего контента."
            )
            return
        lines = ["Использование: /edit <content_id>\n\nДоступно для редактирования:"]
        for item in pending:
            caption_short = (item.original_caption[:60] + "...") if item.original_caption else "(без текста)"
            lines.append(f"  <code>{item.content_id}</code> [{item.type.value}] {caption_short}")
        await message.answer("\n".join(lines), parse_mode="HTML")
        return

    content_id = parts[1].strip()
    store = get_store()
    item = store.get_content_item(content_id)

    if not item:
        await message.answer(f"Контент не найден: {content_id}")
        return

    if item.uniqualization_status != UniqualizationStatus.PENDING:
        await message.answer(
            f"Редактировать можно только 'pending' контент.\n"
            f"Текущий статус: {item.uniqualization_status.value}"
        )
        return

    current_caption = item.original_caption or "(без текста)"
    await state.update_data(edit_content_id=content_id)
    await state.set_state(ContentEdit.awaiting_new_caption)
    await message.answer(
        f"Редактирование <code>{content_id}</code>\n\n"
        f"Текущий текст:\n{current_caption[:500]}\n\n"
        f"Отправь новый текст (или /skip для пустого):",
        parse_mode="HTML",
        reply_markup=cancel_keyboard(),
    )


@router.message(ContentEdit.awaiting_new_caption, F.text)
async def handle_edit_caption(message: Message, state: FSMContext):
    """Process new caption for /edit command."""
    data = await state.get_data()
    content_id = data.get("edit_content_id")

    if message.text == "/skip":
        new_caption = ""
    else:
        new_caption = message.text

    store = get_store()
    success = store.update_content_caption(content_id, new_caption)
    await state.clear()

    if success:
        short = new_caption[:100] + "..." if len(new_caption) > 100 else (new_caption or "(без текста)")
        await message.answer(
            f"Текст обновлён для <code>{content_id}</code>\n"
            f"Новый текст: {short}",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            f"Не удалось обновить. Контент {content_id} уже в обработке или не найден."
        )


# --- /list — recent content ---

@router.message(Command("list"))
async def cmd_list(message: Message):
    """Show recent content items."""
    store = get_store()
    items = store.get_recent_items(limit=10)
    if not items:
        await message.answer("Нет загруженного контента.")
        return

    lines = ["Последний контент:"]
    for item in items:
        status_emoji = {
            "pending": "⏳", "processing": "⚙️", "done": "✅",
            "failed": "❌", "cancelled": "🚫",
        }.get(item.uniqualization_status.value, "❓")
        caption_short = (item.original_caption[:40] + "...") if item.original_caption else "-"
        lines.append(
            f"{status_emoji} <code>{item.content_id}</code> "
            f"[{item.type.value}] v:{item.variants_done}/{item.total_variants_needed} "
            f"{caption_short}"
        )
    await message.answer("\n".join(lines), parse_mode="HTML")


# --- Photo handler ---

@router.message(F.photo, ~F.media_group_id)
async def handle_photo(message: Message, state: FSMContext, bot: Bot):
    """Handle single photo upload (excludes album photos)."""
    content_id = _generate_content_id()
    content_dir = ORIGINALS_DIR / content_id
    content_dir.mkdir(parents=True, exist_ok=True)

    photo = message.photo[-1]
    filename = "photo_01.jpg"
    local_path = str(content_dir / filename)
    await _download_file(bot, photo.file_id, local_path)

    await state.update_data(
        content_id=content_id,
        content_type=ContentType.PHOTO.value,
        posting_flow=PostingFlow.FEED_PHOTO.value,
        media=[{"path": local_path, "order": 1, "mime": "image/jpeg", "filename": filename}],
        caption=message.caption or "",
        telegram_id=message.from_user.id if message.from_user else 0,
        username=message.from_user.username or "" if message.from_user else "",
    )

    if message.caption:
        await state.set_state(ContentUpload.confirming)
        await _send_confirmation(message, state)
    else:
        await state.set_state(ContentUpload.awaiting_caption)
        await message.answer(
            f"Фото получено! ({photo.width}x{photo.height})\n"
            "Отправь текст поста (или /skip без текста)",
            reply_markup=cancel_keyboard(),
        )


# --- Video handler ---

@router.message(F.video)
async def handle_video(message: Message, state: FSMContext, bot: Bot):
    """Handle video upload — ask if Reel or Post."""
    content_id = _generate_content_id()
    content_dir = ORIGINALS_DIR / content_id
    content_dir.mkdir(parents=True, exist_ok=True)

    video = message.video
    ext = ".mp4"
    if video.mime_type and "quicktime" in video.mime_type:
        ext = ".mov"
    filename = f"video_01{ext}"
    local_path = str(content_dir / filename)
    await _download_file(bot, video.file_id, local_path)

    size_mb = (video.file_size or 0) / (1024 * 1024)
    duration = video.duration or 0

    await state.update_data(
        content_id=content_id,
        media=[{"path": local_path, "order": 1, "mime": video.mime_type or "video/mp4", "filename": filename}],
        caption=message.caption or "",
        telegram_id=message.from_user.id if message.from_user else 0,
        username=message.from_user.username or "" if message.from_user else "",
    )
    await state.set_state(ContentUpload.awaiting_type)

    await message.answer(
        f"Видео получено ({size_mb:.1f} MB, {duration}с)\n"
        "Какой тип контента?",
        reply_markup=content_type_keyboard(),
    )


# --- Video type selection callback ---

@router.callback_query(F.data.startswith("type:"))
async def handle_type_selection(callback: CallbackQuery, state: FSMContext):
    type_value = callback.data.split(":")[1]

    type_map = {
        "feed_video": (ContentType.VIDEO, PostingFlow.FEED_VIDEO),
        "reel": (ContentType.REEL, PostingFlow.REEL),
        "story_video": (ContentType.STORY_VIDEO, PostingFlow.STORY_VIDEO),
    }

    content_type, posting_flow = type_map.get(
        type_value, (ContentType.VIDEO, PostingFlow.FEED_VIDEO)
    )

    await state.update_data(
        content_type=content_type.value,
        posting_flow=posting_flow.value,
    )

    data = await state.get_data()
    if data.get("caption"):
        await state.set_state(ContentUpload.confirming)
        await _send_confirmation_cb(callback, state)
    else:
        await state.set_state(ContentUpload.awaiting_caption)
        await callback.message.edit_text(
            f"Тип: {content_type.value}\n"
            "Отправь текст поста (или /skip без текста)"
        )
    await callback.answer()


# --- Caption handler ---

@router.message(ContentUpload.awaiting_caption, F.text)
async def handle_caption(message: Message, state: FSMContext):
    """Handle caption text (or /skip)."""
    if message.text == "/skip":
        await state.update_data(caption="")
    else:
        await state.update_data(caption=message.text)

    await state.set_state(ContentUpload.confirming)
    await _send_confirmation(message, state)


# --- Confirmation ---

async def _send_confirmation(message: Message, state: FSMContext):
    data = await state.get_data()
    text = _build_preview(data)
    await message.answer(text, reply_markup=confirm_keyboard())


async def _send_confirmation_cb(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    text = _build_preview(data)
    await callback.message.edit_text(text, reply_markup=confirm_keyboard())


def _build_preview(data: dict) -> str:
    content_type = data.get("content_type", "photo")
    media = data.get("media", [])
    caption = data.get("caption", "")
    media_count = len(media)

    type_labels = {
        "photo": "Фото",
        "video": "Видео (пост)",
        "carousel": f"Карусель ({media_count} фото)",
        "reel": "Reel",
        "story_photo": "Story (фото)",
        "story_video": "Story (видео)",
    }

    preview = (
        f"Превью:\n"
        f"  Тип: {type_labels.get(content_type, content_type)}\n"
        f"  Файлов: {media_count}\n"
        f"  ID: {data.get('content_id', '???')}\n"
    )
    if caption:
        short = caption[:200] + ("..." if len(caption) > 200 else "")
        preview += f"  Текст: {short}\n"
    else:
        preview += "  Текст: (без текста)\n"

    preview += "\nПодтвердить загрузку?"
    return preview


@router.callback_query(F.data.startswith("confirm:"))
async def handle_confirmation(callback: CallbackQuery, state: FSMContext, bot: Bot):
    action = callback.data.split(":")[1]

    if action == "cancel":
        await state.clear()
        await callback.message.edit_text("Отменено.")
        await callback.answer()
        return

    if action == "edit_caption":
        await state.set_state(ContentUpload.awaiting_caption)
        await callback.message.edit_text("Отправь новый текст поста:")
        await callback.answer()
        return

    if action == "yes":
        data = await state.get_data()
        await _save_content(data)
        await state.clear()

        n_accounts = _count_active_accounts()
        await callback.message.edit_text(
            f"Контент добавлен в очередь!\n"
            f"  ID: {data.get('content_id')}\n"
            f"  Тип: {data.get('content_type')}\n"
            f"  Уникализация: {n_accounts} вариантов\n"
            f"  Статус: ожидает обработки"
        )
        await callback.answer("Добавлено!")


def _count_active_accounts() -> int:
    try:
        from ..content.uniqualization_worker import get_active_accounts
        return len(get_active_accounts())
    except Exception:
        return 40


async def _save_content(data: dict) -> None:
    """Save content item to MongoDB."""
    store = get_store()
    media = [MediaFile(**m) for m in data.get("media", [])]
    content_type = ContentType(data["content_type"])

    item = ContentItem(
        content_id=data["content_id"],
        type=content_type,
        posting_flow=CONTENT_TYPE_TO_FLOW[content_type],
        original_media=media,
        original_caption=data.get("caption", ""),
        uploaded_by=UploadedBy(
            telegram_id=data.get("telegram_id", 0),
            username=data.get("username", ""),
            source="telegram",
        ),
        uploaded_at=datetime.utcnow(),
        confirmed_at=datetime.utcnow(),
        uniqualization_status=UniqualizationStatus.PENDING,
        total_variants_needed=_count_active_accounts(),
    )
    store.save_content_item(item)
    logger.info("Content saved: %s (type=%s)", item.content_id, item.type.value)


# --- Media Group (Album/Carousel) handler ---

_album_buffer: dict[str, dict] = {}


@router.message(F.media_group_id, F.photo)
async def handle_album_photo(message: Message, state: FSMContext, bot: Bot):
    """Collect photos from a media group (album = carousel)."""
    group_id = message.media_group_id
    if group_id not in _album_buffer:
        content_id = _generate_content_id()
        content_dir = ORIGINALS_DIR / content_id
        content_dir.mkdir(parents=True, exist_ok=True)
        _album_buffer[group_id] = {
            "content_id": content_id,
            "content_dir": str(content_dir),
            "media": [],
            "caption": message.caption or "",
            "telegram_id": message.from_user.id if message.from_user else 0,
            "username": message.from_user.username or "" if message.from_user else "",
            "message": message,
        }

    buf = _album_buffer[group_id]
    buf.setdefault("_counter", 0)
    buf["_counter"] += 1
    order = buf["_counter"]

    # Schedule finalize BEFORE any await — counter is still 1 only for
    # the first handler (asyncio is single-threaded in synchronous sections).
    if order == 1:
        import asyncio

        async def _finalize_album():
            await asyncio.sleep(2.0)
            if group_id not in _album_buffer:
                return
            album_data = _album_buffer.pop(group_id)
            n_photos = len(album_data["media"])

            await state.update_data(
                content_id=album_data["content_id"],
                content_type=ContentType.CAROUSEL.value,
                posting_flow=PostingFlow.FEED_CAROUSEL.value,
                media=album_data["media"],
                caption=album_data["caption"],
                telegram_id=album_data["telegram_id"],
                username=album_data["username"],
            )

            msg = album_data["message"]
            if album_data["caption"]:
                await state.set_state(ContentUpload.confirming)
                await _send_confirmation(msg, state)
            else:
                await state.set_state(ContentUpload.awaiting_caption)
                await msg.answer(
                    f"Карусель из {n_photos} фото!\n"
                    "Отправь текст поста (или /skip без текста)",
                    reply_markup=cancel_keyboard(),
                )

        asyncio.create_task(_finalize_album())

    photo = message.photo[-1]
    filename = f"photo_{order:02d}.jpg"
    local_path = str(Path(buf["content_dir"]) / filename)
    await _download_file(bot, photo.file_id, local_path)

    buf["media"].append({
        "path": local_path,
        "order": order,
        "mime": "image/jpeg",
        "filename": filename,
    })

    if message.caption:
        buf["caption"] = message.caption
