"""Свапы NEAR <-> токен через Ref Finance (Rhea), без посредников.

Все транзакции собираются здесь и подписываются локально. Пул и котировка
берутся прямо из контракта Ref, а не из сторонних API.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

from near import Rpc, RpcError, Signer, FunctionCall, TGAS, ONE_YOCTO, YOCTO, TxFailed

log = logging.getLogger("ref")

REF = "v2.ref-finance.near"
DCL = "dclv2.ref-labs.near"           # Ref DCL (concentrated liquidity): здесь торгуются токены nearly.trade
DCL_FEES = (10000, 2000, 400, 100)     # nearly.trade запускает токены в пул с комиссией 1% (10000)
WNEAR = "wrap.near"
DEFAULT_STORAGE = 1_250_000_000_000_000_000_000  # 0.00125 NEAR


class NoPool(Exception):
    pass


@dataclass
class Route:
    """Где торговать токеном: классический пул Ref (pool_id int) или пул DCL (pool_id 'a|b|fee').
    Для DCL бывает и два шага: wNEAR → base → токен (base_pool = пул base/wNEAR, base = промежуточный токен)."""
    kind: str        # "ref" | "dcl"
    pool_id: object
    wnear: int       # резерв wNEAR в пуле (yocto); для двух шагов — в пуле base/wNEAR
    base: str | None = None
    base_pool: str | None = None
    base_symbol: str = ""

    @property
    def label(self) -> str:
        if self.kind == "ref":
            return f"Ref #{self.pool_id}"
        fee = f"{int(str(self.pool_id).split('|')[2]) / 10000:g}%"
        return f"Ref DCL {fee}" + (f" через {self.base_symbol or self.base}" if self.base else "")

    def dcl_path(self, side: str) -> list[str]:
        """pool_ids для свапа: покупка wNEAR→(base→)токен, продажа наоборот."""
        if not self.base:
            return [self.pool_id]
        return [self.base_pool, self.pool_id] if side == "buy" else [self.pool_id, self.base_pool]


class Ref:
    def __init__(self, rpc: Rpc):
        self.rpc = rpc
        self._meta: dict[str, dict] = {}
        self._supply: dict[str, tuple[float, int]] = {}
        self._routes: dict[str, tuple[float, Route]] = {}
        self.pools_by_token: dict[str, list[int]] = {}
        self.indexed = 0
        self._idx_lock = asyncio.Lock()
        # индекс пулов DCL: токен → [(pool_id, другой токен)]; пулы в контракте только добавляются
        self.dcl_by_token: dict[str, list[tuple[str, str]]] = {}
        self.dcl_indexed = 0
        self.dcl_index_ts = 0.0
        self._dcl_lock = asyncio.Lock()
        self._reg: set[tuple[str, str]] = set()  # (токен, аккаунт) с оплаченным storage

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
        recent = ids[-12:]
        got = await asyncio.gather(*(self.rpc.view(REF, "get_pool", {"pool_id": pid}) for pid in recent))
        for pid, p in zip(recent, got):
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

    async def refresh_dcl_index(self, force: bool = False):
        """Догружает новые пулы DCL (list_pools). Не чаще раза в 2 сек, если не force."""
        async with self._dcl_lock:
            if not force and time.time() - self.dcl_index_ts < 2:
                return
            step = 200
            while True:
                batch = await self.rpc.view(DCL, "list_pools", {"from_index": self.dcl_indexed, "limit": step}) or []
                for p in batch:
                    pid, x, y = p.get("pool_id"), p.get("token_x"), p.get("token_y")
                    if not pid or not x or not y:
                        continue
                    self.dcl_by_token.setdefault(x, []).append((pid, y))
                    self.dcl_by_token.setdefault(y, []).append((pid, x))
                self.dcl_indexed += len(batch)
                if len(batch) < step:
                    break
            self.dcl_index_ts = time.time()
            log.info("DCL index: %d pools", self.dcl_indexed)

    async def _dcl_info(self, pid: str) -> dict | None:
        try:
            p = await self.rpc.view(DCL, "get_pool", {"pool_id": pid})
        except RpcError as e:
            if "все RPC" in str(e):
                raise  # сеть недоступна — это не «пула нет»
            return None
        if not p or str(p.get("state", "Running")).lower() not in ("running", ""):
            return None
        return p

    @staticmethod
    def _reserve(p: dict, token: str) -> int:
        return int((p.get("total_x") if p.get("token_x") == token else p.get("total_y")) or 0)

    async def dcl_pool(self, token: str) -> tuple[str, int]:
        """(pool_id, резерв wNEAR) лучшего пула DCL token/wNEAR. Состав пула проверяется в самом контракте."""
        best = None
        a, b = sorted([token, WNEAR])  # в DCL токены в id пула идут по алфавиту
        ids = [f"{a}|{b}|{fee}" for fee in DCL_FEES]
        ids += [pid for pid, other in self.dcl_by_token.get(token, []) if other == WNEAR and pid not in ids]
        infos = await asyncio.gather(*(self._dcl_info(pid) for pid in ids))  # все уровни комиссии сразу
        for pid, p in zip(ids, infos):
            if not p or {p.get("token_x"), p.get("token_y")} != {token, WNEAR}:
                continue
            wn = self._reserve(p, WNEAR)
            if wn > 0 and (best is None or wn > best[1]):
                best = (pid, wn)
        if not best:
            raise NoPool(token)
        return best

    async def dcl_two_hop(self, token: str) -> Route:
        """Токен торгуется в DCL не к wNEAR, а к другому токену (base), у которого есть пул base/wNEAR."""
        best = None
        for pid, base in self.dcl_by_token.get(token, []):
            if base == WNEAR:
                continue
            try:
                bpid, bwn = await self.dcl_pool(base)
            except NoPool:
                continue
            p = await self._dcl_info(pid)
            if not p or {p.get("token_x"), p.get("token_y")} != {token, base} or self._reserve(p, token) <= 0:
                continue
            if best is None or bwn > best.wnear:
                try:
                    sym = str((await self.ft_metadata(base)).get("symbol") or "")[:16]
                except Exception:
                    sym = ""
                best = Route("dcl", pid, bwn, base=base, base_pool=bpid, base_symbol=sym)
        if not best:
            raise NoPool(token)
        return best

    async def route(self, token: str, fresh: bool = False) -> Route:
        """Пул с наибольшей ликвидностью wNEAR: классический Ref или DCL. Кэш 60 сек (сделки берут свежий)."""
        hit = self._routes.get(token)
        if hit and not fresh and time.time() - hit[0] < 60:
            return hit[1]
        cands = []
        try:
            pid, wn = await self.best_pool(token)
            cands.append(Route("ref", pid, wn))
        except NoPool:
            pass
        try:
            pid, wn = await self.dcl_pool(token)
            cands.append(Route("dcl", pid, wn))
        except NoPool:
            pass
        if not cands:  # прямого пула нет — ищем в индексе DCL (свежие пулы и пары не к wNEAR)
            try:
                if token not in self.dcl_by_token:
                    await self.refresh_dcl_index()
                if self.dcl_by_token.get(token):
                    try:
                        pid, wn = await self.dcl_pool(token)
                        cands.append(Route("dcl", pid, wn))
                    except NoPool:
                        cands.append(await self.dcl_two_hop(token))
            except NoPool:
                pass
            except RpcError as e:
                if "все RPC" in str(e):
                    raise
                log.warning("DCL index: %s", str(e)[:200])
        if not cands:
            raise NoPool(token)
        best = max(cands, key=lambda r: r.wnear)
        self._routes[token] = (time.time(), best)
        return best

    async def quote_route(self, r: Route, token_in: str, amount_in: int, token_out: str) -> int:
        if r.kind == "ref":
            return await self.quote(r.pool_id, token_in, amount_in, token_out)
        side = "buy" if token_in == WNEAR else "sell"
        q = await self.rpc.view(DCL, "quote", {"pool_ids": r.dcl_path(side), "input_token": token_in,
                                               "output_token": token_out, "input_amount": str(amount_in)})
        return int((q or {}).get("amount") or 0)

    @staticmethod
    def _dcl_msg(pool_ids: list, token_out: str, min_out: int) -> str:
        return json.dumps({"Swap": {"pool_ids": list(pool_ids), "output_token": token_out,
                                    "min_output_amount": str(min_out)}}, separators=(",", ":"))

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

    async def _registered(self, token: str, account: str) -> bool:
        if (token, account) in self._reg:
            return True
        ok = bool(await self.rpc.view(token, "storage_balance_of", {"account_id": account}))
        if ok:
            self._reg.add((token, account))
        return ok

    async def _register(self, s: Signer, token: str, wait: str = "EXECUTED_OPTIMISTIC"):
        try:
            bounds = await self.rpc.view(token, "storage_balance_bounds")
            dep = int((bounds or {}).get("min") or DEFAULT_STORAGE)
        except Exception:
            dep = DEFAULT_STORAGE
        if dep > YOCTO // 10:
            raise TxFailed(f"токен просит необычно большой storage-депозит ({dep / YOCTO} NEAR), отменено")
        await s.send(token, [FunctionCall("storage_deposit", {"account_id": s.account_id, "registration_only": True},
                                          gas=30 * TGAS, deposit=dep)], wait=wait)
        self._reg.add((token, s.account_id))

    async def ensure_storage(self, s: Signer, token: str):
        if not await self._registered(token, s.account_id):
            await self._register(s, token)

    async def total_supply(self, token: str) -> int:
        hit = self._supply.get(token)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        v = int(await self.rpc.view(token, "ft_total_supply") or 0)
        self._supply[token] = (time.time(), v)
        return v

    async def spot_price(self, r: "Route", token: str, dec: int) -> float:
        """Текущая цена 1 токена в NEAR прямо из пула (без комиссии и проскальзывания)."""
        if r.kind == "ref":
            p = await self.rpc.view(REF, "get_pool", {"pool_id": r.pool_id})
            toks, amts = p["token_account_ids"], [int(a) for a in p["amounts"]]
            wn, tr = amts[toks.index(WNEAR)], amts[toks.index(token)]
            return (wn / YOCTO) / (tr / 10 ** dec) if tr else 0.0
        if r.base:  # цена токена в base × цена base в wNEAR (всё в «сырых» единицах)
            return (await self._dcl_raw(r.pool_id, token, r.base) * await self._dcl_raw(r.base_pool, r.base, WNEAR)
                    * 10 ** dec / YOCTO)
        return await self._dcl_raw(r.pool_id, token, WNEAR) * 10 ** dec / YOCTO

    async def _dcl_raw(self, pid: str, token: str, other: str) -> float:
        """Сколько «сырых» единиц other за 1 «сырую» единицу token по текущей точке пула."""
        p = await self.rpc.view(DCL, "get_pool", {"pool_id": pid})
        raw = 1.0001 ** int(p["current_point"])  # amount_y / amount_x в «сырых» единицах
        if p.get("token_x") == token:
            return raw
        return 1 / raw if raw else 0.0

    async def ft_metadata(self, token: str) -> dict:
        if token not in self._meta:
            self._meta[token] = await self.rpc.view(token, "ft_metadata") or {}
        return self._meta[token]

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

    def _swap_call(self, r: Route, token_in: str, token_out: str, amount: int, min_out: int) -> FunctionCall:
        if r.kind == "ref":
            to, msg = REF, self._msg(r.pool_id, token_in, token_out, amount, min_out)
        else:
            to, msg = DCL, self._dcl_msg(r.dcl_path("buy" if token_in == WNEAR else "sell"), token_out, min_out)
        return FunctionCall("ft_transfer_call", {"receiver_id": to, "amount": str(amount), "msg": msg},
                            gas=180 * TGAS, deposit=ONE_YOCTO)

    async def buy(self, s: Signer, token: str, near_in: int, slippage_bps: int, route: Route | None = None,
                  fast: bool = False) -> dict:
        """fast (снайпер): маршрут уже найден, проверки идут параллельно, а регистрацию в токене не ждём до
        исполнения — свап проходит через wrap.near и Ref на несколько блоков дольше, регистрация успевает раньше."""
        r = route or await self.route(token, fresh=True)
        pool_id = r.pool_id
        expected, reg_tok, reg_w, before = await asyncio.gather(
            self.quote_route(r, WNEAR, near_in, token), self._registered(token, s.account_id),
            self._registered(WNEAR, s.account_id), self.ft_balance(token, s.account_id))
        if expected <= 0:
            raise TxFailed("котировка 0 — в пуле нет ликвидности")
        min_out = expected * (10_000 - slippage_bps) // 10_000
        if not reg_w:
            await self._register(s, WNEAR)
        if not reg_tok:
            await self._register(s, token, wait="INCLUDED" if fast else "EXECUTED_OPTIMISTIC")
        try:
            res = await s.send(WNEAR, [
                FunctionCall("near_deposit", {}, gas=10 * TGAS, deposit=near_in),
                self._swap_call(r, WNEAR, token, near_in, min_out),
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
        return {"pool_id": pool_id, "route": r, "got": got, "expected": expected, "spent": near_in, "hash": res["_hash"]}

    async def sell(self, s: Signer, token: str, amount: int, slippage_bps: int) -> dict:
        r = await self.route(token, fresh=True)
        pool_id = r.pool_id
        expected = await self.quote_route(r, token, amount, WNEAR)
        if expected <= 0:
            raise TxFailed("котировка 0 — в пуле нет ликвидности")
        min_out = expected * (10_000 - slippage_bps) // 10_000
        await self.ensure_storage(s, WNEAR)
        nat0 = await self.native_balance(s.account_id)
        w0 = await self.ft_balance(WNEAR, s.account_id)
        res = await s.send(token, [self._swap_call(r, token, WNEAR, amount, min_out)])
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
        return {"pool_id": pool_id, "route": r, "got": max(got, 0), "expected": expected, "sold": amount, "hash": res["_hash"]}

    async def transfer_ft(self, s: Signer, token: str, to: str, amount: int) -> dict:
        """Перевод токена. Если получатель не зарегистрирован в контракте токена — регистрируем его за свой счёт
        (≈0.00125 NEAR) в той же транзакции, иначе токен не дойдёт."""
        actions = []
        reg = await self.rpc.view(token, "storage_balance_of", {"account_id": to})
        if not reg:
            try:
                bounds = await self.rpc.view(token, "storage_balance_bounds")
                dep = int((bounds or {}).get("min") or DEFAULT_STORAGE)
            except Exception:
                dep = DEFAULT_STORAGE
            if dep > YOCTO // 10:
                raise TxFailed(f"токен просит необычно большой storage-депозит ({dep / YOCTO} NEAR), отменено")
            actions.append(FunctionCall("storage_deposit", {"account_id": to, "registration_only": True},
                                        gas=30 * TGAS, deposit=dep))
        actions.append(FunctionCall("ft_transfer", {"receiver_id": to, "amount": str(amount)},
                                    gas=30 * TGAS, deposit=ONE_YOCTO))
        return await s.send(token, actions)

    async def withdraw_near(self, s: Signer, to: str, amount: int) -> dict:
        from near import Transfer
        return await s.send(to, [Transfer(amount)])
