"""Safety limits, storage durability and refusal rules."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.collector import CollectionSession, same_site
from app.errors import CollectionAnomaly
from app.fetcher import Fetcher, FetchError
from app.service import QualityService
from app.storage import SnapshotStore, url_key


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.requested = []

    def fetch(self, url):
        self.requested.append(url)
        if url not in self.pages:
            raise RuntimeError("missing")
        return self.pages[url]


LIST_HEAD = '<table data-collector="table"><tr><th>公告编号</th><th>标题</th><th>链接</th></tr>'
LIST_TAIL = "</table>"
DETAIL = """<article data-collector="detail">
<h2>公告编号：N-1</h2><p>GTIN：6901234567892</p><p>批号：000123</p></article>"""


class TestCollectionSession(unittest.TestCase):
    BASE = "http://site.test/list"

    def make(self, pages, **kwargs):
        return CollectionSession(fetcher=FakeFetcher(pages), list_url=self.BASE, **kwargs)

    def test_pagination_limit_blocks_publication(self):
        pages = {}
        for page in range(1, 6):
            nxt = f'<a rel="next" href="/list?page={page + 1}">下一页</a>'
            rows = "".join(
                f"<tr><td>N-{page}-{i}</td><td>x</td><td><a href='/d/{page}-{i}'>d</a></td></tr>"
                for i in range(1, 3)
            )
            pages[f"{self.BASE}?page={page}"] = LIST_HEAD + rows + LIST_TAIL + nxt
            for i in range(1, 3):
                pages[f"http://site.test/d/{page}-{i}"] = DETAIL.replace("N-1", f"N-{page}-{i}")
        pages[self.BASE] = pages[f"{self.BASE}?page=1"]
        with self.assertRaises(CollectionAnomaly) as ctx:
            self.make(pages, max_pages=3).collect()
        self.assertIn("分页数超过上限", ctx.exception.reason)

    def test_notice_count_limit(self):
        rows = "".join(
            f"<tr><td>N-{i}</td><td>x</td><td><a href='/d/{i}'>d</a></td></tr>"
            for i in range(6)
        )
        pages = {self.BASE: LIST_HEAD + rows + LIST_TAIL}
        for i in range(6):
            pages[f"http://site.test/d/{i}"] = DETAIL.replace("N-1", f"N-{i}")
        with self.assertRaises(CollectionAnomaly) as ctx:
            self.make(pages, max_notices=4).collect()
        self.assertIn("安全上限", ctx.exception.reason)

    def test_self_referencing_next_is_not_followed(self):
        pages = {self.BASE: LIST_HEAD + LIST_TAIL + '<a rel="next" href="/list">下一页</a>'}
        with self.assertRaises(CollectionAnomaly) as ctx:
            self.make(pages).collect()
        self.assertIn("未明确标示为空", ctx.exception.reason)

    def test_pagination_cycle_detected(self):
        rows = ("<tr><td>N-1</td><td>x</td><td><a href='/d/1'>d</a></td></tr>")
        page1 = (self.BASE, LIST_HEAD + rows + LIST_TAIL +
                 '<a rel="next" href="/list?page=2">下一页</a>')
        page2 = (self.BASE + "?page=2", LIST_HEAD + rows + LIST_TAIL +
                 '<a rel="next" href="/list">下一页</a>')
        pages = dict([page1, page2, ("http://site.test/d/1", DETAIL)])
        with self.assertRaises(CollectionAnomaly) as ctx:
            self.make(pages).collect()
        self.assertIn("循环", ctx.exception.reason)

    def test_offsite_next_link_ends_pagination(self):
        pages = {
            self.BASE: LIST_HEAD + "<tr><td>N-1</td><td>x</td><td><a href='/d/1'>d</a></td></tr>" +
                       LIST_TAIL + '<a rel="next" href="http://else.test/p2">下一页</a>',
            "http://site.test/d/1": DETAIL,
        }
        notices, list_pages = self.make(pages).collect()
        self.assertEqual([p for p in list_pages], [self.BASE])
        self.assertEqual(len(notices), 1)

    def test_all_detail_anomalies_reported_no_partial_list(self):
        pages = {
            self.BASE: LIST_HEAD +
            "<tr><td>N-1</td><td>x</td><td><a href='/d/1'>d</a></td></tr>"
            "<tr><td>N-2</td><td>x</td><td><a href='/d/2'>d</a></td></tr>" + LIST_TAIL,
            "http://site.test/d/1": DETAIL,
            "http://site.test/d/2": "<article data-collector='detail'><h2>公告编号：N-2</h2>"
                                    "<p>GTIN：6901234567892</p></article>",
        }
        with self.assertRaises(CollectionAnomaly) as ctx:
            self.make(pages).collect()
        self.assertIn("批号", ctx.exception.reason)
        self.assertTrue(ctx.exception.details)


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SnapshotStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_atomic_save_and_load(self):
        snapshot = self.store.save("http://x.test/list", [{"number": "N-1"}])
        loaded = self.store.load("http://x.test/list")
        self.assertEqual(loaded.notices, [{"number": "N-1"}])
        self.assertEqual(loaded.collected_at, snapshot.collected_at)
        leftovers = [p for p in os.listdir(self.tmp.name) if p.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_failed_run_keeps_previous_list(self):
        self.store.save("http://x.test/list", [{"number": "N-1"}])
        self.store.mark_failed("http://x.test/list", "boom", ["http://x.test/list"])
        loaded = self.store.load("http://x.test/list")
        self.assertEqual(loaded.status, "stale")
        self.assertEqual(loaded.notices, [{"number": "N-1"}])
        self.assertEqual(loaded.failure_reason, "boom")

    def test_success_clears_stale_marker(self):
        self.store.save("http://x.test/list", [{"number": "N-1"}])
        self.store.mark_failed("http://x.test/list", "boom", [])
        self.store.save("http://x.test/list", [{"number": "N-2"}])
        loaded = self.store.load("http://x.test/list")
        self.assertEqual(loaded.status, "current")
        self.assertIsNone(loaded.failure_reason)

    def test_snapshot_without_success_is_not_created_by_failure(self):
        self.assertIsNone(self.store.mark_failed("http://x.test/new", "boom", []))
        self.assertFalse(self.store.exists("http://x.test/new"))

    def test_corrupt_snapshot_ignored(self):
        path = Path(self.tmp.name) / f"{url_key('http://x.test/list')}.json"
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.store.load("http://x.test/list"))

    def test_list_all_orders_newest_first(self):
        self.store.save("http://x.test/a", [], "2026-01-01T00:00:00Z")
        self.store.save("http://x.test/b", [], "2026-02-01T00:00:00Z")
        items = self.store.list_all()
        self.assertEqual([i.list_url for i in items], ["http://x.test/b", "http://x.test/a"])


class TestFetcher(unittest.TestCase):
    def test_offsite_refused(self):
        fetcher = Fetcher("http://site.test/list")
        with self.assertRaises(FetchError):
            fetcher.fetch("http://evil.test/x")

    def test_cross_scheme_same_host_allowed(self):
        self.assertTrue(same_site("https://site.test/x", "http://site.test/y"))


class TestServiceGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = QualityService(SnapshotStore(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_unknown_fetch_failure_never_publishes(self):
        result = self.service.check("http://127.0.0.1:9/list")
        self.assertFalse(result.ok)
        self.assertEqual(result.notices, [])
        self.assertIsNone(self.service.view("http://127.0.0.1:9/list"))

    def test_invalid_url_rejected_without_anomalies(self):
        result = self.service.check("javascript:alert(1)")
        self.assertFalse(result.ok)
        self.assertIn("http", result.message)


if __name__ == "__main__":
    unittest.main()
