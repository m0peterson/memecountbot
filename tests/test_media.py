from types import SimpleNamespace as NS

from bot.media import build_candidate

DEFAULTS = dict(check_text=True, check_video=False, min_text_len=12, max_text_len=1500)


def msg(**kwargs):
    base = dict(text=None, caption=None, photo=None, sticker=None, animation=None,
                video=None, document=None, voice=None, video_note=None, audio=None,
                contact=None, location=None, poll=None, dice=None, invoice=None,
                forward_origin=None, forward_from=None)
    base.update(kwargs)
    return NS(**base)


def photo_size(file_id, unique, width):
    return NS(file_id=file_id, file_unique_id=unique, width=width, height=width)


def test_photo_picks_midsize_rendition():
    sizes = [photo_size("s", "u1", 90), photo_size("m", "u1", 800), photo_size("l", "u1", 2000)]
    c = build_candidate(msg(photo=sizes), **DEFAULTS)
    assert c.kind == "photo"
    assert c.file_id == "m"
    assert c.cache_key == "photo:u1"


def test_photo_with_caption_is_not_cached():
    sizes = [photo_size("s", "u1", 90)]
    c = build_candidate(msg(photo=sizes, caption="смотри"), **DEFAULTS)
    assert c.text == "смотри"
    assert c.cache_key is None


def test_photo_falls_back_to_smallest_when_all_huge():
    sizes = [photo_size("big", "u9", 3000), photo_size("bigger", "u9", 4000)]
    assert build_candidate(msg(photo=sizes), **DEFAULTS).file_id == "big"


def test_sticker_uses_thumbnail_and_mentions_pack():
    sticker = NS(file_id="sf", file_unique_id="su", emoji="😂", set_name="Pack",
                 is_animated=False, is_video=True, thumbnail=NS(file_id="thumb"))
    c = build_candidate(msg(sticker=sticker), **DEFAULTS)
    assert (c.kind, c.file_id, c.cache_key) == ("sticker", "thumb", "sticker:su")
    assert "Pack" in c.hint and "😂" in c.hint


def test_static_sticker_without_thumbnail_uses_file_itself():
    sticker = NS(file_id="sf", file_unique_id="su", emoji=None, set_name=None,
                 is_animated=False, is_video=False, thumbnail=None)
    assert build_candidate(msg(sticker=sticker), **DEFAULTS).file_id == "sf"


def test_animated_sticker_without_thumbnail_has_no_image():
    sticker = NS(file_id="sf", file_unique_id="su", emoji=None, set_name=None,
                 is_animated=True, is_video=False, thumbnail=None)
    c = build_candidate(msg(sticker=sticker), **DEFAULTS)
    assert c.file_id is None and c.has_image is False


def test_video_ignored_unless_enabled():
    video = NS(file_id="v", file_unique_id="vu", thumbnail=NS(file_id="vt"))
    assert build_candidate(msg(video=video), **DEFAULTS) is None
    on = dict(DEFAULTS, check_video=True)
    assert build_candidate(msg(video=video), **on).file_id == "vt"


def test_document_only_when_image_mime():
    pdf = NS(file_id="d", file_unique_id="du", mime_type="application/pdf", thumbnail=None)
    assert build_candidate(msg(document=pdf), **DEFAULTS) is None
    png = NS(file_id="d", file_unique_id="du", mime_type="image/png", thumbnail=None)
    assert build_candidate(msg(document=png), **DEFAULTS).file_id == "d"


def test_text_rules():
    assert build_candidate(msg(text="ok"), **DEFAULTS) is None            # too short
    assert build_candidate(msg(text="/memestats now"), **DEFAULTS) is None  # command
    c = build_candidate(msg(text="Заходит как-то программист в бар"), **DEFAULTS)
    assert c.kind == "text" and c.has_image is False
    off = dict(DEFAULTS, check_text=False)
    assert build_candidate(msg(text="Заходит как-то программист в бар"), **off) is None


def test_text_is_truncated():
    c = build_candidate(msg(text="a" * 5000), **dict(DEFAULTS, max_text_len=100))
    assert len(c.text) == 100


def test_voice_and_video_notes_are_ignored():
    assert build_candidate(msg(voice=NS(file_id="v")), **DEFAULTS) is None
    assert build_candidate(msg(video_note=NS(file_id="v")), **DEFAULTS) is None
    assert build_candidate(msg(), **DEFAULTS) is None


def test_forwarded_flag_reaches_the_hint():
    c = build_candidate(msg(text="это база, коллеги", forward_from=NS(id=1)), **DEFAULTS)
    assert "forwarded" in c.hint
