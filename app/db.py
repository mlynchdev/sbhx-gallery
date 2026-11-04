"""Tiny database layer with two interchangeable backends.

D1 is SQLite, so the same SQL runs against a local file in development and
against D1's HTTP API in production. Flask can't run inside a Worker, so the
HTTP API is how a normally hosted app reaches D1.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import requests
from flask import Flask, current_app, g


@dataclass
class ExecResult:
    last_row_id: int | None
    changes: int


class SQLiteBackend:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    def _conn(self) -> sqlite3.Connection:
        if "sqlite_conn" not in g:
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            g.sqlite_conn = conn
        return g.sqlite_conn

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        return [dict(r) for r in self._conn().execute(sql, params).fetchall()]

    def execute(self, sql: str, params: Sequence[Any] = ()) -> ExecResult:
        conn = self._conn()
        cur = conn.execute(sql, params)
        conn.commit()
        return ExecResult(cur.lastrowid, cur.rowcount)

    def executescript(self, script: str) -> None:
        conn = sqlite3.connect(self.path)
        conn.executescript(script)
        conn.close()

    def close(self) -> None:
        conn = g.pop("sqlite_conn", None)
        if conn is not None:
            conn.close()


class D1Error(RuntimeError):
    pass


class D1Backend:
    def __init__(self, account_id: str, database_id: str, api_token: str):
        if not (account_id and database_id and api_token):
            raise RuntimeError("D1 backend needs CF_ACCOUNT_ID, D1_DATABASE_ID and CF_API_TOKEN")
        self.url = (
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
            f"/d1/database/{database_id}/query"
        )
        self.http = requests.Session()
        self.http.headers["Authorization"] = f"Bearer {api_token}"

    def _run(self, sql: str, params: Sequence[Any]) -> dict:
        resp = self.http.post(self.url, json={"sql": sql, "params": list(params)}, timeout=15)
        body = resp.json()
        if not resp.ok or not body.get("success"):
            raise D1Error(f"D1 query failed ({resp.status_code}): {body.get('errors')}")
        return body["result"][0]

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        return self._run(sql, params)["results"]

    def execute(self, sql: str, params: Sequence[Any] = ()) -> ExecResult:
        meta = self._run(sql, params).get("meta", {})
        return ExecResult(meta.get("last_row_id"), meta.get("changes", 0))

    def close(self) -> None:
        pass


def init_app(app: Flask) -> None:
    cfg = app.config
    if cfg["DB_BACKEND"] == "d1":
        backend = D1Backend(cfg["CF_ACCOUNT_ID"], cfg["D1_DATABASE_ID"], cfg["CF_API_TOKEN"])
    else:
        backend = SQLiteBackend(cfg["SQLITE_PATH"])
    app.extensions["db"] = backend
    app.teardown_appcontext(lambda exc: backend.close())

    @app.cli.command("init-db")
    def init_db() -> None:
        """Apply migrations/*.sql to the local SQLite database."""
        if not isinstance(backend, SQLiteBackend):
            raise SystemExit("For D1 run: wrangler d1 migrations apply photo-share --remote")
        for path in sorted(Path(app.root_path, "..", "migrations").glob("*.sql")):
            backend.executescript(path.read_text())
            print(f"applied {path.name}")


def db() -> SQLiteBackend | D1Backend:
    return current_app.extensions["db"]
