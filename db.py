"""SQLite: users, wallets, per-user settings, trades, short token ids for buttons."""
from __future__ import annotations

import json
import sqlite3
import time


class DB:
    def __init__(self, path: str):
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.executescript("""
        CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS tokens (id INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT UNIQUE,
                                           symbol TEXT, decimals INTEGER);
        CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, token TEXT,
                                           side TEXT, token_amt TEXT, near_amt TEXT, tx TEXT);
        CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, active_wallet INTEGER, created INTEGER);
        CREATE TABLE IF NOT EXISTS wallets (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                                            name TEXT, account TEXT NOT NULL, secret BLOB NOT NULL,
                                            has_phrase INTEGER, created INTEGER,
                                            UNIQUE(user_id, account));
        """)
        cols = {r[1] for r in self.c.execute("PRAGMA table_info(trades)")}
        if "account" not in cols:  # trades from the single-wallet version
            self.c.execute("ALTER TABLE trades ADD COLUMN account TEXT")
        self.c.commit()

    # settings (global or per user: key "u{id}:name")
    def get(self, k, default=None):
        r = self.c.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def set(self, k, v):
        self.c.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (k, json.dumps(v)))
        self.c.commit()

    # users & wallets
    def ensure_user(self, uid: int):
        self.c.execute("INSERT OR IGNORE INTO users (id, created) VALUES (?,?)", (uid, int(time.time())))
        self.c.commit()

    def wallets(self, uid: int) -> list[tuple]:
        """[(id, name, account, has_phrase)] oldest first."""
        return self.c.execute("SELECT id, name, account, has_phrase FROM wallets WHERE user_id=? ORDER BY id",
                              (uid,)).fetchall()

    def wallet(self, uid: int, wid: int):
        """(id, name, account, secret, has_phrase) — only if it belongs to this user."""
        return self.c.execute("SELECT id, name, account, secret, has_phrase FROM wallets WHERE id=? AND user_id=?",
                              (wid, uid)).fetchone()

    def add_wallet(self, uid: int, name: str, account: str, secret: bytes, has_phrase: bool) -> int:
        cur = self.c.execute("INSERT INTO wallets (user_id, name, account, secret, has_phrase, created) "
                             "VALUES (?,?,?,?,?,?)", (uid, name, account, secret, int(has_phrase), int(time.time())))
        wid = cur.lastrowid
        if not self.active_wallet(uid):
            self.set_active(uid, wid)
        self.c.commit()
        return wid

    def rename_wallet(self, uid: int, wid: int, name: str):
        self.c.execute("UPDATE wallets SET name=? WHERE id=? AND user_id=?", (name, wid, uid))
        self.c.commit()

    def delete_wallet(self, uid: int, wid: int):
        self.c.execute("DELETE FROM wallets WHERE id=? AND user_id=?", (wid, uid))
        if self.active_wallet(uid) == wid:
            rest = self.wallets(uid)
            self.c.execute("UPDATE users SET active_wallet=? WHERE id=?", (rest[0][0] if rest else None, uid))
        self.c.commit()

    def active_wallet(self, uid: int) -> int | None:
        r = self.c.execute("SELECT active_wallet FROM users WHERE id=?", (uid,)).fetchone()
        return r[0] if r else None

    def set_active(self, uid: int, wid: int):
        self.c.execute("UPDATE users SET active_wallet=? WHERE id=?", (wid, uid))
        self.c.commit()

    def has_account(self, uid: int, account: str) -> bool:
        return bool(self.c.execute("SELECT 1 FROM wallets WHERE user_id=? AND account=?", (uid, account)).fetchone())

    # tokens
    def token_id(self, address: str, symbol: str = "", decimals: int = 0) -> int:
        self.c.execute("INSERT OR IGNORE INTO tokens (address, symbol, decimals) VALUES (?,?,?)",
                       (address, symbol, decimals))
        if symbol:
            self.c.execute("UPDATE tokens SET symbol=?, decimals=? WHERE address=?", (symbol, decimals, address))
        self.c.commit()
        return self.c.execute("SELECT id FROM tokens WHERE address=?", (address,)).fetchone()[0]

    def token_by_id(self, tid: int):
        return self.c.execute("SELECT address, symbol, decimals FROM tokens WHERE id=?", (tid,)).fetchone()

    # trades (per wallet account)
    def add_trade(self, account, token, side, token_amt: int, near_amt: int, tx: str):
        self.c.execute("INSERT INTO trades (ts, account, token, side, token_amt, near_amt, tx) VALUES (?,?,?,?,?,?,?)",
                       (int(time.time()), account, token, side, str(token_amt), str(near_amt), tx))
        self.c.commit()

    def traded_tokens(self, account) -> list[str]:
        return [r[0] for r in self.c.execute(
            "SELECT token FROM trades WHERE account=? GROUP BY token ORDER BY MAX(ts) DESC", (account,))]

    def totals(self, account, token) -> tuple[int, int]:
        """(NEAR spent, NEAR received) for a token on this wallet."""
        spent = got = 0
        for side, near in self.c.execute("SELECT side, near_amt FROM trades WHERE account=? AND token=?",
                                         (account, token)):
            if side == "buy":
                spent += int(near)
            else:
                got += int(near)
        return spent, got
