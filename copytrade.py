"""Real-time trades of followed wallets (copytrade source).

Intear events API (wss://api.intea.rs/ws, free, no key) pushes every DEX trade of a subscribed trader.
Rows of one transaction are collected for FLUSH_SEC and handed over as one net change per token:
    on_trade(leader, tx_id, deltas: {token: +/-raw}, ts_ms)
where "near" stands for both NEAR and wNEAR. Only trades that happen after the subscription are passed on —
the bot never copies stale trades (e.g. after a reconnect).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import websockets

log = logging.getLogger("copytrade")

WS_URL = "wss://api.intea.rs/ws"
FIELDS = ["id", "transaction_id", "receipt_id", "pool_id", "token_in", "amount_in", "token_out", "amount_out",
          "timestamp_millis"]
PER_CONN = 90          # server limit is 100 subscriptions per connection
FLUSH_SEC = 2.5        # wait for all hops of a multi-pool swap
STALE_MS = 120_000     # older trades are never copied


def norm(t: str) -> str:
    return "near" if t in ("near", "wrap.near") else t


class Conn:
    def __init__(self, hub: "Hub", n: int):
        self.hub, self.n = hub, n
        self.leaders: set[str] = set()
        self.ws = None
        self.next_id = 1
        self.pending: dict[int, asyncio.Future] = {}
        self.subs: dict[int, dict] = {}       # rid → {leader, columns, since_ms}
        self.by_leader: dict[str, int] = {}
        self.lock = asyncio.Lock()
        self.task = asyncio.create_task(self.run())

    async def run(self):
        while True:
            try:
                async with websockets.connect(self.hub.url, ping_interval=20, ping_timeout=20, max_size=None) as ws:
                    self.ws = ws
                    self.subs.clear()
                    self.by_leader.clear()
                    reader = asyncio.create_task(self.reader(ws))
                    await asyncio.sleep(0.5)
                    for leader in list(self.leaders):
                        await self.subscribe(leader)
                    log.info("copytrade conn %s: %d wallets", self.n, len(self.leaders))
                    await reader
            except Exception as e:
                log.warning("copytrade conn %s: %s", self.n, e)
            self.ws = None
            for f in self.pending.values():
                if not f.done():
                    f.set_exception(ConnectionError("ws closed"))
            self.pending.clear()
            await asyncio.sleep(5)

    async def reader(self, ws):
        async for raw in ws:
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            if "sub" in data:
                self.on_push(data)
            elif data.get("id") in self.pending:
                rid = data["id"]
                if rid in self.subs and isinstance(data.get("result"), dict) and data["result"].get("columns"):
                    self.subs[rid]["columns"] = data["result"]["columns"]
                f = self.pending.pop(rid)
                if not f.done():
                    f.set_result(data)

    async def request(self, method, params, rid=None):
        rid = rid or self.next_id
        self.next_id = max(self.next_id, rid) + 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        await self.ws.send(json.dumps({"id": rid, "method": method, "params": params}))
        data = await asyncio.wait_for(fut, 30)
        if "error" in data:
            raise RuntimeError(f"{method}: {data['error']}")
        return data.get("result")

    async def subscribe(self, leader: str):
        async with self.lock:
            if leader in self.by_leader or not self.ws:
                return
            rid = self.next_id
            self.subs[rid] = {"leader": leader, "columns": FIELDS, "since_ms": time.time() * 1000 - 5000}
            try:
                await self.request("trades.subscribe", {"fields": FIELDS, "trader": leader}, rid)
                self.by_leader[leader] = rid
            except Exception as e:
                self.subs.pop(rid, None)
                log.warning("copytrade subscribe %s: %s", leader, e)

    async def unsubscribe(self, leader: str):
        async with self.lock:
            rid = self.by_leader.pop(leader, None)
            self.subs.pop(rid, None)
            if rid and self.ws:
                try:
                    await self.request("unsubscribe", {"sub": rid})
                except Exception:
                    pass

    def on_push(self, data):
        sub = self.subs.get(data["sub"])
        if not sub:
            return
        if "end" in data:  # e.g. too_far_behind → subscribe again (only new trades from now on)
            leader = sub["leader"]
            self.subs.pop(data["sub"], None)
            self.by_leader.pop(leader, None)
            asyncio.create_task(self.subscribe(leader))
            return
        if data.get("columns"):
            sub["columns"] = data["columns"]
        for row in data.get("rows") or []:
            r = dict(zip(sub["columns"], row)) if isinstance(row, list) else row
            if not isinstance(r, dict):
                continue
            ts = int(r.get("timestamp_millis") or 0)
            if ts and (ts < sub["since_ms"] or ts < time.time() * 1000 - STALE_MS):
                continue
            self.hub.collect(sub["leader"], r)


class Hub:
    def __init__(self, on_trade, url: str = WS_URL):
        self.on_trade, self.url = on_trade, url
        self.conns: list[Conn] = []
        self.buf: dict[tuple[str, str], dict] = {}

    # ------------------------------------------------ subscriptions
    async def follow(self, leader: str):
        if any(leader in c.leaders for c in self.conns):
            return
        conn = next((c for c in self.conns if len(c.leaders) < PER_CONN), None)
        if not conn:
            conn = Conn(self, len(self.conns))
            self.conns.append(conn)
        conn.leaders.add(leader)
        await conn.subscribe(leader)

    async def unfollow(self, leader: str):
        for c in self.conns:
            if leader in c.leaders:
                c.leaders.discard(leader)
                await c.unsubscribe(leader)

    async def sync(self, leaders: set[str]):
        have = {l for c in self.conns for l in c.leaders}
        for l in leaders - have:
            await self.follow(l)
        for l in have - leaders:
            await self.unfollow(l)

    # ------------------------------------------------ one message per transaction
    def collect(self, leader: str, r: dict):
        if not r.get("token_in") or not r.get("token_out"):
            return
        tx = r.get("transaction_id") or r.get("receipt_id") or f"row{r.get('id')}"
        key = (leader, tx)
        if key not in self.buf:
            self.buf[key] = {"rows": {}, "ts": int(r.get("timestamp_millis") or time.time() * 1000)}
            asyncio.get_running_loop().call_later(FLUSH_SEC, lambda: asyncio.create_task(self.flush(key)))
        k = (r.get("receipt_id"), r.get("pool_id"), r.get("token_in"), str(r.get("amount_in")))
        self.buf[key]["rows"][k] = r  # the same hop can arrive twice

    async def flush(self, key):
        ev = self.buf.pop(key, None)
        if not ev:
            return
        deltas: dict[str, int] = {}
        for r in ev["rows"].values():
            ti, to = norm(r["token_in"]), norm(r["token_out"])
            deltas[ti] = deltas.get(ti, 0) - int(r.get("amount_in") or 0)
            deltas[to] = deltas.get(to, 0) + int(r.get("amount_out") or 0)
        deltas = {t: v for t, v in deltas.items() if v}
        try:
            await self.on_trade(key[0], key[1], deltas, ev["ts"])
        except Exception:
            log.exception("copytrade on_trade %s %s", *key)
