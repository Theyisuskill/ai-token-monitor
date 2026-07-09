"""Adapter contract: how a tool's local logs become UsageRecords."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, ClassVar

from ..models import UsageRecord


class Adapter(ABC):
    """One AI tool. Implementations are pure parsers: no I/O beyond the lines
    handed to them, no state. The daemon owns file tailing and offsets, so a
    correct adapter only needs three methods.
    """

    #: Unique key; also the ``adapters.<name>`` section in config.yaml.
    name: ClassVar[str]

    def __init__(self, settings: dict[str, Any]):
        self.settings = settings

    @abstractmethod
    def roots(self) -> list[Path]:
        """Directories to watch recursively."""

    @abstractmethod
    def matches(self, path: Path) -> bool:
        """Whether a file under one of the roots belongs to this adapter."""

    @abstractmethod
    def parse(self, path: Path, lines: Iterable[str]) -> Iterator[UsageRecord]:
        """Turn complete log lines into UsageRecords.

        Must be resilient: skip malformed lines, never raise. ``dedup_key``
        must be derivable from the line itself (message id, uuid, ...) so
        reparsing a file is idempotent.
        """
