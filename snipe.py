"""New token deploys on NEAR memepads (nearly.trade / JustHoot) — the source for the deployer sniper.

Intear (wss://api.intea.rs/ws, free, no key) pushes every newly created token account. For memepad tokens
(X.nearlytrade.near, X.launchpad.justhoot.near, X.v2-launchpad.justhoot.near) the dev = signer of the deploy
transaction, found in the block Intear reported (neardata.xyz, ≤5 block downloads). Then
    on_deploy(token, factory, dev, tx)
is called once per token. Only deploys that happen while the bot is running are passed on.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import httpx
import websockets

log = logging.getLogger("snipe")

INTEAR_WS = os.getenv("INTEAR_WS", "wss://api.intea.rs/ws")
NEARDATA = os.getenv("NEARDATA_URL", "https://mainnet.neardata.xyz").rstrip("/")
FASTNEAR_KEY = os.getenv("FASTNEAR_API_KEY", "").strip()

FACTORIES = {"nearlytrade.near": "nearly", "launchpad.justhoot.near": "justhoot",
             "v2-launchpad.justhoot.near": "justhoot"}
for _f in filter(None, (x.strip() for x in os.getenv("SNIPE_EXTRA_FACTORIES", "").split(","))):
    FACTORIES.setdefault(_f, _f.split(".")[0])


def factory_of(account: str) -> str | None:
    """X.<factory>, где в X нет точек → factory."""
    for fac in FACTORIES:
        if account.endswith("." + fac) and "." not in account[: -len(fac) - 1]:
            return fac
    return None


def parse_block(blk: dict) -> list[dict]:
    """Создания аккаунтов токенов мемпадов в блоке neardata."""
    out = []
    for shard in (blk or {}).get("shards") or []:
        for reo in shard.get("receipt_execution_outcomes") or []:
            r = reo.get("receipt") or {}
            token = r.get("receiver_id") or ""
            fac = factory_of(token)
            if not fac:
                continue
            act = (r.get("receipt") or {}).get("Action")
            if not isinstance(act, dict):
                continue
            actions = act.get("actions") or []
            if not any(a == "CreateAccount" or (isinstance(a, dict) and "CreateAccount" in a) for a in actions):
                continue
            status = ((reo.get("execution_outcome") or {}).get("outcome") or {}).get("status") or {}
            if isinstance(status, dict) and "Failure" in status:
                continue
            out.append({"token": token, "factory": fac, "signer": act.get("signer_id") or "",
                        "predecessor": r.get("predecessor_id") or "", "tx": reo.get("tx_hash") or ""})
    return out


class DeployWatcher:
    def __init__(self, on_deploy, url: str = INTEAR_WS):
        self.on_deploy, self.url = on_deploy, url
        self.http = httpx.AsyncClient(timeout=8, headers={"Authorization": f"Bearer {FASTNEAR_KEY}"}
                                      if FASTNEAR_KEY else None)
        self.seen: dict[str, float] = {}
        self.state = "starting"
        self.last_msg = 0.0
        self.deploys = 0
        self.task = asyncio.create_task(self.run())

    async def block(self, h: int) -> dict | None:
        for path in (f"/v0/block_opt/{h}", f"/v0/block/{h}"):
            try:
                r = await self.http.get(NEARDATA + path, follow_redirects=True)
                if r.status_code == 200 and r.content and r.content != b"null":
                    return json.loads(r.content)
            except (httpx.HTTPError, ValueError):
                pass
        return None

    async def find_dev(self, token: str, height) -> tuple[str, str]:
        if not isinstance(height, int):
            return "", ""
        for h in (height, height - 1, height + 1, height - 2, height - 3):
            for d in parse_block(await self.block(h) or {}):
                if d["token"] == token:
                    dev = d["signer"]
                    if not dev or dev in FACTORIES:
                        dev = d["predecessor"] if d["predecessor"] not in FACTORIES else ""
                    return dev, d["tx"]
        return "", ""

    async def _handle(self, token: str, fac: str, height):
        try:
            dev, tx = await asyncio.wait_for(self.find_dev(token, height), timeout=6)
        except asyncio.TimeoutError:
            dev, tx = "", ""
        if not dev:
            log.info("snipe: dev of %s not found", token)
            return
        log.info("snipe: deploy %s by %s", token, dev)
        try:
            await self.on_deploy(token, fac, dev, tx)
        except Exception:
            log.exception("snipe on_deploy %s", token)

    async def run(self):
        await asyncio.sleep(2)
        while True:
            try:
                async with websockets.connect(self.url, ping_interval=20, ping_timeout=20,
                                              max_size=16 * 1024 * 1024, open_timeout=15) as ws:
                    since = int((time.time() - 60) * 1000)
                    await ws.send(json.dumps({"id": 1, "method": "tokens.subscribe",
                                              "params": {"fields": ["symbol", "decimals", "created_at_millis"],
                                                         "where": {"created_after_millis": since}}}))
                    self.state = "connected"
                    log.info("snipe: Intear connected")
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=60)  # сервер шлёт 'idle' каждые 15 сек
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            continue
                        if not isinstance(msg, dict):
                            continue
                        self.last_msg = time.time()
                        if msg.get("error"):
                            log.warning("snipe: Intear %s", str(msg)[:200])
                        created = msg.get("created") or []
                        if not created and isinstance(msg.get("result"), dict):
                            created = msg["result"].get("created") or []
                        for acc in created:
                            fac = factory_of(acc) if isinstance(acc, str) else None
                            if fac and acc not in self.seen:
                                self.seen[acc] = time.time()
                                self.deploys += 1
                                asyncio.create_task(self._handle(acc, fac, msg.get("height")))
                        if len(self.seen) > 5000:
                            cut = time.time() - 3600
                            self.seen = {k: v for k, v in self.seen.items() if v > cut}
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.state = f"reconnecting ({type(e).__name__})"
                log.info("snipe: Intear %s: %s", self.state, str(e)[:120])
                await asyncio.sleep(3)
