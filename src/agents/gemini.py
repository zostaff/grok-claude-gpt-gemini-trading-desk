"""GeminiImageAnalyst: looks at the token art and grades effort, meme strength and red flags."""

from __future__ import annotations

import asyncio
import time
from io import BytesIO

import google.generativeai as genai
import httpx
from PIL import Image, UnidentifiedImageError

from ..models import Token
from .base import AgentError, BaseAgent

# Public IPFS gateways are slow; a long timeout would stall the whole parallel round.
IMAGE_TIMEOUT = 12.0
MAX_IMAGE_BYTES = 12 * 1024 * 1024
IPFS_GATEWAY = "https://ipfs.io/ipfs/"

PROMPT = """You are looking at the artwork for a Solana memecoin called "{name}" (${symbol}).

Its stated description is: {description}

Judge the image itself. Score each 0.0 to 1.0:
- image_quality: resolution, composition, whether it renders as a deliberate piece of art
  rather than a screenshot of a screenshot.
- meme_strength: does the image carry the joke on its own, with no caption?
- effort_signal: did a person spend real time on this, or is it default-template output,
  a stock photo, or a logo with the ticker typed over it?
- originality_visual: have you seen this exact image on other tokens? Recycled art from
  a known project or a well-known meme reposted unchanged scores low.
- red_flag_visual: HIGH means DANGER. Raise this for: impersonation of a real brand,
  exchange or public figure; text promising guaranteed returns or an airdrop; a QR code;
  a wallet address burned into the image; explicit or hateful content; or art copied
  from an existing project to ride its name. This is the number that can kill the trade,
  so only raise it for something you can actually point at in the image.

Reply with ONLY this JSON object, no prose, no markdown fence:
{{"image_quality": 0.0, "meme_strength": 0.0, "effort_signal": 0.0,
"originality_visual": 0.0, "red_flag_visual": 0.0,
"summary": "one sentence, max 200 chars, describing what is literally in the image"}}"""


class GeminiImageAnalyst(BaseAgent):
    """Grades the token's artwork; vetoes on visual red flags like impersonation or scam text.

    A missing image and a broken API are different failures and are scored differently: a
    launch with no art is uninformative (all zeros, no veto), while an analyst we could not
    reach leaves us blind to visual scams and so assumes the worst.
    """

    name = "gemini"
    SCORE_KEYS = (
        "image_quality",
        "meme_strength",
        "effort_signal",
        "originality_visual",
        "red_flag_visual",
    )

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash") -> None:
        super().__init__(model)
        genai.configure(api_key=api_key)
        self._model = genai.GenerativeModel(model)
        self.http = httpx.AsyncClient(
            timeout=IMAGE_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "multi-model-pipeline/0.1"},
        )

    async def aclose(self) -> None:
        """Release the image-download connection pool."""
        await self.http.aclose()

    def _get_fallback(self) -> dict:
        """System failure: we could not look, so we must assume there is something to see."""
        return {
            "image_quality": 0.0,
            "meme_strength": 0.0,
            "effort_signal": 0.0,
            "originality_visual": 0.0,
            "red_flag_visual": 1.0,
            "summary": "gemini unavailable; unable to inspect artwork",
        }

    @staticmethod
    def _no_image() -> dict:
        """Missing artwork: zero credit, but no veto — absence of data is not evidence of fraud."""
        return {
            "image_quality": 0.0,
            "meme_strength": 0.0,
            "effort_signal": 0.0,
            "originality_visual": 0.0,
            "red_flag_visual": 0.0,
            "summary": "no image available for this token",
            "error": "image_unavailable",
        }

    @staticmethod
    def _normalise_url(url: str) -> str:
        """Rewrite ipfs:// URIs onto an HTTP gateway; pass HTTP(S) through unchanged."""
        if url.startswith("ipfs://"):
            return IPFS_GATEWAY + url[len("ipfs://") :].lstrip("/")
        return url

    async def _download(self, url: str) -> Image.Image | None:
        """Fetch and decode the artwork, resized in a worker thread. None on any failure."""
        try:
            resp = await self.http.get(self._normalise_url(url))
            resp.raise_for_status()
            raw = resp.content
        except httpx.HTTPError as exc:
            print(f"[gemini] image download failed: {type(exc).__name__}: {exc}")
            return None
        if not raw or len(raw) > MAX_IMAGE_BYTES:
            print(f"[gemini] image rejected: {len(raw)} bytes")
            return None

        def decode() -> Image.Image | None:
            """Decode and downscale off the event loop; Pillow is CPU-bound and blocking."""
            try:
                img = Image.open(BytesIO(raw))
                img.load()
            except (UnidentifiedImageError, OSError, ValueError) as exc:
                print(f"[gemini] image decode failed: {type(exc).__name__}: {exc}")
                return None
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.thumbnail((1024, 1024), Image.LANCZOS)
            return img

        return await asyncio.to_thread(decode)

    def _build_prompt(self, token: Token) -> str:
        """Render the vision prompt for one token."""
        return PROMPT.format(
            name=token.name or "(unnamed)",
            symbol=token.symbol or "?",
            description=(token.description or "(none provided)")[:300],
        )

    async def analyze(self, token: Token) -> dict:
        """Grade the token artwork. Never raises; returns a fallback on failure."""
        start = time.monotonic()

        if not token.image_url:
            out = self._no_image()
            out["latency_ms"] = self._elapsed_ms(start)
            return out

        img = await self._download(token.image_url)
        if img is None:
            out = self._no_image()
            out["latency_ms"] = self._elapsed_ms(start)
            return out

        prompt = self._build_prompt(token)

        async def call() -> object:
            # The google-generativeai client is synchronous, so it goes to a thread.
            return await asyncio.to_thread(self._model.generate_content, [prompt, img])

        try:
            resp = await self._call_with_retry(call)
            text = resp.text  # type: ignore[attr-defined]
        except (AgentError, AttributeError, ValueError) as exc:
            print(f"[gemini] call failed: {type(exc).__name__}: {exc}")
            out = self._fallback_with("api_error")
            out["latency_ms"] = self._elapsed_ms(start)
            return out
        finally:
            img.close()

        parsed = self._parse_json(text or "")
        out = {key: self._clamp01(parsed.get(key)) for key in self.SCORE_KEYS}
        if parsed.get("error"):
            # Unreadable output means we did not actually see the image.
            out["red_flag_visual"] = 1.0
            out["error"] = parsed["error"]
        out["summary"] = str(parsed.get("summary", ""))[:300]
        out["latency_ms"] = self._elapsed_ms(start)
        print(
            f"[gemini] {token.symbol or '?'} effort={out['effort_signal']:.2f} "
            f"red_flag={out['red_flag_visual']:.2f} ({out['latency_ms']}ms)"
        )
        return out
