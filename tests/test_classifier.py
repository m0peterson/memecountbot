import io

from PIL import Image

from bot.classifier import encode_image, parse_verdict, verdict_namespace


def test_plain_json():
    v = parse_verdict('{"is_meme": true, "confidence": 0.9, "reason": "joke picture"}', 0.6)
    assert v.is_meme and v.confidence == 0.9 and v.reason == "joke picture"


def test_fenced_json():
    v = parse_verdict('```json\n{"is_meme": false, "confidence": 0.8}\n```', 0.6)
    assert v.is_meme is False


def test_json_wrapped_in_prose():
    raw = 'Sure! Here is my answer:\n{"is_meme": true, "confidence": 0.95, "reason": "demotivator"}\nHope that helps.'
    assert parse_verdict(raw, 0.6).is_meme is True


def test_low_confidence_is_not_counted():
    v = parse_verdict('{"is_meme": true, "confidence": 0.3}', 0.6)
    assert v.is_meme is False
    assert "threshold" in v.reason


def test_string_booleans_and_bad_confidence():
    v = parse_verdict('{"is_meme": "yes", "confidence": "high"}', 0.6)
    assert v.is_meme is True and v.confidence == 1.0


def test_confidence_is_clamped():
    assert parse_verdict('{"is_meme": true, "confidence": 7}', 0.6).confidence == 1.0
    assert parse_verdict('{"is_meme": false, "confidence": -3}', 0.6).confidence == 0.0


def test_unparseable_replies_are_unknown_not_negative():
    # An answer we cannot read is "unknown": it must not be guessed at from stray
    # words, and must not be remembered as a verdict either way.
    assert parse_verdict("Yes, this is clearly a meme.", 0.6) is None
    assert parse_verdict("This is not a meme, it is a dashboard.", 0.6) is None
    assert parse_verdict("", 0.6) is None
    assert parse_verdict("¯\\_(ツ)_/¯", 0.6) is None
    assert parse_verdict("{bad json meme}", 0.6) is None


def test_broken_reply_cannot_sneak_past_the_confidence_threshold():
    assert parse_verdict("{bad json meme}", 0.95) is None
    assert parse_verdict('{"is_meme": true, "confidence": 0.5}', 0.95).is_meme is False


def test_verdict_namespace_tracks_model_and_threshold():
    base = verdict_namespace("google/gemini-2.5-flash", 0.6)
    assert base == verdict_namespace("google/gemini-2.5-flash", 0.6)
    assert base != verdict_namespace("google/gemini-2.5-flash", 0.95)
    assert base != verdict_namespace("openai/gpt-5", 0.6)


def _png(size=(1600, 1200), mode="RGBA"):
    buf = io.BytesIO()
    Image.new(mode, size, (200, 30, 30, 255)).save(buf, format="PNG")
    return buf.getvalue()


def test_encode_image_downscales_to_jpeg_data_url():
    url = encode_image(_png())
    assert url.startswith("data:image/jpeg;base64,")
    import base64
    decoded = base64.b64decode(url.split(",", 1)[1])
    with Image.open(io.BytesIO(decoded)) as img:
        assert max(img.size) <= 768
        assert img.format == "JPEG"


def test_encode_image_handles_webp_and_animation():
    buf = io.BytesIO()
    frames = [Image.new("RGB", (100, 100), c) for c in ((255, 0, 0), (0, 255, 0))]
    frames[0].save(buf, format="WEBP", save_all=True, append_images=frames[1:], duration=100)
    assert encode_image(buf.getvalue()).startswith("data:image/jpeg;base64,")


def test_encode_image_returns_none_for_garbage():
    assert encode_image(b"not an image at all") is None


def test_transparency_is_flattened_onto_white_not_black():
    buf = io.BytesIO()
    Image.new("RGBA", (64, 64), (255, 255, 255, 0)).save(buf, format="PNG")
    url = encode_image(buf.getvalue())
    import base64
    decoded = base64.b64decode(url.split(",", 1)[1])
    with Image.open(io.BytesIO(decoded)) as img:
        assert min(img.convert("RGB").getpixel((32, 32))) > 240


def test_oversized_input_is_rejected_before_decoding():
    from bot import classifier

    # The pixel budget is checked against the header, before any pixel is decoded.
    buf = io.BytesIO()
    Image.new("RGB", (10, 10)).save(buf, format="PNG")
    huge = buf.getvalue()
    original = classifier.MAX_IMAGE_PIXELS
    classifier.MAX_IMAGE_PIXELS = 4
    try:
        assert encode_image(huge) is None
    finally:
        classifier.MAX_IMAGE_PIXELS = original

    assert encode_image(b"x" * (classifier.MAX_IMAGE_BYTES + 1)) is None
    assert encode_image(b"") is None
