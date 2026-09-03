"""Gemini, the image analyst: looks at the artwork nobody else on the panel can see.

A missing image and a broken API are different failures and are scored differently. A
launch with no art is uninformative -- all zeros, no veto, because absence of evidence is
not evidence of fraud. An analyst we could not reach leaves us blind to visual scams, so
that assumes the worst.
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Mapping
from typing import Any

import httpx
from google import genai
from google.genai import types
from PIL import Image, UnidentifiedImageError

from ...config.settings import VetoConfig
from ...domain.evaluation import EvaluationContext
from .base import LLMAgent

logger = logging.getLogger(__name__)

# Public IPFS gateways are slow; a long timeout would stall the whole parallel round.
IMAGE_TIMEOUT = 12.0
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_EDGE_PX = 1024

PROMPT = """You are looking at the artwork for a Solana memecoin called "{name}" (${symbol}).

Its stated description is: {description}

Judge the image itself. Score each 0.0 to 1.0:
- image_quality: resolution, composition, whether it renders as a deliberate piece of art
  rather than a screenshot of a screenshot.
- meme_strength: does the image carry the joke on its own, with no caption?
- effort_signal: did a person spend real time on this, or is it default-template output,
  a stock photo, or a logo with the ticker typed over it?
- originality_visual: have you seen this exact image on other tokens? Recycled art from a
  known project or a well-known meme reposted unchanged scores low.
- red_flag_visual: HIGH means DANGER. Raise this for: impersonation of a real brand,
  exchange or public figure; text promising guaranteed returns or an airdrop; a QR code;
  a wallet address burned into the image; explicit or hateful content; or art copied from
  an existing project to ride its name. This is the number that can kill the trade, so
  only raise it for something you can actually point at in the image.

Reply with ONLY this JSON object, no prose, no markdown fence:
{{"image_quality": 0.0, "meme_strength": 0.0, "effort_signal": 0.0,
"originality_visual": 0.0, "red_flag_visual": 0.0,
"summary": "one sentence, max 200 chars, describing what is literally in the image"}}"""


class MissingArtwork(Exception):
    """Raised when a launch simply has no image. Not a failure of this agent."""


class GeminiImageAnalyst(LLMAgent):
    """Grades the token artwork; vetoes on visual red flags like impersonation or scam text."""

    name = "gemini"
    quality_keys = ("image_quality", "meme_strength", "effort_signal", "originality_visual")
    risk_keys = ("red_flag_visual",)
    # A launch that shipped without art, or whose art will not download, is uninformative
    # -- not suspicious. Only an unreachable MODEL earns the veto-worthy fallback.
    neutral_exceptions = (MissingArtwork,)

    def __init__(
        self,
        api_key: str,
        model: str,
        vetoes: VetoConfig,
        *,
        ipfs_gateway: str = "https://ipfs.io/ipfs/",
    ) -> None:
        super().__init__(model)
        self.vetoes = vetoes
        self.ipfs_gateway = ipfs_gateway
        self.client = genai.Client(api_key=api_key)
        self.http = httpx.AsyncClient(
            timeout=IMAGE_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "trading-desk/0.2"},
        )

    async def aclose(self) -> None:
        """Release the image-download connection pool."""
        await self.http.aclose()

    def _fallback_scores(self) -> dict[str, float]:
        """System failure: we could not look, so we must assume there is something to see."""
        return {
            "image_quality": 0.0,
            "meme_strength": 0.0,
            "effort_signal": 0.0,
            "originality_visual": 0.0,
            "red_flag_visual": 1.0,
        }

    def _veto(self, scores: Mapping[str, float]) -> tuple[bool, str]:
        """Kill the trade on a visual red flag the model could point at."""
        value = scores.get("red_flag_visual", 0.0)
        if value > self.vetoes.max_red_flag_visual:
            return True, f"red_flag_visual {value:.2f} > {self.vetoes.max_red_flag_visual}"
        return False, ""

    def _normalise_url(self, url: str) -> str:
        """Rewrite ipfs:// URIs onto an HTTP gateway; pass HTTP(S) through unchanged."""
        if url.startswith("ipfs://"):
            return self.ipfs_gateway + url[len("ipfs://") :].lstrip("/")
        return url

    async def _download(self, url: str) -> bytes:
        """Fetch and re-encode the artwork, bounded in bytes and pixels."""
        resp = await self.http.get(self._normalise_url(url))
        resp.raise_for_status()
        raw = resp.content
        if not raw:
            raise MissingArtwork("empty image body")
        if len(raw) > MAX_IMAGE_BYTES:
            raise MissingArtwork(f"image too large: {len(raw)} bytes")

        def transcode() -> bytes:
            """Decode and downscale off the event loop; Pillow is CPU-bound and blocking."""
            with Image.open(io.BytesIO(raw)) as opened:
                opened.load()
                img = opened if opened.mode in ("RGB", "L") else opened.convert("RGB")
                img.thumbnail((MAX_EDGE_PX, MAX_EDGE_PX), Image.Resampling.LANCZOS)
                buffer = io.BytesIO()
                img.save(buffer, format="PNG")
                return buffer.getvalue()

        try:
            return await asyncio.to_thread(transcode)
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise MissingArtwork(f"undecodable image: {type(exc).__name__}") from exc

    def _build_prompt(self, context: EvaluationContext) -> str:
        """Render the vision prompt for one token."""
        token = context.token
        return PROMPT.format(
            name=token.name or "(unnamed)",
            symbol=token.symbol or "?",
            description=(token.description or "(none provided)")[:300],
        )

    async def _score(self, context: EvaluationContext) -> tuple[dict[str, float], str]:
        """Look at the artwork and grade it."""
        url = context.token.image_url
        if not url:
            raise MissingArtwork("token declares no image")

        try:
            png = await self._download(url)
        except (httpx.HTTPError, MissingArtwork) as exc:
            raise MissingArtwork(str(exc)) from exc

        prompt = self._build_prompt(context)

        async def call() -> object:
            """One vision request. The google-genai client is natively async."""
            # list is invariant and the SDK's accepted union is enormous; a precise
            # annotation here buys nothing the runtime does not already guarantee.
            contents: list[Any] = [
                prompt,
                types.Part.from_bytes(data=png, mime_type="image/png"),
            ]
            return await self.client.aio.models.generate_content(
                model=self.model, contents=contents
            )

        response = await self._with_retry(call)
        return self._parse_scores(getattr(response, "text", "") or "")
