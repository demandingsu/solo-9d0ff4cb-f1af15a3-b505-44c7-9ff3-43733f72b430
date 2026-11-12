"""Unit tests for HTML recognition, validation and safety rules."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import htmltree
from app.collector import (
    DetailRecognizer,
    ListRecognizer,
    pagination_target,
    same_site,
    validate_target,
)
from app.errors import InvalidTargetURL
from app.validation import (
    gtin_check_digit_ok,
    normalize_batch,
    normalize_notice_number,
    normalize_gtin,
    validate_gtin,
)


class TestHtmlTree(unittest.TestCase):
    def test_unclosed_tags_are_tolerated(self):
        root, truncated = htmltree.parse_html(
            "<table><tr><td>a<td>b<tr><td>c"
        )
        self.assertFalse(truncated)
        cells = [n.text() for n in root.find_all({"td"})]
        self.assertEqual(cells, ["a", "b", "c"])

    def test_script_text_is_excluded(self):
        root, _ = htmltree.parse_html("<div><script>var x='GTIN';</script>hi</div>")
        self.assertEqual(root.find_first({"div"}).text(), "hi")

    def test_find_segment_by_data_attribute(self):
        root, _ = htmltree.parse_html(
            "<div><table data-collector='table'></table></div>"
        )
        self.assertEqual(htmltree.find_segment(root, "table").tag, "table")

    def test_find_segment_by_id_and_class(self):
        root, _ = htmltree.parse_html("<ul id='collector-links'></ul>")
        self.assertIsNotNone(htmltree.find_segment(root, "links"))
        root, _ = htmltree.parse_html("<div class='collector-detail'>x</div>")
        self.assertIsNotNone(htmltree.find_segment(root, "detail"))


class TestValidation(unittest.TestCase):
    def test_gtin_checksum(self):
        self.assertTrue(gtin_check_digit_ok("6901234567892"))
        self.assertFalse(gtin_check_digit_ok("6901234567893"))

    def test_validate_gtin_normalises_separators(self):
        self.assertEqual(validate_gtin("690-1234-567892"), ("6901234567892", True))
        self.assertEqual(normalize_gtin("690 1234 567892"), "6901234567892")

    def test_invalid_gtin(self):
        self.assertEqual(validate_gtin("12345"), (None, False))
        self.assertEqual(validate_gtin("abcdefghijkl"), (None, False))

    def test_batch_must_be_six_digits(self):
        self.assertEqual(normalize_batch("000123"), "000123")
        self.assertIsNone(normalize_batch("12345"))
        self.assertIsNone(normalize_batch("1234567"))
        self.assertIsNone(normalize_batch("00012A"))

    def test_notice_number_normalisation(self):
        self.assertEqual(normalize_notice_number("  n-2026-001 "), "N-2026-001")


class TestListRecognition(unittest.TestCase):
    URL = "http://site.test/recalls/list"

    def recognize(self, markup, url=None):
        return ListRecognizer(url or self.URL).recognize(markup)

    def test_table_layout(self):
        markup = """
        <table data-collector="table">
          <tr><th>公告编号</th><th>标题</th><th>链接</th></tr>
          <tr><td>N-1</td><td>标题</td><td><a href="/d/1">详情</a></td></tr>
        </table>"""
        payload = self.recognize(markup)
        self.assertEqual(len(payload.entries), 1)
        self.assertEqual(payload.entries[0]["detail_url"], "http://site.test/d/1")

    def test_link_list_layout(self):
        markup = """
        <ul data-collector="links">
          <li><a href="/d/1">公告编号：N-1</a></li>
          <li><a href="/d/2">公告编号：N-2</a></li>
        </ul>"""
        payload = self.recognize(markup)
        self.assertEqual([e["number"] for e in payload.entries], ["N-1", "N-2"])

    def test_redesign_without_number_is_unrecognised(self):
        markup = """
        <table data-collector="table">
          <tr><th>标题</th><th>发布日期</th><th>操作</th></tr>
          <tr><td>公告</td><td>2026-01-01</td><td><a href="/d/1">查看</a></td></tr>
        </table>"""
        self.assertIsNone(self.recognize(markup))

    def test_explicit_empty_marker(self):
        markup = """
        <table data-collector="table">
          <tr><th>公告编号</th><th>标题</th><th>链接</th></tr>
          <tr><td colspan="3" data-empty="true">暂无召回公告</td></tr>
        </table>"""
        payload = self.recognize(markup)
        self.assertEqual(payload.entries, [])
        self.assertTrue(payload.explicit_empty)

    def test_empty_table_without_marker_is_not_publishable(self):
        markup = """
        <table data-collector="table">
          <tr><th>公告编号</th><th>标题</th><th>链接</th></tr>
        </table>"""
        payload = self.recognize(markup)
        self.assertEqual(payload.entries, [])
        self.assertFalse(payload.explicit_empty)

    def test_offsite_links_are_flagged(self):
        markup = """
        <table data-collector="table">
          <tr><th>公告编号</th><th>标题</th><th>链接</th></tr>
          <tr><td>N-1</td><td>标题</td><td><a href="http://evil.test/x">详情</a></td></tr>
        </table>"""
        payload = self.recognize(markup)
        self.assertEqual(payload.entries[0]["detail_url"], "")
        self.assertIn("同一站点", payload.entries[0]["error"])

    def test_row_without_link_is_flagged(self):
        markup = """
        <table data-collector="table">
          <tr><th>公告编号</th><th>标题</th></tr>
          <tr><td>N-1</td><td>标题</td></tr>
        </table>"""
        payload = self.recognize(markup)
        self.assertIn("详情链接", payload.entries[0]["error"])


class TestDetailRecognition(unittest.TestCase):
    def validate(self, markup, url="http://site.test/d/1"):
        return DetailRecognizer(url).validate(markup)

    def test_complete_detail(self):
        fields, failures = self.validate("""
        <article data-collector="detail">
          <h2>公告编号：<span data-field="number">N-1</span></h2>
          <p>GTIN：<span data-field="gtin">6901234567892</span></p>
          <p>批号：<span data-field="batch">000123</span></p>
        </article>""")
        self.assertEqual(failures, [])
        self.assertEqual(fields["gtin"], "6901234567892")

    def test_missing_batch_is_anomaly(self):
        fields, failures = self.validate("""
        <article data-collector="detail">
          <h2>公告编号：N-1</h2>
          <p>GTIN：6901234567892</p>
        </article>""")
        self.assertEqual(fields, {})
        self.assertEqual(len(failures), 1)
        self.assertIn("批号", failures[0].reason)

    def test_bad_gtin_checksum_is_anomaly(self):
        fields, failures = self.validate("""
        <article data-collector="detail">
          <h2>公告编号：N-1</h2>
          <p>GTIN：6901234567893</p>
          <p>批号：000123</p>
        </article>""")
        self.assertEqual(fields, {})
        self.assertIn("校验位", failures[0].reason)

    def test_word_batch_in_title_is_not_a_field(self):
        fields, failures = self.validate("""
        <article data-collector="detail">
          <h2>公告编号：N-1</h2>
          <p>某批次花生酱召回公告</p>
          <p>GTIN：6901234567892</p>
        </article>""")
        self.assertEqual(fields, {})
        self.assertEqual(len(failures), 1)
        self.assertIn("缺少六位批号", failures[0].reason)

    def test_batch_length_is_enforced(self):
        fields, failures = self.validate("""
        <article data-collector="detail">
          <h2>公告编号：N-1</h2>
          <p>GTIN：6901234567892</p>
          <p>批号：12345</p>
        </article>""")
        self.assertEqual(fields, {})
        self.assertIn("六位", failures[0].reason)


class TestPagination(unittest.TestCase):
    def test_next_link_is_followed(self):
        root, _ = htmltree.parse_html(
            "<a rel='next' href='/recalls/list?page=2'>下一页</a>"
        )
        self.assertEqual(
            pagination_target(root, "http://site.test/recalls/list"),
            "http://site.test/recalls/list?page=2",
        )

    def test_offsite_next_link_ignored(self):
        root, _ = htmltree.parse_html(
            "<a rel='next' href='http://evil.test/page2'>下一页</a>"
        )
        self.assertIsNone(pagination_target(root, "http://site.test/recalls/list"))

    def test_numbered_pages(self):
        root, _ = htmltree.parse_html(
            "<a href='?page=2'>2</a><a href='?page=3'>3</a>"
        )
        self.assertEqual(
            pagination_target(root, "http://site.test/recalls/list"),
            "http://site.test/recalls/list?page=2",
        )

    def test_no_guessed_page_url(self):
        root, _ = htmltree.parse_html("<p>没有分页</p>")
        self.assertIsNone(pagination_target(root, "http://site.test/recalls/list?page=1"))

    def test_same_site(self):
        self.assertTrue(same_site("http://site.test/a", "https://site.test/b"))
        self.assertFalse(same_site("http://other.test/a", "http://site.test/b"))
        self.assertFalse(same_site("javascript:void(0)", "http://site.test/b"))

    def test_validate_target(self):
        self.assertEqual(validate_target(" http://x.test/l "), "http://x.test/l")
        for bad in ("", "ftp://x.test", "javascript:alert(1)", "http:///nohost"):
            with self.assertRaises(InvalidTargetURL):
                validate_target(bad)


if __name__ == "__main__":
    unittest.main()
