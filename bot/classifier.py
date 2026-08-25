"""Meme detection through OpenRouter."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
from dataclasses import dataclass

import httpx
from PIL import Image, UnidentifiedImageError

log = logging.getLogger(__name__)

MAX_IMAGE_EDGE = 768
JPEG_QUALITY = 82

SYSTEM_PROMPT = """You are a strict content classifier working inside a corporate Telegram group.
Your only job is to decide whether a message is a MEME / entertainment content, or not.

Count as a meme (is_meme = true):
- image macros, joke pictures, demotivators, comics, reaction pictures
- funny or ironic screenshots (tweets, chat fragments, headlines posted for laughs)
- reaction stickers and joke stickers
- funny GIFs and joke video thumbnails
- purely entertaining text jokes, anecdotes, copypasta, or "shitposts"

Do NOT count as a meme (is_meme = false):
- work screenshots: dashboards, tickets, code, logs, spreadsheets, diagrams, designs
- documents, invoices, receipts, contracts, presentations
- product photos, office photos, real event photos shared as information
- neutral news or announcements without a joke
- normal conversation, questions, answers, status updates, links shared for work
- greetings, emoji-only replies, short acknowledgements

Judge the content itself, in any language. If it is genuinely ambiguous, prefer is_meme = false.

Answer with a single JSON object and nothing else:
{"is_meme": true|false, "confidence": 0.0-1.0, "reason": "max 12 words"}"""


@dataclass(frozen=True)
class Verdict:
    is_meme: bool
    confidence: float
    reason: str

    @property
    def as_tuple(self) -> tuple[bool, float, str]:
        return self.is_meme, self.confidence, self.reason


NOT_A_MEME = Verdict(False, 0.0, "not a meme")


def encode_image(raw: bytes) -> str | None:
    """Normalise arbitrary Telegram media bytes into a small base64 JPEG data URL."""
    try:
        with Image.open(io.BytesIO(raw)) as img:
            img.seek(0)  # first frame of animated webp/gif
            rgb = img.convert("RGB")
            rgb.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.LANCZOS)
            buf = io.BytesIO()
            rgb.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        log.warning("could not decode media (%s bytes): %s", len(raw), exc)
        return None
    payload = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{payload}"


def parse_verdict(content: str, min_confidence: float) -> Verdict:
    """Pull the JSON verdict out of a model reply, tolerating fences and prose."""
    if not content:
        return NOT_A_MEME
    data = _loads(content)
    if data is None:
        # Last resort: a bare yes/no somewhere in the text.
        lowered = content.lower()
        if re.search(r"\b(true|yes|meme)\b", lowered) and "not a meme" not in lowered:
            return Verdict(True, 0.5, "unstructured reply")
        return NOT_A_MEME

    raw_flag = data.get("is_meme")
    if isinstance(raw_flag, str):
        is_meme = raw_flag.strip().lower() in {"true", "yes", "1"}
    else:
        is_meme = bool(raw_flag)

    try:
        confidence = float(data.get("confidence", 1.0 if is_meme else 0.0))
    except (TypeError, ValueError):
        confidence = 1.0 if is_meme else 0.0
    confidence = min(max(confidence, 0.0), 1.0)

    reason = str(data.get("reason") or "").strip()[:200]
    if is_meme and confidence < min_confidence:
        return Verdict(False, confidence, f"below confidence threshold: {reason}" if reason else
                       "below confidence threshold")
    return Verdict(is_meme, confidence, reason or ("meme" if is_meme else "not a meme"))


def _loads(content: str) -> dict | None:
    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    for chunk in (text, _first_object(text)):
        if not chunk:
            continue
        try:
            data = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _first_object(text: str) -> str | None:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start:end + 1]


class MemeClassifier:
    """Thin OpenRouter chat-completions client specialised for one question."""

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str = "https://openrouter.ai/api/v1",
        min_confidence: float = 0.6,
        timeout: float = 45.0,
        retries: int = 2,
        referer: str = "https://github.com/m0peterson/memecountbot",
        title: str = "memecountbot",
    ) -> None:
        self._model = model
        self._min_confidence = min_confidence
        self._retries = max(0, retries)
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": referer,
                "X-Title": title,
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def classify(
        self, *, text: str = "", hint: str = "", image: bytes | None = None
    ) -> Verdict | None:
        """Return a verdict, or ``None`` when the model could not be reached.

        The distinction matters: an unreachable API is *unknown*, not *not a meme*,
        and unknown answers must never be cached.
        """
        parts: list[dict] = []
        described = _describe(text, hint, has_image=image is not None)
        parts.append({"type": "text", "text": described})

        if image is not None:
            data_url = encode_image(image)
            if data_url is None:
                if not text:
                    return None  # nothing left to judge
            else:
                parts.append({"type": "image_url", "image_url": {"url": data_url}})

        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": parts},
            ],
            "max_tokens": 200,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }

        content = await self._post(payload)
        if content is None:
            return None
        verdict = parse_verdict(content, self._min_confidence)
        log.debug("verdict=%s reason=%s", verdict.is_meme, verdict.reason)
        return verdict

    async def _post(self, payload: dict) -> str | None:
        delay = 1.0
        attempts_left = self._retries
        last_error: str | None = None
        calls = 0

        while True:
            calls += 1
            retry_now = False  # set for failures that should not cost a backoff wait
            try:
                resp = await self._client.post("/chat/completions", json=payload)
            except httpx.HTTPError as exc:
                last_error = f"network error: {exc}"
            else:
                if resp.status_code == 200:
                    try:
                        body = resp.json()
                        return body["choices"][0]["message"]["content"] or ""
                    except (ValueError, KeyError, IndexError, TypeError) as exc:
                        last_error = f"unexpected response shape: {exc}"
                elif resp.status_code in (400, 422) and "response_format" in payload:
                    # Some models reject JSON mode outright; drop it and try again.
                    # This does not count against the retry budget.
                    payload.pop("response_format", None)
                    last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    retry_now = True
                elif resp.status_code in (408, 409, 429) or resp.status_code >= 500:
                    last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                else:
                    log.error("OpenRouter rejected the request: HTTP %s %s",
                              resp.status_code, resp.text[:300])
                    return None

            if retry_now:
                continue
            if attempts_left <= 0:
                break
            attempts_left -= 1
            await asyncio.sleep(delay)
            delay *= 2

        log.error("OpenRouter request failed after %s call(s): %s", calls, last_error)
        return None


def _describe(text: str, hint: str, *, has_image: bool) -> str:
    lines = ["Classify the following Telegram group message."]
    if hint:
        lines.append(f"Context: {hint}")
    if has_image:
        lines.append("The attached image is the message content.")
    if text:
        label = "Caption" if has_image else "Message text"
        lines.append(f"{label}: <<<{text}>>>")
    elif not has_image:
        lines.append("The message has no content to judge; answer is_meme=false.")
    lines.append('Reply with JSON only: {"is_meme": bool, "confidence": number, "reason": string}')
    return "\n".join(lines)
