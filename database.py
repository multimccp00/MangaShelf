from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

APP_DIR = Path.home() / ".mangashelf"
DB_PATH = APP_DIR / "mangashelf.db"

READING_STATUSES = ["Not Started", "Reading", "Completed", "On Hold", "Dropped", "Planned to Read"]
DEFAULT_GENRES = [
    "Action",
    "Adventure",
    "Comedy",
    "Drama",
    "Fantasy",
    "Horror",
    "Mystery",
    "Romance",
    "Sci-Fi",
    "Slice of Life",
    "Sports",
    "Supernatural",
    "Thriller",
]


class Database:
    def __init__(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(DB_PATH)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        # Read-heavy workload: WAL gives faster reads, NORMAL sync is crash-safe
        # under WAL, and a larger page cache keeps the whole working set in memory.
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.execute("PRAGMA cache_size = -8000")  # ~8 MB
        self.conn.execute("PRAGMA temp_store = MEMORY")
        self._create_tables()
        self._migrate_schema()
        self._create_indexes()
        self._seed_default_genres()

    def close(self) -> None:
        self.conn.close()

    def _create_tables(self) -> None:
        script = """
        CREATE TABLE IF NOT EXISTS libraries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT UNIQUE NOT NULL,
            added_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS series (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            library_id INTEGER REFERENCES libraries(id),
            folder_path TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            series_name TEXT,
            author TEXT,
            language TEXT,
            status TEXT DEFAULT 'Not Started',
            rating INTEGER DEFAULT 0,
            favorite INTEGER DEFAULT 0,
            notes TEXT,
            date_added DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_read DATETIME,
            last_chapter TEXT,
            last_page INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS genres (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        );

        CREATE TABLE IF NOT EXISTS series_genres (
            series_id INTEGER REFERENCES series(id),
            genre_id INTEGER REFERENCES genres(id),
            PRIMARY KEY (series_id, genre_id)
        );

        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            series_id INTEGER REFERENCES series(id),
            tag TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS parodies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            series_id INTEGER REFERENCES series(id),
            parody TEXT NOT NULL
        );
        """
        self.conn.executescript(script)
        self.conn.commit()

    def _create_indexes(self) -> None:
        # Indexes for the columns used in get_series_list() filters/sorts and the
        # bulk tag/genre loads. IF NOT EXISTS keeps this idempotent across runs.
        script = """
        CREATE INDEX IF NOT EXISTS idx_series_library_id ON series(library_id);
        CREATE INDEX IF NOT EXISTS idx_series_status     ON series(status);
        CREATE INDEX IF NOT EXISTS idx_series_favorite   ON series(favorite);
        CREATE INDEX IF NOT EXISTS idx_series_last_read  ON series(last_read);
        CREATE INDEX IF NOT EXISTS idx_series_date_added ON series(date_added);
        CREATE INDEX IF NOT EXISTS idx_series_title_nocase ON series(title COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS idx_tags_series_id    ON tags(series_id);
        CREATE INDEX IF NOT EXISTS idx_tags_tag          ON tags(tag);
        CREATE INDEX IF NOT EXISTS idx_parodies_series_id ON parodies(series_id);
        CREATE INDEX IF NOT EXISTS idx_parodies_parody    ON parodies(parody);
        CREATE INDEX IF NOT EXISTS idx_sg_series_id      ON series_genres(series_id);
        CREATE INDEX IF NOT EXISTS idx_sg_genre_id       ON series_genres(genre_id);
        """
        self.conn.executescript(script)
        self.conn.commit()

    def _migrate_schema(self) -> None:
        # --- libraries table: name + privacy/default flags for multi-library ---
        lib_cols = {
            str(row["name"])
            for row in self.conn.execute("PRAGMA table_info(libraries)").fetchall()
        }
        lib_changed = False
        if "name" not in lib_cols:
            # Display name; defaults to the folder's basename on first use.
            self.conn.execute("ALTER TABLE libraries ADD COLUMN name TEXT")
            lib_changed = True
        if "private" not in lib_cols:
            # 1 = private: switching INTO it can require a confirm.
            self.conn.execute("ALTER TABLE libraries ADD COLUMN private INTEGER DEFAULT 0")
            lib_changed = True
        if "is_default" not in lib_cols:
            # The library the app opens to when "start on safe library" is set.
            self.conn.execute("ALTER TABLE libraries ADD COLUMN is_default INTEGER DEFAULT 0")
            lib_changed = True
        if lib_changed:
            # Backfill name from the folder basename for existing rows.
            for row in self.conn.execute("SELECT id, path FROM libraries").fetchall():
                if not row["name"]:
                    base = Path(str(row["path"])).name or str(row["path"])
                    self.conn.execute("UPDATE libraries SET name=? WHERE id=?", (base, row["id"]))
            self.conn.commit()

        # Idempotent: ensure exactly one default library exists if any libraries
        # do (runs every startup so pre-existing rows without a default get one).
        try:
            have_default = self.conn.execute(
                "SELECT COUNT(*) FROM libraries WHERE is_default=1"
            ).fetchone()[0]
            if not have_default:
                first = self.conn.execute(
                    "SELECT id FROM libraries ORDER BY id LIMIT 1"
                ).fetchone()
                if first:
                    self.conn.execute("UPDATE libraries SET is_default=1 WHERE id=?", (first["id"],))
                    self.conn.commit()
        except Exception:
            pass

        columns = {
            str(row["name"])
            for row in self.conn.execute("PRAGMA table_info(series)").fetchall()
        }
        changed = False
        if "series_name" not in columns:
            self.conn.execute("ALTER TABLE series ADD COLUMN series_name TEXT")
            changed = True
        if "language" not in columns:
            self.conn.execute("ALTER TABLE series ADD COLUMN language TEXT")
            changed = True
        if "last_page" not in columns:
            self.conn.execute("ALTER TABLE series ADD COLUMN last_page INTEGER DEFAULT 0")
            changed = True
        if "nhentai_id" in columns and "external_id" not in columns:
            # Legacy column rename: preserve any existing values.
            self.conn.execute("ALTER TABLE series RENAME COLUMN nhentai_id TO external_id")
            columns = {
                str(row["name"])
                for row in self.conn.execute("PRAGMA table_info(series)").fetchall()
            }
            changed = True
        if "external_id" not in columns:
            self.conn.execute("ALTER TABLE series ADD COLUMN external_id INTEGER")
            changed = True
        if "available" not in columns:
            # Soft-delete / reachability flag. 1 = folder present, 0 = folder
            # currently missing. We never hard-delete on a transient stat() miss
            # (e.g. an offline network/removable drive) — we only flag.
            self.conn.execute("ALTER TABLE series ADD COLUMN available INTEGER DEFAULT 1")
            changed = True
        if "fresh_chapters" not in columns:
            # 1 = a re-sync downloaded new chapters while the reader was already
            # caught up (had read to the end of what existed). Drives the landing
            # screen's Continue Reading priority; cleared on the next progress save.
            self.conn.execute("ALTER TABLE series ADD COLUMN fresh_chapters INTEGER DEFAULT 0")
            changed = True
        if changed:
            self.conn.commit()

    def _seed_default_genres(self) -> None:
        for genre in DEFAULT_GENRES:
            self.conn.execute("INSERT OR IGNORE INTO genres(name) VALUES (?)", (genre,))
        self.conn.commit()

    def add_library(self, path: str, name: str | None = None) -> int:
        path = str(Path(path).resolve())
        nm = (name or Path(path).name or path).strip()
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO libraries(path, name) VALUES (?, ?)", (path, nm)
        )
        self.conn.commit()
        if cur.lastrowid:
            # First library added becomes the default automatically.
            if self.conn.execute("SELECT COUNT(*) FROM libraries").fetchone()[0] == 1:
                self.conn.execute("UPDATE libraries SET is_default=1 WHERE id=?", (cur.lastrowid,))
                self.conn.commit()
            return int(cur.lastrowid)
        row = self.conn.execute("SELECT id FROM libraries WHERE path = ?", (path,)).fetchone()
        return int(row["id"])

    def get_libraries(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, path, name, private, is_default, added_at FROM libraries ORDER BY name COLLATE NOCASE"
        ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["private"] = bool(d.get("private", 0))
            d["is_default"] = bool(d.get("is_default", 0))
            d["name"] = d.get("name") or Path(str(d.get("path") or "")).name
            out.append(d)
        return out

    def update_library(self, library_id: int, name: str | None = None,
                       private: bool | None = None) -> None:
        sets, params = [], []
        if name is not None:
            sets.append("name=?"); params.append(name.strip() or "Library")
        if private is not None:
            sets.append("private=?"); params.append(1 if private else 0)
        if not sets:
            return
        params.append(library_id)
        self.conn.execute(f"UPDATE libraries SET {', '.join(sets)} WHERE id=?", params)
        self.conn.commit()

    def set_default_library(self, library_id: int) -> None:
        """Exactly one library is the default. Clears the flag on all others."""
        self.conn.execute("UPDATE libraries SET is_default=0")
        self.conn.execute("UPDATE libraries SET is_default=1 WHERE id=?", (library_id,))
        self.conn.commit()

    def upsert_series(self, library_id: int, folder_path: str, title: str) -> int:
        folder_path = str(Path(folder_path).resolve())
        self.conn.execute(
            """
            INSERT INTO series(library_id, folder_path, title, available)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(folder_path) DO UPDATE SET
                library_id = excluded.library_id,
                title = COALESCE(NULLIF(series.title, ''), excluded.title),
                available = 1
            """,
            (library_id, folder_path, title),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT id FROM series WHERE folder_path = ?", (folder_path,)).fetchone()
        return int(row["id"])

    def series_id_for_folder(self, folder_path: str) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM series WHERE folder_path = ?", (folder_path,)
        ).fetchone()
        return int(row["id"]) if row else None

    def series_by_external_id(self, external_id: str) -> dict[str, Any] | None:
        """Find an already-imported series by its origin id, for dedup."""
        row = self.conn.execute(
            """
            SELECT s.id, s.title, s.library_id, l.name AS library_name
            FROM series s LEFT JOIN libraries l ON l.id = s.library_id
            WHERE s.external_id = ?
            """,
            (external_id,),
        ).fetchone()
        return dict(row) if row else None

    def set_series_location(self, series_id: int, library_id: int, folder_path: str) -> None:
        """Reassign a series to another library AND point it at its new folder on
        disk. Used by the 'move to another library' feature after the folder has
        been physically moved. Keeps the row (id, progress, rating, etc.)."""
        self.conn.execute(
            "UPDATE series SET library_id=?, folder_path=? WHERE id=?",
            (library_id, folder_path, series_id),
        )
        self.conn.commit()

    def get_series_by_id(self, series_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT s.*, l.path as library_path
            FROM series s
            LEFT JOIN libraries l ON l.id = s.library_id
            WHERE s.id = ?
            """,
            (series_id,),
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["tags"] = self.get_tags_for_series(series_id)
        data["genres"] = self.get_genres_for_series(series_id)
        data["parodies"] = self.get_parodies_for_series(series_id)
        data["favorite"] = bool(data.get("favorite", 0))
        return data

    def update_series_metadata(
        self,
        series_id: int,
        title: str,
        series_name: str,
        author: str,
        status: str,
        rating: int,
        favorite: bool,
        notes: str,
        genres: list[str],
        tags: list[str],
        language: str = "",
        parodies: list[str] | None = None,
    ) -> None:
        def _txt(value: object) -> str:
            return str(value or "").strip()

        status = status if status in READING_STATUSES else "Not Started"
        rating = max(0, min(5, int(rating)))
        # One atomic transaction: the delete-then-reinsert of child rows must
        # never half-apply. `with self.conn:` commits on success and rolls back
        # on any exception, so a mid-sequence failure can't wipe tags/genres.
        with self.conn:
            self.conn.execute(
                """
                UPDATE series
                SET title = ?, series_name = ?, author = ?, language = ?, status = ?, rating = ?, favorite = ?, notes = ?
                WHERE id = ?
                """,
                (
                    _txt(title) or "Untitled",
                    _txt(series_name),
                    _txt(author),
                    _txt(language),
                    status,
                    rating,
                    int(favorite),
                    _txt(notes),
                    series_id,
                ),
            )

            self.conn.execute("DELETE FROM series_genres WHERE series_id = ?", (series_id,))
            for genre_name in sorted(set(g.strip() for g in genres if g.strip()), key=str.lower):
                self.conn.execute("INSERT OR IGNORE INTO genres(name) VALUES (?)", (genre_name,))
                row = self.conn.execute("SELECT id FROM genres WHERE name = ?", (genre_name,)).fetchone()
                self.conn.execute(
                    "INSERT OR IGNORE INTO series_genres(series_id, genre_id) VALUES (?, ?)",
                    (series_id, int(row["id"])),
                )

            self.conn.execute("DELETE FROM tags WHERE series_id = ?", (series_id,))
            for tag in sorted(set(t.strip() for t in tags if t.strip()), key=str.lower):
                self.conn.execute("INSERT INTO tags(series_id, tag) VALUES (?, ?)", (series_id, tag))

            # Parodies (source works this collaborates on / parodies). None = leave as-is.
            if parodies is not None:
                self.conn.execute("DELETE FROM parodies WHERE series_id = ?", (series_id,))
                for p in sorted(set(x.strip() for x in parodies if x.strip()), key=str.lower):
                    self.conn.execute("INSERT INTO parodies(series_id, parody) VALUES (?, ?)", (series_id, p))

    def set_favorite(self, series_id: int, favorite: bool) -> None:
        self.conn.execute("UPDATE series SET favorite = ? WHERE id = ?", (int(favorite), series_id))
        self.conn.commit()

    def set_status(self, series_id: int, status: str) -> None:
        if status not in READING_STATUSES:
            status = "Not Started"
        self.conn.execute("UPDATE series SET status = ? WHERE id = ?", (status, series_id))
        self.conn.commit()

    def update_last_read_progress(self, series_id: int, chapter_name: str, page_number: int) -> None:
        page_number = max(1, int(page_number))
        # Any progress save means the reader is back in this series, so the
        # "new chapters arrived while you were caught up" highlight is served.
        self.conn.execute(
            """
            UPDATE series
            SET last_read = CURRENT_TIMESTAMP,
                last_chapter = ?,
                last_page = ?,
                fresh_chapters = 0
            WHERE id = ?
            """,
            (chapter_name, page_number, series_id),
        )
        self.conn.commit()

    def set_fresh_chapters(self, series_id: int, fresh: bool) -> None:
        """Mark/unmark a series as having chapters newer than the reader's
        caught-up position (set by re-sync, cleared by the next progress save)."""
        self.conn.execute(
            "UPDATE series SET fresh_chapters = ? WHERE id = ?",
            (1 if fresh else 0, series_id),
        )
        self.conn.commit()

    def get_tags_for_series(self, series_id: int) -> list[str]:
        rows = self.conn.execute(
            "SELECT tag FROM tags WHERE series_id = ? ORDER BY tag COLLATE NOCASE",
            (series_id,),
        ).fetchall()
        return [row["tag"] for row in rows]

    def get_parodies_for_series(self, series_id: int) -> list[str]:
        rows = self.conn.execute(
            "SELECT parody FROM parodies WHERE series_id = ? ORDER BY parody COLLATE NOCASE",
            (series_id,),
        ).fetchall()
        return [row["parody"] for row in rows]

    def get_genres_for_series(self, series_id: int) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT g.name
            FROM series_genres sg
            JOIN genres g ON sg.genre_id = g.id
            WHERE sg.series_id = ?
            ORDER BY g.name COLLATE NOCASE
            """,
            (series_id,),
        ).fetchall()
        return [row["name"] for row in rows]

    def get_all_genres(self) -> list[str]:
        rows = self.conn.execute("SELECT name FROM genres ORDER BY name COLLATE NOCASE").fetchall()
        return [row["name"] for row in rows]

    def get_all_tags(self) -> list[str]:
        rows = self.conn.execute("SELECT DISTINCT tag FROM tags ORDER BY tag COLLATE NOCASE").fetchall()
        return [row["tag"] for row in rows]

    def get_all_parodies(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT parody FROM parodies ORDER BY parody COLLATE NOCASE"
        ).fetchall()
        return [row["parody"] for row in rows]

    def get_all_series_names(self) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT DISTINCT series_name
            FROM series
            WHERE TRIM(COALESCE(series_name, '')) != ''
            ORDER BY series_name COLLATE NOCASE
            """
        ).fetchall()
        return [row["series_name"] for row in rows]

    def replace_tags_for_series(self, series_id: int, tags: list[str]) -> None:
        clean_tags = sorted({str(t).strip() for t in tags if str(t).strip()}, key=str.lower)
        self.conn.execute("DELETE FROM tags WHERE series_id = ?", (series_id,))
        for tag in clean_tags:
            self.conn.execute("INSERT INTO tags(series_id, tag) VALUES (?, ?)", (series_id, tag))
        self.conn.commit()

    def get_series_list(
        self,
        search: str = "",
        filter_mode: str = "All",
        status_filter: str | None = None,
        sort_mode: str = "Title A-Z",
        tag_filter: str | None = None,
        series_filter: str | None = None,
        genre_filter: str | None = None,
        library_id: int | None = None,
        parody_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        where_parts: list[str] = []
        params: list[Any] = []

        if library_id is not None:
            where_parts.append("s.library_id = ?")
            params.append(library_id)

        if search.strip():
            where_parts.append("(LOWER(s.title) LIKE ? OR LOWER(COALESCE(s.series_name, '')) LIKE ?)")
            needle = f"%{search.strip().lower()}%"
            params.extend([needle, needle])

        if filter_mode == "Favorites":
            where_parts.append("s.favorite = 1")
        elif filter_mode == "By Status" and status_filter in READING_STATUSES:
            where_parts.append("s.status = ?")
            params.append(status_filter)

        if tag_filter:
            where_parts.append("EXISTS (SELECT 1 FROM tags t WHERE t.series_id = s.id AND t.tag = ?)")
            params.append(tag_filter)

        if parody_filter:
            where_parts.append("EXISTS (SELECT 1 FROM parodies p WHERE p.series_id = s.id AND p.parody = ?)")
            params.append(parody_filter)

        if series_filter:
            where_parts.append("s.series_name = ?")
            params.append(series_filter)

        if genre_filter:
            where_parts.append(
                "EXISTS (SELECT 1 FROM series_genres sg JOIN genres g ON g.id = sg.genre_id WHERE sg.series_id = s.id AND g.name = ?)"
            )
            params.append(genre_filter)

        where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

        order_sql = {
            "Title A-Z": "s.title COLLATE NOCASE ASC",
            "Title Z-A": "s.title COLLATE NOCASE DESC",
            "Last Read": "COALESCE(s.last_read, '1970-01-01') DESC",
            "Date Added": "COALESCE(s.date_added, '1970-01-01') DESC",
            "Rating": "s.rating DESC, s.title COLLATE NOCASE ASC",
        }.get(sort_mode, "s.title COLLATE NOCASE ASC")

        rows = self.conn.execute(
            f"""
            SELECT s.*, l.path AS library_path
            FROM series s
            LEFT JOIN libraries l ON s.library_id = l.id
            {where_sql}
            ORDER BY {order_sql}
            """,
            params,
        ).fetchall()

        if not rows:
            return []

        # Bulk-load tags and genres for all series in 2 queries instead of N*2 queries
        ids = [int(row["id"]) for row in rows]
        ph = ",".join("?" * len(ids))
        tag_rows = self.conn.execute(
            f"SELECT series_id, tag FROM tags WHERE series_id IN ({ph}) ORDER BY tag COLLATE NOCASE",
            ids,
        ).fetchall()
        tags_by_id: dict[int, list[str]] = {}
        for tr in tag_rows:
            tags_by_id.setdefault(int(tr["series_id"]), []).append(tr["tag"])

        genre_rows = self.conn.execute(
            f"""SELECT sg.series_id, g.name
                FROM series_genres sg JOIN genres g ON g.id = sg.genre_id
                WHERE sg.series_id IN ({ph})
                ORDER BY g.name COLLATE NOCASE""",
            ids,
        ).fetchall()
        genres_by_id: dict[int, list[str]] = {}
        for gr in genre_rows:
            genres_by_id.setdefault(int(gr["series_id"]), []).append(gr["name"])

        parody_rows = self.conn.execute(
            f"SELECT series_id, parody FROM parodies WHERE series_id IN ({ph}) ORDER BY parody COLLATE NOCASE",
            ids,
        ).fetchall()
        parodies_by_id: dict[int, list[str]] = {}
        for pr in parody_rows:
            parodies_by_id.setdefault(int(pr["series_id"]), []).append(pr["parody"])

        items: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            sid = int(data["id"])
            data["tags"] = tags_by_id.get(sid, [])
            data["genres"] = genres_by_id.get(sid, [])
            data["parodies"] = parodies_by_id.get(sid, [])
            data["favorite"] = bool(data.get("favorite", 0))
            items.append(data)
        return items

    # ---------------------------------------------------------------- Sidecar sync --

    def sync_series_from_sidecar(self, series_id: int, sc: dict[str, Any]) -> None:
        """Merge sidecar values into the DB row. Only fields present in the sidecar
        are updated — absent keys leave the existing DB value untouched."""
        updates: list[str] = []
        params: list[Any] = []

        if "title" in sc and str(sc["title"]).strip():
            updates.append("title=?")
            params.append(str(sc["title"]).strip())
        if "series" in sc:
            updates.append("series_name=?")
            params.append(str(sc["series"]).strip())
        if "author" in sc:
            updates.append("author=?")
            params.append(sc["author"])
        if "language" in sc:
            updates.append("language=?")
            params.append(str(sc["language"]).strip())
        if "external_id" in sc:
            # Stored as a "<source>:<id>" string (e.g. "mangadex:<uuid>"); keep it
            # verbatim. (Was int-cast back when this column was the numeric
            # nhentai_id — that nulled any non-numeric id.)
            ext_id = str(sc["external_id"]).strip()
            updates.append("external_id=?")
            params.append(ext_id or None)
        if "status" in sc:
            status = sc["status"] if sc["status"] in READING_STATUSES else "Not Started"
            updates.append("status=?")
            params.append(status)
        if "rating" in sc:
            updates.append("rating=?")
            params.append(max(0, min(5, int(sc["rating"]))))
        if "favorite" in sc:
            updates.append("favorite=?")
            params.append(int(bool(sc["favorite"])))
        if "notes" in sc:
            updates.append("notes=?")
            params.append(sc["notes"])
        if "last_chapter" in sc:
            updates.append("last_chapter=?")
            params.append(sc["last_chapter"])
        if "last_read" in sc:
            updates.append("last_read=COALESCE(?, last_read)")
            params.append(sc["last_read"])

        # Atomic: the metadata update + child-row delete/reinsert commit together
        # or not at all, so a failure can't leave the row updated but its tags
        # half-rewritten (or vice versa).
        with self.conn:
            if updates:
                params.append(series_id)
                self.conn.execute(
                    f"UPDATE series SET {', '.join(updates)} WHERE id=?",
                    params,
                )

            if "genres" in sc:
                self.conn.execute("DELETE FROM series_genres WHERE series_id=?", (series_id,))
                for g in sc["genres"]:
                    g = str(g).strip()
                    if not g:
                        continue
                    self.conn.execute("INSERT OR IGNORE INTO genres(name) VALUES(?)", (g,))
                    row = self.conn.execute("SELECT id FROM genres WHERE name=?", (g,)).fetchone()
                    if row:
                        self.conn.execute(
                            "INSERT OR IGNORE INTO series_genres(series_id, genre_id) VALUES(?,?)",
                            (series_id, int(row["id"])),
                        )
            if "tags" in sc:
                self.conn.execute("DELETE FROM tags WHERE series_id=?", (series_id,))
                for t in sc["tags"]:
                    t = str(t).strip()
                    if t:
                        self.conn.execute("INSERT INTO tags(series_id, tag) VALUES(?,?)", (series_id, t))
            if "parodies" in sc:
                self.conn.execute("DELETE FROM parodies WHERE series_id=?", (series_id,))
                for p in sc["parodies"]:
                    p = str(p).strip()
                    if p:
                        self.conn.execute("INSERT INTO parodies(series_id, parody) VALUES(?,?)", (series_id, p))

    # --------------------------------------------------------------- Folder ops --

    def rename_series_folder(self, series_id: int, new_path: str) -> None:
        """Update stored folder path after an on-disk rename."""
        new_path = str(Path(new_path).resolve())
        self.conn.execute("UPDATE series SET folder_path=? WHERE id=?", (new_path, series_id))
        self.conn.commit()

    def series_id_for_path(self, folder_path: str) -> int | None:
        """Return the id of the series whose resolved folder_path matches, if any.
        Used to detect collisions before a rename hits the UNIQUE constraint."""
        resolved = str(Path(folder_path).resolve())
        row = self.conn.execute(
            "SELECT id FROM series WHERE folder_path = ?", (resolved,)
        ).fetchone()
        return int(row["id"]) if row else None

    def delete_series(self, series_id: int) -> None:
        """Remove all DB records for a series (does NOT touch disk). Deletes every
        child table (genres/tags/parodies) so no orphan rows linger to be inherited
        by a future series that reuses the id. Atomic via `with self.conn:`."""
        with self.conn:
            self.conn.execute("DELETE FROM series_genres WHERE series_id=?", (series_id,))
            self.conn.execute("DELETE FROM tags WHERE series_id=?", (series_id,))
            self.conn.execute("DELETE FROM parodies WHERE series_id=?", (series_id,))
            self.conn.execute("DELETE FROM series WHERE id=?", (series_id,))

    def remove_library(self, library_id: int) -> int:
        """Remove a library and every series record in it from the DB — the folder
        and its files on disk are NOT touched (re-adding the folder later rebuilds
        everything except hand-edited metadata). Children first, same as
        delete_series, all in one transaction. Returns how many series were removed."""
        ids = [int(r["id"]) for r in self.conn.execute(
            "SELECT id FROM series WHERE library_id=?", (library_id,)
        ).fetchall()]
        with self.conn:
            if ids:
                ph = ",".join("?" * len(ids))
                self.conn.execute(f"DELETE FROM series_genres WHERE series_id IN ({ph})", ids)
                self.conn.execute(f"DELETE FROM tags WHERE series_id IN ({ph})", ids)
                self.conn.execute(f"DELETE FROM parodies WHERE series_id IN ({ph})", ids)
                self.conn.execute(f"DELETE FROM series WHERE id IN ({ph})", ids)
            self.conn.execute("DELETE FROM libraries WHERE id=?", (library_id,))
        return len(ids)

    def prune_stale_series(self, library_id: int, valid_folders: "set[str]") -> int:
        """Delete rows in `library_id` whose folder is NOT in `valid_folders` —
        i.e. a folder that still exists on disk but is no longer a series (e.g. the
        library root itself once its loose content moved into subfolders, or a
        folder that became a container of sub-series). `purge_missing_series` only
        handles folders that vanished; this handles folders that are no longer
        series. Compares on resolved paths. Returns the number removed.

        IMPORTANT: only called right after a successful scan of this library, so
        `valid_folders` is the authoritative set of real series for it.
        """
        valid = set()
        for f in valid_folders:
            try:
                valid.add(str(Path(f).resolve()))
            except OSError:
                valid.add(str(f))
        rows = self.conn.execute(
            "SELECT id, folder_path FROM series WHERE library_id=?", (library_id,)
        ).fetchall()
        removed = 0
        for row in rows:
            try:
                rp = str(Path(str(row["folder_path"])).resolve())
            except OSError:
                rp = str(row["folder_path"])
            if rp not in valid:
                self.delete_series(int(row["id"]))
                removed += 1
        return removed

    def purge_missing_series(self) -> int:
        """Flag series whose folder is genuinely gone — but NEVER when the whole
        library drive is just offline (unmounted/sleeping/network blip).

        A naive `if not folder.exists(): delete` is catastrophic: when a removable
        or network drive is temporarily inaccessible, EVERY series on it reports
        missing and a single rescan would wipe the user's metadata permanently.

        So we only consider a series missing if its LIBRARY ROOT is reachable but
        the series folder underneath it is not. And we SOFT-delete (available=0)
        rather than DROP rows — metadata is preserved and the series reappears
        (available=1) automatically once the folder comes back. Hard removal is an
        explicit user action (delete_series), never an automatic side effect.

        Returns the number of series newly flagged unavailable.
        """
        # Resolve which library roots are currently reachable.
        lib_rows = self.conn.execute("SELECT id, path FROM libraries").fetchall()
        reachable_root: dict[int, bool] = {}
        for lib in lib_rows:
            try:
                reachable_root[int(lib["id"])] = Path(str(lib["path"])).exists()
            except OSError:
                reachable_root[int(lib["id"])] = False

        rows = self.conn.execute(
            "SELECT id, folder_path, library_id, available FROM series"
        ).fetchall()
        flagged = 0
        for row in rows:
            lib_id = row["library_id"]
            # If the library root is offline (or unknown), treat the series as
            # simply inaccessible — do not touch its availability either way.
            if lib_id is None or not reachable_root.get(int(lib_id), False):
                continue
            try:
                exists = Path(str(row["folder_path"])).exists()
            except OSError:
                # Stat error on a reachable root — be conservative, don't flag.
                continue
            new_avail = 1 if exists else 0
            if int(row["available"] or 0) != new_avail:
                self.conn.execute(
                    "UPDATE series SET available=? WHERE id=?",
                    (new_avail, int(row["id"])),
                )
                if new_avail == 0:
                    flagged += 1
        self.conn.commit()
        return flagged
