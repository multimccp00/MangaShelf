from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Chapter:
    name: str
    path: str
    images: list[str]

    @property
    def page_count(self) -> int:
        return len(self.images)
