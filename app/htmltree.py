"""A small, dependency-free HTML tree used for resilient page inspection.

The standard library only offers :class:`html.parser.HTMLParser`, which streams
events and loses nesting information.  Recall pages are rarely well-formed, so
we build a forgiving tree ourselves: unclosed elements are auto-closed, stray
end tags are ignored, and a token budget stops the parser on runaway input.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser

MAX_TOKENS = 400_000
MAX_DEPTH = 400

_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

# Elements that implicitly close an open <p>, matching browser behaviour.
_CLOSES_P = {
    "address", "article", "aside", "blockquote", "details", "div", "dl",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
    "h4", "h5", "h6", "header", "hr", "main", "nav", "ol", "p", "pre",
    "section", "table", "ul",
}


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Node"] = field(default_factory=list)
    parent: "Node | None" = field(default=None, repr=False)
    _texts: list[str] = field(default_factory=list, repr=False)

    # -- attributes -----------------------------------------------------
    def get(self, name: str, default: str = "") -> str:
        return self.attrs.get(name, default)

    @property
    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    def element_children(self) -> list["Node"]:
        return [child for child in self.children if isinstance(child, Node)]

    # -- text -----------------------------------------------------------
    def append_text(self, text: str) -> None:
        self._texts.append(text)

    def text(self) -> str:
        """Visible-ish text of this subtree, with whitespace normalised."""
        parts: list[str] = list(self._texts)
        for child in self.children:
            if isinstance(child, Node):
                if child.tag in ("script", "style", "template"):
                    continue
                parts.append(child.text())
        return _normalise(" ".join(parts))

    def raw_texts(self) -> str:
        """All text including ignored subtrees, used for diagnostics only."""
        parts: list[str] = list(self._texts)
        for child in self.children:
            if isinstance(child, Node):
                parts.append(child.raw_texts())
        return " ".join(parts)

    # -- traversal ------------------------------------------------------
    def descendants(self):
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.descendants()

    def find_all(self, tags: set[str] | None = None, **attr_filters: str):
        """Yield descendants matching the tag set and exact attribute values.

        ``class_contains`` matches one class token.  An attribute filter of
        ``data-field="gtin"`` therefore compares the whole value.
        """
        class_contains = attr_filters.pop("class_contains", None)
        for node in self.descendants():
            if tags is not None and node.tag not in tags:
                continue
            ok = True
            for key, value in attr_filters.items():
                if node.get(key) != value:
                    ok = False
                    break
            if ok and class_contains is not None:
                ok = class_contains in node.classes
            if ok:
                yield node

    def find_first(self, tags: set[str] | None = None, **attr_filters: str):
        for node in self.find_all(tags, **attr_filters):
            return node
        return None


def _normalise(text: str) -> str:
    return " ".join(text.split())


def depth(node: "Node") -> int:
    """Nesting depth of *node* below the document root."""
    value = 0
    while node.parent is not None:
        value += 1
        node = node.parent
    return value


def outermost(nodes: list["Node"]) -> list["Node"]:
    """Drop nodes that are inside another node of the same list."""
    selected = set(id(node) for node in nodes)
    kept = []
    for node in nodes:
        parent = node.parent
        while parent is not None:
            if id(parent) in selected:
                break
            parent = parent.parent
        else:
            kept.append(node)
    return kept


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.stack: list[Node] = [self.root]
        self.token_count = 0
        self.truncated = False

    def _current(self) -> Node:
        return self.stack[-1]

    def _pop_to(self, tag: str) -> bool:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return True
        return False

    def _close_implied(self, tag: str) -> None:
        if tag in _CLOSES_P and self.stack[-1].tag == "p":
            self.stack.pop()
        # <li> closes previous <li>; <dt>/<dd> close each other; table cells too.
        if tag == "li" and self.stack[-1].tag == "li":
            self.stack.pop()
        if tag in ("dt", "dd") and self.stack[-1].tag in ("dt", "dd"):
            self.stack.pop()
        if tag in ("td", "th", "tr", "tbody", "thead", "tfoot", "table"):
            while self.stack[-1].tag in ("td", "th"):
                self.stack.pop()
        if tag in ("tr", "tbody", "thead", "tfoot", "table") and self.stack[-1].tag == "tr":
            self.stack.pop()
        if tag == "option" and self.stack[-1].tag == "option":
            self.stack.pop()

    def handle_starttag(self, tag, attrs):
        self.token_count += 1
        if self.token_count > MAX_TOKENS:
            self.truncated = True
            return
        tag = tag.lower()
        self._close_implied(tag)
        node = Node(
            tag=tag,
            attrs={name.lower(): (value or "") for name, value in attrs},
            parent=self._current(),
        )
        self._current().children.append(node)
        if tag not in _VOID_TAGS and len(self.stack) < MAX_DEPTH:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.token_count += 1
        if self.token_count > MAX_TOKENS:
            self.truncated = True
            return
        node = Node(
            tag=tag.lower(),
            attrs={name.lower(): (value or "") for name, value in attrs},
            parent=self._current(),
        )
        self._current().children.append(node)

    def handle_endtag(self, tag):
        self.token_count += 1
        if self.token_count > MAX_TOKENS:
            self.truncated = True
            return
        tag = tag.lower()
        if tag in _VOID_TAGS:
            return
        self._pop_to(tag)

    def handle_data(self, data):
        self.token_count += 1
        if self.token_count > MAX_TOKENS:
            self.truncated = True
            return
        if data:
            self._current().append_text(data)


def parse_html(markup: str) -> tuple[Node, bool]:
    """Parse *markup* into a forgiving tree.  Returns ``(root, truncated)``."""
    builder = _TreeBuilder()
    builder.feed(markup)
    builder.close()
    return builder.root, builder.truncated


def find_segment(root: Node, kind: str) -> Node | None:
    """Return the element explicitly marked as the collector *kind* segment.

    Recognised markers (in priority order):
    ``data-collector="kind"``, ``id="collector-kind"`` and class
    ``collector-kind``.  Returns ``None`` when no explicit marker exists.
    """
    for node in root.descendants():
        if node.get("data-collector").strip().lower() == kind:
            return node
    for node in root.descendants():
        if node.get("id") == f"collector-{kind}":
            return node
    for node in root.descendants():
        if f"collector-{kind}" in node.classes:
            return node
    return None
