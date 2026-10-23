from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from .state import now

DONE = {"submitted", "exists", "legacy_uploaded"}
UNCERTAIN = {"submitting", "unknown"}
FINISHED = DONE | {"test_prepared"}
STATUSES = FINISHED | UNCERTAIN | {"pending", "blocked", "failed", "uploading", "ready", "preparing"}


class UploadDatabase:
    """共享视频资产；上传状态、作品映射按平台和账号隔离。"""

    def __init__(self, path: Path, *, preview=False, platform="kuaishou", account_id="default"):
        if not platform.strip() or not account_id.strip():
            raise ValueError("platform/account_id 不得为空")
        self.path, self.platform, self.account_id = path, platform, account_id
        if preview:
            self.connection = sqlite3.connect(":memory:")
            if path.exists():
                source = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
                try:
                    source.backup(self.connection)
                finally:
                    source.close()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(path)
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            tables = {r[0] for r in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'videos' in tables and 'upload_jobs' not in tables:
                backup = path.with_name(path.name + '.before-multiplatform.sqlite3')
                if backup.exists():
                    self.connection.close()
                    raise RuntimeError(f"迁移备份已存在，请先核对：{backup}")
                saved = sqlite3.connect(backup)
                try:
                    self.connection.backup(saved)
                finally:
                    saved.close()
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS assets (
                sha256 TEXT PRIMARY KEY, path TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS upload_jobs (
                platform TEXT NOT NULL, account_id TEXT NOT NULL,
                sha256 TEXT NOT NULL, path TEXT NOT NULL, sidecar TEXT,
                status TEXT NOT NULL, fingerprint TEXT, series_key TEXT,
                series_title TEXT, episode INTEGER, content_id TEXT, episode_id TEXT,
                message TEXT NOT NULL DEFAULT '', details TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL, ledger_dirty INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(platform,account_id,sha256)
            );
            CREATE TABLE IF NOT EXISTS upload_series (
                platform TEXT NOT NULL, account_id TEXT NOT NULL, series_key TEXT NOT NULL,
                content_id TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(platform,account_id,series_key)
            );
            CREATE TABLE IF NOT EXISTS upload_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL, account_id TEXT NOT NULL,
                sha256 TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS schema_migrations(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL);
        """)
        tables = {r[0] for r in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'videos' in tables and not self.connection.execute("SELECT 1 FROM schema_migrations WHERE name='legacy-v1'").fetchone():
            columns = 'sha256,path,sidecar,status,fingerprint,series_key,series_title,episode,content_id,episode_id,message,details,updated_at,ledger_dirty'
            with self.connection:
                self.connection.execute(f"INSERT OR IGNORE INTO upload_jobs(platform,account_id,{columns}) SELECT 'kuaishou','default',{columns} FROM videos")
                self.connection.execute('INSERT OR IGNORE INTO assets SELECT sha256,path,updated_at FROM videos')
                if 'series' in tables:
                    self.connection.execute("INSERT OR IGNORE INTO upload_series SELECT 'kuaishou','default',series_key,content_id,updated_at FROM series")
                self.connection.execute("INSERT INTO schema_migrations VALUES('legacy-v1',?)", (now(),))

    @property
    def scope(self):
        return self.platform, self.account_id

    @property
    def is_legacy_scope(self):
        return self.scope == ('kuaishou', 'default')

    def task_id(self, sha256):
        if self.is_legacy_scope:
            return sha256[:24]
        return hashlib.sha256(f'{self.platform}\0{self.account_id}\0{sha256}'.encode()).hexdigest()[:24]

    def close(self):
        self.connection.close()

    @staticmethod
    def decode(row):
        if row is None:
            return None
        result = dict(row)
        result["details"] = json.loads(result["details"])
        return result

    def get(self, sha256):
        return self.decode(self.connection.execute("SELECT * FROM upload_jobs WHERE platform=? AND account_id=? AND sha256=?", (*self.scope, sha256)).fetchone())

    def update(self, sha256, **changes):
        record = self.get(sha256)
        if record is None:
            if "path" not in changes:
                raise ValueError("新记录缺少视频路径")
            record = {"platform": self.platform, "account_id": self.account_id,
                      "sha256": sha256, "path": changes["path"], "sidecar": None, "status": "pending",
                      "fingerprint": None, "series_key": None, "series_title": None, "episode": None,
                      "content_id": None, "episode_id": None, "message": "", "details": {}, "updated_at": now(),
                      "ledger_dirty": 0}
        if set(changes) & {'platform', 'account_id', 'sha256'}:
            raise ValueError('任务身份不可修改')
        if "details" in changes:
            changes["details"] = {**record["details"], **changes["details"]}
        unknown = set(changes) - set(record)
        if unknown:
            raise ValueError(f"未知数据库字段: {unknown}")
        record.update(changes, updated_at=now())
        if record["status"] not in STATUSES:
            raise ValueError("未知上传状态")
        values = {**record, "details": json.dumps(record["details"], ensure_ascii=False)}
        columns = list(values)
        sql = "INSERT INTO upload_jobs (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ") ON CONFLICT(platform,account_id,sha256) DO UPDATE SET " + ",".join(f"{name}=excluded.{name}" for name in columns if name not in {'platform', 'account_id', 'sha256'})
        with self.connection:
            self.connection.execute('INSERT INTO assets VALUES(?,?,?) ON CONFLICT(sha256) DO UPDATE SET path=excluded.path,updated_at=excluded.updated_at', (sha256, record['path'], now()))
            self.connection.execute(sql, [values[name] for name in columns])
            self.connection.execute('INSERT INTO upload_events(platform,account_id,sha256,payload,created_at) VALUES(?,?,?,?,?)', (*self.scope, sha256, json.dumps(changes, ensure_ascii=False), now()))
        return record

    def events(self, sha256):
        return [dict(r) for r in self.connection.execute('SELECT * FROM upload_events WHERE platform=? AND account_id=? AND sha256=? ORDER BY id', (*self.scope, sha256))]

    def append_event(self, sha256, payload):
        with self.connection:
            self.connection.execute('INSERT INTO upload_events(platform,account_id,sha256,payload,created_at) VALUES(?,?,?,?,?)', (*self.scope, sha256, json.dumps(payload, ensure_ascii=False), now()))

    def records(self, *, dirty_only=False):
        sql = "SELECT * FROM upload_jobs WHERE platform=? AND account_id=?" + (" AND ledger_dirty=1" if dirty_only else "") + " ORDER BY path"
        return [self.decode(row) for row in self.connection.execute(sql, self.scope)]

    def series_id(self, key):
        row = self.connection.execute("SELECT content_id FROM upload_series WHERE platform=? AND account_id=? AND series_key=?", (*self.scope, key)).fetchone()
        return row[0] if row else None

    def remember_series(self, key, content_id):
        if not key:
            raise ValueError('缺少已核对的剧集身份')
        with self.connection:
            self.connection.execute("INSERT INTO upload_series VALUES(?,?,?,?,?) ON CONFLICT(platform,account_id,series_key) DO UPDATE SET updated_at=excluded.updated_at WHERE upload_series.content_id=excluded.content_id",
                                    (*self.scope, key, str(content_id), now()))
            if self.series_id(key) != str(content_id):
                raise ValueError("同一剧对应了不同平台作品 ID，请核对，禁止自动切换")
