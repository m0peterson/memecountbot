"""Exercise the real HTTP path of MemeClassifier against a local stub server."""

import asyncio

from aiohttp import web

from bot.classifier import MemeClassifier


class Stub:
    """Minimal OpenRouter stand-in that replays a scripted list of responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self._runner = None
        self.url = ""

    async def start(self):
        app = web.Application()
        app.router.add_post("/chat/completions", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = self._runner.addresses[0][1]
        self.url = f"http://127.0.0.1:{port}"

    async def stop(self):
        await self._runner.cleanup()

    async def _handle(self, request):
        self.requests.append(await request.json())
        status, body = self.responses.pop(0) if self.responses else (200, _content("{}"))
        return web.json_response(body, status=status) if isinstance(body, dict) \
            else web.Response(text=body, status=status)


def _content(text):
    return {"choices": [{"message": {"content": text}}]}


def run_with_stub(responses, call):
    async def scenario():
        stub = Stub(responses)
        await stub.start()
        clf = MemeClassifier("test-key", "test/model", base_url=stub.url + "/",
                             timeout=5, retries=2)
        try:
            result = await call(clf)
        finally:
            await clf.aclose()
            await stub.stop()
        return result, stub

    return asyncio.run(scenario())


def test_text_request_shape_and_verdict():
    verdict, stub = run_with_stub(
        [(200, _content('{"is_meme": true, "confidence": 0.9, "reason": "anecdote"}'))],
        lambda c: c.classify(text="Заходит программист в бар", hint="forwarded"),
    )
    assert verdict.is_meme is True and verdict.reason == "anecdote"

    body = stub.requests[0]
    assert body["model"] == "test/model"
    assert body["temperature"] == 0
    assert body["messages"][0]["role"] == "system"
    parts = body["messages"][1]["content"]
    assert all(p["type"] == "text" for p in parts)
    assert "Заходит программист в бар" in parts[0]["text"]
    assert "forwarded" in parts[0]["text"]


def test_image_is_sent_as_a_data_url():
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (300, 300), (1, 2, 3)).save(buf, format="PNG")

    _, stub = run_with_stub(
        [(200, _content('{"is_meme": true, "confidence": 1}'))],
        lambda c: c.classify(text="", hint="A Telegram sticker.", image=buf.getvalue()),
    )
    parts = stub.requests[0]["messages"][1]["content"]
    image_part = next(p for p in parts if p["type"] == "image_url")
    assert image_part["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_rate_limit_is_retried():
    verdict, stub = run_with_stub(
        [(429, "slow down"),
         (200, _content('{"is_meme": false, "confidence": 0.9, "reason": "work chat"}'))],
        lambda c: c.classify(text="деплой в 18:00"),
    )
    assert verdict.is_meme is False
    assert len(stub.requests) == 2


def test_models_rejecting_json_mode_are_retried_without_it():
    verdict, stub = run_with_stub(
        [(400, "response_format is not supported"),
         (200, _content('{"is_meme": true, "confidence": 0.8}'))],
        lambda c: c.classify(text="мемчик про понедельник"),
    )
    assert verdict.is_meme is True
    assert "response_format" in stub.requests[0]
    assert "response_format" not in stub.requests[1]


def test_persistent_failure_returns_unknown_not_a_negative_verdict():
    """An outage must not be mistaken for 'not a meme' — that would get cached."""
    verdict, stub = run_with_stub(
        [(500, "boom"), (500, "boom"), (500, "boom")],
        lambda c: c.classify(text="что-то смешное про работу"),
    )
    assert verdict is None
    assert len(stub.requests) == 3  # initial attempt + 2 retries


def test_auth_error_is_not_retried():
    verdict, stub = run_with_stub(
        [(401, "no credits")],
        lambda c: c.classify(text="что-то смешное про работу"),
    )
    assert verdict is None
    assert len(stub.requests) == 1


def test_json_mode_retry_does_not_consume_the_retry_budget():
    """A model that rejects JSON mode still gets its full allowance of real retries."""
    verdict, stub = run_with_stub(
        [(400, "response_format is not supported"),
         (429, "slow down"),
         (500, "boom"),
         (200, _content('{"is_meme": true, "confidence": 0.9}'))],
        lambda c: c.classify(text="мемчик про понедельник"),
    )
    assert verdict.is_meme is True
    assert len(stub.requests) == 4
