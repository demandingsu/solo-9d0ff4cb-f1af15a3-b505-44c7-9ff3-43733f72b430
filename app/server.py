"""Zero-dependency web UI for the recall collection quality check."""

from __future__ import annotations

import html
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from string import Template
from urllib.parse import parse_qs

from .service import QualityService
from .storage import SnapshotStore, is_key, url_key

PAGE_TEMPLATE = Template("""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
         margin: 0; padding: 24px; line-height: 1.5; }
  main { max-width: 1080px; margin: 0 auto; }
  h1 { font-size: 1.4rem; margin: 0 0 4px; }
  .sub { opacity: .7; margin-bottom: 20px; }
  form { display: flex; gap: 8px; margin: 16px 0 24px; flex-wrap: wrap; }
  input[type=url] { flex: 1 1 480px; padding: 10px 12px; font: inherit; border: 1px solid #888;
                     border-radius: 8px; }
  button { padding: 10px 18px; font: inherit; border-radius: 8px; border: 0;
            background: #1f6feb; color: #fff; cursor: pointer; }
  .banner { border-radius: 8px; padding: 12px 16px; margin: 16px 0; }
  .ok { background: #d7f5dd; color: #0a4d1c; }
  .bad { background: #ffe0e0; color: #7a1010; }
  .warn { background: #fff3cd; color: #6b4b00; }
  .muted { background: #eef1f5; color: #333; }
  table { border-collapse: collapse; width: 100%; margin: 12px 0; }
  th, td { border: 1px solid #ccc; padding: 8px; text-align: left; vertical-align: top; }
  th { background: #f2f4f7; }
  .addr { word-break: break-all; font-family: ui-monospace, monospace; font-size: .9em; }
  .reason { color: #7a1010; }
  .stale { display: inline-block; background: #fff3cd; color: #6b4b00;
           border-radius: 999px; padding: 2px 10px; font-size: .85em; }
  .grp { margin-top: 28px; }
  .meta { font-size: .9em; opacity: .8; }
</style>
</head>
<body><main>
<h1>食品召回公告采集质量核查</h1>
<div class="sub">输入公告列表 URL，逐条核对公告编号、GTIN 与六位批号。异常时保留上次成功清单并标注旧数据。</div>
<form method="post" action="/check">
  <input type="url" name="url" required placeholder="https://example.com/recalls/list"
         value="$url_value">
  <button type="submit">采集并核查</button>
</form>
$body
</main></body></html>
""")


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def notices_table(notices: list[dict], empty_message: str | None = None) -> str:
    if not notices:
        return ('<p class="ok">' + _esc(empty_message or "该列表明确标示为空，已发布空清单（0 条公告）。")
                + '</p>')
    rows = "".join(
        "<tr><td>{n}</td><td>{g}</td><td>{b}</td>"
        "<td class='addr'>{a}</td></tr>".format(
            n=_esc(item.get("number")), g=_esc(item.get("gtin")),
            b=_esc(item.get("batch")), a=_esc(item.get("detail_url")),
        )
        for item in notices
    )
    return ("<table><thead><tr><th>公告编号</th><th>GTIN</th><th>六位批号</th>"
            "<th>详情页</th></tr></thead><tbody>" + rows + "</tbody></table>")


def anomalies_table(anomalies: list[dict]) -> str:
    rows = "".join(
        "<tr><td class='addr'>{a}</td><td class='reason'>{r}</td></tr>".format(
            a=_esc(item.get("address")), r=_esc(item.get("reason")),
        )
        for item in anomalies
    )
    return ("<table><thead><tr><th>异常地址</th><th>原因</th></tr></thead>"
            "<tbody>" + rows + "</tbody></table>")


def previous_meta(snapshot) -> str:
    if snapshot is not None and snapshot.collected_at:
        return f"采集时间 {snapshot.collected_at}"
    return "保存时间未知"


def render_result(result, snapshot) -> str:
    parts: list[str] = []
    if result is not None and result.ok:
        parts.append('<div class="banner ok">' + _esc(result.message) +
                     f'<div class="meta">采集时间：{_esc(result.collected_at)} · '
                     f'公告 {len(result.notices)} 条 · 列表页 {len(result.list_pages)} 页</div></div>')
        parts.append(notices_table(result.notices))
        if result.list_pages:
            parts.append('<p class="meta">已读取列表页：<br>' +
                         "<br>".join(f"<span class='addr'>{_esc(u)}</span>" for u in result.list_pages) +
                         "</p>")
    else:
        if result.anomalies:
            parts.append('<div class="banner bad">核查失败，本次结果不予发布。' +
                         f'<div class="meta">{_esc(result.message)}</div></div>')
            parts.append("<h2>异常地址及原因</h2>")
            parts.append(anomalies_table(result.anomalies))
        else:
            parts.append('<div class="banner bad">无法开始核查。' +
                         f'<div class="meta">{_esc(result.message)}</div></div>')
        if result.previous_snapshot_stale:
            parts.append('<div class="banner warn">已保留上次成功清单（' +
                         _esc(previous_meta(snapshot)) + '），并标记为旧数据。</div>')
        else:
            parts.append('<div class="banner muted">该地址暂无上次成功清单可保留，未生成任何清单。</div>')

    if snapshot is not None:
        parts.append('<div class="grp"><h2>已保存的清单 ' +
                     ('' if snapshot.status == 'current' else '<span class="stale">旧数据</span>') +
                     '</h2>')
        parts.append(f'<p class="meta">采集时间：{_esc(snapshot.collected_at)} · '
                     f'公告 {len(snapshot.notices)} 条</p>')
        if snapshot.status == "stale":
            parts.append('<p class="meta">上次核查失败：' + _esc(snapshot.failure_reason or "") +
                         '（' + _esc(snapshot.failed_at or "") + '）</p>')
        parts.append(notices_table(
            snapshot.notices,
            "该地址保存的是明确为空的清单（0 条公告）。",
        ))
        if snapshot.notices:
            parts.append('<form method="post" action="/json">'
                         f'<input type="hidden" name="url" value="{_esc(snapshot.list_url)}">'
                         '<button type="submit">查看保存的清单 JSON</button></form>')
    return "\n".join(parts)


EXAMPLES = (
    ("正常两页列表", "/recalls/list"),
    ("改版卡片列表", "/recalls/list-v2"),
    ("改版缺编号（应报异常）", "/recalls/list-broken"),
    ("明确为空（应发布空清单）", "/recalls/empty"),
    ("空白未标空（应报异常）", "/recalls/empty-broken"),
    ("跨页冲突（应报异常）", "/recalls/conflict"),
    ("详情缺批号（应报异常）", "/recalls/missing"),
    ("可切换列表（验证旧数据保留）", "/recalls/flippable"),
)


def example_links() -> str:
    """Quick-entry buttons for the bundled example site.

    ``EXAMPLE_SITE_INTERNAL_URL`` is what the QC container fetches
    (e.g. http://example-site:8080); ``EXAMPLE_SITE_PUBLIC_URL`` is what the
    operator opens in a browser (e.g. http://localhost:8080).
    """
    internal = os.environ.get("EXAMPLE_SITE_INTERNAL_URL", "").rstrip("/")
    public = os.environ.get("EXAMPLE_SITE_PUBLIC_URL", "").rstrip("/")
    if not internal and not public:
        return ""
    parts = ['<div class="grp"><h2>本地示例站</h2>']
    if public:
        parts.append(f'<p><a href="{_esc(public)}/">在浏览器打开示例站</a>'
                     f' <span class="meta">（{_esc(public)}）</span></p>')
    if internal:
        parts.append('<p class="meta">以下按钮直接对示例站发起核查（由本服务抓取 '
                     f'{_esc(internal)}）：</p><div class="samples">')
        for label, path in EXAMPLES:
            parts.append(
                f'<form method="post" action="/check">'
                f'<input type="hidden" name="url" value="{_esc(internal + path)}">'
                f'<button class="ghost" type="submit">{_esc(label)}</button></form>'
            )
        parts.append('</div>')
    parts.append('</div>')
    return "".join(parts)


def snapshots_summary(service: QualityService) -> str:
    snapshots = service.store.list_all()
    if not snapshots:
        return ""
    items = "".join(
        f"<li><a class='addr' href='/snapshot/{url_key(item.list_url)}'>{_esc(item.list_url)}</a>"
        + ('' if item.status == "current" else ' <span class="stale">旧数据</span>')
        + f"<div class='meta'>采集时间：{_esc(item.collected_at)} · 公告 {len(item.notices)} 条</div></li>"
        for item in snapshots
    )
    return f'<div class="grp"><h2>已保存的公告清单</h2><ul class="snapshots">{items}</ul></div>'


def render_page(title: str, body: str, url_value: str = "") -> str:
    return PAGE_TEMPLATE.substitute(title=_esc(title), body=body, url_value=_esc(url_value))


class Handler(BaseHTTPRequestHandler):
    service: QualityService

    def _send(self, status: int, content: str, content_type: str = "text/html; charset=utf-8"):
        encoded = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, render_page(
                "食品召回公告采集质量核查",
                example_links() + snapshots_summary(self.service)))
            return
        if self.path.startswith("/snapshot/"):
            key = self.path.split("/snapshot/", 1)[1]
            if "/" in key or not is_key(key):
                self._send(404, "未找到该清单。", "text/plain; charset=utf-8")
                return
            snapshot = self._snapshot_by_key(key)
            if snapshot is None:
                self._send(404, "未找到该清单。", "text/plain; charset=utf-8")
                return
            body = (f'<div class="banner {"muted" if snapshot.status == "stale" else "ok"}">'
                    + ('旧数据' if snapshot.status == "stale" else '已保存的清单')
                    + f'<div class="meta">采集时间：{_esc(snapshot.collected_at)} · '
                    f'公告 {len(snapshot.notices)} 条</div></div>')
            if snapshot.status == "stale":
                body += ('<p class="meta">上次核查失败：' + _esc(snapshot.failure_reason or "") +
                         '</p>')
            body += notices_table(snapshot.notices,
                                  "该地址保存的是明确为空的清单（0 条公告）。")
            self._send(200, render_page("已保存的清单", body))
            return
        self._send(404, "页面不存在。", "text/plain; charset=utf-8")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 8192:
            self._send(413, "请求过大。", "text/plain; charset=utf-8")
            return
        form = parse_qs(self.rfile.read(length).decode("utf-8"))
        url = (form.get("url") or [""])[0]
        if self.path == "/check":
            result = self.service.check(url)
            snapshot = self.service.view(result.list_url) if result.list_url else None
            body = render_result(result, snapshot)
            self._send(200, render_page("核查结果", body, result.list_url))
            return
        if self.path == "/json":
            snapshot = self.service.view(url)
            if snapshot is None:
                self._send(404, '{"error":"no snapshot"}', "application/json; charset=utf-8")
                return
            payload = {
                "list_url": snapshot.list_url,
                "status": "stale" if snapshot.status == "stale" else "current",
                "stale": snapshot.status == "stale",
                "collected_at": snapshot.collected_at,
                "failure_reason": snapshot.failure_reason,
                "notices": snapshot.notices,
            }
            self._send(200, json.dumps(payload, ensure_ascii=False, indent=2),
                       "application/json; charset=utf-8")
            return
        self._send(404, "页面不存在。", "text/plain; charset=utf-8")

    def _snapshot_by_key(self, key: str):
        if not is_key(key):
            return None
        path = self.service.store.directory / f"{key}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return self.service.view(data.get("list_url", ""))

    def log_message(self, format, *args):  # noqa: A002
        if os.environ.get("RECALLQC_QUIET") != "1":
            super().log_message(format, *args)


def serve(host: str = "0.0.0.0", port: int = 8000,
          data_dir: str | None = None) -> None:
    data_dir = data_dir or os.environ.get("RECALLQC_DATA_DIR", "/data")
    store = SnapshotStore(data_dir)
    Handler.service = QualityService(store)
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"RecallQC listening on http://{host}:{port} (data: {data_dir})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    host = os.environ.get("RECALLQC_HOST", "0.0.0.0")
    port = int(os.environ.get("RECALLQC_PORT", "8000"))
    data_dir = os.environ.get("RECALLQC_DATA_DIR", "/data")
    for index, argument in enumerate(argv):
        if argument == "--port" and index + 1 < len(argv):
            port = int(argv[index + 1])
        elif argument == "--data" and index + 1 < len(argv):
            data_dir = argv[index + 1]
    serve(host, port, data_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
