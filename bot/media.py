"""Turn a Telegram message into something the classifier can look at.

Everything here is plain attribute access, so it works with aiogram ``Message``
objects and with lightweight stand-ins in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field

IMAGE_DOCUMENT_MIMES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "image/bmp",
}

# Media we never bother the LLM with: voice notes, round videos, music, contacts…
IGNORED_ATTRS = ("voice", "video_note", "audio", "contact", "location", "poll", "dice", "invoice")


@dataclass
class Candidate:
    """A message reduced to what the meme classifier needs."""

    kind: str  # photo | sticker | animation | video | document | text
    text: str = ""
    hint: str = ""
    file_id: str | None = None
    cache_key: str | None = None
    tags: list[str] = field(default_factory=list)

    @property
    def has_image(self) -> bool:
        return self.file_id is not None


def _thumb_file_id(obj: object) -> str | None:
    thumb = getattr(obj, "thumbnail", None) or getattr(obj, "thumb", None)
    return getattr(thumb, "file_id", None) if thumb else None


def _pick_photo(sizes) -> object | None:
    """Prefer a mid-sized rendition: enough detail to read a meme, small to download."""
    if not sizes:
        return None
    usable = [s for s in sizes if (getattr(s, "width", 0) or 0) <= 1280]
    return usable[-1] if usable else sizes[0]


def _forward_hint(message) -> str:
    if getattr(message, "forward_origin", None) or getattr(message, "forward_from", None):
        return "The message was forwarded from another chat or channel."
    if getattr(message, "forward_from_chat", None) or getattr(message, "forward_sender_name", None):
        return "The message was forwarded from another chat or channel."
    return ""


def build_candidate(message, *, check_text: bool, check_video: bool, min_text_len: int,
                    max_text_len: int) -> Candidate | None:
    """Return what to classify, or ``None`` when the message should be ignored."""
    for attr in IGNORED_ATTRS:
        if getattr(message, attr, None) is not None:
            return None

    caption = (getattr(message, "caption", None) or "").strip()
    text = (getattr(message, "text", None) or "").strip()
    forwarded = _forward_hint(message)

    sticker = getattr(message, "sticker", None)
    if sticker is not None:
        file_id = _thumb_file_id(sticker)
        if file_id is None and not (getattr(sticker, "is_animated", False)
                                    or getattr(sticker, "is_video", False)):
            file_id = getattr(sticker, "file_id", None)
        bits = ["A Telegram sticker."]
        if getattr(sticker, "emoji", None):
            bits.append(f"Associated emoji: {sticker.emoji}.")
        if getattr(sticker, "set_name", None):
            bits.append(f"Sticker pack: {sticker.set_name}.")
        if getattr(sticker, "is_video", False) or getattr(sticker, "is_animated", False):
            bits.append("It is animated; you only see its first frame.")
        return Candidate(
            kind="sticker",
            text=caption,
            hint=" ".join(bits + ([forwarded] if forwarded else [])),
            file_id=file_id,
            cache_key=_cache_key("sticker", sticker),
            tags=["sticker"],
        )

    photos = getattr(message, "photo", None)
    if photos:
        size = _pick_photo(list(photos))
        return Candidate(
            kind="photo",
            text=caption,
            hint=forwarded,
            file_id=getattr(size, "file_id", None),
            cache_key=_cache_key("photo", photos[-1]) if not caption else None,
            tags=["photo"],
        )

    animation = getattr(message, "animation", None)
    if animation is not None:
        return Candidate(
            kind="animation",
            text=caption,
            hint=" ".join(filter(None, [
                "An animated GIF; you only see its first frame.", forwarded,
            ])),
            file_id=_thumb_file_id(animation),
            cache_key=_cache_key("animation", animation) if not caption else None,
            tags=["animation"],
        )

    video = getattr(message, "video", None)
    if video is not None:
        if not check_video:
            return None
        return Candidate(
            kind="video",
            text=caption,
            hint=" ".join(filter(None, [
                "A video; you only see its thumbnail.", forwarded,
            ])),
            file_id=_thumb_file_id(video),
            cache_key=_cache_key("video", video) if not caption else None,
            tags=["video"],
        )

    document = getattr(message, "document", None)
    if document is not None:
        mime = (getattr(document, "mime_type", None) or "").lower()
        if mime not in IMAGE_DOCUMENT_MIMES:
            return None
        file_id = _thumb_file_id(document) or getattr(document, "file_id", None)
        return Candidate(
            kind="document",
            text=caption,
            hint=" ".join(filter(None, [
                f"An image sent as a file ({mime}).", forwarded,
            ])),
            file_id=file_id,
            cache_key=_cache_key("document", document) if not caption else None,
            tags=["document"],
        )

    if text:
        if not check_text:
            return None
        if text.startswith("/"):
            return None
        if len(text) < min_text_len:
            return None
        return Candidate(
            kind="text",
            text=text[:max_text_len],
            hint=forwarded,
            tags=["text"],
        )

    return None


def _cache_key(prefix: str, obj: object) -> str | None:
    unique = getattr(obj, "file_unique_id", None)
    return f"{prefix}:{unique}" if unique else None
