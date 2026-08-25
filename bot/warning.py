"""Sending the 'you have posted too many memes' payload."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import FSInputFile, Message

log = logging.getLogger(__name__)

EXT_KIND = {
    ".jpg": "photo", ".jpeg": "photo", ".png": "photo",
    ".webp": "sticker", ".tgs": "sticker",
    ".gif": "animation", ".mp4": "animation", ".webm": "animation",
}


@dataclass(frozen=True)
class WarningPayload:
    source: str          # local path, http(s) URL or Telegram file_id
    kind: str            # photo | sticker | animation | video | document | text
    is_local: bool


def resolve_payload(warning_file: str, warning_type: str) -> WarningPayload:
    """Work out what we are about to send and how."""
    source = (warning_file or "").strip()
    if not source or warning_type == "text":
        return WarningPayload(source="", kind="text", is_local=False)

    is_url = source.startswith(("http://", "https://"))
    is_local = not is_url and os.path.isfile(source)
    looks_like_path = not is_url and (os.sep in source or "." in os.path.basename(source))

    if warning_type != "auto":
        kind = warning_type
    else:
        ext = os.path.splitext(source)[1].lower()
        kind = EXT_KIND.get(ext, "photo")

    if looks_like_path and not is_local:
        # A path was configured but the file is not there — degrade to text.
        log.warning("WARNING_FILE %r not found on disk; falling back to a text reply", source)
        return WarningPayload(source="", kind="text", is_local=False)

    return WarningPayload(source=source, kind=kind, is_local=is_local)


def cache_key(payload: WarningPayload) -> str | None:
    """Key for remembering the uploaded file_id so we upload the asset only once."""
    if not payload.is_local:
        return None
    try:
        stat = os.stat(payload.source)
    except OSError:
        return None
    return f"warning_file_id:{payload.kind}:{int(stat.st_mtime)}:{stat.st_size}"


def _extract_file_id(sent: Message) -> str | None:
    if sent.photo:
        return sent.photo[-1].file_id
    for attr in ("sticker", "animation", "video", "document"):
        obj = getattr(sent, attr, None)
        if obj is not None:
            return obj.file_id
    return None


class WarningSender:
    """Sends the configured warning asset, remembering the uploaded file_id."""

    def __init__(self, bot: Bot, storage, *, warning_file: str, warning_type: str,
                 warning_text: str, send_caption: bool) -> None:
        self._bot = bot
        self._storage = storage
        self._text = warning_text
        self._send_caption = send_caption
        self.payload = resolve_payload(warning_file, warning_type)

    @property
    def describes(self) -> str:
        if self.payload.kind == "text":
            return "text reply"
        location = "file" if self.payload.is_local else "file_id/URL"
        return f"{self.payload.kind} ({location}: {self.payload.source})"

    async def send(self, message: Message) -> bool:
        """Reply to ``message`` with the warning. Returns True when something was sent."""
        payload = self.payload
        if payload.kind == "text":
            return await self._send_text(message)

        key = cache_key(payload)
        cached = await self._storage.get_kv(key) if key else None
        media = cached or (FSInputFile(payload.source) if payload.is_local else payload.source)

        fallbacks = [payload.kind] + [k for k in ("photo", "document") if k != payload.kind]
        for kind in fallbacks:
            try:
                sent = await self._dispatch(message, kind, media)
            except TelegramAPIError as exc:
                log.warning("could not send warning as %s: %s", kind, exc)
                if cached:  # a stale file_id: retry from the original asset
                    cached = None
                    media = FSInputFile(payload.source) if payload.is_local else payload.source
                continue
            if key and not cached:
                file_id = _extract_file_id(sent)
                if file_id:
                    await self._storage.set_kv(key, file_id)
            return True

        return await self._send_text(message)

    async def _dispatch(self, message: Message, kind: str, media) -> Message:
        caption = self._text if (self._send_caption and kind != "sticker") else None
        if kind == "sticker":
            return await message.reply_sticker(media)
        if kind == "animation":
            return await message.reply_animation(media, caption=caption)
        if kind == "video":
            return await message.reply_video(media, caption=caption)
        if kind == "document":
            return await message.reply_document(media, caption=caption)
        return await message.reply_photo(media, caption=caption)

    async def _send_text(self, message: Message) -> bool:
        if not self._text:
            return False
        try:
            await message.reply(self._text)
        except TelegramAPIError as exc:
            log.warning("could not send warning text: %s", exc)
            return False
        return True
