#!/usr/bin/env python3
"""
yt-archive: a thin wrapper around yt-dlp that catalogues downloads in SQLite.
"""

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS collections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    url         TEXT    NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS videos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    extractor       TEXT    NOT NULL,
    video_id        TEXT    NOT NULL,
    collection_id   INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    title           TEXT,
    uploader        TEXT,
    uploader_id     TEXT,
    upload_date     TEXT,
    duration        REAL,
    resolution      TEXT,
    file_path       TEXT,
    status          TEXT    NOT NULL DEFAULT 'downloaded',
    downloaded_at   TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(extractor, video_id, collection_id)
);
"""


def _db_path() -> Path:
    default = Path.home() / ".local" / "share" / "yt-archive" / "db.sqlite"
    return Path(os.environ.get("YT_ARCHIVE_DB", str(default)))


def get_db() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = Path(
    os.environ.get(
        "YT_ARCHIVE_CONFIG",
        str(Path.home() / ".config" / "yt-archive" / "config.json"),
    )
)

# The config file is plain JSON whose keys are yt-dlp YoutubeDL options,
# passed straight through.  See https://github.com/yt-dlp/yt-dlp#embedding-yt-dlp
#
# Example config.json:
# {
#   "outtmpl": "%(uploader)s (%(uploader_id)s)/%(upload_date)s - %(title)s - (%(duration)ss) [%(resolution)s] [%(id)s].%(ext)s",
#   "paths": {"home": "/mnt/archive/youtube", "temp": "/home/archiver/.yt-dlp"},
#   "merge_output_format": "mkv",
#   "writesubtitles": true,
#   "allsubtitles": true,
#   "writedescription": true,
#   "writethumbnail": true,
#   "cookiefile": "cookies.txt",
#   "sleep_interval": 2,
#   "max_sleep_interval": 6,
#   "verbose": true,
#   "postprocessors": [
#     {"key": "FFmpegSubtitlesConvertor", "format": "srt"},
#     {"key": "FFmpegMetadata"}
#   ]
# }


def load_config(path: Path | None = None) -> dict:
    p = path or DEFAULT_CONFIG_PATH
    if not p.exists():
        return {}
    with open(p) as f:
        return json.load(f)


def config_to_ytdlp_opts(cfg: dict) -> dict:
    """Return a yt-dlp options dict from the config. Keys pass straight through."""
    opts = dict(cfg)
    # Always ignore errors so one bad video doesn't abort the whole run
    opts.setdefault("ignoreerrors", True)
    return opts


# ---------------------------------------------------------------------------
# Sync logic
# ---------------------------------------------------------------------------


def _write_temp_archive(db: sqlite3.Connection, collection_id: int) -> Path:
    """Write a temp archive file with all known video IDs for a collection."""
    rows = db.execute(
        "SELECT extractor, video_id FROM videos WHERE collection_id = ?",
        (collection_id,),
    ).fetchall()

    tmp = tempfile.NamedTemporaryFile(
        mode="w", prefix="yt-archive-", suffix=".txt", delete=False
    )
    for row in rows:
        tmp.write(f"{row['extractor']} {row['video_id']}\n")
    tmp.flush()
    tmp.close()
    return Path(tmp.name)


def _read_archive_file(path: Path) -> set[tuple[str, str]]:
    """Read an archive file and return a set of (extractor, video_id) tuples."""
    entries = set()
    if not path.exists():
        return entries
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) == 2:
                entries.add((parts[0], parts[1]))
    return entries


def _insert_video(
    db: sqlite3.Connection,
    collection_id: int,
    extractor: str,
    video_id: str,
    info: dict | None = None,
    status: str = "downloaded",
):
    """Insert a video record, ignoring duplicates."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    info = info or {}
    db.execute(
        """
        INSERT OR IGNORE INTO videos
            (extractor, video_id, collection_id, title, uploader, uploader_id,
             upload_date, duration, resolution, file_path, status, downloaded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            extractor,
            video_id,
            collection_id,
            info.get("title"),
            info.get("uploader"),
            info.get("uploader_id"),
            info.get("upload_date"),
            info.get("duration"),
            info.get("resolution"),
            info.get("filepath") or info.get("_filename"),
            status,
            now,
        ),
    )
    db.commit()


def sync_collection(
    db: sqlite3.Connection, collection_id: int, collection_url: str, cfg: dict
):
    """Download new videos for a collection and catalogue them."""
    from yt_dlp import YoutubeDL

    archive_path = _write_temp_archive(db, collection_id)
    known_before = _read_archive_file(archive_path)

    opts = config_to_ytdlp_opts(cfg)
    opts["download_archive"] = str(archive_path)

    # postprocessor_hooks stashes the info_dict keyed by filepath after each
    # PP stage.  post_hooks fires once after all post-processing AND the file
    # move to the final destination (NFS), so the DB insert only happens when
    # the file is safely in place.
    pending_info: dict[str, dict] = {}

    def pp_hook(d):
        """Stash info_dict so post_hook can use it."""
        if d.get("status") == "finished":
            info = d.get("info_dict", {})
            filepath = info.get("filepath")
            if filepath:
                pending_info[filepath] = info

    def post_hook(filepath):
        """Called after all post-processing and file move. Insert into DB."""
        info = pending_info.pop(filepath, {})
        extractor = info.get("extractor_key", info.get("ie_key", "youtube")).lower()
        vid = info.get("id", "")
        if vid:
            _insert_video(
                db, collection_id, extractor, vid, info=info, status="downloaded"
            )

    opts["postprocessor_hooks"] = [pp_hook]
    opts["post_hooks"] = [post_hook]

    try:
        with YoutubeDL(opts) as ydl:
            ydl.download([collection_url])
    finally:
        # Also pick up anything the hooks missed by diffing the archive file
        known_after = _read_archive_file(archive_path)
        new_entries = known_after - known_before
        for extractor, vid in new_entries:
            _insert_video(db, collection_id, extractor, vid, status="downloaded")

        archive_path.unlink(missing_ok=True)


def fetch_metadata_for_collection(
    db: sqlite3.Connection, collection_id: int, cfg: dict
):
    """Backfill metadata for videos that are missing it (e.g. imported from archive)."""
    from yt_dlp import YoutubeDL

    rows = db.execute(
        "SELECT extractor, video_id FROM videos WHERE collection_id = ? AND title IS NULL",
        (collection_id,),
    ).fetchall()

    if not rows:
        print("  No videos missing metadata.")
        return

    opts = config_to_ytdlp_opts(cfg)
    # We only want metadata, not downloads
    opts["skip_download"] = True
    opts["ignoreerrors"] = True
    # Don't write files for metadata-only runs
    opts.pop("writedescription", None)
    opts.pop("writethumbnail", None)
    opts.pop("writesubtitles", None)
    opts.pop("allsubtitles", None)
    opts.pop("postprocessors", None)

    with YoutubeDL(opts) as ydl:
        for row in rows:
            url = f"https://www.youtube.com/watch?v={row['video_id']}"
            print(f"  Fetching metadata for {row['video_id']}...")
            try:
                info = ydl.extract_info(url, download=False)
                if info:
                    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    db.execute(
                        """
                        UPDATE videos SET
                            title = ?, uploader = ?, uploader_id = ?,
                            upload_date = ?, duration = ?, resolution = ?
                        WHERE extractor = ? AND video_id = ? AND collection_id = ?
                        """,
                        (
                            info.get("title"),
                            info.get("uploader"),
                            info.get("uploader_id"),
                            info.get("upload_date"),
                            info.get("duration"),
                            info.get("resolution"),
                            row["extractor"],
                            row["video_id"],
                            collection_id,
                        ),
                    )
                    db.commit()
            except Exception as e:
                print(f"  Failed to fetch metadata for {row['video_id']}: {e}")


# ---------------------------------------------------------------------------
# Import existing archive
# ---------------------------------------------------------------------------


def import_archive(db: sqlite3.Connection, archive_path: Path, collection_id: int):
    """Import an existing yt-dlp archive.txt into the database."""
    entries = _read_archive_file(archive_path)
    count = 0
    for extractor, video_id in entries:
        try:
            db.execute(
                """
                INSERT OR IGNORE INTO videos (extractor, video_id, collection_id, status)
                VALUES (?, ?, ?, 'imported')
                """,
                (extractor, video_id, collection_id),
            )
            count += 1
        except sqlite3.IntegrityError:
            pass
    db.commit()
    print(f"Imported {count} entries into the database.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cmd_add(args):
    db = get_db()
    try:
        db.execute(
            "INSERT INTO collections (name, url) VALUES (?, ?)",
            (args.name, args.url),
        )
        db.commit()
        print(f"Added collection '{args.name}'.")
    except sqlite3.IntegrityError:
        print(f"Error: collection '{args.name}' already exists.", file=sys.stderr)
        sys.exit(1)
    finally:
        db.close()


def cmd_list(args):
    db = get_db()
    rows = db.execute(
        "SELECT c.*, COUNT(v.id) as video_count "
        "FROM collections c LEFT JOIN videos v ON v.collection_id = c.id "
        "GROUP BY c.id ORDER BY c.name"
    ).fetchall()
    db.close()

    if not rows:
        print("No collections.")
        return

    for r in rows:
        enabled = "enabled" if r["enabled"] else "disabled"
        print(f"  [{r['id']}] {r['name']} ({enabled}) — {r['video_count']} videos")
        print(f"      {r['url']}")


def cmd_remove(args):
    db = get_db()
    cur = db.execute("DELETE FROM collections WHERE name = ?", (args.name,))
    db.commit()
    if cur.rowcount:
        print(f"Removed collection '{args.name}' and its videos.")
    else:
        print(f"Error: collection '{args.name}' not found.", file=sys.stderr)
        sys.exit(1)
    db.close()


def _resolve_collections(db, args) -> list[sqlite3.Row]:
    if args.all:
        return db.execute("SELECT * FROM collections WHERE enabled = 1").fetchall()
    elif args.name:
        row = db.execute(
            "SELECT * FROM collections WHERE name = ?", (args.name,)
        ).fetchone()
        if not row:
            print(f"Error: collection '{args.name}' not found.", file=sys.stderr)
            sys.exit(1)
        return [row]
    else:
        print("Error: provide a collection name or --all.", file=sys.stderr)
        sys.exit(1)


def cmd_sync(args):
    db = get_db()
    cfg = load_config(Path(args.config) if args.config else None)
    collections = _resolve_collections(db, args)

    for col in collections:
        print(f"Syncing '{col['name']}' ({col['url']})...")
        sync_collection(db, col["id"], col["url"], cfg)
        print(f"Done syncing '{col['name']}'.")

    db.close()


def cmd_fetch_metadata(args):
    db = get_db()
    cfg = load_config(Path(args.config) if args.config else None)
    collections = _resolve_collections(db, args)

    for col in collections:
        print(f"Fetching metadata for '{col['name']}'...")
        fetch_metadata_for_collection(db, col["id"], cfg)

    db.close()


def cmd_import(args):
    db = get_db()
    row = db.execute(
        "SELECT * FROM collections WHERE name = ?", (args.collection,)
    ).fetchone()
    if not row:
        print(f"Error: collection '{args.collection}' not found.", file=sys.stderr)
        print("Create it first with: yt-archive add <name> <url>")
        sys.exit(1)
    path = Path(args.file)
    if not path.exists():
        print(f"Error: file '{path}' not found.", file=sys.stderr)
        sys.exit(1)

    import_archive(db, path, row["id"])
    db.close()


def cmd_status(args):
    db = get_db()
    total_collections = db.execute("SELECT COUNT(*) as c FROM collections").fetchone()[
        "c"
    ]
    total_videos = db.execute("SELECT COUNT(*) as c FROM videos").fetchone()["c"]
    with_metadata = db.execute(
        "SELECT COUNT(*) as c FROM videos WHERE title IS NOT NULL"
    ).fetchone()["c"]
    imported = db.execute(
        "SELECT COUNT(*) as c FROM videos WHERE status = 'imported'"
    ).fetchone()["c"]

    print(f"Database: {_db_path()}")
    print(f"Collections: {total_collections}")
    print(
        f"Videos: {total_videos} ({with_metadata} with metadata, {imported} imported)"
    )

    rows = db.execute(
        "SELECT c.name, c.enabled, COUNT(v.id) as video_count, "
        "SUM(CASE WHEN v.title IS NOT NULL THEN 1 ELSE 0 END) as with_meta "
        "FROM collections c LEFT JOIN videos v ON v.collection_id = c.id "
        "GROUP BY c.id ORDER BY c.name"
    ).fetchall()
    if rows:
        print()
        for r in rows:
            enabled = "✓" if r["enabled"] else "✗"
            print(
                f"  {enabled} {r['name']}: {r['video_count']} videos "
                f"({r['with_meta']} with metadata)"
            )

    db.close()


def main():
    parser = argparse.ArgumentParser(
        prog="yt-archive",
        description="A thin wrapper around yt-dlp that catalogues downloads in SQLite.",
    )
    parser.add_argument(
        "--config",
        help="Path to config.json (default: $YT_ARCHIVE_CONFIG or ~/.config/yt-archive/config.json)",
    )
    sub = parser.add_subparsers(dest="command")

    # add
    p_add = sub.add_parser("add", help="Add a collection (channel or playlist)")
    p_add.add_argument("name", help="Unique name for this collection")
    p_add.add_argument("url", help="YouTube channel or playlist URL")

    # list
    sub.add_parser("list", help="List all collections")

    # remove
    p_rm = sub.add_parser("remove", help="Remove a collection and its videos")
    p_rm.add_argument("name", help="Collection name")

    # sync
    p_sync = sub.add_parser("sync", help="Download new videos for a collection")
    p_sync_g = p_sync.add_mutually_exclusive_group(required=True)
    p_sync_g.add_argument("name", nargs="?", help="Collection name")
    p_sync_g.add_argument(
        "--all", action="store_true", help="Sync all enabled collections"
    )

    # fetch-metadata
    p_meta = sub.add_parser(
        "fetch-metadata", help="Backfill metadata for imported videos"
    )
    p_meta_g = p_meta.add_mutually_exclusive_group(required=True)
    p_meta_g.add_argument("name", nargs="?", help="Collection name")
    p_meta_g.add_argument("--all", action="store_true", help="All collections")

    # import-archive
    p_imp = sub.add_parser(
        "import-archive", help="Import an existing yt-dlp archive.txt"
    )
    p_imp.add_argument("file", help="Path to the archive.txt file")
    p_imp.add_argument("--collection", required=True, help="Target collection name")

    # status
    sub.add_parser("status", help="Show database stats")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    dispatch = {
        "add": cmd_add,
        "list": cmd_list,
        "remove": cmd_remove,
        "sync": cmd_sync,
        "fetch-metadata": cmd_fetch_metadata,
        "import-archive": cmd_import,
        "status": cmd_status,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
