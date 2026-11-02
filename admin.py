"""Админ-утилита на сервере (ключи вводятся скрыто, в чат не попадают).

    sudo -u tradebot venv/bin/python admin.py payout-key   — ключ кошелька выплат рефералам (= FEE_ACCOUNT)
    sudo -u tradebot venv/bin/python admin.py payout-clear — удалить этот ключ из бота
    sudo -u tradebot venv/bin/python admin.py stats        — комиссии и рефералка
"""
from __future__ import annotations

import getpass
import os
import sys

from dotenv import load_dotenv

load_dotenv()

import wallets  # noqa: E402
from db import DB  # noqa: E402
from near import YOCTO  # noqa: E402

FEE_ACCOUNT = os.getenv("FEE_ACCOUNT", "buddhadefi-hot.tg").strip().lower()


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    db = DB(os.getenv("DB_PATH", "trade.db"))
    if cmd == "payout-key":
        vault = wallets.Vault(wallets.load_master_key())
        print(f"Кошелёк выплат: {FEE_ACCOUNT}")
        print("Вставьте сид-фразу или приватный ключ ed25519:… этого кошелька (ввод не отображается).")
        print("Ключ должен быть FullAccess для этого аккаунта. Держите на нём только сумму под выплаты.")
        secret = getpass.getpass("> ")
        try:
            kp, _ = wallets.parse_secret(secret)
        except wallets.WalletError as e:
            sys.exit(f"Ошибка: {e}")
        db.set("payout_wallet", {"account": FEE_ACCOUNT, "public_key": kp.public_key,
                                 "blob": vault.seal(0, FEE_ACCOUNT, kp, None).hex()})
        print(f"✅ Сохранено (зашифровано MASTER_KEY). Публичный ключ: {kp.public_key}")
        print("Перезапустите бота: systemctl restart tradebot. Проверка ключа — команда /admin в боте.")
    elif cmd == "payout-clear":
        db.set("payout_wallet", None)
        print("Ключ выплат удалён. Перезапустите бота.")
    elif cmd == "stats":
        st = db.fee_stats()
        n = lambda y: f"{y / YOCTO:.4f} NEAR"
        print(f"Собрано комиссий: {n(st['paid'])} (ждут {n(st['pending'])}, не дошли {n(st['failed'])})")
        print(f"Рефералам начислено {n(st['ref_accrued'])}, выплачено {n(st['ref_paid'])}")
        print(f"Пользователей {st['users']}, пришли по рефке {st['with_ref']}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
