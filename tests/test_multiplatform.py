import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from kuaishou_upload.config import Settings
from kuaishou_upload.database import UploadDatabase
from kuaishou_upload.ledger import Ledger, sync_ledger
from kuaishou_upload.platforms import AdapterSpec, get_adapter, register_adapter
from kuaishou_upload.scan import scan_directory
from kuaishou_upload.tools import UploadTask
from support import make_materials


class FixtureTask:
    def __init__(self, *args, **kwargs):
        self.selected = 'fixture-platform'


class MultiPlatformTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / 'jobs.sqlite3'
        self.opened = []

    def db(self, platform='kuaishou', account_id='default', **kwargs):
        db = UploadDatabase(self.path, platform=platform, account_id=account_id, **kwargs)
        self.opened.append(db)
        return db

    def tearDown(self):
        for db in self.opened:
            db.close()
        self.tmp.cleanup()

    def test_same_hash_status_ids_and_events_are_isolated(self):
        original, account, platform = self.db(), self.db(account_id='other'), self.db(platform='fixture')
        sha = 'a' * 64
        original.update(sha, path='same.mp4', status='submitted', details={'submissionAttempted': True})
        for db in (account, platform):
            self.assertIsNone(db.get(sha))
            db.update(sha, path='same.mp4', status='pending')
            db.remember_series('series', db.account_id + db.platform)
        self.assertEqual(original.get(sha)['status'], 'submitted')
        self.assertNotEqual(account.series_id('series'), platform.series_id('series'))
        self.assertIsNone(original.series_id('series'))
        self.assertEqual(len({db.task_id(sha) for db in (original, account, platform)}), 3)
        self.assertEqual(len(original.events(sha)), 1)
        self.assertEqual(original.connection.execute('SELECT COUNT(*) FROM assets').fetchone()[0], 1)

    def legacy_database(self):
        with closing(sqlite3.connect(self.path)) as c:
            c.executescript('''CREATE TABLE videos (
                sha256 TEXT PRIMARY KEY,path TEXT NOT NULL,sidecar TEXT,status TEXT NOT NULL,
                fingerprint TEXT,series_key TEXT,series_title TEXT,episode INTEGER,
                content_id TEXT,episode_id TEXT,message TEXT NOT NULL DEFAULT '',
                details TEXT NOT NULL DEFAULT '{}',updated_at TEXT NOT NULL,ledger_dirty INTEGER DEFAULT 0);
                CREATE TABLE series(series_key TEXT PRIMARY KEY,content_id TEXT NOT NULL,updated_at TEXT NOT NULL);''')
            c.execute("INSERT INTO videos(sha256,path,status,content_id,details,updated_at) VALUES('abc','video.mp4','unknown','C1','{\"submissionAttempted\":true}','yesterday')")
            c.execute("INSERT INTO series VALUES('S','C1','yesterday')")
            c.commit()

    def test_legacy_migration_is_one_time_and_preserves_uncertainty(self):
        self.legacy_database()
        other = self.db(account_id='other')  # even if a new scope opens first
        self.assertIsNone(other.get('abc'))
        original = self.db()
        self.assertEqual(original.get('abc')['status'], 'unknown')
        self.assertTrue(original.get('abc')['details']['submissionAttempted'])
        self.assertEqual(original.series_id('S'), 'C1')
        self.assertIsNone(other.series_id('S'))
        self.assertTrue(self.path.with_name(self.path.name + '.before-multiplatform.sqlite3').exists())
        original.update('abc', status='submitted')
        reopened = self.db()
        self.assertEqual(reopened.get('abc')['status'], 'submitted')
        self.assertEqual(reopened.connection.execute('SELECT status FROM videos').fetchone()[0], 'unknown')

    def test_preview_migrates_only_in_memory(self):
        self.legacy_database()
        before = self.path.read_bytes()
        preview = self.db(preview=True)
        self.assertEqual(preview.get('abc')['status'], 'unknown')
        self.assertEqual(self.path.read_bytes(), before)
        with closing(sqlite3.connect(self.path)) as c:
            self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name='upload_jobs'").fetchone())
        self.assertFalse(self.path.with_name(self.path.name + '.before-multiplatform.sqlite3').exists())

    def test_series_mapping_cannot_be_reassigned(self):
        db = self.db()
        db.remember_series('S', 'C1')
        with self.assertRaises(ValueError):
            db.remember_series('S', 'C2')
        self.assertEqual(db.series_id('S'), 'C1')

    def test_ledger_keeps_separate_rows_and_never_imports_other_account(self):
        path = self.root / 'ledger.xlsx'
        video = self.root / 'same.mp4'
        sha = 'a' * 64
        for db in (self.db(), self.db(account_id='other'), self.db(platform='fixture')):
            db.update(sha, path=str(video), status='submitted', ledger_dirty=1)
            self.assertEqual(sync_ledger(db, Ledger(path)), [])
        rows = Ledger(path).read()
        self.assertEqual(len(rows), 3)
        for scope in [('kuaishou', 'default'), ('kuaishou', 'other'), ('fixture', 'default')]:
            row = Ledger.match(rows, video, sha, unique_name=True, platform=scope[0], account_id=scope[1])
            self.assertEqual((row['平台'], row['账号']), scope)
        legacy = [{'文件名': 'same.mp4', '发布状态': 'published'}]
        self.assertIsNone(Ledger.match(legacy, video, sha, unique_name=True, account_id='other'))

    def test_new_account_does_not_inherit_sidecar_platform_id(self):
        folder = self.root / 'videos'
        sidecar = make_materials(folder)
        data = json.loads(sidecar.read_text(encoding='utf-8'))
        data['platformHistory']['contentId'] = 'OLD-ACCOUNT-CONTENT'
        sidecar.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        settings = Settings(folder, self.root / 'state', folder / 'ledger.xlsx', stable_seconds=0, account_id='other')
        db = self.db(account_id='other')
        report = scan_directory(settings, db)
        self.assertEqual(len(report.jobs), 1)
        self.assertIsNone(db.records()[0]['content_id'])
        self.assertNotEqual(settings.runtime_root, settings.root)

    def test_identity_is_immutable_and_mismatched_settings_stop_scan(self):
        db = self.db()
        db.update('abc', path='video.mp4')
        with self.assertRaises(ValueError):
            db.update('abc', account_id='other')
        settings = Settings(self.root, self.root / 'state', self.root / 'ledger.xlsx', account_id='other')
        with self.assertRaisesRegex(ValueError, '不一致'):
            scan_directory(settings, db)

    def test_second_adapter_dispatches_without_modifying_tools(self):
        from kuaishou_upload.platforms import _ADAPTERS
        spec = AdapterSpec('fixture', 'unused:Browser', f'{__name__}:FixtureTask',
                           'unused.md', '.fixture.json', 'unused:loader', 'unused:builder', 'unused:identity')
        with patch.dict(_ADAPTERS, {}, clear=False):
            register_adapter(spec)
            settings = Settings(self.root, self.root / 'state', self.root / 'ledger.xlsx', platform='fixture')
            task = UploadTask(None, 'abc', settings, self.db(platform='fixture'), {})
            self.assertEqual(task.selected, 'fixture-platform')
            with self.assertRaises(ValueError):
                register_adapter(spec)
        with self.assertRaisesRegex(ValueError, '尚未实现'):
            get_adapter('unregistered-platform')
