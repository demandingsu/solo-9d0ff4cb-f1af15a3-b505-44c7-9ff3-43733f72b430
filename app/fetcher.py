"""HTTP fetching with strict limits and no redirects to off-site hosts."""

from __future__ import annotations

import gzip
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass

from .collector import same_site

MAX_BYTES = 3 * 1024_000
TIMEOUT_SECONDS = 15
USER_AGENT = "RecallQC/1.0 (quality checker)"


class FetchError(RuntimeError):
    pass


@dataclass
class Fetcher:
    target_url: str
    timeout: float = TIMEOUT_SECONDS
    max_bytes: int = MAX_BYTES

    def fetch(self, url: str) -> str:
        if not same_site(url, self.target_url):
            raise FetchError("拒绝访问站外地址。")
        request = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Encoding": "gzip, deflate",
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                final_url = response.geturl()
                if not same_site(final_url, self.target_url):
                    raise FetchError("重定向指向站外地址，已中止。")
                data = response.read(self.max_bytes + 1)
                if len(data) > self.max_bytes:
                    raise FetchError("页面体积超过上限。")
                encoding = response.headers.get("Content-Encoding", "").lower()
        except FetchError:
            raise
        except urllib.error.HTTPError as exc:
            raise FetchError(f"HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise FetchError(f"网络错误：{exc.reason}") from exc
        except Exception as exc:  # noqa: BLE001 - sockets, timeouts, ssl
            raise FetchError(f"读取失败：{type(exc).__name__}") from exc

        if encoding == "gzip":
            try:
                data = gzip.decompress(data)
            except OSError as exc:
                raise FetchError("压缩内容损坏。") from exc
        elif encoding == "deflate":
            try:
                data = zlib.decompress(data)
            except zlib.error as exc:
                raise FetchError("压缩内容损坏。") from exc
        charset = _charset(response.headers.get("Content-Type", "")) or "utf-8"
        try:
            return data.decode(charset)
        except (LookupError, UnicodeDecodeError):
            return data.decode("utf-8", errors="replace")


def _charset(content_type: str) -> str | None:
    for part in content_type.split(";")[1:]:
        name, _, value = part.strip().partition("=")
        if name.lower() == "charset" and value:
            return value.strip("\"' ")
    return None
