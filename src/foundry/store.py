from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def uid(prefix: str) -> str:
    return prefix + "_" + uuid4().hex[:20]


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class DomainError(Exception):
    def __init__(self, message: str, status: int = 422):
        self.message, self.status = message, status
        super().__init__(message)


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=15000")
        return con

    def initialize(self):
        with closing(self.connect()) as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript("""
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT OR IGNORE INTO metadata VALUES ('schema_version','1');
            CREATE TABLE IF NOT EXISTS principals(
              id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL,
              scopes TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
              revoked INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS sessions(
              token_hash TEXT PRIMARY KEY, principal_id TEXT NOT NULL REFERENCES principals(id),
              expires_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS businesses(
              id TEXT PRIMARY KEY, data TEXT NOT NULL, version INTEGER NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS objects(
              id TEXT PRIMARY KEY, business_id TEXT NOT NULL REFERENCES businesses(id),
              kind TEXT NOT NULL, title TEXT NOT NULL, data TEXT NOT NULL, state TEXT NOT NULL,
              version INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS objects_business_kind ON objects(business_id,kind);
            CREATE TABLE IF NOT EXISTS revisions(
              object_id TEXT NOT NULL, version INTEGER NOT NULL, snapshot TEXT NOT NULL,
              actor_id TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(object_id,version));
            CREATE TABLE IF NOT EXISTS events(
              seq INTEGER PRIMARY KEY AUTOINCREMENT, business_id TEXT NOT NULL,
              actor_id TEXT NOT NULL, action TEXT NOT NULL, object_id TEXT NOT NULL,
              payload TEXT NOT NULL, created_at TEXT NOT NULL, prev_hash TEXT NOT NULL,
              hash TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_business ON events(business_id,seq);
            CREATE TABLE IF NOT EXISTS commands(
              principal_id TEXT NOT NULL, command_key TEXT NOT NULL, request_hash TEXT NOT NULL,
              response TEXT NOT NULL, created_at TEXT NOT NULL,
              PRIMARY KEY(principal_id,command_key));
            CREATE TABLE IF NOT EXISTS payment_references(
              business_id TEXT NOT NULL, reference TEXT NOT NULL, payment_id TEXT NOT NULL,
              PRIMARY KEY(business_id,reference));
            CREATE TABLE IF NOT EXISTS ai_calls(
              id TEXT PRIMARY KEY, business_id TEXT NOT NULL, principal_id TEXT NOT NULL,
              command_key TEXT NOT NULL, request_hash TEXT NOT NULL, data TEXT NOT NULL,
              state TEXT NOT NULL, created_at TEXT NOT NULL,
              UNIQUE(principal_id,command_key));
            """)
            version = con.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0]
            if version != "1":
                raise RuntimeError("Unsupported database schema. Use a compatible release.")
        os.chmod(self.path, 0o600)

    @contextmanager
    def write(self):
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    @contextmanager
    def read(self):
        con = self.connect()
        try:
            con.execute("BEGIN")
            yield con
        finally:
            con.rollback()
            con.close()

    def command(self, actor: dict, key: str, operation: str, payload: dict, callback):
        if not key or len(key) > 200:
            raise DomainError("Supply an Idempotency-Key of 1-200 characters.", 400)
        fingerprint = digest(canonical({"operation": operation, "payload": payload}))
        with self.write() as con:
            prior = con.execute(
                "SELECT * FROM commands WHERE principal_id=? AND command_key=?", (actor["id"], key)
            ).fetchone()
            if prior:
                if prior["request_hash"] != fingerprint:
                    raise DomainError("This idempotency key was already used for a different request.", 409)
                return json.loads(prior["response"])
            result = callback(con)
            con.execute(
                "INSERT INTO commands VALUES(?,?,?,?,?)",
                (actor["id"], key, fingerprint, canonical(result), now()),
            )
            return result

    def create_principal(self, con, name: str, role: str, scopes: list[str]):
        token = "bf_" + secrets.token_urlsafe(36)
        item = {"id": uid("person"), "name": name, "role": role, "business_ids": scopes, "created_at": now()}
        con.execute(
            "INSERT INTO principals(id,name,role,scopes,key_hash,created_at) VALUES(?,?,?,?,?,?)",
            (item["id"], name, role, canonical(scopes), digest(token), item["created_at"]),
        )
        return {**item, "token": token}

    def bootstrap(self, name="Local owner", token_file: Path | None = None):
        with self.write() as con:
            if con.execute("SELECT 1 FROM principals LIMIT 1").fetchone():
                return None
            item = self.create_principal(con, name, "owner", [])
            if token_file:
                token_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as stream:
                    stream.write(item["token"] + "\n")
            return item

    def authenticate(self, token: str, session=False):
        with self.read() as con:
            if session:
                row = con.execute(
                    "SELECT p.* FROM principals p JOIN sessions s ON p.id=s.principal_id WHERE s.token_hash=? AND s.expires_at>? AND p.revoked=0",
                    (digest(token), now()),
                ).fetchone()
            else:
                row = con.execute(
                    "SELECT * FROM principals WHERE key_hash=? AND revoked=0", (digest(token),)
                ).fetchone()
            if not row:
                raise DomainError("Sign in with a valid access key.", 401)
            return {
                "id": row["id"],
                "name": row["name"],
                "role": row["role"],
                "business_ids": json.loads(row["scopes"]),
            }

    def session(self, actor):
        value = secrets.token_urlsafe(40)
        expiry = (datetime.now(UTC) + timedelta(hours=8)).isoformat(timespec="microseconds")
        with self.write() as con:
            con.execute("DELETE FROM sessions WHERE expires_at<?", (now(),))
            con.execute("INSERT INTO sessions VALUES(?,?,?)", (digest(value), actor["id"], expiry))
        return value

    def event(self, con, business_id, actor, action, object_id, payload):
        prev = con.execute(
            "SELECT hash FROM events WHERE business_id=? ORDER BY seq DESC LIMIT 1", (business_id,)
        ).fetchone()
        data = {
            "business_id": business_id,
            "actor_id": actor["id"],
            "action": action,
            "object_id": object_id,
            "payload": payload,
            "created_at": now(),
            "prev_hash": prev[0] if prev else "0" * 64,
        }
        h = digest(canonical(data))
        con.execute(
            "INSERT INTO events(business_id,actor_id,action,object_id,payload,created_at,prev_hash,hash) VALUES(?,?,?,?,?,?,?,?)",
            (
                business_id,
                actor["id"],
                action,
                object_id,
                canonical(payload),
                data["created_at"],
                data["prev_hash"],
                h,
            ),
        )
        return h

    def add(self, con, business_id, kind, title, data, actor, state="draft", object_id=None):
        stamp = now()
        item = {
            "id": object_id or uid(kind),
            "business_id": business_id,
            "kind": kind,
            "title": title,
            "data": data,
            "state": state,
            "version": 1,
            "created_at": stamp,
            "updated_at": stamp,
        }
        con.execute(
            "INSERT INTO objects VALUES(?,?,?,?,?,?,?,?,?)",
            (item["id"], business_id, kind, title, canonical(data), state, 1, stamp, stamp),
        )
        con.execute(
            "INSERT INTO revisions VALUES(?,?,?,?,?)", (item["id"], 1, canonical(item), actor["id"], stamp)
        )
        self.event(con, business_id, actor, f"{kind}.created", item["id"], item)
        return item

    def update(self, con, item, data, state, actor, expected_version, title=None, action=None):
        if item["version"] != expected_version:
            raise DomainError("This record changed. Refresh it before saving.", 409)
        item = {
            **item,
            "data": data,
            "state": state,
            "title": title or item["title"],
            "version": expected_version + 1,
            "updated_at": now(),
        }
        changed = con.execute(
            "UPDATE objects SET data=?,state=?,title=?,version=?,updated_at=? WHERE id=? AND version=?",
            (
                canonical(data),
                state,
                item["title"],
                item["version"],
                item["updated_at"],
                item["id"],
                expected_version,
            ),
        )
        if changed.rowcount != 1:
            raise DomainError("This record changed. Refresh it before saving.", 409)
        con.execute(
            "INSERT INTO revisions VALUES(?,?,?,?,?)",
            (item["id"], item["version"], canonical(item), actor["id"], item["updated_at"]),
        )
        self.event(con, item["business_id"], actor, action or f"{item['kind']}.updated", item["id"], item)
        return item

    @staticmethod
    def unpack(row):
        if not row:
            raise DomainError("Record not found.", 404)
        value = dict(row)
        value["data"] = json.loads(value["data"])
        return value

    def business(self, con, business_id):
        return self.unpack(con.execute("SELECT * FROM businesses WHERE id=?", (business_id,)).fetchone())

    def get(self, con, business_id, object_id, kind=None):
        item = self.unpack(
            con.execute(
                "SELECT * FROM objects WHERE id=? AND business_id=?", (object_id, business_id)
            ).fetchone()
        )
        if kind and item["kind"] != kind:
            raise DomainError("Unexpected record type.", 422)
        return item

    def objects(self, con, business_id, kind=None):
        sql, params = "SELECT * FROM objects WHERE business_id=?", [business_id]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        return [self.unpack(x) for x in con.execute(sql + " ORDER BY created_at,id", params)]

    def verify_chain(self, con, business_id):
        prev, count = "0" * 64, 0
        for row in con.execute("SELECT * FROM events WHERE business_id=? ORDER BY seq", (business_id,)):
            value = {
                k: row[k]
                for k in ("business_id", "actor_id", "action", "object_id", "created_at", "prev_hash")
            }
            value["payload"] = json.loads(row["payload"])
            if value["prev_hash"] != prev or digest(canonical(value)) != row["hash"]:
                return {"valid": False, "events_checked": count, "failed_sequence": row["seq"]}
            prev = row["hash"]
            count += 1
        return {"valid": True, "events_checked": count, "head_hash": prev}
