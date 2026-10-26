"""Зашифрованное хранилище ключа + консольная утилита.

Ключ лежит на диске только в зашифрованном виде (scrypt + AES-256-GCM).
Пароль на сервере не хранится: бот после запуска заблокирован, пока вы не
пришлёте пароль командой /unlock (сообщение с паролем бот сразу удаляет).

Использование на сервере (по SSH):
    venv/bin/python keystore.py new       — создать новый торговый кошелёк
    venv/bin/python keystore.py import    — импортировать существующий кошелёк
    venv/bin/python keystore.py show      — показать адрес
    venv/bin/python keystore.py export    — показать приватный ключ (для бэкапа)
    venv/bin/python keystore.py passwd    — сменить пароль
"""
from __future__ import annotations

import base64
import getpass
import json
import os
import re
import sys

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from near import KeyPair

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

KEYSTORE_PATH = os.getenv("KEYSTORE_PATH", "wallet.enc.json")

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 16, 8, 1
ACCOUNT_RE = re.compile(r"^(?:[0-9a-f]{64}|(?:[a-z0-9]+[-_])*[a-z0-9]+(?:\.(?:[a-z0-9]+[-_])*[a-z0-9]+)*)$")


def _kdf(password: str, salt: bytes, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(password.encode())


def encrypt(account_id: str, kp: KeyPair, password: str) -> dict:
    salt, nonce = os.urandom(16), os.urandom(12)
    key = _kdf(password, salt)
    aad = account_id.encode()
    ct = AESGCM(key).encrypt(nonce, kp.seed(), aad)
    return {"version": 1, "account_id": account_id, "public_key": kp.public_key,
            "kdf": {"name": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
                    "salt": base64.b64encode(salt).decode()},
            "nonce": base64.b64encode(nonce).decode(), "ciphertext": base64.b64encode(ct).decode()}


def decrypt(store: dict, password: str) -> KeyPair:
    k = store["kdf"]
    key = _kdf(password, base64.b64decode(k["salt"]), k["n"], k["r"], k["p"])
    try:
        seed = AESGCM(key).decrypt(base64.b64decode(store["nonce"]), base64.b64decode(store["ciphertext"]),
                                   store["account_id"].encode())
    except Exception:
        raise ValueError("неверный пароль") from None
    kp = KeyPair(seed)
    if kp.public_key != store["public_key"]:
        raise ValueError("файл ключа повреждён")
    return kp


def load(path=KEYSTORE_PATH) -> dict | None:
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return json.load(fh)


def save(store: dict, path=KEYSTORE_PATH):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(store, fh, indent=1)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


# ---------------------------------------------------------------- CLI
def _new_password() -> str:
    while True:
        p1 = getpass.getpass("Придумайте пароль (минимум 12 символов): ")
        if len(p1) < 12:
            print("Слишком короткий пароль.")
            continue
        if getpass.getpass("Повторите пароль: ") != p1:
            print("Пароли не совпадают.")
            continue
        return p1


def _refuse_overwrite():
    if load():
        print(f"Файл {KEYSTORE_PATH} уже существует. Сначала сделайте бэкап и удалите его вручную.")
        sys.exit(1)


def cmd_new():
    _refuse_overwrite()
    kp = KeyPair.generate()
    account = kp.implicit_account
    save(encrypt(account, kp, _new_password()))
    print("\n✅ Кошелёк создан и зашифрован.")
    print("Адрес (пополните его NEAR, чтобы он активировался):\n" + account)
    print("\nСделайте бэкап ключа командой 'export' и храните его офлайн.")


def cmd_import():
    _refuse_overwrite()
    account = input("Адрес аккаунта (например, mytrader.near): ").strip().lower()
    if not ACCOUNT_RE.match(account) or not 2 <= len(account) <= 64:
        print("Неверный адрес.")
        sys.exit(1)
    try:
        kp = KeyPair.from_string(getpass.getpass("Приватный ключ ed25519:... (ввод скрыт): "))
    except ValueError as e:
        print("Ошибка ключа:", e)
        sys.exit(1)
    if re.fullmatch(r"[0-9a-f]{64}", account) and account != kp.implicit_account:
        print("Этот ключ не от указанного implicit-аккаунта.")
        sys.exit(1)
    save(encrypt(account, kp, _new_password()))
    print(f"\n✅ Импортирован {account}. Публичный ключ: {kp.public_key}")
    print("Бот при разблокировке проверит, что ключ привязан к аккаунту.")


def cmd_show():
    s = load()
    print(f"{s['account_id']}\n{s['public_key']}" if s else "Кошелёк не создан.")


def cmd_export():
    s = load()
    if not s:
        print("Кошелёк не создан.")
        return
    kp = decrypt(s, getpass.getpass("Пароль: "))
    print("\n⚠️  Никому не показывайте этот ключ. Сохраните офлайн и очистите экран (clear).")
    print(kp.secret_string())


def cmd_passwd():
    s = load()
    if not s:
        print("Кошелёк не создан.")
        return
    kp = decrypt(s, getpass.getpass("Текущий пароль: "))
    save(encrypt(s["account_id"], kp, _new_password()))
    print("✅ Пароль изменён.")


if __name__ == "__main__":
    cmds = {"new": cmd_new, "import": cmd_import, "show": cmd_show, "export": cmd_export, "passwd": cmd_passwd}
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        print(__doc__)
        sys.exit(1)
    try:
        cmds[sys.argv[1]]()
    except ValueError as e:
        print("Ошибка:", e)
        sys.exit(1)
