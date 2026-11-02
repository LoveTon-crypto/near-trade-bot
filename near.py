"""Минимальное ядро NEAR: ключи, сборка и подпись транзакций, RPC.

Написано без сторонних NEAR-библиотек, чтобы приватный ключ не проходил
через чужой код. Подпись — Ed25519 из пакета `cryptography`.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import struct
import time
from dataclasses import dataclass

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

log = logging.getLogger("near")

YOCTO = 10 ** 24
TGAS = 10 ** 12
ONE_YOCTO = 1

# ---------------------------------------------------------------- base58
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = len(b) - len(b.lstrip(b"\0"))
    return "1" * pad + out


def b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        i = _B58.find(ch)
        if i < 0:
            raise ValueError("invalid base58")
        n = n * 58 + i
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\0" * pad + body


# ---------------------------------------------------------------- keys
class KeyPair:
    """Ed25519-ключ NEAR. Секрет хранится только в памяти процесса."""

    def __init__(self, seed: bytes):
        if len(seed) != 32:
            raise ValueError("seed must be 32 bytes")
        self._sk = Ed25519PrivateKey.from_private_bytes(seed)
        self.public_bytes = self._sk.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    @classmethod
    def generate(cls) -> "KeyPair":
        sk = Ed25519PrivateKey.generate()
        seed = sk.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                serialization.NoEncryption())
        return cls(seed)

    @classmethod
    def from_string(cls, s: str) -> "KeyPair":
        """Принимает 'ed25519:<base58>' — 64 байта (seed+pub, как в кошельках) или 32 байта seed."""
        s = s.strip()
        if s.startswith("ed25519:"):
            s = s[len("ed25519:"):]
        raw = b58decode(s)
        if len(raw) == 64:
            kp = cls(raw[:32])
            if kp.public_bytes != raw[32:]:
                raise ValueError("публичная часть ключа не совпадает с секретной")
            return kp
        if len(raw) == 32:
            return cls(raw)
        raise ValueError("неверная длина ключа")

    def seed(self) -> bytes:
        return self._sk.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                      serialization.NoEncryption())

    def secret_string(self) -> str:
        return "ed25519:" + b58encode(self.seed() + self.public_bytes)

    @property
    def public_key(self) -> str:
        return "ed25519:" + b58encode(self.public_bytes)

    @property
    def implicit_account(self) -> str:
        return self.public_bytes.hex()

    def sign(self, msg: bytes) -> bytes:
        return self._sk.sign(msg)


# ---------------------------------------------------------------- borsh
def _u32(n): return struct.pack("<I", n)
def _u64(n): return struct.pack("<Q", n)
def _u128(n): return int(n).to_bytes(16, "little")
def _str(s: str): b = s.encode(); return _u32(len(b)) + b
def _bytes(b: bytes): return _u32(len(b)) + b


@dataclass
class FunctionCall:
    method: str
    args: dict
    gas: int = 30 * TGAS
    deposit: int = 0

    def borsh(self) -> bytes:
        args = json.dumps(self.args, separators=(",", ":")).encode()
        return b"\x02" + _str(self.method) + _bytes(args) + _u64(self.gas) + _u128(self.deposit)


@dataclass
class Transfer:
    deposit: int

    def borsh(self) -> bytes:
        return b"\x03" + _u128(self.deposit)


def serialize_tx(signer: str, pub: bytes, nonce: int, receiver: str, block_hash: bytes, actions) -> bytes:
    out = _str(signer) + b"\x00" + pub + _u64(nonce) + _str(receiver) + block_hash
    out += _u32(len(actions))
    for a in actions:
        out += a.borsh()
    return out


def sign_tx(kp: KeyPair, signer: str, nonce: int, receiver: str, block_hash: bytes, actions) -> tuple[bytes, str]:
    """Возвращает (signed_tx_bytes, tx_hash_base58)."""
    tx = serialize_tx(signer, kp.public_bytes, nonce, receiver, block_hash, actions)
    h = hashlib.sha256(tx).digest()
    sig = kp.sign(h)
    return tx + b"\x00" + sig, b58encode(h)


# ---------------------------------------------------------------- RPC
class RpcError(Exception):
    pass


class TxFailed(Exception):
    def __init__(self, msg, tx_hash=None):
        super().__init__(msg)
        self.tx_hash = tx_hash


class Rpc:
    """JSON-RPC с перебором узлов по кругу.

    Ошибка самого NEAR (у nearcore всегда есть поле "name", напр. HANDLER_ERROR) отдаётся наверх.
    Всё остальное — 429, «endpoint discontinued», «method not found», не-JSON, 5xx — сбой узла:
    пробуем следующий, а узел, который не умеет нужный метод или закрыт, исключаем на 30 минут.
    """

    BENCH_SEC = 1800

    def __init__(self, urls: list[str], api_key: str = ""):
        self.urls = urls
        self.api_key = api_key
        self._rr = 0
        self._bench: dict[str, float] = {}  # url → до какого времени не использовать
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(15.0, read=60.0),
                                      headers={"User-Agent": "near-trade-bot/1.0"})

    def _headers(self, url: str) -> dict | None:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key and "fastnear" in url else None

    @staticmethod
    def _is_chain_error(err: dict) -> bool:
        name = err.get("name") or ""
        if name in ("TIMEOUT_ERROR", "INTERNAL_ERROR"):
            return False
        return bool(name)  # HANDLER_ERROR / REQUEST_VALIDATION_ERROR — настоящий ответ NEAR

    def _order(self) -> list[str]:
        now = time.time()
        live = [u for u in self.urls if self._bench.get(u, 0) < now] or list(self.urls)
        self._rr = (self._rr + 1) % len(live)
        return live[self._rr:] + live[:self._rr]

    async def call(self, method: str, params) -> dict:
        last = None
        for attempt in range(3):
            for url in self._order():
                try:
                    r = await self.http.post(url, json={"jsonrpc": "2.0", "id": "t", "method": method, "params": params},
                                             headers=self._headers(url))
                    data = r.json()
                except (httpx.HTTPError, ValueError) as e:
                    last = e
                    continue
                if not isinstance(data, dict):
                    last = RpcError(f"{url}: bad response")
                    continue
                if "error" in data:
                    err = data["error"] if isinstance(data["error"], dict) else {"message": str(data["error"])}
                    if self._is_chain_error(err):
                        raise RpcError(json.dumps(err)[:800])
                    text = json.dumps(err)
                    if err.get("code") == -32601 or "discontinued" in text or "not exist" in text or r.status_code in (401, 403, 404):
                        self._bench[url] = time.time() + self.BENCH_SEC
                        log.warning("RPC %s benched: %s", url, text[:120])
                    last = RpcError(text[:300])
                    continue
                if "result" not in data:
                    if r.status_code in (401, 403, 404, 410) or "discontinued" in str(data):
                        self._bench[url] = time.time() + self.BENCH_SEC
                        log.warning("RPC %s benched: %s", url, str(data)[:120])
                    last = RpcError(f"{url}: {str(data)[:200]}")
                    continue
                return data["result"]
            await asyncio.sleep(0.6 * (attempt + 1))
        raise RpcError(f"все RPC недоступны: {last}")

    async def view(self, contract: str, method: str, args: dict | None = None, finality="optimistic"):
        res = await self.call("query", {
            "request_type": "call_function", "finality": finality, "account_id": contract,
            "method_name": method,
            "args_base64": base64.b64encode(json.dumps(args or {}).encode()).decode()})
        raw = bytes(res.get("result") or [])
        return json.loads(raw.decode()) if raw else None

    async def account(self, account_id: str) -> dict | None:
        try:
            return await self.call("query", {"request_type": "view_account", "finality": "optimistic",
                                             "account_id": account_id})
        except RpcError as e:
            if "UNKNOWN_ACCOUNT" in str(e) or "does not exist" in str(e):
                return None
            raise

    async def access_key(self, account_id: str, public_key: str) -> dict:
        return await self.call("query", {"request_type": "view_access_key", "finality": "final",
                                         "account_id": account_id, "public_key": public_key})

    async def close(self):
        await self.http.aclose()


def _failures(result: dict) -> list[str]:
    out = []
    st = result.get("status") or {}
    if "Failure" in st:
        out.append(json.dumps(st["Failure"])[:400])
    for ro in result.get("receipts_outcome") or []:
        s = (ro.get("outcome") or {}).get("status") or {}
        if "Failure" in s:
            out.append(json.dumps(s["Failure"])[:400])
    return out


class Signer:
    """Отправка транзакций от имени одного аккаунта. Один замок — один nonce за раз."""

    def __init__(self, rpc: Rpc, account_id: str, kp: KeyPair):
        self.rpc = rpc
        self.account_id = account_id
        self.kp = kp
        self._lock = asyncio.Lock()
        self._last_nonce = 0

    async def check_key(self) -> dict:
        """Проверяет, что ключ действительно привязан к аккаунту и даёт полный доступ."""
        ak = await self.rpc.access_key(self.account_id, self.kp.public_key)
        if ak.get("permission") != "FullAccess":
            raise RpcError("ключ не FullAccess — свапы с депозитом им подписать нельзя")
        return ak

    async def send(self, receiver: str, actions: list, wait="EXECUTED_OPTIMISTIC") -> dict:
        async with self._lock:
            ak = await self.rpc.access_key(self.account_id, self.kp.public_key)
            nonce = max(int(ak["nonce"]), self._last_nonce) + 1
            block_hash = b58decode(ak["block_hash"])
            signed, tx_hash = sign_tx(self.kp, self.account_id, nonce, receiver, block_hash, actions)
            self._last_nonce = nonce
        b64 = base64.b64encode(signed).decode()
        try:
            res = await self.rpc.call("send_tx", {"signed_tx_base64": b64, "wait_until": wait})
        except RpcError as e:
            if "TIMEOUT" not in str(e) and "все RPC" not in str(e):
                raise TxFailed(f"транзакция отклонена: {e}", tx_hash)
            res = await self._poll(tx_hash)
        fails = _failures(res)
        if fails:
            raise TxFailed("; ".join(fails), tx_hash)
        res["_hash"] = tx_hash
        return res

    async def _poll(self, tx_hash: str) -> dict:
        for _ in range(20):
            await asyncio.sleep(3)
            try:
                return await self.rpc.call("tx", {"tx_hash": tx_hash, "sender_account_id": self.account_id,
                                                  "wait_until": "EXECUTED_OPTIMISTIC"})
            except RpcError as e:
                if "UNKNOWN_TRANSACTION" in str(e) or "TIMEOUT" in str(e):
                    continue
                raise
        raise TxFailed("не дождались результата транзакции, проверьте по хэшу", tx_hash)


def fmt_near(yocto: int, digits=4) -> str:
    v = yocto / YOCTO
    return f"{v:,.{digits}f}".rstrip("0").rstrip(".") if v else "0"


def to_yocto(near: float | str) -> int:
    from decimal import Decimal
    return int(Decimal(str(near)) * YOCTO)
