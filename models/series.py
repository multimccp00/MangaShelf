from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class Series:
    id: int | None
    library_id: int | None
    folder_path: str
    title: str
    author: str = ""
    status: str = "Not Started"
    rating: int = 0
    favorite: bool = False
    notes: str = ""
    date_added: datetime | None = None
    last_read: datetime | None = None
    last_chapter: str | None = None
    tags: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)

    @property
    def folder_name(self) -> str:
        return Path(self.folder_path).name

    @property
    def is_available(self) -> bool:
        return Path(self.folder_path).exists()
