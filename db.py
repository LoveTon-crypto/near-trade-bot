"""SQLite: настройки, сделки, короткие id токенов для кнопок."""
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
        """)
        self.c.commit()

    # settings
    def get(self, k, default=None):
        r = self.c.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def set(self, k, v):
        self.c.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (k, json.dumps(v)))
        self.c.commit()

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

    # trades
    def add_trade(self, token, side, token_amt: int, near_amt: int, tx: str):
        self.c.execute("INSERT INTO trades (ts, token, side, token_amt, near_amt, tx) VALUES (?,?,?,?,?,?)",
                       (int(time.time()), token, side, str(token_amt), str(near_amt), tx))
        self.c.commit()

    def traded_tokens(self) -> list[str]:
        return [r[0] for r in self.c.execute("SELECT token FROM trades GROUP BY token ORDER BY MAX(ts) DESC")]

    def totals(self, token) -> tuple[int, int]:
        """(всего потрачено NEAR, всего получено NEAR) по токену."""
        spent = got = 0
        for side, near in self.c.execute("SELECT side, near_amt FROM trades WHERE token=?", (token,)):
            if side == "buy":
                spent += int(near)
            else:
                got += int(near)
        return spent, got
