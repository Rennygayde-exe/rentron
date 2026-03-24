from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "usage_log.db"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS command_usage(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                command_name TEXT NOT NULL,
                command_type TEXT NOT NULL,
                guild_id INTEGER,
                role_id INTEGER,
                role_name TEXT,
                used_at TEXT NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS response_usage(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                response_id TEXT NOT NULL,
                guild_id INTEGER,
                role_id INTEGER,
                role_name TEXT,
                used_at TEXT NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS sunsets(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_type TEXT NOT NULL,
                target_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                sunset_days INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                reason TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                UNIQUE(target_type, target_name)
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS sunset_usage(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sunset_id INTEGER NOT NULL,
                role_id INTEGER,
                role_name TEXT,
                used_at TEXT NOT NULL,
                FOREIGN KEY(sunset_id) REFERENCES sunsets(id)
            )
            """
        )
        con.commit()


def log_command_usage(
    command_name: str,
    command_type: str,
    guild_id: Optional[int],
    role_id: Optional[int],
    role_name: Optional[str],
    used_at: Optional[datetime] = None,
) -> None:
    if not command_name:
        return
    ensure_db()
    stamp = (used_at or _utc_now()).isoformat()
    try:
        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                """
                INSERT INTO command_usage(command_name, command_type, guild_id, role_id, role_name, used_at)
                VALUES(?,?,?,?,?,?)
                """,
                (command_name, command_type, guild_id, role_id, role_name, stamp),
            )
            con.commit()
    except sqlite3.Error:
        return


def log_response_usage(
    response_id: str,
    guild_id: Optional[int],
    role_id: Optional[int],
    role_name: Optional[str],
    used_at: Optional[datetime] = None,
) -> None:
    if not response_id:
        return
    ensure_db()
    stamp = (used_at or _utc_now()).isoformat()
    try:
        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                """
                INSERT INTO response_usage(response_id, guild_id, role_id, role_name, used_at)
                VALUES(?,?,?,?,?)
                """,
                (response_id, guild_id, role_id, role_name, stamp),
            )
            con.commit()
    except sqlite3.Error:
        return


def upsert_sunset(target_type: str, target_name: str, days: int, reason: str | None = None) -> None:
    ensure_db()
    now = _utc_now()
    expires = now + timedelta(days=days)
    try:
        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                """
                INSERT INTO sunsets(target_type, target_name, created_at, sunset_days, expires_at, reason, active)
                VALUES(?,?,?,?,?,?,1)
                ON CONFLICT(target_type, target_name) DO UPDATE SET
                    created_at=excluded.created_at,
                    sunset_days=excluded.sunset_days,
                    expires_at=excluded.expires_at,
                    reason=excluded.reason,
                    active=1
                """,
                (target_type, target_name, now.isoformat(), days, expires.isoformat(), reason),
            )
            con.commit()
    except sqlite3.Error:
        return


def expire_old_sunsets() -> None:
    ensure_db()
    now = _utc_now().isoformat()
    try:
        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                "UPDATE sunsets SET active=0 WHERE active=1 AND expires_at <= ?",
                (now,),
            )
            con.commit()
    except sqlite3.Error:
        return


def get_active_sunset(target_type: str, target_name: str) -> Optional[dict]:
    ensure_db()
    expire_old_sunsets()
    try:
        with sqlite3.connect(DB_PATH) as con:
            row = con.execute(
                """
                SELECT id, target_type, target_name, created_at, sunset_days, expires_at, reason
                FROM sunsets
                WHERE target_type=? AND target_name=? AND active=1
                """,
                (target_type, target_name),
            ).fetchone()
    except sqlite3.Error:
        row = None
    if not row:
        return None
    return {
        "id": int(row[0]),
        "target_type": str(row[1]),
        "target_name": str(row[2]),
        "created_at": str(row[3]),
        "sunset_days": int(row[4]),
        "expires_at": str(row[5]),
        "reason": str(row[6]) if row[6] is not None else None,
    }


def log_sunset_usage(sunset_id: int, role_id: Optional[int], role_name: Optional[str]) -> None:
    ensure_db()
    stamp = _utc_now().isoformat()
    try:
        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                """
                INSERT INTO sunset_usage(sunset_id, role_id, role_name, used_at)
                VALUES(?,?,?,?)
                """,
                (sunset_id, role_id, role_name, stamp),
            )
            con.commit()
    except sqlite3.Error:
        return


def get_usage_window_start(days: int) -> datetime:
    return _utc_now() - timedelta(days=days)

