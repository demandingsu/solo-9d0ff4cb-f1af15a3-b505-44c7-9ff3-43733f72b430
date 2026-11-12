"""A self-contained example recall site for offline verification.

Scenarios (all reachable from the index page):

* ``/recalls/list``      — normal two-page table list (3 notices)
* ``/recalls/list-v2``   — same data, redesigned list (cards) + detail layout
* ``/recalls/list-broken`` — redesign that drops the notice-number column
                            (collector must refuse, not report "no recall")
* ``/recalls/empty``     — explicitly empty list (publishable empty list)
* ``/recalls/empty-broken`` — blank list without an explicit empty marker
* ``/recalls/conflict``  — one notice number appears on two pages with
                            contradicting GTIN/batch in detail pages
* ``/recalls/missing``   — a detail page lacks the batch field
"""

from __future__ import annotations

import argparse
import html
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

NOTICES = [
    {
        "number": "N-2026-001",
        "title": "某品牌麦芽糖浆召回公告",
        "gtin": "6901234567892",
        "batch": "000123",
    },
    {
        "number": "N-2026-002",
        "title": "某批次花生酱召回公告",
        "gtin": "6909876543212",
        "batch": "000456",
    },
    {
        "number": "N-2026-003",
        "title": "某品牌速溶咖啡召回公告",
        "gtin": "6912345678902",
        "batch": "000789",
    },
]

_CONFLICT_SECOND = {**NOTICES[0], "gtin": "6909876543212", "batch": "000999"}

# Toggled at runtime so the "failure keeps the previous list" flow can be
# exercised in a browser.  The QC store (not this flag) survives restarts.
_FLIPPED = {"value": False}

PAGE_SHELL = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{title}</title>
<style>
 body {{ font-family: "PingFang SC", "Microsoft YaHei", sans-serif; max-width: 900px;
        margin: 24px auto; padding: 0 16px; line-height: 1.6; }}
 table {{ border-collapse: collapse; width: 100%; }}
 th, td {{ border: 1px solid #bbb; padding: 8px; text-align: left; }}
 .card {{ border: 1px solid #bbb; border-radius: 8px; padding: 12px; margin: 8px 0; }}
 .note {{ color: #555; font-size: .9em; }}
 nav a {{ margin-right: 12px; }}
</style></head><body>
<nav><a href="/">示例站首页</a><a href="/recalls/list">正常列表</a>
<a href="/recalls/list-v2">改版列表</a><a href="/recalls/list-broken">改版缺字段</a></nav>
<h1>{title}</h1>
{body}
</body></html>
"""

INDEX_BODY = """
<ul>
  <li><a href="/recalls/list">正常公告列表（两页，3 条）</a></li>
  <li><a href="/recalls/list-v2">页面改版后的公告列表（卡片式，2 页）</a></li>
  <li><a href="/recalls/list-broken">改版故障：列表缺少公告编号列</a></li>
  <li><a href="/recalls/empty">明确标示为空的公告列表</a></li>
  <li><a href="/recalls/empty-broken">空白列表（未标示为空）</a></li>
  <li><a href="/recalls/conflict">跨页冲突：同一公告编号内容不同</a></li>
  <li><a href="/recalls/missing">详情缺字段：缺少六位批号</a></li>
  <li><a href="/recalls/flippable">可切换列表（用于验证失败后保留旧清单）</a>
      — <a href="/_scenario/flippable/break">切换为改版故障</a> /
      <a href="/_scenario/flippable/restore">恢复正常</a></li>
</ul>
"""


def _notice(n, with_batch=True, with_gtin=True):
    def row(label, key):
        if not (with_batch if key == "batch" else with_gtin):
            return ""
        return f"<p>{label}：<span data-field='{key}'>{html.escape(n[key])}</span></p>"
    return f"""
<article data-collector="detail">
  <h2>公告编号：<span data-field="number">{html.escape(n['number'])}</span></h2>
  <p>{html.escape(n['title'])}</p>
  {row('GTIN', 'gtin')}
  {row('批号', 'batch')}
</article>"""


def table_list(page: int) -> str:
    if page == 1:
        rows = NOTICES[:2]
    else:
        rows = NOTICES[2:]
    body = "".join(
        "<tr><td>{n}</td><td>{t}</td><td><a href='/recalls/detail/{n}'>查看详情</a></td></tr>".format(
            n=html.escape(n["number"]), t=html.escape(n["title"]))
        for n in rows
    )
    next_link = ("<a href='/recalls/list?page=2' rel='next'>下一页</a>"
                 if page == 1 else "")
    return f"""
<table data-collector="table">
  <thead><tr><th>公告编号</th><th>标题</th><th>链接</th></tr></thead>
  <tbody>{body}</tbody>
</table>
<p>{next_link}</p>"""


def card_list(page: int) -> str:
    rows = NOTICES[:2] if page == 1 else NOTICES[2:]
    cards = "".join(
        "<li class='card'><span class='label'>公告编号：</span>"
        "<span class='notice-no'>{n}</span><br>"
        "<span>{t}</span><br>"
        "<a href='/recalls/detail-v2/{n}'>详情</a></li>".format(
            n=html.escape(n["number"]), t=html.escape(n["title"]))
        for n in rows
    )
    next_link = ("<a class='next' href='/recalls/list-v2?page=2'>下一页</a>"
                 if page == 1 else "")
    return (f"<ul data-collector='links'>{cards}</ul><p>{next_link}</p>")


def flippable_list(page: int) -> str:
    if _FLIPPED["value"]:
        return broken_list()
    return table_list(page)


def broken_list() -> str:
    """A redesign: the notice number column is gone; only title + date."""
    rows = "".join(
        "<tr><td>{t}</td><td>2026-10-{d:02d}</td><td><a href='/recalls/detail-v2-broken/{n}'>查看</a></td></tr>".format(
            t=html.escape(n["title"]), d=index + 1, n=html.escape(n["number"]))
        for index, n in enumerate(NOTICES)
    )
    return f"""
<table data-collector="table">
  <thead><tr><th>标题</th><th>发布日期</th><th>操作</th></tr></thead>
  <tbody>{rows}</tbody>
</table>
"""


def detail_v2(n) -> str:
    return f"""
<article class="recall-detail" data-collector="detail">
  <h2>{html.escape(n['title'])}</h2>
  <dl>
    <dt>公告编号</dt><dd><span data-field="notice-number">{html.escape(n['number'])}</span></dd>
    <dt>GTIN</dt><dd><span data-field="gtin">{n['gtin']}</span></dd>
    <dt>批号</dt><dd><span data-field="batch">{n['batch']}</span></dd>
  </dl>
</article>"""


def detail_plain_redesign(n) -> str:
    """Redesign using free-form labels and no explicit markers at all."""
    return f"""
<h2>{html.escape(n['title'])}</h2>
<div>
  公告编号：{html.escape(n['number'])}<br>
  GTIN：{n['gtin']}<br>
  批号：{n['batch']}
</div>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "RecallExample/1.0"

    def _send(self, body: str, status: int = 200) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _page(self, title: str, body: str, status: int = 200) -> None:
        self._send(PAGE_SHELL.format(title=html.escape(title), body=body), status)

    def do_GET(self):
        parts = urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        query = parse_qs(parts.query)
        page = 1
        try:
            page = max(1, int((query.get("page") or ["1"])[0]))
        except ValueError:
            page = 1

        if path == "/":
            self._page("食品召回示例站", INDEX_BODY)
            return
        if path == "/recalls/list":
            if page > 2:
                self._page("公告列表 - 第 3 页",
                           "<table data-collector='table'>"
                           "<thead><tr><th>公告编号</th><th>标题</th><th>链接</th></tr></thead>"
                           "<tbody><tr><td>暂无召回公告</td><td></td><td></td></tr></tbody></table>")
                return
            self._page(f"公告列表 - 第 {page} 页", table_list(page))
            return
        if path == "/recalls/list-v2":
            if page > 2:
                self._page("改版公告列表",
                           "<ul data-collector='links'><li data-empty='true'>暂无召回公告</li></ul>")
                return
            self._page(f"改版公告列表 - 第 {page} 页", card_list(page))
            return
        if path.startswith("/_scenario/flippable/"):
            action = path.rsplit("/", 1)[1]
            if action == "break":
                _FLIPPED["value"] = True
                self._page("已切换为改版故障", "<p>现在访问 <a href='/recalls/flippable'>可切换列表</a> 将缺少公告编号列。</p>")
                return
            if action == "restore":
                _FLIPPED["value"] = False
                self._page("已恢复正常", "<p>现在访问 <a href='/recalls/flippable'>可切换列表</a> 恢复正常结构。</p>")
                return
            self._page("未知操作", "<p>404</p>", status=404)
            return
        if path == "/recalls/flippable":
            self._page(f"可切换列表 - 第 {page} 页", flippable_list(page))
            return
        if path == "/recalls/list-broken":
            self._page("改版公告列表（缺少编号）", broken_list())
            return
        if path == "/recalls/empty":
            self._page("公告列表", """
<table data-collector="table">
  <thead><tr><th>公告编号</th><th>标题</th><th>链接</th></tr></thead>
  <tbody><tr><td colspan="3" data-empty="true">暂无召回公告</td></tr></tbody>
</table>""")
            return
        if path == "/recalls/empty-broken":
            self._page("公告列表", """
<table data-collector="table">
  <thead><tr><th>公告编号</th><th>标题</th><th>链接</th></tr></thead>
  <tbody></tbody>
</table>""")
            return
        if path == "/recalls/conflict" and page == 1:
            self._page("公告列表（跨页冲突）", """
<table data-collector="table">
  <thead><tr><th>公告编号</th><th>标题</th><th>链接</th></tr></thead>
  <tbody>
    <tr><td>N-2026-001</td><td>某品牌麦芽糖浆召回公告</td>
        <td><a href="/recalls/conflict-detail-a">查看详情</a></td></tr>
  </tbody>
</table>
<p><a rel="next" href="/recalls/conflict?page=2">下一页</a></p>""")
            return
        if path == "/recalls/missing":
            self._page("公告列表（详情缺字段）", """
<table data-collector="table">
  <thead><tr><th>公告编号</th><th>标题</th><th>链接</th></tr></thead>
  <tbody><tr><td>N-2026-002</td><td>某批次花生酱召回公告</td>
      <td><a href="/recalls/detail-missing-batch">查看详情</a></td></tr></tbody>
</table>""")
            return

        if path.startswith("/recalls/detail/"):
            number = path.rsplit("/", 1)[1]
            notice = next((n for n in NOTICES if n["number"] == number), None)
            if notice is None:
                self._page("未找到公告", "<p>未找到公告。</p>", status=404)
                return
            self._page(notice["title"], _notice(notice))
            return
        if path.startswith("/recalls/detail-v2/"):
            number = path.rsplit("/", 1)[1]
            notice = next((n for n in NOTICES if n["number"] == number), None)
            if notice is None:
                self._page("未找到公告", "<p>未找到公告。</p>", status=404)
                return
            self._page(notice["title"], detail_v2(notice))
            return
        if path.startswith("/recalls/detail-v2-broken/"):
            number = path.rsplit("/", 1)[1]
            notice = next((n for n in NOTICES if n["number"] == number), None)
            if notice is None:
                self._page("未找到公告", "<p>未找到公告。</p>", status=404)
                return
            self._page(notice["title"], detail_plain_redesign(notice))
            return
        if path == "/recalls/conflict-detail-a":
            self._page("冲突详情 A", detail_v2(NOTICES[0]))
            return
        if path == "/recalls/detail-missing-batch":
            self._page("详情缺批号", _notice(NOTICES[1], with_batch=False))
            return

        if path == "/recalls/conflict" and page == 2:
            self._page("公告列表（跨页冲突）第 2 页", """
<table data-collector="table">
  <thead><tr><th>公告编号</th><th>标题</th><th>链接</th></tr></thead>
  <tbody>
    <tr><td>N-2026-001</td><td>某品牌麦芽糖浆召回公告（重复）</td>
        <td><a href="/recalls/conflict-detail-b">查看详情</a></td></tr>
  </tbody>
</table>""")
            return
        if path == "/recalls/conflict-detail-b":
            self._page("冲突详情 B", detail_v2(_CONFLICT_SECOND))
            return

        self._page("未找到页面", "<p>404</p>", status=404)

    def log_message(self, format, *args):  # noqa: A002
        if os.environ.get("EXAMPLE_QUIET") != "1":
            super().log_message(format, *args)


def serve(host: str = "0.0.0.0", port: int = 8080) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Example recall site on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    serve(args.host, args.port)
    sys.exit(0)
