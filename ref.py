"""Свапы NEAR <-> токен через Ref Finance (Rhea), без посредников.

Все транзакции собираются здесь и подписываются локально. Пул и котировка
берутся прямо из контракта Ref, а не из сторонних API.
"""
from __future__ import annotations

import asyncio
import json
import logging

from near import Rpc, Signer, FunctionCall, TGAS, ONE_YOCTO, YOCTO, TxFailed

log = logging.getLogger("ref")

REF = "v2.ref-finance.near"
WNEAR = "wrap.near"
DEFAULT_STORAGE = 1_250_000_000_000_000_000_000  # 0.00125 NEAR


class NoPool(Exception):
    pass


class Ref:
    def __init__(self, rpc: Rpc):
        self.rpc = rpc
        self.pools_by_token: dict[str, list[int]] = {}
        self.indexed = 0
        self._idx_lock = asyncio.Lock()

    # ------------------------------------------------------------ пулы
    async def refresh_index(self):
        """Догружает новые пулы Ref (с wNEAR в паре) в локальный индекс."""
        async with self._idx_lock:
            total = int(await self.rpc.view(REF, "get_number_of_pools"))
            step = 250
            while self.indexed < total:
                batch = await self.rpc.view(REF, "get_pools", {"from_index": self.indexed, "limit": step}) or []
                for i, p in enumerate(batch):
                    toks = p.get("token_account_ids") or []
                    if len(toks) == 2 and WNEAR in toks:
                        other = toks[0] if toks[1] == WNEAR else toks[1]
                        self.pools_by_token.setdefault(other, []).append(self.indexed + i)
                if not batch:
                    break
                self.indexed += len(batch)
            log.info("Ref index: %d pools, %d tokens paired with wNEAR", self.indexed, len(self.pools_by_token))

    async def best_pool(self, token: str) -> tuple[int, int]:
        """(pool_id, резерв wNEAR) — пул с наибольшей ликвидностью wNEAR."""
        if token not in self.pools_by_token:
            await self.refresh_index()
        ids = self.pools_by_token.get(token) or []
        if not ids:
            raise NoPool(token)
        best = None
        for pid in ids[-12:]:
            p = await self.rpc.view(REF, "get_pool", {"pool_id": pid})
            toks, amts = p["token_account_ids"], [int(a) for a in p["amounts"]]
            if set(toks) != {WNEAR, token}:
                continue  # перестраховка: проверяем состав пула прямо в контракте
            wn = amts[toks.index(WNEAR)]
            if best is None or wn > best[1]:
                best = (pid, wn)
        if not best or best[1] == 0:
            raise NoPool(token)
        return best

    async def quote(self, pool_id: int, token_in: str, amount_in: int, token_out: str) -> int:
        r = await self.rpc.view(REF, "get_return", {"pool_id": pool_id, "token_in": token_in,
                                                     "amount_in": str(amount_in), "token_out": token_out})
        return int(r)

    # ------------------------------------------------------------ балансы и storage
    async def ft_balance(self, token: str, account: str) -> int:
        return int(await self.rpc.view(token, "ft_balance_of", {"account_id": account}) or 0)

    async def native_balance(self, account: str) -> int:
        """Доступный NEAR: баланс минус стоимость хранения данных аккаунта."""
        a = await self.rpc.account(account)
        if not a:
            return 0
        storage = int(a.get("storage_usage", 0)) * 10 ** 19
        return max(int(a["amount"]) - storage, 0)

    async def ensure_storage(self, s: Signer, token: str):
        reg = await self.rpc.view(token, "storage_balance_of", {"account_id": s.account_id})
        if reg:
            return
        try:
            bounds = await self.rpc.view(token, "storage_balance_bounds")
            dep = int((bounds or {}).get("min") or DEFAULT_STORAGE)
        except Exception:
            dep = DEFAULT_STORAGE
        if dep > YOCTO // 10:
            raise TxFailed(f"токен просит необычно большой storage-депозит ({dep / YOCTO} NEAR), отменено")
        await s.send(token, [FunctionCall("storage_deposit", {"account_id": s.account_id, "registration_only": True},
                                          gas=30 * TGAS, deposit=dep)])

    async def ft_metadata(self, token: str) -> dict:
        return await self.rpc.view(token, "ft_metadata") or {}

    # ------------------------------------------------------------ свапы
    @staticmethod
    def _msg(pool_id, token_in, token_out, amount_in, min_out) -> str:
        return json.dumps({"force": 0, "actions": [{
            "pool_id": pool_id, "token_in": token_in, "token_out": token_out,
            "amount_in": str(amount_in), "min_amount_out": str(min_out)}]}, separators=(",", ":"))

    @staticmethod
    def _burnt(res: dict) -> int:
        tot = int((res.get("transaction_outcome") or {}).get("outcome", {}).get("tokens_burnt", 0))
        for ro in res.get("receipts_outcome") or []:
            tot += int((ro.get("outcome") or {}).get("tokens_burnt", 0))
        return tot

    async def _collect_ref_deposit(self, s: Signer, token: str):
        """Если аккаунт зарегистрирован в Ref, токены оседают во внутреннем балансе Ref — выводим их."""
        try:
            dep = int(await self.rpc.view(REF, "get_deposit", {"account_id": s.account_id, "token_id": token}) or 0)
        except Exception:
            return
        if dep > 0:
            await s.send(REF, [FunctionCall("withdraw", {"token_id": token, "amount": str(dep)},
                                            gas=100 * TGAS, deposit=ONE_YOCTO)])

    async def unwrap_all(self, s: Signer):
        w = await self.ft_balance(WNEAR, s.account_id)
        if w > 0:
            await s.send(WNEAR, [FunctionCall("near_withdraw", {"amount": str(w)}, gas=10 * TGAS, deposit=ONE_YOCTO)])

    async def buy(self, s: Signer, token: str, near_in: int, slippage_bps: int) -> dict:
        pool_id, _ = await self.best_pool(token)
        expected = await self.quote(pool_id, WNEAR, near_in, token)
        if expected <= 0:
            raise TxFailed("котировка 0 — в пуле нет ликвидности")
        min_out = expected * (10_000 - slippage_bps) // 10_000
        await self.ensure_storage(s, token)
        await self.ensure_storage(s, WNEAR)
        before = await self.ft_balance(token, s.account_id)
        try:
            res = await s.send(WNEAR, [
                FunctionCall("near_deposit", {}, gas=10 * TGAS, deposit=near_in),
                FunctionCall("ft_transfer_call", {"receiver_id": REF, "amount": str(near_in),
                                                  "msg": self._msg(pool_id, WNEAR, token, near_in, min_out)},
                             gas=180 * TGAS, deposit=ONE_YOCTO),
            ])
        finally:
            # при неудачном свапе wNEAR возвращается на кошелёк — разворачиваем обратно в NEAR
            try:
                await self._collect_ref_deposit(s, WNEAR)
                await self.unwrap_all(s)
            except Exception as e:
                log.warning("unwrap after buy failed: %s", e)
        await self._collect_ref_deposit(s, token)
        got = await self.ft_balance(token, s.account_id) - before
        if got <= 0:
            raise TxFailed("свап не дал токенов (возможно, слиппедж). NEAR возвращён.", res.get("_hash"))
        return {"pool_id": pool_id, "got": got, "expected": expected, "spent": near_in, "hash": res["_hash"]}

    async def sell(self, s: Signer, token: str, amount: int, slippage_bps: int) -> dict:
        pool_id, _ = await self.best_pool(token)
        expected = await self.quote(pool_id, token, amount, WNEAR)
        if expected <= 0:
            raise TxFailed("котировка 0 — в пуле нет ликвидности")
        min_out = expected * (10_000 - slippage_bps) // 10_000
        await self.ensure_storage(s, WNEAR)
        nat0 = await self.native_balance(s.account_id)
        w0 = await self.ft_balance(WNEAR, s.account_id)
        res = await s.send(token, [FunctionCall("ft_transfer_call", {
            "receiver_id": REF, "amount": str(amount),
            "msg": self._msg(pool_id, token, WNEAR, amount, min_out)}, gas=180 * TGAS, deposit=ONE_YOCTO)])
        burnt = self._burnt(res)
        await self._collect_ref_deposit(s, WNEAR)
        w1 = await self.ft_balance(WNEAR, s.account_id)
        nat1 = await self.native_balance(s.account_id)
        # выручка = рост wNEAR + рост NEAR (если Ref сам развернул wNEAR) + сожжённый газ этой транзакции
        got = (w1 - w0) + (nat1 - nat0) + burnt
        try:
            await self.unwrap_all(s)
        except Exception as e:
            log.warning("unwrap after sell failed: %s", e)
        return {"pool_id": pool_id, "got": max(got, 0), "expected": expected, "sold": amount, "hash": res["_hash"]}

    async def withdraw_near(self, s: Signer, to: str, amount: int) -> dict:
        from near import Transfer
        return await s.send(to, [Transfer(amount)])
