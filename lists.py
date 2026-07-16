from __future__ import annotations

import json
from pathlib import Path

APP_DIR = Path.home() / ".mangashelf"
LISTS_PATH = APP_DIR / "lists.json"

_DEFAULT_GENRES = [
    "Action", "Adventure", "Comedy", "Drama", "Fantasy",
    "Horror", "Mystery", "Romance", "Sci-Fi", "Slice of Life",
    "Sports", "Supernatural", "Thriller",
]

_DEFAULT_TAGS = [
    "Anthology",
    "College",
    "Cooking",
    "Cyberpunk",
    "Delinquents",
    "Detective",
    "Gore",
    "Historical",
    "Isekai",
    "Magic",
    "Martial Arts",
    "Mecha",
    "Medical",
    "Military",
    "Monsters",
    "Office Workers",
    "One-shot",
    "Political",
    "Post-Apocalyptic",
    "Psychological",
    "Reincarnation",
    "School Life",
    "Super Power",
    "Survival",
    "Time Travel",
]


class GlobalLists:
    """Persistent, ever-growing list of genres and tags stored in ~/.mangashelf/lists.json.
    Acts as the shared vocabulary for the app and any companion app (phone, etc.)
    that reads the same lists.json file or a copy of it.
    """

    def __init__(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self._genres: list[str] = []
        self._tags: list[str] = []
        self.load()

    # ------------------------------------------------------------------ IO --

    def load(self) -> None:
        if LISTS_PATH.exists():
            try:
                data = json.loads(LISTS_PATH.read_text(encoding="utf-8"))
                self._genres = sorted(
                    {str(g).strip() for g in data.get("genres", []) if str(g).strip()},
                    key=str.lower,
                )
                self._tags = sorted(
                    {str(t).strip() for t in data.get("tags", []) if str(t).strip()},
                    key=str.lower,
                )
                return
            except (json.JSONDecodeError, OSError):
                pass
        self._genres = sorted(_DEFAULT_GENRES, key=str.lower)
        self._tags = sorted(_DEFAULT_TAGS, key=str.lower)
        self.save()

    def save(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        LISTS_PATH.write_text(
            json.dumps({"genres": self._genres, "tags": self._tags}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # ------------------------------------------------------------ Read API --

    def get_genres(self) -> list[str]:
        return list(self._genres)

    def get_tags(self) -> list[str]:
        return list(self._tags)

    # ----------------------------------------------------------- Write API --

    def add_genres(self, genres: list[str]) -> None:
        existing = {g.lower() for g in self._genres}
        changed = False
        for g in genres:
            g = g.strip()
            if g and g.lower() not in existing:
                self._genres.append(g)
                existing.add(g.lower())
                changed = True
        if changed:
            self._genres.sort(key=str.lower)
            self.save()

    def add_tags(self, tags: list[str]) -> None:
        existing = {t.lower() for t in self._tags}
        changed = False
        for t in tags:
            t = t.strip()
            if t and t.lower() not in existing:
                self._tags.append(t)
                existing.add(t.lower())
                changed = True
        if changed:
            self._tags.sort(key=str.lower)
            self.save()

    def remove_genre(self, genre: str) -> None:
        needle = genre.strip().lower()
        if not needle:
            return
        updated = [existing for existing in self._genres if existing.lower() != needle]
        if len(updated) != len(self._genres):
            self._genres = updated
            self.save()

    def remove_tag(self, tag: str) -> None:
        needle = tag.strip().lower()
        if not needle:
            return
        updated = [existing for existing in self._tags if existing.lower() != needle]
        if len(updated) != len(self._tags):
            self._tags = updated
            self.save()
