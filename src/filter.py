"""CodeFilter: live pump.fun WebSocket feed, cheap metric gate, and mint deduplication."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import AsyncIterator

import httpx
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from .config import Settings
from .data import DataFetcher
from .models import Token, curve_pct_from_reserves

# A launch has to sit in the buffer long enough to accumulate buyers before we can
# judge it, so the WS feed and the gate are decoupled by this queue.
QUEUE_MAXSIZE = 256
# Remember this many emitted mints so a re-subscribe after a reconnect cannot
# replay tokens we have already sent downstream.
DEDUP_MEMORY = 5_000
# ipfs.io is slow often enough that a long timeout would stall the sweeper.
METADATA_TIMEOUT = 8.0
# Public IPFS gateways rate-limit hard. pump.fun launches arrive faster than they will
# serve, so metadata fetches queue behind a small semaphore instead of stampeding.
METADATA_CONCURRENCY = 4


@dataclass
class _Candidate:
    """A launch being watched: the token plus trade activity accumulated since creation."""

    token: Token
    buyers: set[str] = field(default_factory=set)
    volume_sol: float = 0.0
    metadata_fetched: bool = False
    metrics_polled: bool = False


class CodeFilter:
    """Streams pump.fun launches and yields only the ones worth paying four LLMs to read.

    pumpportal's `subscribeNewToken` fires at creation, when a token has no trade history
    and no metadata yet. So creations go into a watch buffer, off-chain metadata is
    fetched from the token URI, and a sweeper applies `_passes()` once the launch is old
    enough to judge.

    Buyer and volume metrics come from one of two real sources. `subscribeTokenTrade` on
    the same socket is free and live, but PumpPortal gates it behind an API key funded
    with at least 0.02 SOL. Without that key the sweeper polls Solana Tracker's trade tape
    once per candidate instead: identical numbers, one API call per launch watched.
    """

    def __init__(self, settings: Settings, data: DataFetcher | None = None) -> None:
        self.settings = settings
        self.f = settings.filter
        self.data = data
        self.use_ws_trades = bool(settings.pumpportal_api_key)
        self._queue: asyncio.Queue[Token] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self._pending: dict[str, _Candidate] = {}
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._http = httpx.AsyncClient(
            timeout=METADATA_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "multi-model-pipeline/0.1"},
        )
        self._tasks: list[asyncio.Task] = []
        self._metadata_sem = asyncio.Semaphore(METADATA_CONCURRENCY)
        self._gate_sem = asyncio.Semaphore(max(1, self.f.gate_concurrency))
        self.stats = {"seen": 0, "passed": 0, "rejected": 0, "expired": 0}

    # --- public API -----------------------------------------------------------

    async def stream(self) -> AsyncIterator[Token]:
        """Yield tokens that clear the metric gate, forever, reconnecting as needed."""
        self._tasks = [
            asyncio.create_task(self._ws_loop(), name="codefilter-ws"),
            asyncio.create_task(self._sweep_loop(), name="codefilter-sweep"),
        ]
        try:
            while True:
                token = await self._queue.get()
                yield token
        finally:
            await self.close()

    async def close(self) -> None:
        """Cancel background tasks and release the HTTP/WS connections."""
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        await self._http.aclose()

    # --- the gate -------------------------------------------------------------

    def _passes(self, token: Token) -> tuple[bool, str]:
        """Return (passed, reason). Thresholds are inclusive: exactly at the bound passes."""
        if not token.address:
            return False, "no_mint_address"
        if self.f.require_metadata and not token.has_metadata:
            return False, "no_metadata"
        if token.age_minutes < self.f.min_age_minutes:
            return False, f"too_young ({token.age_minutes:.1f}m < {self.f.min_age_minutes}m)"
        if token.unique_buyers < self.f.min_buyers:
            return False, f"few_buyers ({token.unique_buyers} < {self.f.min_buyers})"
        if token.volume_sol < self.f.min_volume_sol:
            return False, f"low_volume ({token.volume_sol:.2f} < {self.f.min_volume_sol} SOL)"
        if token.bonding_curve_pct > self.f.max_curve_pct:
            return False, f"curve_too_far ({token.bonding_curve_pct:.1f}% > {self.f.max_curve_pct}%)"
        return True, "ok"

    # --- WebSocket ------------------------------------------------------------

    async def _ws_loop(self) -> None:
        """Hold a pumpportal connection open, reconnecting with exponential backoff."""
        delay = 1.0
        while True:
            try:
                async with websockets.connect(
                    self._ws_url(),
                    ping_interval=20,
                    ping_timeout=20,
                    max_queue=1024,
                ) as ws:
                    self._ws = ws
                    source = "websocket" if self.use_ws_trades else "solana tracker polling"
                    print(f"[filter] connected to {self.settings.ws_url} (metrics via {source})")
                    delay = 1.0
                    await ws.send(
                        json.dumps(
                            {"method": "subscribeNewToken", "params": {"launchpad": "pumpfun"}}
                        )
                    )
                    # A reconnect loses the per-token trade subscriptions; restore them.
                    if self.use_ws_trades and self._pending:
                        await self._subscribe_trades(list(self._pending))
                    async for msg in ws:
                        await self._handle_message(msg)
            except asyncio.CancelledError:
                raise
            except (ConnectionClosed, WebSocketException, OSError) as exc:
                print(f"[filter] websocket dropped ({type(exc).__name__}: {exc}); "
                      f"reconnecting in {delay:.0f}s")
            finally:
                self._ws = None
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)

    def _ws_url(self) -> str:
        """The feed URL, with the PumpPortal API key appended when one is configured."""
        url = self.settings.ws_url
        key = self.settings.pumpportal_api_key
        if not key:
            return url
        return f"{url}{'&' if '?' in url else '?'}api-key={key}"

    async def _handle_message(self, msg: str | bytes) -> None:
        """Route one pumpportal frame to the create or the trade path."""
        try:
            data = json.loads(msg)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(data, dict):
            return
        # pumpportal acks subscriptions with a bare {"message": "..."} frame.
        if "txType" not in data:
            return
        tx_type = data.get("txType")
        if tx_type == "create":
            await self._on_create(data)
        elif tx_type in ("buy", "sell"):
            self._on_trade(data)

    async def _on_create(self, data: dict) -> None:
        """Register a new launch and start watching its trades and metadata."""
        token = Token.from_ws(data)
        if not token.address or token.address in self._pending or token.address in self._seen:
            return
        self.stats["seen"] += 1
        candidate = _Candidate(token=token)
        # The creator's own initial buy is the first data point, not organic demand,
        # but it is real volume so it counts toward volume_sol.
        if token.creator_address:
            candidate.buyers.add(token.creator_address)
        candidate.volume_sol = token.volume_sol
        self._pending[token.address] = candidate
        if self.use_ws_trades:
            await self._subscribe_trades([token.address])
        uri = str(data.get("uri") or "")
        if uri:
            self._spawn(self._fetch_metadata(token.address, uri))

    def _on_trade(self, data: dict) -> None:
        """Fold a buy/sell into the watched candidate's buyer set and volume."""
        mint = str(data.get("mint") or "")
        candidate = self._pending.get(mint)
        if candidate is None:
            return
        trader = str(data.get("traderPublicKey") or "")
        if trader and data.get("txType") == "buy":
            candidate.buyers.add(trader)
        try:
            candidate.volume_sol += abs(float(data.get("solAmount") or 0.0))
        except (TypeError, ValueError):
            pass
        v_tokens = data.get("vTokensInBondingCurve")
        if v_tokens is not None:
            try:
                candidate.token.bonding_curve_pct = curve_pct_from_reserves(float(v_tokens))
            except (TypeError, ValueError):
                pass

    async def _subscribe_trades(self, mints: list[str]) -> None:
        """Ask pumpportal to stream trades for these mints on the open connection."""
        ws = self._ws
        if ws is None or not mints:
            return
        with contextlib.suppress(ConnectionClosed, WebSocketException, OSError):
            await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": mints}))

    async def _unsubscribe_trades(self, mints: list[str]) -> None:
        """Stop streaming trades for mints we have finished with."""
        ws = self._ws
        if ws is None or not mints or not self.use_ws_trades:
            return
        with contextlib.suppress(ConnectionClosed, WebSocketException, OSError):
            await ws.send(json.dumps({"method": "unsubscribeTokenTrade", "keys": mints}))

    # --- metadata -------------------------------------------------------------

    async def _fetch_metadata(self, mint: str, uri: str) -> None:
        """Pull the off-chain metadata JSON and fill description/image/socials."""
        candidate = self._pending.get(mint)
        if candidate is None:
            return
        async with self._metadata_sem:
            if mint not in self._pending:
                return
            try:
                resp = await self._http.get(uri)
                resp.raise_for_status()
                meta = resp.json()
            except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
                print(f"[filter] metadata fetch failed for {mint[:8]}: {type(exc).__name__}")
                candidate.metadata_fetched = True
                return
        if not isinstance(meta, dict):
            candidate.metadata_fetched = True
            return
        token = candidate.token
        token.description = str(meta.get("description") or "")
        token.image_url = str(meta.get("image") or "")
        token.twitter = str(meta.get("twitter") or "")
        token.website = str(meta.get("website") or "")
        token.telegram = str(meta.get("telegram") or "")
        # "Has metadata" means a human filled something in, not merely that the URI resolved.
        token.has_metadata = bool(token.description or token.image_url or token.twitter)
        candidate.metadata_fetched = True

    def _spawn(self, coro) -> None:
        """Fire-and-forget a coroutine while keeping a reference so it is not GC'd."""
        task = asyncio.create_task(coro)
        self._tasks.append(task)
        task.add_done_callback(lambda t: self._tasks.remove(t) if t in self._tasks else None)

    # --- sweeper --------------------------------------------------------------

    async def _sweep_loop(self) -> None:
        """Every few seconds, judge candidates that have aged into decidability."""
        while True:
            await asyncio.sleep(5.0)
            try:
                await self._sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # sweeper must never die on one bad candidate
                print(f"[filter] sweep error: {type(exc).__name__}: {exc}")

    async def _sweep_once(self) -> None:
        """One pass over the watch buffer: emit passers, expire the stale."""
        now = time.time()
        drop: list[str] = []
        to_poll: list[_Candidate] = []
        for mint, candidate in list(self._pending.items()):
            token = candidate.token
            age = token.refresh_age()

            if age < self.f.min_age_minutes:
                continue
            # Give a slow IPFS gateway one extra sweep before judging on missing metadata.
            if not candidate.metadata_fetched and age < self.f.min_age_minutes + 0.5:
                continue

            if self.use_ws_trades:
                token.unique_buyers = len(candidate.buyers)
                token.volume_sol = candidate.volume_sol
            elif not candidate.metrics_polled:
                # Metrics are not on the socket, so they cost one Solana Tracker call.
                # Poll once per candidate, after everything cheaper has already passed.
                if self._passes_static(token):
                    to_poll.append(candidate)
                    continue
                drop.append(mint)
                self.stats["rejected"] += 1
                continue

            passed, reason = self._passes(token)
            if passed:
                drop.append(mint)
                self._remember(mint)
                self.stats["passed"] += 1
                print(
                    f"[filter] PASS {token.symbol or '?'} {mint[:8]} "
                    f"buyers={token.unique_buyers} vol={token.volume_sol:.2f} SOL "
                    f"curve={token.bonding_curve_pct:.1f}% age={age:.1f}m"
                )
                try:
                    self._queue.put_nowait(token)
                except asyncio.QueueFull:
                    print("[filter] downstream is saturated, dropping a passing token")
                continue

            # Not a pass yet. Keep watching until it either passes or times out, unless
            # the rejection is permanent (the curve only ever moves one way).
            terminal = reason.startswith(("curve_too_far", "no_mint_address"))
            if terminal or now - token.first_seen_ts > self.f.max_age_minutes * 60:
                drop.append(mint)
                self.stats["rejected" if terminal else "expired"] += 1

        if drop:
            for mint in drop:
                self._pending.pop(mint, None)
            await self._unsubscribe_trades(drop)

        if to_poll:
            await asyncio.gather(*(self._poll_metrics(c) for c in to_poll))

    def _passes_static(self, token: Token) -> bool:
        """The half of the gate that needs no trade data, checked before paying for any."""
        if not token.address:
            return False
        if self.f.require_metadata and not token.has_metadata:
            return False
        return token.bonding_curve_pct <= self.f.max_curve_pct

    async def _poll_metrics(self, candidate: _Candidate) -> None:
        """Fill buyers and volume from Solana Tracker, then re-run the full gate.

        Used only when there is no funded PumpPortal key. The fetched tape is cached by
        DataFetcher, so the pipeline's own enrichment call moments later is free.
        """
        token = candidate.token
        if self.data is None:
            print("[filter] no data fetcher and no PumpPortal key: cannot read trade metrics")
            candidate.metrics_polled = True
            return
        async with self._gate_sem:
            if token.address not in self._pending:
                return
            trades = await self.data.fetch_trades(token)
        candidate.metrics_polled = True
        buyers, volume = DataFetcher.metrics_from_trades(trades)
        token.unique_buyers = buyers
        token.volume_sol = max(volume, token.volume_sol)
        token.refresh_age()

        passed, reason = self._passes(token)
        if passed:
            self._pending.pop(token.address, None)
            self._remember(token.address)
            self.stats["passed"] += 1
            print(
                f"[filter] PASS {token.symbol or '?'} {token.address[:8]} "
                f"buyers={token.unique_buyers} vol={token.volume_sol:.2f} SOL "
                f"curve={token.bonding_curve_pct:.1f}% age={token.age_minutes:.1f}m"
            )
            try:
                self._queue.put_nowait(token)
            except asyncio.QueueFull:
                print("[filter] downstream is saturated, dropping a passing token")
            return

        # One poll per candidate: a launch that has not attracted buyers by now is not
        # worth a second API call, so it is dropped rather than re-polled every sweep.
        self._pending.pop(token.address, None)
        self.stats["rejected"] += 1

    def _remember(self, mint: str) -> None:
        """Record an emitted mint in a bounded LRU so reconnects cannot duplicate it."""
        self._seen[mint] = None
        self._seen.move_to_end(mint)
        while len(self._seen) > DEDUP_MEMORY:
            self._seen.popitem(last=False)
