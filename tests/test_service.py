"""End-to-end tests: service against the bundled example site + HTTP UI."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("RECALLQC_QUIET", "1")
os.environ.setdefault("EXAMPLE_QUIET", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.fetcher import Fetcher
from app.server import Handler
from app.service import QualityService
from app.storage import SnapshotStore
from example_site import server as example


def _start(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class ExampleSiteCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.example = _start(ThreadingHTTPServer(("127.0.0.1", 0), example.Handler))
        cls.example_port = cls.example.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.example_port}"

    @classmethod
    def tearDownClass(cls):
        cls.example.shutdown()
        cls.example.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SnapshotStore(self.tmp.name)
        self.service = QualityService(self.store)
        example._FLIPPED["value"] = False

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, path):
        return self.service.check(self.base + path)


class TestScenarios(ExampleSiteCase):
    def test_正常列表完整发布(self):
        result = self.check("/recalls/list")
        self.assertTrue(result.ok)
        self.assertEqual(len(result.notices), 3)
        self.assertEqual(result.list_pages, [self.base + "/recalls/list",
                                             self.base + "/recalls/list?page=2"])
        self.assertEqual(
            [(n["gtin"], n["batch"]) for n in result.notices],
            [("6901234567892", "000123"), ("6909876543212", "000456"),
             ("6912345678902", "000789")],
        )

    def test_改版卡片列表仍可采集(self):
        result = self.check("/recalls/list-v2")
        self.assertTrue(result.ok)
        self.assertEqual(len(result.notices), 3)

    def test_改版缺编号必须报异常而非无召回(self):
        result = self.check("/recalls/list-broken")
        self.assertFalse(result.ok)
        self.assertEqual(result.notices, [])
        self.assertEqual(result.anomalies[0]["address"], self.base + "/recalls/list-broken")
        self.assertIn("无法识别", result.anomalies[0]["reason"])
        self.assertFalse(self.store.exists(self.base + "/recalls/list-broken"))

    def test_只有明确为空才发布空清单(self):
        result = self.check("/recalls/empty")
        self.assertTrue(result.ok)
        self.assertEqual(result.notices, [])
        snapshot = self.service.view(self.base + "/recalls/empty")
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.notices, [])

    def test_空白列表未标空不得发布(self):
        result = self.check("/recalls/empty-broken")
        self.assertFalse(result.ok)
        self.assertEqual(result.notices, [])
        self.assertIsNone(self.service.view(self.base + "/recalls/empty-broken"))

    def test_跨页同编号冲突(self):
        result = self.check("/recalls/conflict")
        self.assertFalse(result.ok)
        self.assertEqual(result.notices, [])
        reasons = " ".join(row["reason"] for row in result.anomalies)
        self.assertIn("跨页详情内容冲突", reasons)
        self.assertIn("6909876543212", reasons)
        self.assertIsNone(self.service.view(self.base + "/recalls/conflict"))

    def test_详情缺字段(self):
        result = self.check("/recalls/missing")
        self.assertFalse(result.ok)
        reasons = " ".join(row["reason"] for row in result.anomalies)
        self.assertIn("批号", reasons)
        self.assertEqual(result.anomalies[0]["address"],
                         self.base + "/recalls/detail-missing-batch")

    def test_失败保留上次清单并跨重启标记旧数据(self):
        ok = self.check("/recalls/flippable")
        self.assertTrue(ok.ok)
        urllib.request.urlopen(self.base + "/_scenario/flippable/break").read()
        failed = self.check("/recalls/flippable")
        self.assertFalse(failed.ok)
        self.assertTrue(failed.previous_snapshot_stale)

        snapshot = self.service.view(self.base + "/recalls/flippable")
        self.assertEqual(snapshot.status, "stale")
        self.assertEqual(len(snapshot.notices), 3)
        self.assertEqual(snapshot.collected_at, ok.collected_at)

        # Simulate a process restart with a fresh store on the same directory.
        restarted = QualityService(SnapshotStore(self.tmp.name))
        view = restarted.view(self.base + "/recalls/flippable")
        self.assertEqual(view.status, "stale")
        self.assertEqual(len(view.notices), 3)
        self.assertIsNotNone(view.failure_reason)

        urllib.request.urlopen(self.base + "/_scenario/flippable/restore").read()
        recovered = restarted.check(self.base + "/recalls/flippable")
        self.assertTrue(recovered.ok)
        self.assertEqual(restarted.view(self.base + "/recalls/flippable").status, "current")

    def test_采集时间被保存(self):
        result = self.check("/recalls/list")
        snapshot = self.service.view(self.base + "/recalls/list")
        self.assertEqual(snapshot.collected_at, result.collected_at)
        self.assertRegex(snapshot.collected_at, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_站外目标被拒绝(self):
        result = self.service.check("http://127.0.0.1:1/lists")
        self.assertFalse(result.ok)


class TestHttpUi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = SnapshotStore(cls.tmp.name)
        Handler.service = QualityService(cls.store)
        cls.server = _start(ThreadingHTTPServer(("127.0.0.1", 0), Handler))
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def post(self, path, url):
        data = urllib.parse.urlencode({"url": url}).encode()
        request = urllib.request.Request(self.base + path, data=data, method="POST")
        with urllib.request.urlopen(request) as response:
            return response.read().decode("utf-8")

    def test_index_loads(self):
        with urllib.request.urlopen(self.base + "/") as response:
            self.assertEqual(response.status, 200)
            self.assertIn("食品召回公告采集质量核查", response.read().decode())

    def test_unknown_fetch_error_shows_anomaly(self):
        body = self.post("/check", "http://127.0.0.1:1/none")
        self.assertIn("核查失败", body)
        self.assertIn("异常地址", body)
        self.assertIn("未生成任何清单", body)

    def test_json_view_404_without_snapshot(self):
        request = urllib.request.Request(
            self.base + "/json",
            data=urllib.parse.urlencode({"url": "http://none.test/x"}).encode(),
            method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request)
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
