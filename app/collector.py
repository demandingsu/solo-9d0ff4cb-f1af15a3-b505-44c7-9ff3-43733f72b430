"""Collect recall notices from a list page, following same-site pagination.

Design principles:
* Never guess.  If the list structure cannot be recognised, or the page
  looks like a redesign, raise :class:`CollectionAnomaly` instead of
  returning an empty or partial list.
* An empty list is publishable only when the page explicitly says so.
* Every detail URL must be on the same site as the list URL.
* A notice number appearing on several pages must have identical GTIN and
  batch data in every detail page; otherwise the whole collection fails.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from . import htmltree
from .errors import CollectionAnomaly, InvalidTargetURL
from .validation import (
    normalize_batch,
    normalize_notice_number,
    normalize_text,
    notice_number_plausible,
    validate_gtin,
)

MAX_PAGES = 300
MAX_NOTICES_DEFAULT = 1000

_EMPTY_PHRASES = (
    "暂无召回公告",
    "暂无公告",
    "暂无数据",
    "暂无记录",
    "没有召回公告",
    "没有公告",
    "无召回公告",
    "无公告",
    "no recalls",
    "no recall notices",
    "no notices",
    "empty list",
)
_NOTICE_ALT = (
    r"(?:[^\s|｜<]{0,12}(?:第\s*[0-9]+\s*号|〔[^〕]{0,12}〕\s*[0-9]+\s*号)"
    r"|[A-Za-z0-9][A-Za-z0-9\-_.·—/／]*)"
)
_NOTICE_LABEL = re.compile(
    r"(?:公告|通知|通告|警示|召回)?\s*编\s*号\s*(?:[:：=]|是|为)?\s*(" + _NOTICE_ALT + r")"
)
_NOTICE_WITH_PREFIX = re.compile(
    r"((?:食药监|市监|市场监管|总局|卫健|海关|FDA|召回)?[^\s|｜<]{0,10}?第\s*[0-9]+\s*号)"
)
_BATCH_LABEL = re.compile(
    r"(?:生产)?(?:批\s*号|批号|批次号|lot|batch)\s*(?:[:：=]|为|是)?\s*"
    r"([0-9][0-9A-Za-z\-]{0,19})", re.I
)
_GTIN_LABEL = re.compile(
    r"(?:GTIN|商品条码|条形码|条码)\s*[:：=]?\s*([0-9][0-9\s\-]{6,20})", re.I
)
_PAGE_KEY_NAMES = ("page", "p", "pageindex", "page_no", "pageno")
_MAX_DETAIL_TEXT = 40_000


@dataclass
class Failure:
    address: str
    reason: str


@dataclass
class ListPayload:
    entries: list[dict]
    explicit_empty: bool = False


def site_key(url: str) -> str:
    """Same-site identity: host and non-default port, scheme ignored."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    port = parts.port
    if port and port not in (80, 443):
        host = f"{host}:{port}"
    return host


def same_site(candidate: str, target: str) -> bool:
    parts = urlsplit(candidate)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    return site_key(candidate) == site_key(target)


def validate_target(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise InvalidTargetURL("请输入公告列表 URL。")
    if len(url) > 2048 or any(char in url for char in "\r\n\t "):
        raise InvalidTargetURL("公告列表 URL 格式无效。")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise InvalidTargetURL("公告列表 URL 必须以 http:// 或 https:// 开头并包含主机名。")
    return url


def _absolute_same_site(href: str | None, page_url: str) -> str | None:
    if not href:
        return None
    href = href.strip()
    if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
        return None
    absolute = urljoin(page_url, href)
    if not same_site(absolute, page_url):
        return None
    parts = urlsplit(absolute)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def _extract_notice_number(text: str) -> str:
    """Extract a notice number from free text (list rows, link labels)."""
    text = normalize_text(text)
    match = _NOTICE_LABEL.search(text)
    if match:
        candidate = normalize_text(match.group(1))
        candidate = re.split(r"\s{2,}|[(（\[]", candidate)[0]
        return candidate.strip("：: ")
    match = _NOTICE_WITH_PREFIX.search(text)
    if match:
        return normalize_text(match.group(1))
    return ""


# ---------------------------------------------------------------------------
# List page recognition
# ---------------------------------------------------------------------------

class ListRecognizer:
    """Recognise a notice list in one of several known layout conventions."""

    def __init__(self, page_url: str):
        self.page_url = page_url

    def recognize(self, markup: str) -> ListPayload | None:
        root, truncated = htmltree.parse_html(markup)
        if truncated:
            raise CollectionAnomaly(
                "页面内容过大，解析被截断，无法完整核对公告清单。",
                [self.page_url],
            )
        if not root.descendants():
            return None

        segments = self._candidate_segments(root)
        explicit_marker = self._has_explicit_segment(root)
        empty_container_seen = False
        for segment, strategy in segments:
            payload = strategy(segment)
            if payload is not None and payload.entries:
                return payload
            if payload is not None and payload.explicit_empty:
                return payload
            if _is_explicit_empty(segment):
                return ListPayload(entries=[], explicit_empty=True)
            if strategy == self._entries_from_table and self._has_notice_table(segment):
                # A well-formed notice table with no rows is empty only when
                # it explicitly says so; remember the ambiguity.
                empty_container_seen = True
            elif strategy == self._entries_from_links and segment.tag in ("ul", "ol"):
                empty_container_seen = True
            if explicit_marker:
                # Explicit markers are authoritative; never fall back to
                # scanning unrelated page parts (which could mask a redesign).
                break
        if empty_container_seen:
            return ListPayload(entries=[], explicit_empty=False)
        return None

    @staticmethod
    def _has_explicit_segment(root) -> bool:
        return any(htmltree.find_segment(root, kind) is not None
                   for kind in ("table", "links", "cards"))

    def _candidate_segments(self, root):
        """Return ``(segment, strategy)`` candidates, strongest first."""
        explicit = []
        for kind, strategy in (
            ("table", self._entries_from_table),
            ("links", self._entries_from_links),
            ("cards", self._entries_from_cards),
        ):
            segment = htmltree.find_segment(root, kind)
            if segment is not None:
                explicit.append((segment, strategy))
        if explicit:
            return explicit

        tables = htmltree.outermost(list(root.find_all({"table"})))
        lists = htmltree.outermost(list(root.find_all({"ul", "ol"})))
        natural = [(table, self._entries_from_table) for table in tables]
        natural += [(node, self._entries_from_links) for node in lists]
        natural.append((root, self._entries_from_text))
        return natural

    # -- table layout ---------------------------------------------------
    def _has_notice_table(self, segment) -> bool:
        tables = [segment] if segment.tag == "table" else list(segment.find_all({"table"}))
        for table in tables:
            rows = list(table.find_all({"tr"}))
            if rows and self._header_map(rows[0]) is not None:
                return True
        return False

    def _entries_from_table(self, segment) -> ListPayload | None:
        tables = [segment] if segment.tag == "table" else list(segment.find_all({"table"}))
        entries: list[dict] = []
        seen: set[tuple[str, str]] = set()
        empty = False
        for table in tables:
            rows = list(table.find_all({"tr"}))
            if not rows:
                continue
            header = self._header_map(rows[0])
            if header is None:
                continue
            if _rows_explicitly_empty(rows[1:]):
                empty = True
                continue
            for row in rows[1:]:
                entry = self._entry_from_row(row, header)
                if entry is None:
                    continue
                key = (entry["number"], entry["detail_url"] or entry.get("error", ""))
                if key in seen:
                    continue
                seen.add(key)
                entries.append(entry)
        if entries:
            return ListPayload(entries=entries)
        if empty:
            return ListPayload(entries=[], explicit_empty=True)
        return None

    @staticmethod
    def _header_map(row) -> dict[str, int] | None:
        cells = [normalize_text(cell.text()).lower() for cell in row.find_all({"th", "td"})]
        index: dict[str, int] = {}
        for position, name in enumerate(cells):
            if name in ("公告编号", "编号", "公告文号", "文号", "notice no.", "notice no", "no.") and "number" not in index:
                index["number"] = position
            elif name in ("标题", "公告标题", "title", "名称") and "title" not in index:
                index["title"] = position
            elif name in ("链接", "详情", "link", "url") and "link" not in index:
                index["link"] = position
        return index if "number" in index else None

    def _entry_from_row(self, row, header: dict[str, int]) -> dict | None:
        cells = list(row.find_all({"th", "td"}))
        if not cells:
            return None
        number = ""
        if header["number"] < len(cells):
            number = normalize_text(cells[header["number"]].text())
        if not number:
            if "title" in header and header["title"] < len(cells):
                number = _extract_notice_number(cells[header["title"]].text())
        if not number or not notice_number_plausible(number):
            return None
        href, link_error = self._row_href(row, cells, header)
        if href is None:
            return {"number": number, "detail_url": "", "error": link_error or "列表行缺少同站详情链接。"}
        return {"number": number, "detail_url": href}

    def _row_href(self, row, cells, header: dict[str, int]) -> tuple[str | None, str | None]:
        link = None
        if "link" in header and header["link"] < len(cells):
            link = next(cells[header["link"]].find_all({"a"}), None)
        link = link or next(row.find_all({"a"}), None)
        if link is None or not link.get("href"):
            return None, "列表行缺少详情链接。"
        href = _absolute_same_site(link.get("href"), self.page_url)
        if href is None:
            return None, "详情链接不在同一站点（缺失或指向站外）。"
        return href, None

    # -- link list layout ----------------------------------------------
    def _entries_from_links(self, segment) -> ListPayload | None:
        items = list(segment.find_all({"li"}))
        if not items and segment.tag in ("ul", "ol"):
            items = segment.element_children()
        if not items:
            return None
        entries: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for item in items:
            link = next(item.find_all({"a"}), None)
            if link is None:
                continue
            number = _extract_notice_number(link.text())
            if not number:
                number = _extract_notice_number(item.text())
            if not number or not notice_number_plausible(number):
                continue
            if not link.get("href"):
                entries.append({"number": number, "detail_url": "",
                                "error": "列表行缺少详情链接。"})
                continue
            href = _absolute_same_site(link.get("href"), self.page_url)
            if href is None:
                entries.append({"number": number, "detail_url": "",
                                "error": "详情链接不在同一站点（缺失或指向站外）。"})
                continue
            key = (number, href)
            if key in seen:
                continue
            seen.add(key)
            entries.append({"number": number, "detail_url": href})
        if not entries:
            return None
        return ListPayload(entries=entries)

    # -- card layout ----------------------------------------------------
    def _entries_from_cards(self, segment) -> ListPayload | None:
        candidates = [
            node for node in segment.find_all({"div", "li", "article", "section", "a"})
            if node.get("data-notice-number") or "number" in node.get("data-field", "").lower()
        ]
        entries: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for node in candidates:
            number = normalize_text(node.get("data-notice-number") or "")
            if not number:
                number = _extract_notice_number(node.text())
            if not number or not notice_number_plausible(number):
                continue
            link = node if node.tag == "a" else next(node.find_all({"a"}), None)
            if link is None or not link.get("href"):
                entries.append({"number": number, "detail_url": "",
                                "error": "列表行缺少详情链接。"})
                continue
            href = _absolute_same_site(link.get("href"), self.page_url)
            if href is None:
                entries.append({"number": number, "detail_url": "",
                                "error": "详情链接不在同一站点（缺失或指向站外）。"})
                continue
            key = (number, href)
            if key in seen:
                continue
            seen.add(key)
            entries.append({"number": number, "detail_url": href})
        if not entries:
            return None
        return ListPayload(entries=entries)

    # -- plain anchors fallback -----------------------------------------
    def _entries_from_text(self, segment) -> ListPayload | None:
        entries: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for anchor in segment.find_all({"a"}):
            number = _extract_notice_number(anchor.text())
            if not number or not notice_number_plausible(number):
                continue
            href = _absolute_same_site(anchor.get("href"), self.page_url)
            if href is None:
                entries.append({"number": number, "detail_url": "",
                                "error": "详情链接不在同一站点（缺失或指向站外）。"})
                continue
            key = (number, href)
            if key in seen:
                continue
            seen.add(key)
            entries.append({"number": number, "detail_url": href})
        if not entries:
            return None
        return ListPayload(entries=entries)


_EMPTY_ELEMENT_TAGS = ("p", "div", "span", "li", "td", "th", "tr", "h1", "h2",
                       "h3", "h4", "section", "article", "main", "ul", "ol", "table")


def _element_empty_marker(segment) -> bool:
    """A short element that says the list is empty, e.g. <p>暂无召回公告</p>."""
    for node in segment.descendants():
        if node.tag not in _EMPTY_ELEMENT_TAGS:
            continue
        text = normalize_text(node.text()).lower()
        if not text or len(text) > 40:
            continue
        if any(phrase in text for phrase in _EMPTY_PHRASES):
            return True
    return False


def _rows_explicitly_empty(rows) -> bool:
    """True only when every non-header row is blank or an empty marker."""
    if not rows:
        return False
    saw_marker = False
    for row in rows:
        if row.find_all({"a"}):
            return False
        text = normalize_text(row.text()).lower()
        if not text:
            continue
        if any(phrase in text for phrase in _EMPTY_PHRASES):
            saw_marker = True
        else:
            return False
    return saw_marker


def _is_explicit_empty(segment) -> bool:
    """An empty statement counts only inside a notice container.

    Phrase matching on the whole document is deliberately refused: a footer
    or unrelated block saying "暂无公告" must not turn a broken list into a
    published empty list.
    """
    if segment.find_first(tags=None, **{"data-empty": "true"}) is not None:
        return True
    if segment.tag == "#document":
        return False
    return _element_empty_marker(segment)


# ---------------------------------------------------------------------------
# Detail page recognition
# ---------------------------------------------------------------------------

class DetailRecognizer:
    def __init__(self, detail_url: str):
        self.detail_url = detail_url

    def validate(self, markup: str) -> tuple[dict, list[Failure]]:
        failures: list[Failure] = []
        root, truncated = htmltree.parse_html(markup)
        if truncated:
            return {}, [Failure(self.detail_url, "详情页内容过大，解析被截断。")]
        scope = htmltree.find_segment(root, "detail") or root
        text = normalize_text(scope.text())
        if len(text) > _MAX_DETAIL_TEXT:
            return {}, [Failure(self.detail_url, "详情页正文异常过长，疑似非公告详情。")]

        number = self._field_value(scope, "notice-number", _NOTICE_LABEL)
        gtin_raw = self._field_value(scope, "gtin", _GTIN_LABEL)
        batch_raw = self._field_value(scope, "batch", _BATCH_LABEL)

        if not number:
            failures.append(Failure(self.detail_url, "详情页缺少公告编号。"))
        if gtin_raw is None:
            failures.append(Failure(self.detail_url, "详情页缺少 GTIN（商品条码）。"))
        if batch_raw is None:
            failures.append(Failure(self.detail_url, "详情页缺少六位批号。"))

        gtin = None
        if gtin_raw is not None:
            gtin, gtin_ok = validate_gtin(gtin_raw)
            if gtin is None:
                failures.append(Failure(
                    self.detail_url,
                    f"GTIN 格式无效：{normalize_text(gtin_raw)[:40]}",
                ))
            elif not gtin_ok:
                failures.append(Failure(self.detail_url, f"GTIN 校验位无效：{gtin}"))
        batch = None
        if batch_raw is not None:
            batch = normalize_batch(batch_raw)
            if batch is None:
                failures.append(Failure(
                    self.detail_url,
                    f"批号不是六位数字：{normalize_text(batch_raw)[:40]}",
                ))
        if failures:
            return {}, failures
        return {
            "number": normalize_notice_number(number),
            "gtin": gtin,
            "batch": batch,
        }, []

    def _field_value(self, scope, kind: str, label_pattern: re.Pattern) -> str | None:
        marker = htmltree.find_segment(scope, kind)
        if marker is not None:
            value = normalize_text(marker.text()) or marker.get("data-value")
            if value:
                return value
        field_names = {kind}
        if kind == "notice-number":
            field_names |= {"number", "notice-number", "notice_number"}
        for node in scope.find_all(tags=None):
            if node.get("data-field", "").strip().lower() in field_names:
                value = node.get("data-value") or normalize_text(node.text())
                if value:
                    return normalize_text(value)
        match = label_pattern.search(normalize_text(scope.text()))
        if match:
            return normalize_text(match.group(1))
        return None


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def pagination_target(root, page_url: str) -> str | None:
    """Return the next same-site list page URL, or ``None``.

    Follows explicit "next" links and numbered page links only; it never
    fabricates ``?page=n`` URLs, because a redesign may leave stale links.
    Self-references are ignored (they would create a pagination loop).
    """
    target = _pagination_candidate(root, page_url)
    if target is None:
        return None
    if _same_page(target, page_url):
        return None
    return target


def _same_page(a: str, b: str) -> bool:
    parts_a, parts_b = urlsplit(a), urlsplit(b)
    return (parts_a.netloc, parts_a.path, sorted(parse_qsl(parts_a.query))) == \
           (parts_b.netloc, parts_b.path, sorted(parse_qsl(parts_b.query)))


def _pagination_candidate(root, page_url: str) -> str | None:
    for anchor in root.find_all({"a"}):
        rel_tokens = anchor.get("rel", "").lower().split()
        label = normalize_text(anchor.text()).lower()
        if ("next" in rel_tokens
                or "next" in anchor.classes
                or anchor.get("data-page") == "next"
                or label in ("下一页", "下页", "next", "next page", "›", "»")):
            target = _absolute_same_site(anchor.get("href"), page_url)
            if target:
                return target
    for link in root.find_all({"link"}):
        if "next" in link.get("rel", "").lower().split():
            target = _absolute_same_site(link.get("href"), page_url)
            if target:
                return target
    return _next_numbered_page(root, page_url)


def _page_number(url: str) -> int | None:
    for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        if key.lower() in _PAGE_KEY_NAMES:
            try:
                return int(value)
            except ValueError:
                return None
    return None


def _next_numbered_page(root, page_url: str) -> str | None:
    current = _page_number(page_url)
    if current is None:
        current = 1
    best: tuple[int, str] | None = None
    for anchor in root.find_all({"a"}):
        label = normalize_text(anchor.text())
        if not label.isdigit():
            continue
        number = int(label)
        if number <= current:
            continue
        target = _absolute_same_site(anchor.get("href"), page_url)
        if target is None or _page_number(target) != number:
            continue
        if best is None or number < best[0]:
            best = (number, target)
    return best[1] if best else None


# ---------------------------------------------------------------------------
# Collection session
# ---------------------------------------------------------------------------

@dataclass
class CollectionSession:
    fetcher: object
    list_url: str
    max_pages: int = MAX_PAGES
    max_notices: int = MAX_NOTICES_DEFAULT

    def __post_init__(self):
        self.list_url = validate_target(self.list_url)

    def collect(self) -> tuple[list[dict], list[str]]:
        """Collect and cross-check the complete notice list.

        Returns ``(notices, list_page_urls)`` on success and raises
        :class:`CollectionAnomaly` otherwise.  No partial result is ever
        returned.  ``anomaly.exceptions`` carries one :class:`Failure`
        per problematic address for the UI.
        """
        pages = self._read_list_pages()
        entries = self._merge_list_entries(pages)
        if not entries:
            return [], [url for url, _ in pages]
        notices, anomalies = self._read_details(entries)
        if anomalies:
            summary = "；".join(f.reason for f in anomalies[:4])
            if len(anomalies) > 4:
                summary += f"（共 {len(anomalies)} 处异常）"
            raise CollectionAnomaly(
                "公告核对失败：" + summary,
                [f.address for f in anomalies] + [url for url, _ in pages],
                details=anomalies,
            )
        return notices, [url for url, _ in pages]

    # -- list reading ---------------------------------------------------
    def _read_list_pages(self) -> list[tuple[str, str]]:
        pages: list[tuple[str, str]] = []
        visited: set[str] = set()
        next_url: str | None = self.list_url
        while next_url is not None:
            if next_url in visited:
                raise CollectionAnomaly("分页链接出现循环，已中止采集。", [next_url])
            if len(pages) >= self.max_pages:
                raise CollectionAnomaly(
                    "分页数超过上限，无法确认已获取完整公告清单。",
                    [url for url, _ in pages],
                )
            visited.add(next_url)
            try:
                markup = self.fetcher.fetch(next_url)
            except Exception as exc:  # noqa: BLE001
                raise CollectionAnomaly(f"列表页读取失败：{exc}", [next_url]) from exc
            pages.append((next_url, markup))
            root, _truncated = htmltree.parse_html(markup)
            next_url = pagination_target(root, next_url)
        return pages

    def _merge_list_entries(self, pages: list[tuple[str, str]]) -> list[dict]:
        """Recognise every list page, enforcing explicitness and completeness."""
        entries: list[dict] = []
        seen_numbers: set[str] = set()
        saw_empty = False
        for page_url, markup in pages:
            payload = ListRecognizer(page_url).recognize(markup)
            if payload is None:
                raise CollectionAnomaly(
                    "列表结构无法识别（页面可能已改版，或该地址不是公告列表）。",
                    [url for url, _ in pages],
                )
            if not payload.entries:
                if not payload.explicit_empty:
                    raise CollectionAnomaly(
                        "列表页没有公告，但页面未明确标示为空，无法区分“无召回”与抓取失败。",
                        [page_url],
                    )
                if entries or saw_empty:
                    raise CollectionAnomaly(
                        "分页序列中一页明确为空而其他页有公告，清单完整性无法确认。",
                        [url for url, _ in pages],
                    )
                saw_empty = True
                continue
            if saw_empty:
                raise CollectionAnomaly(
                    "分页序列中先出现空页、后出现公告，清单完整性无法确认。",
                    [url for url, _ in pages],
                )
            for entry in payload.entries:
                if entry.get("error"):
                    raise CollectionAnomaly(
                        f"公告“{entry['number']}”无法取得详情：{entry['error']}",
                        [page_url],
                        details=[Failure(page_url,
                                         f"公告“{entry['number']}”：{entry['error']}")],
                    )
                entries.append({
                    "number": entry["number"],
                    "detail_url": entry["detail_url"],
                    "source_page": page_url,
                })
                if len(entries) > self.max_notices:
                    raise CollectionAnomaly(
                        "公告数量超过安全上限，疑似分页循环，已中止采集。",
                        [url for url, _ in pages],
                    )
        return entries

    # -- details --------------------------------------------------------
    def _read_details(self, entries: list[dict]) -> tuple[list[dict], list[Failure]]:
        """Fetch every detail page and cross-check all listed notices."""
        anomalies: list[Failure] = []
        notices: list[dict] = []
        # number -> list of (detail_url, source_page, fields)
        by_number: dict[str, list[tuple[str, str, dict]]] = {}
        for entry in entries:
            url = entry["detail_url"]
            try:
                markup = self.fetcher.fetch(url)
            except Exception as exc:  # noqa: BLE001
                anomalies.append(Failure(url, f"详情页读取失败：{exc}"))
                continue
            fields, failures = DetailRecognizer(url).validate(markup)
            if failures:
                anomalies.extend(failures)
                continue
            listed = normalize_notice_number(entry["number"])
            if listed != fields["number"]:
                anomalies.append(Failure(
                    url,
                    f"列表公告编号“{entry['number']}”与详情编号“{fields['number']}”不一致。",
                ))
                continue
            by_number.setdefault(listed, []).append(
                (url, entry["source_page"], fields)
            )

        for number, records in by_number.items():
            distinct_urls = {url for url, _page, _fields in records}
            distinct_payloads = {
                (fields["gtin"], fields["batch"]) for _url, _page, fields in records
            }
            if len(distinct_payloads) > 1:
                detail = "；".join(
                    f"{url}（来自 {page}）→ GTIN {fields['gtin']} / 批号 {fields['batch']}"
                    for url, page, fields in records
                )
                anomalies.append(Failure(
                    records[0][0],
                    f"同一公告编号“{number}”的跨页详情内容冲突：{detail}",
                ))
                continue
            if len(distinct_urls) > 1:
                # Same number, different detail pages, identical data: the
                # list itself is inconsistent, so do not publish.
                detail = "；".join(
                    f"{url}（来自 {page}）" for url, page, _fields in records
                )
                anomalies.append(Failure(
                    records[0][0],
                    f"同一公告编号“{number}”对应多个不同详情页：{detail}",
                ))
                continue
            (url, _page, fields) = records[0]
            notices.append({
                "number": number,
                "detail_url": url,
                "gtin": fields["gtin"],
                "batch": fields["batch"],
                "collected_from": self.list_url,
            })
        return notices, anomalies
