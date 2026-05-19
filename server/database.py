"""SQLite data access layer for the chat system."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import sqlite3
from typing import Any, Iterator
from uuid import uuid4


DEFAULT_DB_PATH = Path("data") / "chat.db"
PBKDF2_ITERATIONS = 120_000


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    if not password:
        raise ValueError("password must not be empty")
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        PBKDF2_ITERATIONS,
    ).hex()
    return salt, digest


def _verify_password(password: str, salt: str, expected_hash: str) -> bool:
    _, candidate = _hash_password(password, salt)
    return hmac.compare_digest(candidate, expected_hash)


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class ChatDatabase:
    """Small repository-style wrapper around SQLite.

    The server should depend on these methods instead of composing SQL in
    routing code. This keeps protocol handling, business rules, and storage
    easier to test independently.
    """

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        if self.db_path != Path(":memory:"):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    salt TEXT NOT NULL,
                    display_name TEXT,
                    status TEXT NOT NULL DEFAULT 'offline',
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT
                );

                CREATE TABLE IF NOT EXISTS groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    owner_username TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (owner_username) REFERENCES users(username)
                );

                CREATE TABLE IF NOT EXISTS group_members (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    username TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'member',
                    joined_at TEXT NOT NULL,
                    UNIQUE (group_id, username),
                    FOREIGN KEY (group_id) REFERENCES groups(group_id) ON DELETE CASCADE,
                    FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL UNIQUE,
                    message_type TEXT NOT NULL,
                    sender TEXT,
                    receiver TEXT,
                    group_id TEXT,
                    content TEXT,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (sender) REFERENCES users(username),
                    FOREIGN KEY (receiver) REFERENCES users(username),
                    FOREIGN KEY (group_id) REFERENCES groups(group_id)
                );

                CREATE TABLE IF NOT EXISTS file_transfers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id TEXT NOT NULL UNIQUE,
                    sender TEXT NOT NULL,
                    receiver TEXT,
                    group_id TEXT,
                    filename TEXT NOT NULL,
                    filesize INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    offset INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (sender) REFERENCES users(username),
                    FOREIGN KEY (receiver) REFERENCES users(username),
                    FOREIGN KEY (group_id) REFERENCES groups(group_id)
                );

                CREATE INDEX IF NOT EXISTS idx_messages_private
                    ON messages(sender, receiver, created_at);
                CREATE INDEX IF NOT EXISTS idx_messages_group
                    ON messages(group_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_group_members_username
                    ON group_members(username);
                """
            )

    def create_user(self, username: str, password: str, display_name: str | None = None) -> dict[str, Any]:
        salt, password_hash = _hash_password(password)
        now = utc_now()
        try:
            with self.connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO users (username, password_hash, salt, display_name, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (username, password_hash, salt, display_name or username, now),
                )
                user_id = cur.lastrowid
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"user already exists: {username}") from exc
        return {
            "id": user_id,
            "username": username,
            "display_name": display_name or username,
            "status": "offline",
            "created_at": now,
        }

    def authenticate_user(self, username: str, password: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT password_hash, salt FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        if row is None:
            return False
        return _verify_password(password, row["salt"], row["password_hash"])

    def get_user(self, username: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, username, display_name, status, created_at, last_seen_at
                FROM users
                WHERE username = ?
                """,
                (username,),
            ).fetchone()
        return _row_to_dict(row)

    def set_user_status(self, username: str, status: str) -> None:
        if status not in {"online", "offline", "away"}:
            raise ValueError(f"invalid user status: {status}")
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET status = ?, last_seen_at = ? WHERE username = ?",
                (status, utc_now(), username),
            )

    def create_group(self, name: str, owner_username: str, group_id: str | None = None) -> dict[str, Any]:
        group_id = group_id or uuid4().hex
        now = utc_now()
        with self.connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO groups (group_id, name, owner_username, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (group_id, name, owner_username, now),
                )
                conn.execute(
                    """
                    INSERT INTO group_members (group_id, username, role, joined_at)
                    VALUES (?, ?, 'owner', ?)
                    """,
                    (group_id, owner_username, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"cannot create group {name!r}: {exc}") from exc
        return {
            "group_id": group_id,
            "name": name,
            "owner_username": owner_username,
            "created_at": now,
        }

    def join_group(self, group_id: str, username: str) -> dict[str, Any]:
        now = utc_now()
        try:
            with self.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO group_members (group_id, username, role, joined_at)
                    VALUES (?, ?, 'member', ?)
                    """,
                    (group_id, username, now),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"cannot join group {group_id}: {exc}") from exc
        return {"group_id": group_id, "username": username, "role": "member", "joined_at": now}

    def leave_group(self, group_id: str, username: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM group_members WHERE group_id = ? AND username = ?",
                (group_id, username),
            )

    def list_group_members(self, group_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT username, role, joined_at
                FROM group_members
                WHERE group_id = ?
                ORDER BY joined_at ASC
                """,
                (group_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_message(
        self,
        *,
        message_type: str,
        sender: str | None = None,
        receiver: str | None = None,
        group_id: str | None = None,
        content: str | None = None,
        payload: dict[str, Any] | None = None,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        message_id = message_id or uuid4().hex
        payload_json = json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":"))
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO messages (
                    message_id, message_type, sender, receiver, group_id,
                    content, payload_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (message_id, message_type, sender, receiver, group_id, content, payload_json, now),
            )
        return {
            "message_id": message_id,
            "message_type": message_type,
            "sender": sender,
            "receiver": receiver,
            "group_id": group_id,
            "content": content,
            "payload": payload or {},
            "created_at": now,
        }

    def get_private_history(self, user_a: str, user_b: str, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT message_id, message_type, sender, receiver, group_id, content,
                       payload_json, created_at
                FROM messages
                WHERE group_id IS NULL
                  AND ((sender = ? AND receiver = ?) OR (sender = ? AND receiver = ?))
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (user_a, user_b, user_b, user_a, limit),
            ).fetchall()
        return list(reversed([self._message_row(row) for row in rows]))

    def get_group_history(self, group_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT message_id, message_type, sender, receiver, group_id, content,
                       payload_json, created_at
                FROM messages
                WHERE group_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (group_id, limit),
            ).fetchall()
        return list(reversed([self._message_row(row) for row in rows]))

    def create_file_transfer(
        self,
        *,
        sender: str,
        filename: str,
        filesize: int,
        receiver: str | None = None,
        group_id: str | None = None,
        file_id: str | None = None,
    ) -> dict[str, Any]:
        if filesize < 0:
            raise ValueError("filesize must not be negative")
        file_id = file_id or uuid4().hex
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO file_transfers (
                    file_id, sender, receiver, group_id, filename, filesize,
                    status, offset, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'started', 0, ?, ?)
                """,
                (file_id, sender, receiver, group_id, filename, filesize, now, now),
            )
        return {
            "file_id": file_id,
            "sender": sender,
            "receiver": receiver,
            "group_id": group_id,
            "filename": filename,
            "filesize": filesize,
            "status": "started",
            "offset": 0,
            "created_at": now,
            "updated_at": now,
        }

    def update_file_transfer(self, file_id: str, *, status: str, offset: int | None = None) -> dict[str, Any]:
        if status not in {"started", "transferring", "finished", "failed"}:
            raise ValueError(f"invalid file transfer status: {status}")
        now = utc_now()
        with self.connect() as conn:
            if offset is None:
                conn.execute(
                    "UPDATE file_transfers SET status = ?, updated_at = ? WHERE file_id = ?",
                    (status, now, file_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE file_transfers
                    SET status = ?, offset = ?, updated_at = ?
                    WHERE file_id = ?
                    """,
                    (status, offset, now, file_id),
                )
            row = conn.execute(
                """
                SELECT file_id, sender, receiver, group_id, filename, filesize,
                       status, offset, created_at, updated_at
                FROM file_transfers
                WHERE file_id = ?
                """,
                (file_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"file transfer not found: {file_id}")
        return dict(row)

    @staticmethod
    def _message_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["payload"] = json.loads(data.pop("payload_json") or "{}")
        return data


def init_db(db_path: str | Path = DEFAULT_DB_PATH) -> ChatDatabase:
    """Initialize the database and return a ready-to-use wrapper."""

    db = ChatDatabase(db_path)
    db.init_db()
    return db

