#!/usr/bin/env python3
"""
yt-archive: a thin wrapper around yt-dlp that catalogues downloads in SQLite.
"""

import argparse
import copy
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
    opts = copy.deepcopy(cfg)
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

    # We key pending_info by video ID (not filepath) because post-processors
    # can change the filepath (e.g. merge → .mkv, subtitle conversion, etc.),
    # causing a mismatch between what pp_hook stores and what post_hook receives.
    #
    # pp_hook fires after each post-processor stage and gives us info_dict with
    # all the metadata.  We keep updating per video ID so we always have the
    # latest info_dict (with the most up-to-date filepath).
    #
    # post_hook fires once after ALL post-processing with the final filepath.
    # We cannot directly get the video ID from the filepath string alone, so we
    # maintain a reverse mapping (filepath → video ID) that pp_hook keeps
    # current.  post_hook looks up the video ID via the final filepath, then
    # grabs the stashed info_dict, patches in the final filepath, and inserts.
    pending_info: dict[str, dict] = {}  # video_id → info_dict
    filepath_to_vid: dict[str, str] = {}  # filepath → video_id

    def pp_hook(d):
        """Stash info_dict keyed by video ID after each PP stage."""
        status = d.get("status")
        postprocessor = d.get("postprocessor", "?")
        print(f"  [pp_hook] status={status} postprocessor={postprocessor}")
        if status == "finished":
            info = d.get("info_dict", {})
            vid = info.get("id")
            filepath = info.get("filepath")
            title = info.get("title")
            print(
                f"  [pp_hook] finished: vid={vid} title={title!r} filepath={filepath}"
            )
            if vid:
                pending_info[vid] = info
                if filepath:
                    filepath_to_vid[filepath] = vid
                print(
                    f"  [pp_hook] stashed vid={vid}, filepath_to_vid has {len(filepath_to_vid)} entries"
                )
            else:
                print(
                    f"  [pp_hook] WARNING: no video ID in info_dict, keys={list(info.keys())[:10]}"
                )

    def post_hook(filepath):
        """Called after all post-processing and file move. Insert into DB."""
        print(f"  [post_hook] called with filepath={filepath}")
        print(f"  [post_hook] filepath_to_vid keys: {list(filepath_to_vid.keys())}")
        vid = filepath_to_vid.pop(filepath, None)
        if vid is None:
            print(
                f"  [post_hook] WARNING: filepath not found in filepath_to_vid, no vid match"
            )
        else:
            print(f"  [post_hook] matched vid={vid}")
        info = pending_info.pop(vid, {}) if vid else {}
        has_metadata = bool(info.get("title"))
        extractor = info.get("extractor_key", info.get("ie_key", "youtube")).lower()
        vid = vid or info.get("id", "")
        print(
            f"  [post_hook] vid={vid} extractor={extractor} has_metadata={has_metadata} title={info.get('title')!r}"
        )
        if vid:
            # Use the final filepath (the one post_hook received) so the DB
            # records where the file actually ended up.
            info["filepath"] = filepath
            _insert_video(
                db, collection_id, extractor, vid, info=info, status="downloaded"
            )
            print(f"  [post_hook] inserted vid={vid} with metadata via post_hook")
        else:
            print(
                f"  [post_hook] WARNING: no vid, skipping DB insert (will rely on archive diff fallback)"
            )

    opts["postprocessor_hooks"] = [pp_hook]
    opts["post_hooks"] = [post_hook]

    try:
        with YoutubeDL(opts) as ydl:
            ydl.download([collection_url])
    finally:
        # Also pick up anything the hooks missed by diffing the archive file.
        # If pending_info still has entries the post_hook never consumed, use
        # them so we don't lose metadata.
        known_after = _read_archive_file(archive_path)
        new_entries = known_after - known_before
        print(
            f"  [fallback] archive diff: {len(new_entries)} new entries, {len(pending_info)} leftover in pending_info"
        )
        if pending_info:
            print(
                f"  [fallback] leftover pending_info vids: {list(pending_info.keys())}"
            )
        for extractor, vid in new_entries:
            info = pending_info.pop(vid, {})
            has_metadata = bool(info.get("title"))
            print(
                f"  [fallback] inserting extractor={extractor} vid={vid} has_metadata={has_metadata} title={info.get('title')!r}"
            )
            _insert_video(
                db, collection_id, extractor, vid, info=info, status="downloaded"
            )

        archive_path.unlink(missing_ok=True)


def fetch_metadata_for_collection(
    db: sqlite3.Connection, collection_id: int, cfg: dict, reassign: bool = False
):
    """Backfill metadata for videos that are missing it (e.g. imported from archive).

    If reassign=True, videos whose channel_id doesn't match their current
    collection are moved to the correct channel-based collection (auto-created
    if needed).  This is the intended workflow after a bulk import into a
    dummy/catch-all collection.
    """
    from yt_dlp import YoutubeDL

    rows = db.execute(
        "SELECT id, extractor, video_id FROM videos WHERE collection_id = ? AND title IS NULL",
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

    reassigned = 0
    total = len(rows)

    with YoutubeDL(opts) as ydl:
        for i, row in enumerate(rows, 1):
            url = f"https://www.youtube.com/watch?v={row['video_id']}"
            print(f"  [{i}/{total}] Fetching metadata for {row['video_id']}...")
            try:
                info = ydl.extract_info(url, download=False)
                if not info:
                    continue

                target_collection_id = collection_id

                if reassign:
                    channel_id = info.get("channel_id")
                    if channel_id:
                        channel_name = (
                            info.get("uploader") or info.get("channel") or channel_id
                        )
                        proper_id = _get_or_create_collection(
                            db, channel_id, channel_name
                        )
                        if proper_id != collection_id:
                            # Check if video already exists in the target collection
                            dup = db.execute(
                                "SELECT id FROM videos WHERE extractor = ? AND video_id = ? AND collection_id = ?",
                                (row["extractor"], row["video_id"], proper_id),
                            ).fetchone()
                            if dup:
                                # Already in the right collection; delete the stale row
                                db.execute(
                                    "DELETE FROM videos WHERE id = ?", (row["id"],)
                                )
                                db.commit()
                                reassigned += 1
                                continue
                            target_collection_id = proper_id
                            reassigned += 1

                db.execute(
                    """
                    UPDATE videos SET
                        collection_id = ?,
                        title = ?, uploader = ?, uploader_id = ?,
                        upload_date = ?, duration = ?, resolution = ?
                    WHERE id = ?
                    """,
                    (
                        target_collection_id,
                        info.get("title"),
                        info.get("uploader"),
                        info.get("uploader_id"),
                        info.get("upload_date"),
                        info.get("duration"),
                        info.get("resolution"),
                        row["id"],
                    ),
                )
                db.commit()
            except Exception as e:
                print(f"  Failed to fetch metadata for {row['video_id']}: {e}")

    if reassign and reassigned:
        print(f"  Reassigned {reassigned} videos to their proper collections.")
        # Report if the source collection is now empty
        remaining = db.execute(
            "SELECT COUNT(*) as c FROM videos WHERE collection_id = ?",
            (collection_id,),
        ).fetchone()["c"]
        if remaining == 0:
            col_name = db.execute(
                "SELECT name FROM collections WHERE id = ?", (collection_id,)
            ).fetchone()
            if col_name:
                print(
                    f"  Collection '{col_name['name']}' is now empty and can be removed with: "
                    f"yt-archive remove {col_name['name']}"
                )


# ---------------------------------------------------------------------------
# Import existing archive
# ---------------------------------------------------------------------------


def _get_or_create_collection(
    db: sqlite3.Connection, channel_id: str, channel_name: str
) -> int:
    """Find an existing collection by channel URL, or create one."""
    channel_url = f"https://www.youtube.com/channel/{channel_id}"
    row = db.execute(
        "SELECT id FROM collections WHERE url = ?", (channel_url,)
    ).fetchone()
    if row:
        return row["id"]
    # Use the channel name as the collection name, dedup if needed
    base_name = channel_name or channel_id
    name = base_name
    suffix = 1
    while True:
        try:
            db.execute(
                "INSERT INTO collections (name, url) VALUES (?, ?)",
                (name, channel_url),
            )
            db.commit()
            row = db.execute(
                "SELECT id FROM collections WHERE url = ?", (channel_url,)
            ).fetchone()
            print(f"  Auto-created collection '{name}' for channel {channel_id}")
            return row["id"]
        except sqlite3.IntegrityError:
            suffix += 1
            name = f"{base_name} ({suffix})"


def _resolve_video_collection(
    db: sqlite3.Connection, extractor: str, video_id: str, cfg: dict
) -> int | None:
    """Use yt-dlp to fetch lightweight metadata and resolve the collection for a video."""
    from yt_dlp import YoutubeDL

    url = f"https://www.youtube.com/watch?v={video_id}"

    opts = config_to_ytdlp_opts(cfg)
    opts["skip_download"] = True
    opts["ignoreerrors"] = True
    opts["quiet"] = True
    # Strip options that write files
    for key in (
        "writedescription",
        "writethumbnail",
        "writesubtitles",
        "allsubtitles",
        "postprocessors",
        "outtmpl",
        "paths",
    ):
        opts.pop(key, None)

    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return None
        channel_id = info.get("channel_id")
        if not channel_id:
            return None
        channel_name = info.get("uploader") or info.get("channel") or channel_id
        collection_id = _get_or_create_collection(db, channel_id, channel_name)

        # Also backfill metadata while we have it
        db.execute(
            """
            UPDATE videos SET
                title = COALESCE(title, ?), uploader = COALESCE(uploader, ?),
                uploader_id = COALESCE(uploader_id, ?),
                upload_date = COALESCE(upload_date, ?),
                duration = COALESCE(duration, ?), resolution = COALESCE(resolution, ?)
            WHERE extractor = ? AND video_id = ? AND collection_id = ?
            """,
            (
                info.get("title"),
                info.get("uploader"),
                info.get("uploader_id"),
                info.get("upload_date"),
                info.get("duration"),
                info.get("resolution"),
                extractor,
                video_id,
                collection_id,
            ),
        )
        db.commit()
        return collection_id
    except Exception as e:
        print(f"  Warning: could not resolve collection for {video_id}: {e}")
        return None


def import_archive(
    db: sqlite3.Connection,
    archive_path: Path,
    collection_id: int | None = None,
    cfg: dict | None = None,
):
    """Import an existing yt-dlp archive.txt into the database.

    If collection_id is provided, all entries go into that collection.
    Otherwise, yt-dlp is used to fetch metadata for each video to determine
    its channel, and collections are auto-created as needed.
    """
    entries = _read_archive_file(archive_path)
    count = 0
    skipped = 0

    if collection_id is not None:
        # Simple mode: all videos go to one collection
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
    else:
        # Auto-discover mode: resolve collection per video via yt-dlp
        cfg = cfg or {}
        total = len(entries)
        for i, (extractor, video_id) in enumerate(entries, 1):
            # Skip if already in the DB under any collection
            existing = db.execute(
                "SELECT id FROM videos WHERE extractor = ? AND video_id = ?",
                (extractor, video_id),
            ).fetchone()
            if existing:
                skipped += 1
                continue

            print(f"  [{i}/{total}] Resolving {extractor} {video_id}...")
            resolved_id = _resolve_video_collection(db, extractor, video_id, cfg)
            if resolved_id is None:
                print(f"  Could not resolve collection for {video_id}, skipping.")
                skipped += 1
                continue

            try:
                db.execute(
                    """
                    INSERT OR IGNORE INTO videos (extractor, video_id, collection_id, status)
                    VALUES (?, ?, ?, 'imported')
                    """,
                    (extractor, video_id, resolved_id),
                )
                count += 1
            except sqlite3.IntegrityError:
                pass
        db.commit()

    print(f"Imported {count} entries into the database.")
    if skipped:
        print(f"Skipped {skipped} entries (already present or unresolvable).")


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
    reassign = getattr(args, "reassign", False)

    for col in collections:
        print(f"Fetching metadata for '{col['name']}'...")
        fetch_metadata_for_collection(db, col["id"], cfg, reassign=reassign)

    db.close()


def cmd_import(args):
    db = get_db()
    cfg = load_config(Path(args.config) if args.config else None)

    collection_id = None
    if args.collection:
        row = db.execute(
            "SELECT * FROM collections WHERE name = ?", (args.collection,)
        ).fetchone()
        if not row:
            print(f"Error: collection '{args.collection}' not found.", file=sys.stderr)
            print("Create it first with: yt-archive add <name> <url>")
            sys.exit(1)
        collection_id = row["id"]

    path = Path(args.file)
    if not path.exists():
        print(f"Error: file '{path}' not found.", file=sys.stderr)
        sys.exit(1)

    if collection_id is None:
        print(
            "No --collection specified; auto-discovering channels via yt-dlp metadata..."
        )

    import_archive(db, path, collection_id=collection_id, cfg=cfg)
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
    p_meta.add_argument(
        "--reassign",
        action="store_true",
        default=False,
        help="Reassign videos to their proper channel-based collections (useful after bulk import into a dummy collection)",
    )

    # import-archive
    p_imp = sub.add_parser(
        "import-archive", help="Import an existing yt-dlp archive.txt"
    )
    p_imp.add_argument("file", help="Path to the archive.txt file")
    p_imp.add_argument(
        "--collection",
        required=False,
        default=None,
        help="Target collection name (if omitted, collections are auto-created per channel)",
    )

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
