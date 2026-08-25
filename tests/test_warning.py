from bot.warning import cache_key, resolve_payload


def test_missing_local_file_degrades_to_text():
    p = resolve_payload("assets/warning.jpg", "auto")
    assert p.kind == "text" and p.source == ""


def test_existing_file_kind_from_extension(tmp_path):
    for name, kind in [("w.jpg", "photo"), ("w.png", "photo"), ("w.webp", "sticker"),
                       ("w.tgs", "sticker"), ("w.gif", "animation"), ("w.mp4", "animation")]:
        f = tmp_path / name
        f.write_bytes(b"x")
        p = resolve_payload(str(f), "auto")
        assert (p.kind, p.is_local) == (kind, True), name


def test_explicit_type_overrides_extension(tmp_path):
    f = tmp_path / "w.jpg"
    f.write_bytes(b"x")
    assert resolve_payload(str(f), "sticker").kind == "sticker"


def test_urls_and_file_ids_are_passed_through():
    url = resolve_payload("https://example.com/meme.png", "auto")
    assert url.kind == "photo" and url.is_local is False

    file_id = resolve_payload("CAACAgIAAxkBAAEB1234", "auto")
    assert file_id.kind == "photo" and file_id.is_local is False
    assert file_id.source == "CAACAgIAAxkBAAEB1234"


def test_empty_or_text_type_means_text_reply(tmp_path):
    assert resolve_payload("", "auto").kind == "text"
    f = tmp_path / "w.jpg"
    f.write_bytes(b"x")
    assert resolve_payload(str(f), "text").kind == "text"


def test_cache_key_tracks_file_identity(tmp_path):
    f = tmp_path / "w.jpg"
    f.write_bytes(b"x")
    p = resolve_payload(str(f), "auto")
    first = cache_key(p)
    assert first and first.startswith("warning_file_id:photo:")

    f.write_bytes(b"much longer content")
    assert cache_key(p) != first          # replaced asset invalidates the file_id
    assert cache_key(resolve_payload("https://example.com/x.png", "auto")) is None
