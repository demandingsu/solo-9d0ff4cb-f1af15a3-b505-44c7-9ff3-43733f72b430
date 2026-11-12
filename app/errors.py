"""Error types used by the recall-collection quality checker."""

from __future__ import annotations


class InvalidTargetURL(ValueError):
    """The address typed by the operator is not a usable http(s) URL."""


class CollectionAnomaly(Exception):
    """A completeness or consistency problem that must block publication.

    ``addresses`` contains the concrete page URL(s) that the operator has to
    inspect.  ``reason`` is a human readable Chinese explanation.
    """

    def __init__(self, reason: str, addresses: list[str] | tuple[str, ...],
                 details: list | None = None):
        super().__init__(reason)
        self.reason = reason
        seen: list[str] = []
        for address in addresses:
            if address and address not in seen:
                seen.append(address)
        self.addresses: list[str] = seen
        self.details = list(details or [])
