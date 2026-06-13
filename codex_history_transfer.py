#!/usr/bin/env python3
"""Move local Codex Desktop history between machines.

This tool works only with local files. It never uploads conversations.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import zipfile
from pathlib import Path
from typing import Any


APP_NAME = "codex-history-transfer"
__version__ = "1.0.1"
MANIFEST_SCHEMA = "codex-history-transfer"
MANIFEST_VERSION = 1

DEFAULT_EXCLUDES = [
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
]


class TransferError(RuntimeError):
    pass


def eprint(message: str) -> None:
    print(message, file=sys.stderr)


def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_codex_home() -> Path:
    if os.environ.get("CODEX_HOME"):
        return Path(os.environ["CODEX_HOME"]).expanduser()
    if os.environ.get("USERPROFILE"):
        return Path(os.environ["USERPROFILE"]) / ".codex"
    return Path.home() / ".codex"


def strip_extended_prefix(value: str | None) -> str | None:
    if not value:
        return value
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def path_from_value(value: str | None) -> Path | None:
    value = strip_extended_prefix(value)
    if not value:
        return None
    return Path(value).expanduser()


def path_for_db(path: Path) -> str:
    return str(path)


def slugify(value: str, fallback: str = "codex-session") -> str:
    value = value.strip().replace("\\", "-").replace("/", "-")
    value = re.sub(r'[<>:"|?*\x00-\x1f]+', "-", value)
    value = re.sub(r"\s+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-. ")
    return value[:80] or fallback


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_state_db(codex_home: Path) -> Path:
    candidates: list[Path] = []
    for base in [codex_home / "sqlite", codex_home]:
        if base.is_dir():
            candidates.extend(base.glob("state_*.sqlite"))
    if not candidates:
        raise TransferError(f"No state_*.sqlite found under {codex_home}")

    def sort_key(path: Path) -> tuple[int, float]:
        match = re.search(r"state_(\d+)\.sqlite$", path.name)
        version = int(match.group(1)) if match else -1
        return version, path.stat().st_mtime

    return sorted(candidates, key=sort_key, reverse=True)[0]


def connect_state_db(codex_home: Path) -> tuple[Path, sqlite3.Connection]:
    db_path = find_state_db(codex_home)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return db_path, conn


def table_columns(conn: sqlite3.Connection, table: str = "threads") -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if not rows:
        raise TransferError(f"Table {table!r} was not found in the state database")
    return [row["name"] for row in rows]


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def get_thread(conn: sqlite3.Connection, thread_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
    data = row_to_dict(row)
    if not data:
        raise TransferError(f"Thread not found in state DB: {thread_id}")
    return data


def find_rollout_by_thread_id(codex_home: Path, thread_id: str) -> Path | None:
    sessions = codex_home / "sessions"
    if not sessions.is_dir():
        return None
    matches = sorted(sessions.rglob(f"*{thread_id}*.jsonl"))
    return matches[0] if matches else None


def rollout_path_for_thread(codex_home: Path, thread: dict[str, Any]) -> Path:
    rollout_path = path_from_value(thread.get("rollout_path"))
    if rollout_path and rollout_path.exists():
        return rollout_path
    found = find_rollout_by_thread_id(codex_home, str(thread["id"]))
    if found and found.exists():
        return found
    raise TransferError(f"Rollout JSONL was not found for thread {thread['id']}")


def read_rollout_meta(data: str) -> dict[str, Any]:
    first_line = data.split("\n", 1)[0]
    try:
        meta = json.loads(first_line)
    except json.JSONDecodeError as exc:
        raise TransferError(f"First rollout line is not valid JSON: {exc}") from exc
    if meta.get("type") != "session_meta" or not isinstance(meta.get("payload"), dict):
        raise TransferError("First rollout line is not a session_meta payload")
    return meta


def patch_rollout_meta(data: str, *, provider: str | None, cwd: str | None) -> tuple[str, dict[str, Any]]:
    if "\n" in data:
        first_line, rest = data.split("\n", 1)
        separator = "\n"
    else:
        first_line, rest, separator = data, "", ""
    meta = json.loads(first_line)
    if meta.get("type") != "session_meta" or not isinstance(meta.get("payload"), dict):
        raise TransferError("First rollout line is not a session_meta payload")
    if provider:
        meta["payload"]["model_provider"] = provider
    if cwd:
        meta["payload"]["cwd"] = cwd
    patched = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
    return patched + separator + rest, meta


def epoch_from_iso(value: str | None) -> int | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        return int(dt.datetime.fromisoformat(text).timestamp())
    except ValueError:
        return None


def ms_from_seconds(value: int | None) -> int | None:
    return value * 1000 if isinstance(value, int) else None


def date_parts_from_rollout(meta: dict[str, Any], fallback_name: str) -> tuple[str, str, str, str]:
    payload = meta.get("payload", {})
    timestamp = payload.get("timestamp") or meta.get("timestamp")
    if isinstance(timestamp, str):
        try:
            parsed = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            return f"{parsed.year:04d}", f"{parsed.month:02d}", f"{parsed.day:02d}", fallback_name
        except ValueError:
            pass
    match = re.search(r"rollout-(\d{4})-(\d{2})-(\d{2})", fallback_name)
    if match:
        return match.group(1), match.group(2), match.group(3), fallback_name
    today = dt.datetime.now()
    return f"{today.year:04d}", f"{today.month:02d}", f"{today.day:02d}", fallback_name


def detect_current_provider(conn: sqlite3.Connection) -> str | None:
    rows = conn.execute(
        """
        SELECT model_provider, COUNT(*) AS n, MAX(updated_at) AS last_seen
        FROM threads
        WHERE model_provider IS NOT NULL AND model_provider != ''
        GROUP BY model_provider
        ORDER BY n DESC, last_seen DESC
        LIMIT 1
        """
    ).fetchall()
    if rows:
        return rows[0]["model_provider"]
    return None


def choose_provider(requested: str, conn: sqlite3.Connection, original: str | None) -> str | None:
    if requested == "keep":
        return original
    if requested == "current":
        return detect_current_provider(conn) or original or "custom"
    return requested


def backup_sqlite_files(db_path: Path) -> list[Path]:
    backups: list[Path] = []
    stamp = now_stamp()
    for suffix in ["", "-wal", "-shm"]:
        source = Path(str(db_path) + suffix)
        if source.exists():
            target = Path(str(source) + f".bak-{APP_NAME}-{stamp}")
            shutil.copy2(source, target)
            backups.append(target)
    return backups


def backup_file(path: Path) -> Path:
    target = Path(str(path) + f".bak-{APP_NAME}-{now_stamp()}")
    shutil.copy2(path, target)
    return target


def should_exclude(rel: Path, patterns: list[str]) -> bool:
    parts = rel.parts
    rel_text = rel.as_posix()
    for pattern in patterns:
        if any(part == pattern for part in parts):
            return True
        if fnmatch.fnmatch(rel_text, pattern):
            return True
    return False


def iter_workspace_files(root: Path, patterns: list[str], output_zip: Path | None = None):
    output_resolved = output_zip.resolve() if output_zip else None
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if output_resolved and path.resolve() == output_resolved:
            continue
        if should_exclude(rel, patterns):
            continue
        yield path, rel


def format_time(seconds: int | None) -> str:
    if not seconds:
        return ""
    return dt.datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M")


def truncate(value: Any, width: int) -> str:
    text = "" if value is None else str(value).replace("\n", " ")
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


def print_rows(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("No threads found.")
        return
    for row in rows:
        archived = " archived" if row.get("archived") else ""
        title = row.get("title") or row.get("preview") or "(untitled)"
        print(f"{format_time(row.get('updated_at'))}  {row.get('id')}  [{row.get('model_provider') or '?'}]{archived}")
        print(f"  {truncate(title, 100)}")
        if row.get("cwd"):
            print(f"  cwd: {strip_extended_prefix(row.get('cwd'))}")


def command_list(args: argparse.Namespace) -> int:
    codex_home = Path(args.codex_home).expanduser() if args.codex_home else default_codex_home()
    _, conn = connect_state_db(codex_home)
    try:
        where = []
        params: list[Any] = []
        if not args.all:
            where.append("archived = 0")
        if args.query:
            like = f"%{args.query}%"
            where.append("(id LIKE ? OR title LIKE ? OR preview LIKE ? OR first_user_message LIKE ?)")
            params.extend([like, like, like, like])
        sql = "SELECT id,title,preview,cwd,model_provider,model,archived,created_at,updated_at FROM threads"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(args.limit)
        rows = [row_to_dict(row) for row in conn.execute(sql, params).fetchall()]
        print_rows([row for row in rows if row])
        return 0
    finally:
        conn.close()


def command_export(args: argparse.Namespace) -> int:
    codex_home = Path(args.codex_home).expanduser() if args.codex_home else default_codex_home()
    db_path, conn = connect_state_db(codex_home)
    try:
        thread = get_thread(conn, args.id)
        rollout_path = rollout_path_for_thread(codex_home, thread)
        rollout_text = rollout_path.read_text(encoding="utf-8")
        meta = read_rollout_meta(rollout_text)
        payload = meta["payload"]
        thread_id = str(payload.get("id") or thread["id"])
        if thread_id != args.id:
            raise TransferError(f"Rollout id mismatch: DB has {args.id}, JSONL has {thread_id}")

        cwd = path_from_value(thread.get("cwd") or payload.get("cwd"))
        out_path = Path(args.out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)

        manifest: dict[str, Any] = {
            "schema": MANIFEST_SCHEMA,
            "version": MANIFEST_VERSION,
            "exported_at": utc_now_iso(),
            "source": {
                "codex_home": str(codex_home),
                "state_db": str(db_path),
            },
            "thread_id": args.id,
            "thread": thread,
            "rollout": {
                "archive_path": f"sessions/{rollout_path.name}",
                "original_path": str(rollout_path),
                "sha256": hash_file(rollout_path),
            },
            "workspace": {
                "included": bool(args.include_workspace),
                "archive_prefix": "workspace/",
                "original_cwd": str(cwd) if cwd else None,
                "name": cwd.name if cwd else None,
                "excludes": args.exclude,
            },
        }

        workspace_files = 0
        with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            zf.write(rollout_path, manifest["rollout"]["archive_path"])
            if args.include_workspace:
                if not cwd or not cwd.is_dir():
                    eprint(f"Warning: workspace cwd does not exist, skipped: {cwd}")
                else:
                    for file_path, rel in iter_workspace_files(cwd, args.exclude, out_path):
                        zf.write(file_path, "workspace/" + rel.as_posix())
                        workspace_files += 1
        print(f"Exported thread {args.id}")
        print(f"Package: {out_path}")
        print(f"Workspace files: {workspace_files if args.include_workspace else 'not included'}")
        return 0
    finally:
        conn.close()


def read_package(package: Path) -> tuple[dict[str, Any], str]:
    with zipfile.ZipFile(package, "r") as zf:
        try:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        except KeyError as exc:
            raise TransferError("Package is missing manifest.json") from exc
        if manifest.get("schema") != MANIFEST_SCHEMA:
            raise TransferError("This does not look like a codex-history-transfer package")
        rollout_archive_path = manifest.get("rollout", {}).get("archive_path")
        if not rollout_archive_path:
            raise TransferError("Package manifest is missing rollout.archive_path")
        rollout_text = zf.read(rollout_archive_path).decode("utf-8")
        return manifest, rollout_text


def extract_workspace(
    package: Path,
    manifest: dict[str, Any],
    target_cwd: Path,
    *,
    dry_run: bool,
    overwrite: bool,
) -> int:
    workspace = manifest.get("workspace", {})
    if not workspace.get("included"):
        return 0
    prefix = workspace.get("archive_prefix") or "workspace/"
    with zipfile.ZipFile(package, "r") as zf:
        names = [name for name in zf.namelist() if name.startswith(prefix) and not name.endswith("/")]
        if dry_run:
            return len(names)
        if target_cwd.exists() and any(target_cwd.iterdir()) and not overwrite:
            raise TransferError(f"Target workspace is not empty, use --overwrite to merge: {target_cwd}")
        target_cwd.mkdir(parents=True, exist_ok=True)
        for name in names:
            rel = Path(name[len(prefix) :])
            if rel.is_absolute() or ".." in rel.parts:
                raise TransferError(f"Unsafe workspace path in package: {name}")
            target = target_cwd / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as source, target.open("wb") as dest:
                shutil.copyfileobj(source, dest)
        return len(names)


def build_thread_values(
    columns: list[str],
    source_thread: dict[str, Any],
    meta: dict[str, Any],
    *,
    rollout_path: Path,
    cwd: str,
    provider: str | None,
    title_override: str | None,
) -> dict[str, Any]:
    payload = meta["payload"]
    timestamp_epoch = epoch_from_iso(payload.get("timestamp") or meta.get("timestamp"))
    created_at = source_thread.get("created_at") or timestamp_epoch or int(dt.datetime.now().timestamp())
    updated_at = source_thread.get("updated_at") or created_at
    first_message = source_thread.get("first_user_message") or source_thread.get("preview") or source_thread.get("title") or ""
    title = title_override or source_thread.get("title") or first_message or "Imported Codex session"

    defaults: dict[str, Any] = {
        "id": payload.get("id") or source_thread.get("id"),
        "rollout_path": path_for_db(rollout_path),
        "created_at": created_at,
        "updated_at": updated_at,
        "source": source_thread.get("source") or payload.get("source") or "vscode",
        "model_provider": provider,
        "cwd": cwd,
        "title": title,
        "sandbox_policy": source_thread.get("sandbox_policy") or '{"type":"disabled"}',
        "approval_mode": source_thread.get("approval_mode") or "never",
        "tokens_used": source_thread.get("tokens_used") or 0,
        "has_user_event": source_thread.get("has_user_event") or 0,
        "archived": 0,
        "archived_at": None,
        "git_sha": source_thread.get("git_sha"),
        "git_branch": source_thread.get("git_branch"),
        "git_origin_url": source_thread.get("git_origin_url"),
        "cli_version": source_thread.get("cli_version") or payload.get("cli_version") or "",
        "first_user_message": first_message,
        "agent_nickname": source_thread.get("agent_nickname"),
        "agent_role": source_thread.get("agent_role"),
        "memory_mode": source_thread.get("memory_mode") or "enabled",
        "model": source_thread.get("model"),
        "reasoning_effort": source_thread.get("reasoning_effort"),
        "agent_path": source_thread.get("agent_path"),
        "created_at_ms": source_thread.get("created_at_ms") or ms_from_seconds(created_at),
        "updated_at_ms": source_thread.get("updated_at_ms") or ms_from_seconds(updated_at),
        "thread_source": source_thread.get("thread_source") or payload.get("thread_source") or "user",
        "preview": source_thread.get("preview") or first_message or title,
    }
    return {column: defaults.get(column) for column in columns}


def upsert_thread(
    conn: sqlite3.Connection,
    values: dict[str, Any],
    *,
    overwrite: bool,
) -> str:
    thread_id = values["id"]
    exists = conn.execute("SELECT 1 FROM threads WHERE id = ?", (thread_id,)).fetchone() is not None
    if exists and not overwrite:
        raise TransferError(f"Thread already exists, use --overwrite to update it: {thread_id}")
    if exists:
        columns = [column for column in values.keys() if column != "id"]
        assignments = ", ".join(f"{column} = ?" for column in columns)
        params = [values[column] for column in columns] + [thread_id]
        conn.execute(f"UPDATE threads SET {assignments} WHERE id = ?", params)
        return "updated"
    columns = list(values.keys())
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO threads ({', '.join(columns)}) VALUES ({placeholders})",
        [values[column] for column in columns],
    )
    return "inserted"


def command_import(args: argparse.Namespace) -> int:
    package = Path(args.package).expanduser()
    if not package.exists():
        raise TransferError(f"Package not found: {package}")
    codex_home = Path(args.codex_home).expanduser() if args.codex_home else default_codex_home()
    manifest, rollout_text = read_package(package)
    meta = read_rollout_meta(rollout_text)
    payload = meta["payload"]
    thread_id = str(payload.get("id") or manifest.get("thread_id"))
    if not thread_id:
        raise TransferError("Package has no thread id")

    db_path, conn = connect_state_db(codex_home)
    try:
        original_provider = payload.get("model_provider") or manifest.get("thread", {}).get("model_provider")
        provider = choose_provider(args.provider, conn, original_provider)
        workspace = manifest.get("workspace", {})
        source_workspace_name = workspace.get("name") or slugify(thread_id)

        if args.target_cwd:
            target_cwd = Path(args.target_cwd).expanduser()
        elif workspace.get("included"):
            root = Path(args.workspace_root).expanduser() if args.workspace_root else Path.home() / "Documents" / "CodexImported"
            target_cwd = root / slugify(source_workspace_name, thread_id)
        else:
            target_cwd = path_from_value(payload.get("cwd") or manifest.get("thread", {}).get("cwd")) or (
                Path.home() / "Documents" / "CodexImported" / slugify(thread_id)
            )
        target_cwd_str = str(target_cwd)

        patched_rollout, patched_meta = patch_rollout_meta(
            rollout_text,
            provider=provider,
            cwd=target_cwd_str,
        )
        rollout_name = Path(manifest.get("rollout", {}).get("archive_path", f"{thread_id}.jsonl")).name
        year, month, day, rollout_name = date_parts_from_rollout(patched_meta, rollout_name)
        target_rollout = codex_home / "sessions" / year / month / day / rollout_name

        print(f"Import package: {package}")
        print(f"Thread: {thread_id}")
        print(f"Provider: {original_provider!r} -> {provider!r}")
        print(f"Target rollout: {target_rollout}")
        print(f"Target cwd: {target_cwd}")
        if args.dry_run:
            workspace_count = extract_workspace(package, manifest, target_cwd, dry_run=True, overwrite=args.overwrite)
            print(f"Dry run: would extract workspace files: {workspace_count}")
            print("Dry run: no files or databases were changed.")
            return 0

        backups = backup_sqlite_files(db_path)
        target_rollout.parent.mkdir(parents=True, exist_ok=True)
        if target_rollout.exists():
            if not args.overwrite:
                raise TransferError(f"Target rollout exists, use --overwrite: {target_rollout}")
            backup = backup_file(target_rollout)
            print(f"Backed up existing rollout: {backup}")
        workspace_count = extract_workspace(package, manifest, target_cwd, dry_run=False, overwrite=args.overwrite)
        target_rollout.write_text(patched_rollout, encoding="utf-8")

        columns = table_columns(conn)
        values = build_thread_values(
            columns,
            manifest.get("thread", {}),
            patched_meta,
            rollout_path=target_rollout,
            cwd=target_cwd_str,
            provider=provider,
            title_override=args.title,
        )
        with conn:
            action = upsert_thread(conn, values, overwrite=args.overwrite)

        print(f"Backed up DB files: {len(backups)}")
        print(f"Workspace files extracted: {workspace_count}")
        print(f"State DB row {action}: {thread_id}")
        return 0
    finally:
        conn.close()


def verify_one(codex_home: Path, conn: sqlite3.Connection, thread_id: str) -> tuple[int, int]:
    warnings = 0
    errors = 0
    try:
        thread = get_thread(conn, thread_id)
    except TransferError as exc:
        print(f"ERROR {thread_id}: {exc}")
        return 0, 1

    print(f"Thread: {thread_id}")
    print(f"  title: {thread.get('title')}")
    print(f"  provider(db): {thread.get('model_provider')}")
    rollout = path_from_value(thread.get("rollout_path"))
    if not rollout or not rollout.exists():
        found = find_rollout_by_thread_id(codex_home, thread_id)
        print(f"  ERROR rollout missing: {thread.get('rollout_path')}")
        if found:
            print(f"  found possible rollout: {found}")
        errors += 1
    else:
        print(f"  rollout: {rollout}")
        try:
            meta = read_rollout_meta(rollout.read_text(encoding="utf-8"))
            provider = meta["payload"].get("model_provider")
            print(f"  provider(jsonl): {provider}")
            if provider != thread.get("model_provider"):
                print("  WARNING provider mismatch between DB and JSONL")
                warnings += 1
        except Exception as exc:  # noqa: BLE001 - verification should report and continue.
            print(f"  ERROR failed to read rollout metadata: {exc}")
            errors += 1
    cwd = path_from_value(thread.get("cwd"))
    if cwd and cwd.exists():
        print(f"  cwd: {cwd}")
    else:
        print(f"  WARNING cwd missing: {thread.get('cwd')}")
        warnings += 1
    return warnings, errors


def command_verify(args: argparse.Namespace) -> int:
    codex_home = Path(args.codex_home).expanduser() if args.codex_home else default_codex_home()
    db_path, conn = connect_state_db(codex_home)
    try:
        print(f"Codex home: {codex_home}")
        print(f"State DB: {db_path}")
        if args.id:
            warnings, errors = verify_one(codex_home, conn, args.id)
        else:
            rows = conn.execute(
                "SELECT id FROM threads WHERE archived = 0 ORDER BY updated_at DESC LIMIT ?",
                (args.limit,),
            ).fetchall()
            warnings = errors = 0
            for row in rows:
                w, e = verify_one(codex_home, conn, row["id"])
                warnings += w
                errors += e
        print(f"Verification complete: {warnings} warning(s), {errors} error(s)")
        return 1 if errors else 0
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Export and import local Codex Desktop history packages.",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--codex-home", help="Codex home directory. Defaults to CODEX_HOME or ~/.codex.")

    list_parser = subparsers.add_parser("list", parents=[common], help="List local Codex threads from SQLite.")
    list_parser.add_argument("--query", help="Filter by id, title, preview, or first user message.")
    list_parser.add_argument("--limit", type=int, default=20)
    list_parser.add_argument("--all", action="store_true", help="Include archived threads.")
    list_parser.set_defaults(func=command_list)

    export_parser = subparsers.add_parser("export", parents=[common], help="Export one thread to a zip package.")
    export_parser.add_argument("--id", required=True, help="Thread/session id to export.")
    export_parser.add_argument("--out", required=True, help="Output .zip path.")
    export_parser.add_argument("--include-workspace", action="store_true", help="Also pack the thread cwd.")
    export_parser.add_argument(
        "--exclude",
        action="append",
        default=list(DEFAULT_EXCLUDES),
        help="Workspace path or glob to exclude. Can be repeated.",
    )
    export_parser.set_defaults(func=command_export)

    import_parser = subparsers.add_parser("import", parents=[common], help="Import a zip package into this machine.")
    import_parser.add_argument("package", help="Package created by the export command.")
    import_parser.add_argument(
        "--provider",
        default="current",
        help="Provider to write into metadata: current, keep, or a literal value like custom.",
    )
    import_parser.add_argument("--workspace-root", help="Root folder for an included workspace.")
    import_parser.add_argument("--target-cwd", help="Exact cwd to write into the imported session.")
    import_parser.add_argument("--title", help="Override the thread title in the target SQLite index.")
    import_parser.add_argument("--overwrite", action="store_true", help="Overwrite an existing thread/rollout/workspace.")
    import_parser.add_argument("--dry-run", action="store_true", help="Show planned changes without writing.")
    import_parser.set_defaults(func=command_import)

    verify_parser = subparsers.add_parser("verify", parents=[common], help="Check DB and JSONL metadata consistency.")
    verify_parser.add_argument("--id", help="Verify one thread id. Defaults to recent active threads.")
    verify_parser.add_argument("--limit", type=int, default=20)
    verify_parser.set_defaults(func=command_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except TransferError as exc:
        eprint(f"Error: {exc}")
        return 2
    except KeyboardInterrupt:
        eprint("Interrupted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
