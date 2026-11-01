"""Wallets: seed phrases, key derivation and encrypted storage.

Seed phrases are standard BIP39 (12 words) derived with SLIP-10 ed25519 on the
path m/44'/397'/0' — the same as Meteor, MyNearWallet and near-seed-phrase, so a
wallet created in the bot opens in any NEAR wallet and vice versa.

Secrets (32-byte key seed + phrase) are encrypted with AES-256-GCM under
MASTER_KEY from .env. The database alone is useless without that key. The
ciphertext is bound to its owner and account (AAD), so rows can't be swapped
between users.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import struct
import unicodedata
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from near import KeyPair

NEAR_PATH = (44, 397, 0)
# Official BIP39 English list (sha256 2f5eed53…dbda); BIP39 is implemented here, no third-party code touches keys
WORDS = (Path(__file__).with_name("bip39_english.txt")).read_text().split()
_INDEX = {w: i for i, w in enumerate(WORDS)}
assert len(WORDS) == 2048
ACCOUNT_RE = re.compile(r"^(?:[0-9a-f]{64}|(?:[a-z0-9]+[-_])*[a-z0-9]+(?:\.(?:[a-z0-9]+[-_])*[a-z0-9]+)+)$")


class WalletError(ValueError):
    pass


# ---------------------------------------------------------------- derivation
def slip10_ed25519(seed: bytes, path=NEAR_PATH) -> bytes:
    """SLIP-10 ed25519 private key for a fully hardened path."""
    i = hmac.new(b"ed25519 seed", seed, hashlib.sha512).digest()
    key, chain = i[:32], i[32:]
    for idx in path:
        data = b"\x00" + key + struct.pack(">I", idx | 0x80000000)
        i = hmac.new(chain, data, hashlib.sha512).digest()
        key, chain = i[:32], i[32:]
    return key


def normalize_phrase(text: str) -> str:
    return " ".join(text.lower().split())


def entropy_to_phrase(entropy: bytes) -> str:
    bits = len(entropy) * 8
    cs = bits // 32
    n = (int.from_bytes(entropy, "big") << cs) | (hashlib.sha256(entropy).digest()[0] >> (8 - cs))
    count = (bits + cs) // 11
    return " ".join(WORDS[(n >> (11 * (count - 1 - i))) & 2047] for i in range(count))


def new_phrase() -> str:
    return entropy_to_phrase(os.urandom(16))  # 12 words


def check_phrase(phrase: str) -> bool:
    words = normalize_phrase(phrase).split()
    if len(words) not in (12, 15, 18, 21, 24) or any(w not in _INDEX for w in words):
        return False
    n = 0
    for w in words:
        n = (n << 11) | _INDEX[w]
    cs = len(words) * 11 // 33
    entropy = (n >> cs).to_bytes((len(words) * 11 - cs) // 8, "big")
    return entropy_to_phrase(entropy) == " ".join(words)


def phrase_to_seed(phrase: str, passphrase: str = "") -> bytes:
    p = unicodedata.normalize("NFKD", phrase).encode()
    salt = unicodedata.normalize("NFKD", "mnemonic" + passphrase).encode()
    return hashlib.pbkdf2_hmac("sha512", p, salt, 2048)


def is_phrase(text: str) -> bool:
    words = normalize_phrase(text).split()
    return len(words) in (12, 15, 18, 21, 24) and all(w in _INDEX for w in words)


def keypair_from_phrase(phrase: str) -> KeyPair:
    phrase = normalize_phrase(phrase)
    if not check_phrase(phrase):
        raise WalletError("сид-фраза с ошибкой: проверьте слова и их порядок")
    return KeyPair(slip10_ed25519(phrase_to_seed(phrase)))


def parse_secret(text: str) -> tuple[KeyPair, str | None]:
    """Seed phrase or private key (ed25519:...) → (keypair, phrase or None)."""
    t = text.strip()
    if is_phrase(t):
        return keypair_from_phrase(t), normalize_phrase(t)
    try:
        return KeyPair.from_string(t), None
    except Exception:
        raise WalletError("это не сид-фраза и не приватный ключ ed25519:…")


# ---------------------------------------------------------------- encryption
def load_master_key() -> bytes:
    raw = os.getenv("MASTER_KEY", "").strip()
    if not raw:
        raise SystemExit("MASTER_KEY is not set in .env (deploy.sh generates it; back it up!)")
    key = base64.b64decode(raw)
    if len(key) != 32:
        raise SystemExit("MASTER_KEY must be 32 bytes, base64-encoded")
    return key


class Vault:
    def __init__(self, master_key: bytes):
        self._aead = AESGCM(master_key)

    @staticmethod
    def _aad(user_id: int, account: str) -> bytes:
        return f"near-trade-bot:{user_id}:{account}".encode()

    def seal(self, user_id: int, account: str, kp: KeyPair, phrase: str | None) -> bytes:
        nonce = os.urandom(12)
        body = json.dumps({"seed": kp.seed().hex(), "phrase": phrase}).encode()
        return nonce + self._aead.encrypt(nonce, body, self._aad(user_id, account))

    def open(self, user_id: int, account: str, blob: bytes) -> tuple[KeyPair, str | None]:
        body = json.loads(self._aead.decrypt(blob[:12], blob[12:], self._aad(user_id, account)))
        return KeyPair(bytes.fromhex(body["seed"])), body.get("phrase")
