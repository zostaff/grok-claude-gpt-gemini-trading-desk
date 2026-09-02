"""DataFetcher: Solana Tracker REST calls plus RPC wallet enrichment for the auditor."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

import httpx

from .config import Settings
from .models import Token

LAMPORTS_PER_SOL = 1_000_000_000
# getMultipleAccounts accepts at most 100 pubkeys per call.
RPC_ACCOUNT_BATCH = 100
# Signature paging is the expensive part, so only the wallets that matter get aged.
MAX_AGED_WALLETS = 20
AGE_CONCURRENCY = 4
# CodeFilter gates on the trade tape and the pipeline enriches moments later; a short TTL
# means that pair of calls costs one request instead of two.
FETCH_CACHE_TTL = 45.0


@dataclass
class RiskData:
    """Solana Tracker's rug-risk summary for one token."""

    risk_score: float = 0.0
    rugged: bool = False
    risks: list[dict] = field(default_factory=list)
    liquidity_usd: float = 0.0
    market_cap_usd: float = 0.0
    holder_count: int = 0
    mint_authority_revoked: bool | None = None
    freeze_authority_revoked: bool | None = None
    lp_burned: bool | None = None

    def to_dict(self) -> dict:
        """Flatten for prompts and JSONL logging."""
        return {
            "risk_score": self.risk_score,
            "rugged": self.rugged,
            "risks": [r.get("name", "") for r in self.risks],
            "liquidity_usd": round(self.liquidity_usd, 2),
            "market_cap_usd": round(self.market_cap_usd, 2),
            "holder_count": self.holder_count,
            "mint_authority_revoked": self.mint_authority_revoked,
            "freeze_authority_revoked": self.freeze_authority_revoked,
            "lp_burned": self.lp_burned,
        }

    def summary(self) -> str:
        """One-line human/prompt rendering."""
        names = ", ".join(r.get("name", "?") for r in self.risks[:6]) or "none reported"
        return (
            f"risk_score={self.risk_score} rugged={self.rugged} "
            f"liquidity=${self.liquidity_usd:,.0f} mcap=${self.market_cap_usd:,.0f} "
            f"holders={self.holder_count} flags=[{names}]"
        )


@dataclass
class TokenData:
    """Everything the LLM panel needs about one token beyond the Token record itself."""

    trades: list[dict] = field(default_factory=list)
    holders: list[dict] = field(default_factory=list)
    risk: RiskData = field(default_factory=RiskData)
    errors: list[str] = field(default_factory=list)

    def __iter__(self) -> Iterator[Any]:
        """Allow `trades, holders, risk = await data.fetch(token)`."""
        return iter((self.trades, self.holders, self.risk))


def _num(value: object, default: float = 0.0) -> float:
    """Coerce an untrusted API value to float without raising."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_list(payload: object, *keys: str) -> list[dict]:
    """Solana Tracker returns bare lists on some routes and wrapped objects on others."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in keys:
            inner = payload.get(key)
            if isinstance(inner, list):
                return [item for item in inner if isinstance(item, dict)]
    return []


class DataFetcher:
    """Fetches token info, top holders and recent trades, then enriches wallets via RPC.

    Solana Tracker gives the trade tape and the holder table; it does not give wallet
    balance or wallet age, which is exactly what the wallet auditor needs to spot fresh
    sniper wallets. Those two fields come from the Solana RPC, batched and bounded, and
    degrade to None (rendered "?") rather than being invented when the RPC is unhappy.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(
            base_url=settings.data_api,
            headers={"x-api-key": settings.data_api_key},
            timeout=15,
        )
        self.rpc = httpx.AsyncClient(base_url=settings.rpc_url, timeout=15)
        self._rpc_ok = True
        self._cache: dict[str, tuple[float, TokenData]] = {}

    async def aclose(self) -> None:
        """Close both HTTP clients."""
        await self.client.aclose()
        await self.rpc.aclose()

    # --- public API -----------------------------------------------------------

    async def fetch_trades(self, token: Token) -> list[dict]:
        """Fetch just the trade tape. This is the cheap call CodeFilter gates on."""
        try:
            payload = await self._get(f"/tokens/{token.address}/trades", params={"limit": 50})
        except httpx.HTTPError as exc:
            print(f"[data] trades for {token.address[:8]} failed: {type(exc).__name__}: {exc}")
            return []
        return self._parse_trades(payload, token)

    @staticmethod
    def metrics_from_trades(trades: list[dict]) -> tuple[int, float]:
        """Reduce a trade tape to (unique buying wallets, total SOL volume)."""
        buyers = {t["wallet"] for t in trades if t["wallet"] and t["side"] == "buy"}
        volume = sum(abs(t["amount_sol"]) for t in trades)
        return len(buyers), volume

    async def fetch(self, token: Token) -> TokenData:
        """Fetch info, holders and trades in parallel, then enrich the wallets seen."""
        address = token.address
        cached = self._cache.get(address)
        if cached and time.time() - cached[0] < FETCH_CACHE_TTL:
            return cached[1]
        info_r, holders_r, trades_r = await asyncio.gather(
            self._get(f"/tokens/{address}"),
            self._get(f"/tokens/{address}/holders/top"),
            self._get(f"/tokens/{address}/trades", params={"limit": 50}),
            return_exceptions=True,
        )

        data = TokenData()
        info = self._unwrap(info_r, "token info", data)
        holders_raw = self._unwrap(holders_r, "holders", data)
        trades_raw = self._unwrap(trades_r, "trades", data)

        data.risk = self._parse_risk(info)
        data.holders = self._parse_holders(holders_raw)
        data.trades = self._parse_trades(trades_raw, token)

        await self._enrich_wallets(data)
        self._cache[address] = (time.time(), data)
        self._prune_cache()
        return data

    def _prune_cache(self) -> None:
        """Drop expired cache entries so a long run cannot grow the dict without bound."""
        now = time.time()
        for address, (stamp, _) in list(self._cache.items()):
            if now - stamp >= FETCH_CACHE_TTL:
                del self._cache[address]

    # --- HTTP -----------------------------------------------------------------

    async def _get(self, path: str, params: dict | None = None) -> Any:
        """One GET against Solana Tracker, raising httpx.HTTPError on failure."""
        resp = await self.client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    def _unwrap(self, result: Any, label: str, data: TokenData) -> Any:
        """Turn a gather() result into a payload, recording the error if it failed."""
        if isinstance(result, BaseException):
            msg = f"{label}: {type(result).__name__}: {result}"
            print(f"[data] {msg}")
            data.errors.append(msg)
            return {}
        return result

    # --- parsing --------------------------------------------------------------

    def _parse_risk(self, info: Any) -> RiskData:
        """Pull the risk block, pools and holder count out of /tokens/{address}."""
        if not isinstance(info, dict):
            return RiskData()
        risk_block = info.get("risk") if isinstance(info.get("risk"), dict) else {}
        pools = info.get("pools") if isinstance(info.get("pools"), list) else []
        pool = pools[0] if pools and isinstance(pools[0], dict) else {}

        def _flag(name: str) -> bool | None:
            """Tri-state: True/False when the API says so, None when it is silent."""
            value = pool.get(name)
            if isinstance(value, bool):
                return value
            security = pool.get("security") if isinstance(pool.get("security"), dict) else {}
            value = security.get(name)
            return value if isinstance(value, bool) else None

        liquidity = pool.get("liquidity") if isinstance(pool.get("liquidity"), dict) else {}
        market_cap = pool.get("marketCap") if isinstance(pool.get("marketCap"), dict) else {}

        return RiskData(
            risk_score=_num(risk_block.get("score")),
            rugged=bool(risk_block.get("rugged", False)),
            risks=[r for r in (risk_block.get("risks") or []) if isinstance(r, dict)],
            liquidity_usd=_num(liquidity.get("usd")),
            market_cap_usd=_num(market_cap.get("usd")),
            holder_count=int(_num(info.get("holders"))),
            mint_authority_revoked=_flag("mintAuthority") is False
            if _flag("mintAuthority") is not None
            else None,
            freeze_authority_revoked=_flag("freezeAuthority") is False
            if _flag("freezeAuthority") is not None
            else None,
            lp_burned=_flag("lpBurn") if isinstance(_flag("lpBurn"), bool) else None,
        )

    def _parse_holders(self, payload: Any) -> list[dict]:
        """Normalise the top-holders route into a uniform list of dicts."""
        rows = _as_list(payload, "accounts", "holders", "data")
        out: list[dict] = []
        for row in rows:
            address = str(row.get("address") or row.get("wallet") or row.get("owner") or "")
            if not address:
                continue
            out.append(
                {
                    "address": address,
                    "percentage": _num(row.get("percentage")),
                    "amount": _num(row.get("amount")),
                    "value_usd": _num((row.get("value") or {}).get("usd")
                                      if isinstance(row.get("value"), dict) else row.get("value")),
                    "is_sniper": bool(row.get("sniper") or row.get("isSniper") or False),
                    "balance_sol": None,
                    "age_days": None,
                }
            )
        out.sort(key=lambda h: h["percentage"], reverse=True)
        return out

    def _parse_trades(self, payload: Any, token: Token) -> list[dict]:
        """Normalise the trade tape, oldest first, timed relative to the launch."""
        rows = _as_list(payload, "trades", "data")
        launch_ts = token.first_seen_ts
        out: list[dict] = []
        for row in rows:
            wallet = str(row.get("wallet") or row.get("owner") or "")
            # Solana Tracker reports trade time in milliseconds since epoch.
            raw_time = _num(row.get("time") or row.get("timestamp"))
            ts = raw_time / 1000.0 if raw_time > 1e11 else raw_time
            side = str(row.get("type") or "").lower() or "buy"
            amount_sol = _num(row.get("amountSol"))
            if not amount_sol:
                # Fall back to the SOL leg of the swap when the field is absent.
                for key in ("volumeSol", "solAmount"):
                    amount_sol = _num(row.get(key))
                    if amount_sol:
                        break
            out.append(
                {
                    "wallet": wallet,
                    "side": side,
                    "amount_sol": amount_sol,
                    "volume_usd": _num(row.get("volume")),
                    "price_usd": _num(row.get("priceUsd")),
                    "timestamp": ts,
                    "seconds_after_launch": max(0.0, ts - launch_ts) if ts and launch_ts else None,
                    "balance_sol": None,
                    "age_days": None,
                }
            )
        out.sort(key=lambda t: t["timestamp"] or 0.0)
        return out

    # --- RPC enrichment -------------------------------------------------------

    async def _enrich_wallets(self, data: TokenData) -> None:
        """Attach SOL balance and a first-activity lower bound to the wallets we care about."""
        if not self._rpc_ok:
            return
        wallets: list[str] = []
        for holder in data.holders[:15]:
            if holder["address"] not in wallets:
                wallets.append(holder["address"])
        for trade in data.trades[:40]:
            if trade["wallet"] and trade["wallet"] not in wallets:
                wallets.append(trade["wallet"])
        if not wallets:
            return

        balances = await self._get_balances(wallets)
        ages = await self._get_ages(wallets[:MAX_AGED_WALLETS])

        for holder in data.holders:
            holder["balance_sol"] = balances.get(holder["address"])
            holder["age_days"] = ages.get(holder["address"])
        for trade in data.trades:
            trade["balance_sol"] = balances.get(trade["wallet"])
            trade["age_days"] = ages.get(trade["wallet"])

    async def _rpc(self, method: str, params: list) -> Any:
        """One JSON-RPC call. Returns None and disables enrichment on repeated failure."""
        try:
            resp = await self.rpc.post(
                "", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            )
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            print(f"[data] rpc {method} failed ({type(exc).__name__}); wallet enrichment off")
            self._rpc_ok = False
            return None
        if isinstance(body, dict) and body.get("error"):
            print(f"[data] rpc {method} error: {body['error']}; wallet enrichment off")
            self._rpc_ok = False
            return None
        return body.get("result") if isinstance(body, dict) else None

    async def _get_balances(self, wallets: list[str]) -> dict[str, float]:
        """Batch getMultipleAccounts to read SOL balances, 100 pubkeys per call."""
        balances: dict[str, float] = {}
        for i in range(0, len(wallets), RPC_ACCOUNT_BATCH):
            chunk = wallets[i : i + RPC_ACCOUNT_BATCH]
            result = await self._rpc(
                "getMultipleAccounts", [chunk, {"encoding": "base64", "commitment": "confirmed"}]
            )
            if not isinstance(result, dict):
                break
            values = result.get("value") or []
            for wallet, account in zip(chunk, values):
                if isinstance(account, dict):
                    balances[wallet] = _num(account.get("lamports")) / LAMPORTS_PER_SOL
                elif account is None:
                    # Account does not exist on chain: an empty wallet, balance zero.
                    balances[wallet] = 0.0
        return balances

    async def _get_ages(self, wallets: list[str]) -> dict[str, float]:
        """Age wallets from their oldest reachable signature (a lower bound, not exact).

        getSignaturesForAddress returns newest-first and caps at 1000 per page. One page
        is enough to separate a wallet minted minutes ago from one with real history,
        which is the only distinction fresh_wallet_pct needs.
        """
        ages: dict[str, float] = {}
        semaphore = asyncio.Semaphore(AGE_CONCURRENCY)
        now = time.time()

        async def one(wallet: str) -> None:
            async with semaphore:
                if not self._rpc_ok:
                    return
                result = await self._rpc("getSignaturesForAddress", [wallet, {"limit": 1000}])
                if not isinstance(result, list) or not result:
                    return
                oldest = result[-1]
                block_time = _num(oldest.get("blockTime")) if isinstance(oldest, dict) else 0.0
                if block_time > 0:
                    ages[wallet] = max(0.0, (now - block_time) / 86400.0)

        await asyncio.gather(*(one(w) for w in wallets))
        return ages
