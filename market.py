"""Рыночные данные токена для карточки трейд-бота (адаптировано из near-analyzer-bot/scanner.py).

Только чтение публичных данных: ключи и подпись сюда не попадают.

Данные:
  • DexScreener — цена, FDV, ликвидность, объём, сделки, изменения цены
  • NEAR RPC (FastNEAR) — название, тикер, decimals, total supply из контракта
  • FastNEAR API — топ держателей токена
  • GeckoTerminal — свечи для графика (если пул там проиндексирован)
"""
from __future__ import annotations

import asyncio
import base64
import io
import os
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx



def f(x, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def usd(x: float) -> str:
    x = f(x)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(x) >= div:
            return f"${x / div:.1f}{suf}"
    return f"${x:,.0f}"

log = logging.getLogger("scanner")

RPC_URLS = ["https://rpc.mainnet.fastnear.com", "https://free.rpc.fastnear.com", "https://rpc.mainnet.near.org"]
FASTNEAR = "https://api.fastnear.com"
GECKO = "https://api.geckoterminal.com/api/v2"
DEXS = "https://api.dexscreener.com"
UA = {"User-Agent": "near-trade-bot/1.0"}

# Адрес токена NEAR: something.near / something.tg / 64 hex-символа
ADDR_RE = re.compile(r"(?<![\w.-])((?:[a-z0-9_\-]+\.)+(?:near|tg)|[0-9a-f]{64})(?![\w.-])")
URL_RES = [
    re.compile(r"nearly\.trade/([a-z0-9_\-.]+)"),
    re.compile(r"justhoot\.fun/p/([a-z0-9_\-.]+)"),
    re.compile(r"nearblocks\.io/(?:token|address)/([a-z0-9_\-.]+)"),
]
DS_PAIR_RE = re.compile(r"dexscreener\.com/near/([a-z0-9_\-.:]+)")
GT_POOL_RE = re.compile(r"geckoterminal\.com/near/pools/([a-z0-9_\-.:]+)")

# Контракты DEX / лаунчпадов: среди холдеров помечаем их как пул, а не как человека
POOL_ACCOUNTS = {"nearlytrade.near", "launchpad.justhoot.near", "v2-launchpad.justhoot.near", "intents.near",
                 "veax.near", "v1.orderbook.near", "dex.intear.near", "aidols.near"}
DCL_CONTRACT = "dclv2.ref-labs.near"
DCL_FEES = (10000, 2000, 100, 400)   # nearly.trade launches go to the 1% (10000) pool
NEARLY = "https://nearly.trade/api"
NEARLY_HDR = {"User-Agent": "Mozilla/5.0 (compatible; near-analyzer-bot/1.0)", "Accept": "application/json",
              "Referer": "https://nearly.trade/"}
POOL_SUFFIXES = (".ref-finance.near", ".ref-labs.near", ".rhealab.near", "ref-finance.near")


@dataclass
class TokenCard:
    address: str
    name: str = "?"
    symbol: str = "?"
    decimals: int = 0
    supply_raw: int = 0
    price: float = 0.0
    fdv: float = 0.0
    liq: float = 0.0
    vol24: float = 0.0
    txns24: int = 0
    chg: dict = field(default_factory=dict)
    created_ms: float = 0.0
    dex: str = ""
    pair_address: str = ""
    pair_url: str = ""
    holders: list = field(default_factory=list)   # [(account, pct, is_pool)]
    holders_total: int = 0
    holders_more: bool = False                     # держателей больше, чем вернул API
    candles: list = field(default_factory=list)   # [(ts, o, h, l, c, v)] по возрастанию времени
    near_usd: float = 0.0
    ath_mcap: float = 0.0
    links: list = field(default_factory=list)      # [(label, url)] сайт / X / Telegram / Discord
    onchain: bool = False                          # цена не с DexScreener: из nearly.trade API или пула Ref DCL
    src: str = ""                                  # "nearly" | "dcl" — откуда цена, если onchain

    @property
    def supply(self) -> float:
        return self.supply_raw / 10 ** self.decimals if self.decimals else float(self.supply_raw)

    @property
    def top10(self) -> float:
        return sum(p for _, p, pool in self.holders if not pool)


def extract_address(text: str) -> tuple[str | None, str | None]:
    """Ищет в тексте адрес токена или ссылку. Возвращает (адрес токена, адрес пары/пула для дорезолва)."""
    t = (text or "").strip().lower()
    for rx in URL_RES:
        m = rx.search(t)
        if m:
            return m.group(1).rstrip("."), None
    m = DS_PAIR_RE.search(t)
    if m:
        return None, "ds:" + m.group(1)
    m = GT_POOL_RE.search(t)
    if m:
        return None, "gt:" + m.group(1)
    m = ADDR_RE.search(t)
    return (m.group(1), None) if m else (None, None)


def is_pool_account(acc: str, token: str) -> bool:
    return acc == token or acc in POOL_ACCOUNTS or acc.endswith(POOL_SUFFIXES)


def _add_link(c: "TokenCard", label: str, url: str):
    if not url or not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return
    if any(u == url for _, u in c.links) or any(lb == label for lb, _ in c.links):
        return
    c.links.append((label, url))


SOCIAL_LABELS = {"twitter": "X", "x": "X", "telegram": "Telegram", "discord": "Discord", "website": "Website"}


class Scanner:
    def __init__(self, rpc=None):
        self.rpc = rpc  # near.Rpc трейд-бота: перебор бесплатных узлов
        self.client = httpx.AsyncClient(timeout=15, headers=UA)
        self._gt_pool: dict[str, str | None] = {}       # токен -> пул GeckoTerminal (кэш)
        self._chart_cache: dict[str, tuple[float, list]] = {}
        self._ath_cache: dict[str, tuple[float, float]] = {}     # токен -> (время, макс. цена)
        self._dcl_cache: dict[str, tuple[float, str | None]] = {}
        self._ath_busy: set[str] = set()
        self._prefetching: set[str] = set()  # токен -> (время, pool_id DCL или None)
        self._near_px: tuple[float, float] | None = None
        self._meta: dict[str, tuple[float, dict, int]] = {}
        self._nearly: dict[str, tuple[float, dict]] = {}       # токен -> (время, /api/launch)   # токен -> (время, ft_metadata, supply_raw)
        # bot plugs these in: save every on-chain price / read them back, so un-indexed pools still get a chart
        self.px_sink = None   # (token, ts, price) -> None
        self.px_hist = None   # (token, since_ts) -> [(ts, price)]

    async def _json(self, method: str, url: str, **kw):
        try:
            r = await self.client.request(method, url, **kw)
            if r.status_code >= 400:
                log.info("HTTP %s %s", r.status_code, url)
                return None
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            log.info("Ошибка запроса %s: %s", url, e)
            return None

    async def _view(self, account: str, method: str, args: dict | None = None):
        if self.rpc is not None:
            try:
                return await self.rpc.view(account, method, args, finality="final")
            except Exception:
                return None
        raw = json.dumps(args or {}).encode()
        payload = {"jsonrpc": "2.0", "id": "1", "method": "query",
                   "params": {"request_type": "call_function", "finality": "final", "account_id": account,
                              "method_name": method, "args_base64": base64.b64encode(raw).decode()}}
        for url in RPC_URLS:
            data = await self._json("POST", url, json=payload)
            if not data or data.get("error"):  # node down / rate-limited → try the next RPC, it's not "no such token"
                continue
            res = (data.get("result") or {}).get("result")
            if res is None:
                return None  # метода нет — это не токен
            try:
                return json.loads(bytes(res).decode())
            except ValueError:
                return None
        return None

    async def resolve_pair(self, ref: str) -> str | None:
        """Ссылка на пару DexScreener / пул GeckoTerminal -> адрес токена."""
        kind, pid = ref.split(":", 1)
        if kind == "ds":
            data = await self._json("GET", f"{DEXS}/latest/dex/pairs/near/{pid}")
            pairs = (data or {}).get("pairs") or []
            return pairs[0]["baseToken"]["address"] if pairs else None
        data = await self._json("GET", f"{GECKO}/networks/near/pools/{pid}")
        try:
            return data["data"]["relationships"]["base_token"]["data"]["id"].split("_", 1)[1]
        except (TypeError, KeyError, IndexError):
            return None

    async def card(self, address: str, with_chart: bool = True, with_holders: bool = True) -> TokenCard | None:
        c = TokenCard(address=address)
        # независимые запросы — параллельно (раньше шли по очереди и карточка ждала каждый)
        meta, supply, _, _ = await asyncio.gather(
            self._view(address, "ft_metadata"), self._view(address, "ft_total_supply"),
            self._fill_market(c), self._gt_pool_of(address) if with_chart else asyncio.sleep(0))
        if not isinstance(meta, dict):
            return None
        c.name = str(meta.get("name") or "?")[:60]
        c.symbol = str(meta.get("symbol") or "?")[:20]
        c.decimals = int(f(meta.get("decimals")))
        try:
            c.supply_raw = int(supply)
        except (TypeError, ValueError):
            c.supply_raw = 0
        if c.price and not c.fdv:
            c.fdv = c.price * c.supply

        if not c.price and c.address.endswith(".nearlytrade.near"):
            await self._fill_nearly(c)
        if not c.price:
            await self._fill_onchain(c)
        if with_holders:
            await self._fill_holders(c)
        if with_chart:
            await self._fill_chart(c)
        else:
            self._cached_ath(c)
            self.prefetch_chart(c)
        return c

    def _cached_ath(self, c: TokenCard):
        """ATH из уже загруженных свечей (без новых запросов) — для быстрой карточки без графика."""
        best = max((r[2] for r in (self._chart_cache.get(c.address) or (0, []))[1]), default=0.0)
        best = max(best, (self._ath_cache.get(c.address) or (0, 0.0))[1])
        if best and c.supply:
            c.ath_mcap = max(c.ath_mcap, best * c.supply, c.fdv)

    def prefetch_chart(self, c: TokenCard):
        """Свечи и ATH грузим в фоне: кнопка «График» откроется быстрее, ATH появится при обновлении."""
        if c.address in self._prefetching:
            return
        self._prefetching.add(c.address)
        tmp = TokenCard(address=c.address, symbol=c.symbol, decimals=c.decimals, supply_raw=c.supply_raw,
                        price=c.price, fdv=c.fdv)

        async def run():
            try:
                await self._fill_chart(tmp)
            except Exception as e:
                log.info("prefetch chart %s: %s", c.address, e)
            finally:
                self._prefetching.discard(c.address)
        asyncio.create_task(run())

    async def _fill_market(self, c: TokenCard):
        data = await self._json("GET", f"{DEXS}/tokens/v1/near/{c.address}")
        pairs = [p for p in (data if isinstance(data, list) else [])
                 if (p.get("baseToken") or {}).get("address") == c.address]
        if not pairs:
            return
        best = max(pairs, key=lambda p: f((p.get("liquidity") or {}).get("usd")))
        c.price = f(best.get("priceUsd"))
        c.liq = sum(f((p.get("liquidity") or {}).get("usd")) for p in pairs)
        c.vol24 = sum(f((p.get("volume") or {}).get("h24")) for p in pairs)
        c.txns24 = sum(int(f(((p.get("txns") or {}).get("h24") or {}).get(k)))
                       for p in pairs for k in ("buys", "sells"))
        c.chg = {k: f(v) for k, v in (best.get("priceChange") or {}).items()}
        c.created_ms = c.created_ms or f(best.get("pairCreatedAt"))
        c.pair_url = best.get("url") or ""
        info = best.get("info") or {}
        for sc in info.get("socials") or []:
            _add_link(c, SOCIAL_LABELS.get(str(sc.get("type")).lower(), str(sc.get("type")).title()), sc.get("url"))
        for w in info.get("websites") or []:
            _add_link(c, "Website", w.get("url"))
        c.created_ms = min(f(p.get("pairCreatedAt")) or 9e15 for p in pairs)
        c.created_ms = 0 if c.created_ms >= 9e15 else c.created_ms
        c.dex = best.get("dexId", "")
        c.pair_address = best.get("pairAddress", "")
        c.pair_url = best.get("url") or f"https://dexscreener.com/near/{c.pair_address}"
        c.fdv = f(best.get("fdv") or best.get("marketCap")) or c.price * c.supply

    async def _near_usd(self) -> float:
        hit = self._near_px
        if hit and time.time() - hit[0] < 120:
            return hit[1]
        data = await self._json("GET", "https://api.coingecko.com/api/v3/simple/price",
                                params={"ids": "near", "vs_currencies": "usd"})
        px = f(((data or {}).get("near") or {}).get("usd"))
        if not px:
            data = await self._json("GET", f"{DEXS}/tokens/v1/near/wrap.near")
            pairs = [p for p in (data if isinstance(data, list) else [])
                     if (p.get("baseToken") or {}).get("address") == "wrap.near"]
            if pairs:
                px = f(max(pairs, key=lambda p: f((p.get("liquidity") or {}).get("usd"))).get("priceUsd"))
        if px:
            self._near_px = (time.time(), px)
        return px or (hit[1] if hit else 0.0)

    async def _dcl_pool(self, token: str) -> tuple[str, dict, bool] | None:
        """Find the token/wNEAR pool on Ref DCL v2 (where nearly.trade migrates). (pool_id, info, token_is_x)."""
        cached = self._dcl_cache.get(token)
        if cached and time.time() - cached[0] < 3600 and cached[1] is None:
            return None
        ids = [cached[1]] if cached and cached[1] else [
            f"{a}|{b}|{fee}" for fee in DCL_FEES for a, b in ((token, "wrap.near"), ("wrap.near", token))]
        for pid in ids:
            info = await self._view(DCL_CONTRACT, "get_pool", {"pool_id": pid})
            if isinstance(info, dict) and info.get("current_point") is not None:
                self._dcl_cache[token] = (time.time(), pid)
                return pid, info, pid.startswith(token + "|")
        self._dcl_cache[token] = (time.time(), None)
        return None

    async def _fill_onchain(self, c: TokenCard):
        """No DexScreener pair yet (fresh migration) → price straight from the Ref DCL pool on-chain."""
        try:
            found = await self._dcl_pool(c.address)
            if not found:
                return
            pid, info, token_is_x = found
            near = await self._near_usd()
            if not near:
                return
            raw = 1.0001 ** int(info["current_point"])        # amount_y / amount_x in raw units
            if token_is_x:
                in_near = raw * 10 ** (c.decimals - 24)
                near_raw = f(info.get("total_y"))
            else:
                in_near = (1 / raw) * 10 ** (c.decimals - 24) if raw else 0
                near_raw = f(info.get("total_x"))
            if in_near <= 0:
                return
            c.price = in_near * near
            c.fdv = c.price * c.supply
            c.liq = 2 * near_raw / 1e24 * near if near_raw else 0
            c.dex = "ref-dcl"
            c.pair_address = pid
            c.onchain = True
            c.src = "dcl"
            if self.px_sink:
                self.px_sink(c.address, time.time(), c.price)
        except Exception as e:  # pool layout surprises must never break the card
            log.info("DCL price %s: %s", c.address, e)

    async def onchain_mcap(self, token: str) -> float:
        """Market cap for tokens DexScreener doesn't know yet (used by the calls refresher)."""
        ms = await self._meta_supply(token)
        if not ms:
            return 0.0
        c = TokenCard(address=token, decimals=int(f(ms[0].get("decimals"))), supply_raw=ms[1])
        await self._fill_onchain(c)
        return c.fdv

    async def _nearly_launch(self, token: str) -> dict | None:
        """nearly.trade's own data for its launches (price, FDV, liquidity, volume, ATH) — cached 30s."""
        hit = self._nearly.get(token)
        if hit and time.time() - hit[0] < 30:
            return hit[1]
        d = await self._json("GET", f"{NEARLY}/launch/{token}", headers=NEARLY_HDR)
        if not isinstance(d, dict) or not d.get("token"):
            return hit[1] if hit else None
        self._nearly[token] = (time.time(), d)
        return d

    async def _fill_nearly(self, c: TokenCard):
        try:
            d = await self._nearly_launch(c.address)
            if not d:
                return
            near = f(d.get("near_usd")) or await self._near_usd()
            c.price = f(d.get("price_usd")) or f(d.get("price_near")) * near
            if not c.price:
                return
            c.fdv = f(d.get("fdv_usd")) or c.price * c.supply
            c.liq = f(d.get("liquidity_near")) * near
            c.vol24 = f(d.get("volume_24h_near")) * near
            c.txns24 = int(f(d.get("trades_24h")))
            if d.get("change_24h") is not None:
                c.chg["h24"] = f(d.get("change_24h")) * 100
            c.created_ms = f(d.get("created_at_ms"))
            c.ath_mcap = max(f(d.get("ath_fdv_usd")), c.fdv)
            c.dex = "nearly"
            c.pair_address = str(d.get("pool_id") or "")
            if c.pair_address:
                self._dcl_cache[c.address] = (time.time(), c.pair_address)
            c.onchain, c.src = True, "nearly"
            c.near_usd = near
            for k, v in (d.get("links") or {}).items():
                _add_link(c, SOCIAL_LABELS.get(str(k).lower(), str(k).title()), v)
            if self.px_sink:
                self.px_sink(c.address, time.time(), c.price)
        except Exception as e:
            log.info("nearly launch %s: %s", c.address, e)

    async def _nearly_chart(self, c: TokenCard) -> bool:
        """15m candles straight from nearly.trade (full history since launch), converted from NEAR to USD."""
        d = await self._json("GET", f"{NEARLY}/candles/{c.address}", params={"tf": "15m", "limit": 96},
                             headers=NEARLY_HDR)
        if not isinstance(d, list) or not d:
            return False
        near = c.near_usd or await self._near_usd()
        rows = []
        for r in d:
            try:
                rows.append((f(r["t"]) / 1000, f(r["o"]) * near, f(r["h"]) * near, f(r["l"]) * near,
                             f(r["c"]) * near, f(r.get("v")) * near))
            except (KeyError, TypeError):
                continue
        rows.sort(key=lambda r: r[0])
        if not rows:
            return False
        c.candles = rows[-96:]
        if c.supply:
            c.ath_mcap = max(c.ath_mcap, max(r[2] for r in c.candles) * c.supply, c.fdv)
        now = time.time()
        for key, sec in (("h1", 3600), ("h6", 21600)):
            ref = next((r[1] for r in c.candles if r[0] >= now - sec), c.candles[0][1])
            if ref and key not in c.chg:
                c.chg[key] = (c.price / ref - 1) * 100
        return True

    async def _meta_supply(self, token: str) -> tuple[dict, int] | None:
        """ft_metadata + total supply, cached 10 min — the background checks must not hammer the RPC."""
        hit = self._meta.get(token)
        if hit and time.time() - hit[0] < 600:
            return hit[1], hit[2]
        meta = await self._view(token, "ft_metadata")
        if not isinstance(meta, dict):
            return (hit[1], hit[2]) if hit else None
        try:
            sup = int(await self._view(token, "ft_total_supply"))
        except (TypeError, ValueError):
            return (hit[1], hit[2]) if hit else None
        self._meta[token] = (time.time(), meta, sup)
        return meta, sup

    def has_dcl_pool(self, token: str) -> bool | None:
        """True/False if we already know whether the token has a Ref DCL pool, None if never checked (or expired)."""
        cached = self._dcl_cache.get(token)
        if not cached:
            return None
        if cached[1]:
            return True
        return False if time.time() - cached[0] < 3600 else None

    async def onchain_pair(self, token: str) -> dict | None:
        """DexScreener-shaped pair dict built from the Ref DCL pool (for alerts on tokens DexScreener hasn't indexed)."""
        ms = await self._meta_supply(token)
        if not ms:
            return None
        meta, sup = ms
        c = TokenCard(address=token, decimals=int(f(meta.get("decimals"))), supply_raw=sup)
        if token.endswith(".nearlytrade.near"):
            await self._fill_nearly(c)
        if not c.fdv:
            await self._fill_onchain(c)
        if not c.fdv:
            return None
        nd = self._nearly.get(token, (0, {}))[1] if c.src == "nearly" else {}
        near = c.near_usd or 0
        if nd:
            return {"chainId": "near", "_onchain": True, "url": f"https://nearly.trade/{token}",
                    "baseToken": {"address": token, "symbol": str(nd.get("symbol") or meta.get("symbol") or "")[:20],
                                  "name": str(nd.get("name") or meta.get("name") or "")[:60]},
                    "priceUsd": c.price, "marketCap": c.fdv, "fdv": c.fdv, "liquidity": {"usd": c.liq},
                    "volume": {"h1": f(nd.get("volume_1h_near")) * near, "h24": c.vol24},
                    "priceChange": {"h24": c.chg.get("h24", 0)}, "pairCreatedAt": c.created_ms,
                    "info": {"imageUrl": nd.get("icon"),
                             "websites": [{"label": "Website", "url": u} for u in [(nd.get("links") or {}).get("website")] if u],
                             "socials": [{"type": k, "url": v} for k, v in (nd.get("links") or {}).items()
                                         if k in ("twitter", "telegram") and v]}}
        return {"chainId": "near", "_onchain": True, "url": f"https://nearblocks.io/token/{token}",
                "baseToken": {"address": token, "symbol": str(meta.get("symbol") or "")[:20],
                              "name": str(meta.get("name") or "")[:60]},
                "priceUsd": c.price, "marketCap": c.fdv, "fdv": c.fdv, "liquidity": {"usd": c.liq},
                "info": {"imageUrl": meta.get("icon") if str(meta.get("icon") or "").startswith("http") else None}}

    async def _fill_holders(self, c: TokenCard):
        data = await self._json("GET", f"{FASTNEAR}/v1/ft/{c.address}/top")
        accounts = (data or {}).get("accounts") or []
        if not accounts or not c.supply_raw:
            return
        rows = []
        for a in accounts:
            try:
                bal = int(a.get("balance") or 0)
            except ValueError:
                continue
            if bal > 0:
                acc = a.get("account_id", "")
                rows.append((acc, bal * 100 / c.supply_raw, is_pool_account(acc, c.address)))
        rows.sort(key=lambda r: r[1], reverse=True)
        c.holders_total = len(rows)
        c.holders_more = len(accounts) >= 100
        people = [r for r in rows if not r[2]][:10]
        pools = [r for r in rows if r[2]][:2]
        c.holders = people + pools

    async def _gt_pool_of(self, address: str):
        if address in self._gt_pool:
            return
        data = await self._json("GET", f"{GECKO}/networks/near/tokens/{address}/pools", params={"page": 1})
        pools = (data or {}).get("data") or []
        try:
            self._gt_pool[address] = pools[0]["attributes"]["address"] if pools else None
        except (KeyError, TypeError):
            self._gt_pool[address] = None

    async def _long_ath(self, address: str, pool: str):
        data = await self._json("GET", f"{GECKO}/networks/near/pools/{pool}/ohlcv/hour",
                                params={"aggregate": 4, "limit": 1000, "currency": "usd", "token": address})
        rows = (((data or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        self._ath_cache[address] = (time.time(), max((float(row[2]) for row in rows if len(row) >= 6), default=0.0))

    async def _fill_chart(self, c: TokenCard):
        await self._gt_pool_of(c.address)
        pool = self._gt_pool.get(c.address)
        if not pool:  # пула нет или запрос не прошёл — проверим снова при следующем скане
            self._gt_pool.pop(c.address, None)
        if not pool:
            if (c.src == "nearly" or c.address.endswith(".nearlytrade.near")) and await self._nearly_chart(c):
                return
            if c.onchain:
                self._onchain_chart(c)
            return
        cached = self._chart_cache.get(c.address)
        if cached and time.time() - cached[0] < 60:
            c.candles = cached[1]
        else:
            data = await self._json("GET", f"{GECKO}/networks/near/pools/{pool}/ohlcv/minute",
                                    params={"aggregate": 15, "limit": 96, "currency": "usd", "token": c.address})
            ohlcv = (((data or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
            c.candles = sorted((tuple(float(x) for x in row[:6]) for row in ohlcv if len(row) >= 6),
                               key=lambda r: r[0])
            if len(c.candles) < 3 and c.address.endswith(".nearlytrade.near") and await self._nearly_chart(c):
                pass  # GeckoTerminal знает пул, но свечей нет — берём свечи nearly.trade
            self._chart_cache[c.address] = (time.time(), c.candles)
        ath_price = max((r[2] for r in c.candles), default=0.0)
        cached_ath = self._ath_cache.get(c.address)
        if cached_ath:
            ath_price = max(ath_price, cached_ath[1])
        if not cached_ath or time.time() - cached_ath[0] >= 600:
            # ATH за полгода (4-часовые свечи) — тяжёлый запрос: не ждём его, он догрузится к следующему открытию
            if c.address not in self._ath_busy:
                self._ath_busy.add(c.address)
                t = asyncio.create_task(self._long_ath(c.address, pool))
                t.add_done_callback(lambda _t, a=c.address: self._ath_busy.discard(a))
        if ath_price and c.supply:
            c.ath_mcap = max(ath_price * c.supply, c.fdv)

    def _onchain_chart(self, c: TokenCard, step: int = 900, n: int = 96):
        """15m candles from the prices the bot recorded from the Ref DCL pool (GeckoTerminal doesn't see it yet)."""
        if not self.px_hist:
            return
        now = time.time()
        rows = sorted(self.px_hist(c.address, now - step * n))
        if not rows:
            return
        buckets: dict[int, list[float]] = {}
        for ts, px in rows:
            buckets.setdefault(int(ts // step) * step, []).append(px)
        candles, prev = [], None
        for b in range(min(buckets), int(now // step) * step + 1, step):
            pts = buckets.get(b)
            if pts:
                o = prev if prev is not None else pts[0]
                candles.append((float(b), o, max(o, *pts), min(o, *pts), pts[-1], 0.0))
                prev = pts[-1]
            elif prev is not None:  # no sample in this 15m → flat candle at the last price
                candles.append((float(b), prev, prev, prev, prev, 0.0))
        c.candles = candles[-n:]
        if c.candles and c.supply:
            c.ath_mcap = max(max(r[2] for r in c.candles) * c.supply, c.fdv)
        first = c.candles[0][1] if c.candles else 0
        for key, sec in (("h1", 3600), ("h6", 21600), ("h24", 86400)):
            ref = next((r[1] for r in c.candles if r[0] >= now - sec), first)
            if ref:
                c.chg.setdefault(key, (c.price / ref - 1) * 100)

    async def close(self):
        await self.client.aclose()


# ---------- форматирование ----------
SUB = str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉")


def price_fmt(p: float) -> str:
    """$0.0₅1417 — как в сканерах: число нулей после запятой пишется маленькой цифрой."""
    if p <= 0:
        return "$0"
    if p >= 1:
        return f"${p:,.4f}"
    from decimal import Decimal
    p = float(p)  # np.float64 из matplotlib → обычный float (repr numpy-числа Decimal не понимает)
    if p != p or p in (float("inf"), float("-inf")):
        return "$0"
    s = format(Decimal(repr(p)), "f").split(".")[1] + "0" * 20  # repr: 0.0014 не превращается в 0.0013999…
    zeros = len(s) - len(s.lstrip("0"))
    digits = s[zeros:zeros + 4].rstrip("0") or "0"
    if zeros >= 4:
        return f"$0.0{str(zeros).translate(SUB)}{digits}"
    return f"$0.{'0' * zeros}{digits}"


def age_fmt(created_ms: float) -> str:
    if not created_ms:
        return "?"
    sec = max(0, time.time() - created_ms / 1000)
    if sec < 3600:
        return f"{int(sec // 60)}m"
    if sec < 86400:
        return f"{int(sec // 3600)}h"
    return f"{int(sec // 86400)}d"


def pct_fmt(x: float) -> str:
    return f"{x:+.2f}%"


def render_chart(c: TokenCard) -> bytes | None:
    """Свечной график 15m с шапкой (тикер, MCAP, LIQ, VOL, TXNS, 1H, 6H) — картинка PNG."""
    if len(c.candles) < 3:
        return None
    os.environ.setdefault("MPLCONFIGDIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), ".mpl"))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    bg, fg, dim, up, down = "#15161c", "#e8e8ea", "#8a8d98", "#26a69a", "#ef5350"
    fig = plt.figure(figsize=(8, 4.8), dpi=130, facecolor=bg)
    head = fig.add_axes([0, 0.68, 1, 0.32], facecolor=bg)
    head.axis("off")
    head.set_ylim(0, 1)
    ax = fig.add_axes([0.04, 0.09, 0.84, 0.52], facecolor=bg)

    ch = c.chg.get("h24", 0)
    head.text(0.03, 0.84, c.symbol.upper(), color=fg, fontsize=17, fontweight="bold", va="center")
    head.text(0.03, 0.62, f"{c.name}  ·  {age_fmt(c.created_ms)}", color=dim, fontsize=10, va="center")
    head.text(0.97, 0.84, f"{'↑' if ch >= 0 else '↓'}{abs(ch):.2f}%", color=up if ch >= 0 else down,
              fontsize=17, fontweight="bold", ha="right", va="center")
    cols = [("MCAP", usd(c.fdv), fg), ("LIQ", usd(c.liq), fg), ("VOL", "—" if c.src == "dcl" else usd(c.vol24), fg),
            ("TXNS", "—" if c.src == "dcl" else str(c.txns24), fg)]
    for k in ("h1", "h6"):
        v = c.chg.get(k, 0)
        cols.append((k[1:] + "H", f"{'↑' if v >= 0 else '↓'}{abs(v):.2f}%", up if v >= 0 else down))
    for i, (label, val, color) in enumerate(cols):
        x = 0.03 + i * 0.16
        head.text(x, 0.36, label, color=dim, fontsize=9, va="center")
        head.text(x, 0.17, val, color=color, fontsize=11, fontweight="bold", va="center")

    w = 0.6
    for i, (_, o, h, l, cl, _) in enumerate(c.candles):
        color = up if cl >= o else down
        ax.plot([i, i], [l, h], color=color, linewidth=0.9)
        ax.add_patch(Rectangle((i - w / 2, min(o, cl)), w, max(abs(cl - o), (h - l) * 0.02 or 1e-18),
                               facecolor=color, edgecolor=color))
    last = c.candles[-1][4]
    ax.axhline(last, color=down, linestyle=":", linewidth=0.8)
    ax.set_xlim(-1, len(c.candles))
    lo, hi = min(r[3] for r in c.candles), max(r[2] for r in c.candles)
    pad = (hi - lo) * 0.08 or hi * 0.05 or 1e-18
    ax.set_ylim(lo - pad, hi + pad)
    ax.yaxis.tick_right()
    ax.tick_params(colors=dim, labelsize=8, length=0)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: price_fmt(v)[1:]))
    step = max(1, len(c.candles) // 6)
    idx = list(range(0, len(c.candles), step))
    ax.set_xticks(idx)
    ax.set_xticklabels([datetime.fromtimestamp(c.candles[i][0], timezone.utc).strftime("%H:%M") for i in idx])
    for s in ax.spines.values():
        s.set_visible(False)
    ax.grid(color="#262833", linewidth=0.6)
    ax.text(0.0, 1.02, "USD 15m · UTC", transform=ax.transAxes, color=dim, fontsize=8)
    ax.text(len(c.candles) + 0.3, last, " " + price_fmt(last)[1:] + " ", color="white", fontsize=8,
            va="center", ha="left", bbox=dict(facecolor=down, edgecolor="none", pad=1.5), clip_on=False)
    fig.text(0.5, 0.4, "NDbot", color="#ffffff", alpha=0.05, fontsize=40, ha="center", va="center",
             fontweight="bold")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=bg)
    plt.close(fig)
    return buf.getvalue()
