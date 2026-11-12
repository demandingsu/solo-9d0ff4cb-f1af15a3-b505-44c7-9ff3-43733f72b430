"""Application service: run a check, publish or preserve the snapshot."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .collector import CollectionSession, validate_target
from .errors import CollectionAnomaly, InvalidTargetURL
from .fetcher import Fetcher
from .storage import Snapshot, SnapshotStore


@dataclass
class CheckResult:
    ok: bool
    list_url: str
    notices: list[dict] = field(default_factory=list)
    list_pages: list[str] = field(default_factory=list)
    collected_at: str | None = None
    anomalies: list[dict] = field(default_factory=list)
    previous_snapshot_stale: bool = False
    message: str = ""


def _detail_rows(exc: CollectionAnomaly, target: str) -> list[dict]:
    """Per-address anomaly rows; fall back to the summary when unstructured."""
    rows = [{"address": item.address, "reason": item.reason} for item in exc.details]
    covered = {row["address"] for row in rows}
    for address in exc.addresses:
        if address not in covered:
            rows.append({"address": address, "reason": exc.reason})
    if not rows:
        rows.append({"address": target, "reason": exc.reason})
    return rows


class QualityService:
    def __init__(self, store: SnapshotStore,
                 fetcher_factory: Callable[[str], object] = Fetcher):
        self.store = store
        self.fetcher_factory = fetcher_factory

    def check(self, url: str) -> CheckResult:
        try:
            target = validate_target(url)
        except InvalidTargetURL as exc:
            return CheckResult(ok=False, list_url=url, message=str(exc))

        fetcher = self.fetcher_factory(target)
        try:
            notices, list_pages = CollectionSession(fetcher=fetcher,
                                                    list_url=target).collect()
        except CollectionAnomaly as exc:
            previous = self.store.mark_failed(target, exc.reason, exc.addresses)
            return CheckResult(
                ok=False,
                list_url=target,
                anomalies=(_detail_rows(exc, target)),
                previous_snapshot_stale=previous is not None,
                message=exc.reason,
            )
        except Exception as exc:  # noqa: BLE001 - never publish on unknown errors
            reason = f"采集过程异常：{type(exc).__name__}"
            previous = self.store.mark_failed(target, reason, [target])
            return CheckResult(
                ok=False,
                list_url=target,
                anomalies=[{"address": target, "reason": reason}],
                previous_snapshot_stale=previous is not None,
                message=reason,
            )

        snapshot = self.store.save(target, notices)
        return CheckResult(
            ok=True,
            list_url=target,
            notices=notices,
            list_pages=list_pages,
            collected_at=snapshot.collected_at,
            message=("已发布完整公告清单。" if notices else "列表明确为空，已发布空清单。"),
        )

    def view(self, url: str) -> Snapshot | None:
        return self.store.load(url)
