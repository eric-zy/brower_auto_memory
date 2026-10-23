import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kuaishou_upload.config import PROJECT, Settings
from kuaishou_upload.database import UploadDatabase
from kuaishou_upload.scan import scan_directory
from kuaishou_upload.schema import load_job, normalize_manifest
from kuaishou_upload.state import atomic_json
from support import make_materials


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.directory = self.base / "videos"
        self.source = make_materials(self.directory)
        self.original = json.loads(self.source.read_text(encoding="utf-8"))
        self.config = self.base / "upload_config.json"
        self.defaults = json.loads((PROJECT / "upload_config.example.json").read_text(encoding="utf-8"))
        self.defaults.update(seriesTitle="固定测试剧", copyright=self.original["copyright"], filing=self.original["filing"])
        atomic_json(self.config, self.defaults)
        self.settings = Settings(self.directory, self.base / "state", self.directory / "发布记录.xlsx",
                                 stable_seconds=0, config=self.config)
        self.db = UploadDatabase(self.settings.root / "uploads.sqlite3")
        self.probe = patch("kuaishou_upload.prepare.probe_duration", return_value=45.5).start()
        def cover(video, destination, duration):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((self.directory / "cover.png").read_bytes())
            return destination
        self.cover = patch("kuaishou_upload.prepare.extract_cover", side_effect=cover).start()

    def tearDown(self):
        patch.stopall()
        self.db.close()
        self.temp.cleanup()

    def scan(self):
        return scan_directory(self.settings, self.db)

    def test_video_without_json_gets_full_generated_job(self):
        self.source.unlink()
        result = self.scan()
        self.assertEqual(result.summary()["counts"], {"pending": 1})
        self.assertEqual(result.summary()["generated"], 1)
        job = next(iter(result.jobs.values()))
        self.assertEqual(job.manifest.seriesTitle, "固定测试剧")
        self.assertEqual(job.manifest.episodes[0].number, 1)
        self.assertEqual(job.manifest.mediaInfo.durationSeconds, 45.5)
        self.assertEqual(job.manifest.copyright.proofFile, self.original["copyright"]["proofFile"])
        self.assertTrue(job.source.is_relative_to(self.settings.root / "manifests"))
        self.assertFalse(self.source.exists())
        self.assertTrue(job.files["cover"].exists())

    def test_fixed_config_repairs_old_empty_fields_without_changing_source(self):
        data = copy.deepcopy(self.original)
        data["episodes"][0].pop("number")
        data["episodes"][0].update(title="第3集：测试", price=None, coverPath="")
        data["copyright"] = {"proofType": "", "proofFile": "", "authorizedForUpload": False}
        data["filing"] = {"hasNumber": None, "number": "", "numberType": ""}
        data.pop("coverFile")
        data.pop("sellingMode")
        data.update(coverPath="", content={"category": ""}, sale={"mode": "", "seriesPrice": None},
                    metadataSources=[{"source": "legacy"}], preparation={"status": "needs_metadata"})
        data["mediaInfo"].update(width=1080, height=1920, frameRate=60, sizeBytes=12)
        atomic_json(self.source, data)
        before = self.source.read_bytes()
        result = self.scan()
        job = next(iter(result.jobs.values()))
        self.assertEqual(job.manifest.episodes[0].number, 3)
        self.assertEqual(job.manifest.episodes[0].price, 0)
        self.assertTrue(job.manifest.copyright.authorizedForUpload)
        self.assertEqual(self.source.read_bytes(), before)

    def test_missing_public_details_still_generates_json_and_human_issues(self):
        self.source.unlink()
        self.config.unlink()  # 自动创建公共配置模板。
        result = self.scan()
        self.assertEqual(result.summary()["generated"], 1)
        item = result.items[0]
        self.assertEqual(item["status"], "blocked")
        self.assertIn("版权证明文件", item["issues"])
        self.assertIn("备案号", item["issues"])
        self.assertNotIn("缺少 demo.kuaishou.json", item["reason"])
        self.assertTrue(Path(item["manifest"]).exists())
        self.assertTrue(self.config.exists())

    def test_media_is_cached_by_hash_and_not_redecoded_on_restart(self):
        self.source.unlink()
        self.scan()
        self.scan()
        self.assertEqual(self.probe.call_count, 1)
        self.assertEqual(self.cover.call_count, 1)

    def test_completed_hash_skips_metadata_and_cover_generation(self):
        sha = next(iter(self.scan().jobs))
        self.db.update(sha, status="submitted")
        self.probe.reset_mock()
        self.cover.reset_mock()
        self.source.unlink()
        result = self.scan()
        self.assertEqual(result.summary()["counts"], {"skipped": 1})
        self.probe.assert_not_called()
        self.cover.assert_not_called()

    def test_paid_mode_uses_fixed_price_and_does_not_assume_free(self):
        self.defaults.update(sellingMode="paid", episodePrice=3.5)
        atomic_json(self.config, self.defaults)
        job = next(iter(self.scan().jobs.values()))
        self.assertTrue(job.manifest.episodes[0].paid)
        self.assertEqual(job.manifest.episodes[0].price, 3.5)

    def test_source_and_title_episode_conflict_stops_before_media_work(self):
        data = copy.deepcopy(self.original)
        data["episodes"][0]["sourceFile"] = "D:/old/第2集.mp4"
        atomic_json(self.source, data)
        result = self.scan()
        self.assertFalse(result.jobs)
        self.assertIn("集号不一致", result.items[0]["reason"])
        self.probe.assert_not_called()

    def test_shared_config_change_after_platform_action_blocks_resume(self):
        sha = next(iter(self.scan().jobs))
        self.db.update(sha, status="ready", content_id="C1")
        self.defaults["filing"]["number"] = "V11111111111111"
        atomic_json(self.config, self.defaults)
        result = self.scan()
        self.assertFalse(result.jobs)
        self.assertIn("资料发生变化", result.items[0]["reason"])

    def test_duplicate_episode_numbers_with_different_content_are_both_blocked(self):
        make_materials(self.directory, name="other", episode=1)
        result = self.scan()
        self.assertFalse(result.jobs)
        self.assertTrue(all("多个不同视频" in item["reason"] for item in result.items))

    def test_known_cover_and_sale_aliases_keep_valid_explicit_values(self):
        data = copy.deepcopy(self.original)
        data["coverPath"] = data.pop("coverFile")
        data["sale"] = {"mode": data.pop("sellingMode"), "seriesPrice": None}
        data["episodes"][0]["price"] = None
        atomic_json(self.source, data)
        job = load_job(self.source)
        self.assertEqual(job.manifest.sellingMode, "free")
        self.assertEqual(job.manifest.episodes[0].price, 0)

    def test_unknown_nonempty_business_fields_are_not_silently_dropped(self):
        data = copy.deepcopy(self.original)
        data["content"] = {"category": "爱情"}
        atomic_json(self.source, data)
        with self.assertRaisesRegex(ValueError, "content"):
            load_job(self.source)
        self.assertIn("content", normalize_manifest(data))

    def test_existing_wrong_hash_cannot_be_replaced_by_auto_generated_hash(self):
        data = copy.deepcopy(self.original)
        data["mediaInfo"]["sha256"] = "0" * 64
        atomic_json(self.source, data)
        result = self.scan()
        self.assertFalse(result.jobs)
        self.assertIn("SHA256 不匹配", result.items[0]["reason"])
        self.probe.assert_not_called()

    def test_implicit_episode_order_includes_already_uploaded_files(self):
        self.source.unlink()
        (self.directory / "demo.mp4").rename(self.directory / "aaa.mp4")
        (self.directory / "bbb.mp4").write_bytes(b"LOCAL TEST ONLY SECOND VIDEO")
        first = self.scan()
        first_sha = next(sha for sha, job in first.jobs.items() if job.manifest.episodes[0].number == 1)
        self.db.update(first_sha, status="submitted")
        result = self.scan()
        self.assertEqual(len(result.jobs), 1)
        self.assertEqual(next(iter(result.jobs.values())).manifest.episodes[0].number, 2)

    def test_auto_prepare_in_dry_run_keeps_original_ledger_and_database_unchanged(self):
        import main
        self.source.unlink()
        before = self.db.records()
        with patch.object(main.Settings, "from_env", return_value=self.settings), patch.object(main, "process_queue", side_effect=AssertionError("不能上传")):
            self.assertEqual(main.main(["--dry-run"]), 0)
        self.assertEqual(self.db.records(), before)
        self.assertFalse(self.settings.ledger.exists())
        self.assertFalse(self.source.exists())
        self.assertEqual(len(list((self.settings.root / "manifests").glob("*.json"))), 1)

    def test_mock_defaults_mark_every_generated_job_and_allow_browser_preparation(self):
        self.defaults["testMode"] = True
        atomic_json(self.config, self.defaults)
        result = self.scan()
        self.assertEqual(len(result.jobs), 1)
        self.assertEqual(result.summary()["test_jobs"], 1)
        generated = json.loads(Path(result.items[0]["manifest"]).read_text(encoding="utf-8"))
        self.assertIs(generated["testMode"], True)
        # 生成资料供准备流程使用，默认的正式校验仍区分测试数据。
        self.assertTrue(load_job(Path(result.items[0]["manifest"]), live=False).manifest.testMode)
        with self.assertRaisesRegex(ValueError, "testMode=true"):
            load_job(Path(result.items[0]["manifest"]))

    def test_public_defaults_cannot_clear_source_test_flag(self):
        data = copy.deepcopy(self.original)
        data["testMode"] = True
        atomic_json(self.source, data)
        self.defaults["testMode"] = False
        atomic_json(self.config, self.defaults)
        result = self.scan()
        self.assertEqual(len(result.jobs), 1)
        self.assertTrue(next(iter(result.jobs.values())).manifest.testMode)


if __name__ == "__main__":
    unittest.main()
