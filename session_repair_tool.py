import json
import shutil
import sqlite3
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageDraw, ImageTk


CODEX_HOME = Path.home() / ".codex"
SESSIONS_DIR = CODEX_HOME / "sessions"
SESSION_INDEX_PATH = CODEX_HOME / "session_index.jsonl"
STATE_DB_PATH = CODEX_HOME / "state_5.sqlite"
GLOBAL_STATE_PATH = CODEX_HOME / ".codex-global-state.json"
EXPORT_MANIFEST_NAME = "codex_session_export_manifest.json"
ERROR_LOG_PATH = CODEX_HOME / "CodexSessionRepair-error.log"
CONFIG_PATH = CODEX_HOME / "config.toml"
CURATED_CACHE_DIR = CODEX_HOME / "plugins" / "cache" / "openai-curated"
CURATED_MARKETPLACE_ROOT = CODEX_HOME / ".tmp" / "plugins"
PRIMARY_RUNTIME_ROOT = Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "plugins" / "openai-primary-runtime"
REPAIR_REPORT_NAME = "plugin_skill_repair_report.md"


def parse_iso_utc(text: str) -> datetime:
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def to_epoch_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def to_epoch_s(dt: datetime) -> int:
    return int(dt.timestamp())


def safe_read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass
class RepairContext:
    source_path: Path
    thread_id: str
    title: str
    preview: str
    first_user_message: str
    created_at_utc: datetime
    created_at_local: datetime
    updated_at_utc: datetime
    updated_at_local: datetime
    cwd: str
    cli_version: str
    source: str
    model_provider: str
    thread_source: str
    model: str | None
    reasoning_effort: str | None
    approval_mode: str
    sandbox_policy: dict[str, Any]
    has_user_event: int
    tokens_used: int
    destination_path: Path
    source_thread_id: str | None = None


@dataclass
class ExistingThreadInfo:
    title: str | None = None
    preview: str | None = None
    updated_at_utc: datetime | None = None


@dataclass
class SessionListItem:
    thread_id: str
    title: str
    source_path: Path
    created_at_ms: int | None = None
    updated_at_ms: int | None = None
    preview: str | None = None


class SessionRepairError(Exception):
    pass


def normalize_path_for_db(path: Path) -> str:
    return f"\\\\?\\{path}"


def strip_extended_prefix(path_text: str) -> str:
    if path_text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path_text[8:]
    if path_text.startswith("\\\\?\\"):
        return path_text[4:]
    return path_text


def normalize_display_path(path: Path | str) -> Path:
    return Path(strip_extended_prefix(str(path)))


def resource_path(relative_path: str) -> Path:
    base_path = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base_path / relative_path


def destination_path_for_thread(created_at_local: datetime, thread_id: str) -> Path:
    destination_dir = SESSIONS_DIR / f"{created_at_local:%Y}" / f"{created_at_local:%m}" / f"{created_at_local:%d}"
    destination_name = f"rollout-{created_at_local:%Y-%m-%dT%H-%M-%S}-{thread_id}.jsonl"
    return destination_dir / destination_name


def make_backup(path: Path, suffix: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = path.with_name(f"{path.name}.{suffix}.{timestamp}.bak")
    shutil.copy2(path, backup_path)
    return backup_path


def iter_jsonl(path: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SessionRepairError(f"第 {line_no} 行不是合法 JSON: {exc}") from exc
    if not items:
        raise SessionRepairError("会话文件为空。")
    return items


def derive_title(message: str, fallback: str) -> str:
    text = message.strip()
    if not text:
        return fallback
    single_line = " ".join(text.split())
    return single_line[:120]


def load_existing_thread_info(thread_id: str) -> ExistingThreadInfo:
    info = ExistingThreadInfo()

    if SESSION_INDEX_PATH.exists():
        for line in SESSION_INDEX_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("id") != thread_id:
                continue
            if entry.get("thread_name"):
                info.title = entry["thread_name"]
            if entry.get("updated_at"):
                try:
                    info.updated_at_utc = parse_iso_utc(entry["updated_at"])
                except ValueError:
                    pass

    if STATE_DB_PATH.exists():
        con = sqlite3.connect(STATE_DB_PATH)
        con.row_factory = sqlite3.Row
        try:
            row = con.execute(
                "SELECT title, preview, updated_at_ms FROM threads WHERE id = ?",
                (thread_id,),
            ).fetchone()
            if row:
                if row["title"]:
                    info.title = row["title"]
                if row["preview"]:
                    info.preview = row["preview"]
        finally:
            con.close()

    return info


def load_existing_thread_ids() -> set[str]:
    thread_ids: set[str] = set()

    if SESSION_INDEX_PATH.exists():
        for line in SESSION_INDEX_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            thread_id = entry.get("id")
            if thread_id:
                thread_ids.add(thread_id)

    if STATE_DB_PATH.exists():
        con = sqlite3.connect(STATE_DB_PATH)
        try:
            rows = con.execute("SELECT id FROM threads").fetchall()
            thread_ids.update(row[0] for row in rows if row and row[0])
        finally:
            con.close()

    return thread_ids


def generate_unique_thread_id(existing_ids: set[str]) -> str:
    while True:
        thread_id = str(uuid4())
        if thread_id not in existing_ids:
            existing_ids.add(thread_id)
            return thread_id


def find_duplicate_thread_ids_for_import(root: Path) -> set[str]:
    existing_ids = load_existing_thread_ids()
    duplicate_ids: set[str] = set()

    for jsonl_path in sorted(root.rglob("*.jsonl")):
        try:
            context = build_context(jsonl_path)
        except Exception:
            continue
        if context.thread_id in existing_ids:
            duplicate_ids.add(context.thread_id)

    return duplicate_ids


def build_context(source_path: Path, thread_id_override: str | None = None) -> RepairContext:
    items = iter_jsonl(source_path)

    session_meta = None
    first_user_message = ""
    latest_title = ""
    latest_preview = ""
    latest_model = None
    latest_reasoning = None
    latest_turn_context = None
    has_user_event = 0
    latest_timestamp = None
    token_total = 0

    for item in items:
        payload = item.get("payload", {})
        item_type = item.get("type")
        timestamp_text = item.get("timestamp")
        if timestamp_text:
            current_ts = parse_iso_utc(timestamp_text)
            if latest_timestamp is None or current_ts > latest_timestamp:
                latest_timestamp = current_ts

        if item_type == "session_meta":
            session_meta = payload
        elif item_type == "turn_context":
            latest_turn_context = payload
            latest_model = payload.get("model", latest_model)
            collab = payload.get("collaboration_mode", {}).get("settings", {})
            latest_reasoning = collab.get("reasoning_effort", latest_reasoning)
        elif item_type == "event_msg" and payload.get("type") == "user_message":
            has_user_event = 1
            message = payload.get("message", "")
            if not first_user_message and message.strip():
                first_user_message = message.strip()
            latest_preview = message.strip() or latest_preview
            latest_title = derive_title(message, latest_title)
        elif item_type == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
            parts = payload.get("content", [])
            text_parts = [part.get("text", "") for part in parts if part.get("type") == "input_text"]
            combined = "\n".join([part for part in text_parts if part]).strip()
            if combined:
                if not first_user_message:
                    first_user_message = combined
                latest_preview = combined
                latest_title = derive_title(combined, latest_title)
        elif item_type == "event_msg" and payload.get("type") == "task_started":
            token_total = max(token_total, int(payload.get("model_context_window", 0) or 0))

    if not session_meta:
        raise SessionRepairError("缺少 session_meta，无法识别线程。")
    if not latest_turn_context:
        raise SessionRepairError("缺少 turn_context，无法恢复运行参数。")
    if latest_timestamp is None:
        raise SessionRepairError("缺少有效时间戳。")

    original_thread_id = session_meta.get("id")
    if not original_thread_id:
        raise SessionRepairError("session_meta 中缺少线程 ID。")
    thread_id = thread_id_override or original_thread_id

    existing = load_existing_thread_info(thread_id)

    created_at_utc = parse_iso_utc(session_meta["timestamp"])
    created_at_local = created_at_utc.astimezone()
    updated_at_utc = latest_timestamp
    updated_at_local = updated_at_utc.astimezone()

    destination_path = destination_path_for_thread(created_at_local, thread_id)

    title = existing.title or latest_title or derive_title(first_user_message, thread_id)
    preview = existing.preview or latest_preview or first_user_message or title

    return RepairContext(
        source_path=source_path,
        thread_id=thread_id,
        title=title,
        preview=preview[:500],
        first_user_message=first_user_message[:2000],
        created_at_utc=created_at_utc,
        created_at_local=created_at_local,
        updated_at_utc=updated_at_utc,
        updated_at_local=updated_at_local,
        cwd=session_meta.get("cwd", ""),
        cli_version=session_meta.get("cli_version", ""),
        source=session_meta.get("source", "vscode"),
        model_provider=session_meta.get("model_provider", "custom"),
        thread_source=session_meta.get("thread_source", "user"),
        model=latest_model,
        reasoning_effort=latest_reasoning,
        approval_mode=latest_turn_context.get("approval_policy", "never"),
        sandbox_policy=latest_turn_context.get("sandbox_policy", {"type": "danger-full-access"}),
        has_user_event=has_user_event,
        tokens_used=token_total,
        destination_path=destination_path,
        source_thread_id=original_thread_id if thread_id_override else None,
    )


def ensure_destination_copy(context: RepairContext, logs: list[str]) -> None:
    context.destination_path.parent.mkdir(parents=True, exist_ok=True)
    if context.source_thread_id and context.source_thread_id != context.thread_id:
        with context.source_path.open("r", encoding="utf-8") as source, context.destination_path.open(
            "w",
            encoding="utf-8",
        ) as destination:
            for line in source:
                stripped = line.strip()
                if not stripped:
                    destination.write(line)
                    continue
                item = json.loads(line)
                if item.get("type") == "session_meta":
                    payload = item.setdefault("payload", {})
                    if payload.get("id") == context.source_thread_id:
                        payload["id"] = context.thread_id
                destination.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
        logs.append(f"已增量复制会话：{context.source_thread_id} -> {context.thread_id}")
        logs.append(f"增量会话文件：{context.destination_path}")
        return

    if context.source_path.resolve() != context.destination_path.resolve():
        shutil.copy2(context.source_path, context.destination_path)
        logs.append(f"已复制标准化会话文件到：{context.destination_path}")
    else:
        logs.append("源文件已经在标准路径，无需复制。")


def rewrite_session_index(context: RepairContext, logs: list[str]) -> None:
    entries: list[dict[str, Any]] = []
    if SESSION_INDEX_PATH.exists():
        for line in SESSION_INDEX_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("id") != context.thread_id:
                entries.append(entry)

    entries.append(
        {
            "id": context.thread_id,
            "thread_name": context.title,
            "updated_at": context.updated_at_utc.isoformat().replace("+00:00", "Z"),
        }
    )
    entries.sort(key=lambda item: item.get("updated_at", ""))

    if SESSION_INDEX_PATH.exists():
        backup = make_backup(SESSION_INDEX_PATH, f"session-index-{context.thread_id}")
        logs.append(f"已备份 session_index：{backup}")

    text = "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n"
    SESSION_INDEX_PATH.write_text(text, encoding="utf-8")
    logs.append("已重写 session_index.jsonl。")


def ensure_threads_table_row(context: RepairContext, logs: list[str]) -> None:
    if not STATE_DB_PATH.exists():
        raise SessionRepairError(f"未找到状态库：{STATE_DB_PATH}")

    backup = make_backup(STATE_DB_PATH, f"state5-{context.thread_id}")
    logs.append(f"已备份状态库：{backup}")

    con = sqlite3.connect(STATE_DB_PATH)
    try:
        cur = con.cursor()
        db_rollout_path = normalize_path_for_db(context.destination_path)
        existing = cur.execute("SELECT id FROM threads WHERE id = ?", (context.thread_id,)).fetchone()
        values = {
            "rollout_path": db_rollout_path,
            "created_at": to_epoch_s(context.created_at_utc),
            "updated_at": to_epoch_s(context.updated_at_utc),
            "source": context.source,
            "model_provider": context.model_provider,
            "cwd": normalize_path_for_db(Path(context.cwd)) if context.cwd else "",
            "title": context.title,
            "sandbox_policy": json.dumps(context.sandbox_policy, ensure_ascii=False),
            "approval_mode": context.approval_mode,
            "tokens_used": context.tokens_used,
            "has_user_event": context.has_user_event,
            "archived": 0,
            "archived_at": None,
            "git_sha": None,
            "git_branch": None,
            "git_origin_url": None,
            "cli_version": context.cli_version,
            "first_user_message": context.first_user_message,
            "agent_nickname": None,
            "agent_role": None,
            "memory_mode": "enabled",
            "model": context.model,
            "reasoning_effort": context.reasoning_effort,
            "agent_path": None,
            "created_at_ms": to_epoch_ms(context.created_at_utc),
            "updated_at_ms": to_epoch_ms(context.updated_at_utc),
            "thread_source": context.thread_source,
            "preview": context.preview,
        }

        if existing:
            set_clause = ", ".join(f"{key} = ?" for key in values.keys())
            cur.execute(
                f"UPDATE threads SET {set_clause} WHERE id = ?",
                [*values.values(), context.thread_id],
            )
            logs.append("已更新 threads 表中的现有线程记录。")
        else:
            columns = ", ".join(["id", *values.keys()])
            placeholders = ", ".join(["?"] * (len(values) + 1))
            cur.execute(
                f"INSERT INTO threads ({columns}) VALUES ({placeholders})",
                [context.thread_id, *values.values()],
            )
            logs.append("已插入新的 threads 线程记录。")
        con.commit()
    finally:
        con.close()


def update_global_state(context: RepairContext, logs: list[str]) -> None:
    state = safe_read_json(GLOBAL_STATE_PATH)
    if not state:
        logs.append("未检测到 .codex-global-state.json，跳过全局状态修复。")
        return

    backup = make_backup(GLOBAL_STATE_PATH, f"global-state-{context.thread_id}")
    logs.append(f"已备份全局状态：{backup}")

    atom_state = state.setdefault("electron-persisted-atom-state", {})
    prompt_history = atom_state.setdefault("prompt-history", {})
    thread_prompts = prompt_history.setdefault(context.thread_id, [])
    if context.first_user_message and context.first_user_message not in thread_prompts:
        thread_prompts.append(context.first_user_message)

    pinned_ids = state.setdefault("pinned-thread-ids", [])
    if context.thread_id not in pinned_ids:
        pinned_ids.append(context.thread_id)

    permissions = atom_state.setdefault("heartbeat-thread-permissions-by-id", {})
    permissions.setdefault(
        context.thread_id,
        {
            "approvalPolicy": "never",
            "approvalsReviewer": "user",
            "sandboxPolicy": {"type": "dangerFullAccess"},
        },
    )

    GLOBAL_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    logs.append("已更新 .codex-global-state.json。")


def apply_repair_context(context: RepairContext, logs: list[str]) -> None:
    logs.append(f"线程 ID：{context.thread_id}")
    logs.append(f"标准路径：{context.destination_path}")
    ensure_destination_copy(context, logs)
    rewrite_session_index(context, logs)
    ensure_threads_table_row(context, logs)
    update_global_state(context, logs)


def repair_session(source_path: Path) -> tuple[RepairContext, list[str]]:
    if not source_path.exists():
        raise SessionRepairError(f"文件不存在：{source_path}")
    if source_path.suffix.lower() != ".jsonl":
        raise SessionRepairError("请选择 .jsonl 会话文件。")

    logs: list[str] = []
    context = build_context(source_path)
    apply_repair_context(context, logs)
    logs.append("修复完成。建议重启 Codex 以刷新界面缓存。")
    return context, logs


def collect_contexts_from_directory(root: Path, duplicate_mode: str = "replace") -> list[RepairContext]:
    if not root.exists():
        raise SessionRepairError(f"目录不存在：{root}")
    if not root.is_dir():
        raise SessionRepairError("选择的路径不是目录。")

    best_by_thread: dict[str, RepairContext] = {}
    contexts: list[RepairContext] = []
    errors: list[str] = []
    existing_ids = load_existing_thread_ids()

    for jsonl_path in sorted(root.rglob("*.jsonl")):
        try:
            original_context = build_context(jsonl_path)
        except Exception as exc:
            errors.append(f"{jsonl_path}: {exc}")
            continue

        if duplicate_mode == "incremental" and original_context.thread_id in existing_ids:
            context = build_context(jsonl_path, generate_unique_thread_id(existing_ids))
            contexts.append(context)
            continue

        existing_ids.add(original_context.thread_id)
        existing = best_by_thread.get(original_context.thread_id)
        if existing is None or original_context.updated_at_utc > existing.updated_at_utc:
            best_by_thread[original_context.thread_id] = original_context

    contexts.extend(best_by_thread.values())
    if not contexts:
        detail = "\n".join(errors[:10])
        raise SessionRepairError(f"目录下没有可修复的会话文件。\n{detail}")

    return sorted(contexts, key=lambda item: item.updated_at_utc)


def path_is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def load_session_items(sessions_root: Path | None = None) -> list[SessionListItem]:
    root = sessions_root or SESSIONS_DIR
    items: list[SessionListItem] = []

    if STATE_DB_PATH.exists():
        con = sqlite3.connect(STATE_DB_PATH)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                """
                SELECT id, title, preview, rollout_path, created_at_ms, updated_at_ms
                FROM threads
                WHERE archived = 0
                ORDER BY COALESCE(updated_at_ms, updated_at * 1000, 0) DESC
                """
            ).fetchall()
            for row in rows:
                rollout_path = normalize_display_path(row["rollout_path"])
                if sessions_root and root.exists() and not path_is_under(rollout_path, root):
                    continue
                title = (row["title"] or row["preview"] or row["id"]).strip()
                items.append(
                    SessionListItem(
                        thread_id=row["id"],
                        title=title,
                        source_path=rollout_path,
                        created_at_ms=row["created_at_ms"],
                        updated_at_ms=row["updated_at_ms"],
                        preview=row["preview"],
                    )
                )
        finally:
            con.close()

    if items:
        return items

    if not root.exists():
        return []

    for jsonl_path in sorted(root.rglob("*.jsonl")):
        try:
            context = build_context(jsonl_path)
            items.append(
                SessionListItem(
                    thread_id=context.thread_id,
                    title=context.title,
                    source_path=jsonl_path,
                    created_at_ms=to_epoch_ms(context.created_at_utc),
                    updated_at_ms=to_epoch_ms(context.updated_at_utc),
                    preview=context.preview,
                )
            )
        except Exception:
            continue

    return sorted(items, key=lambda item: item.updated_at_ms or 0, reverse=True)


def repair_directory(root: Path, duplicate_mode: str = "replace") -> list[str]:
    contexts = collect_contexts_from_directory(root, duplicate_mode=duplicate_mode)
    mode_label = "增量" if duplicate_mode == "incremental" else "替换"
    logs = [
        f"扫描目录：{root}",
        f"重复 ID 处理方式：{mode_label}",
        f"去重后准备修复 {len(contexts)} 个线程。",
    ]

    for index, context in enumerate(contexts, 1):
        logs.append("")
        logs.append(f"[{index}/{len(contexts)}] {context.thread_id}")
        apply_repair_context(context, logs)

    logs.append("")
    logs.append("批量修复完成。建议重启 Codex 以刷新界面缓存。")
    return logs


def _legacy_export_sessions_unused(export_root: Path) -> list[str]:
    if not SESSIONS_DIR.exists():
        raise SessionRepairError(f"未找到 Codex 会话目录：{SESSIONS_DIR}")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination_root = export_root / f"codex-sessions-export-{timestamp}"
    exported_sessions_dir = destination_root / "sessions"
    exported_sessions_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "exported_at": datetime.now().isoformat(),
        "source_sessions_dir": str(SESSIONS_DIR),
        "items": [],
        "errors": [],
    }

    jsonl_files = sorted(SESSIONS_DIR.rglob("*.jsonl"))
    for source_path in jsonl_files:
        relative_path = source_path.relative_to(SESSIONS_DIR)
        destination_path = exported_sessions_dir / relative_path
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination_path)

        item: dict[str, Any] = {
            "source_path": str(source_path),
            "exported_path": str(destination_path),
        }
        try:
            context = build_context(source_path)
            item.update(
                {
                    "thread_id": context.thread_id,
                    "title": context.title,
                    "created_at": context.created_at_utc.isoformat(),
                    "updated_at": context.updated_at_utc.isoformat(),
                }
            )
        except Exception as exc:
            manifest["errors"].append({"path": str(source_path), "error": str(exc)})
        manifest["items"].append(item)

    manifest_path = destination_root / EXPORT_MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    return [
        f"导出目录：{destination_root}",
        f"已导出 {len(jsonl_files)} 个会话文件。",
        f"清单文件：{manifest_path}",
        "导出的文件夹可以直接用“导入文件夹”导回 Codex。",
    ]


def export_sessions(
    export_root: Path,
    source_paths: list[Path] | None = None,
    selection_label: str | None = None,
) -> list[str]:
    if not SESSIONS_DIR.exists():
        raise SessionRepairError(f"未找到 Codex 会话目录：{SESSIONS_DIR}")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination_root = export_root / f"codex-sessions-export-{timestamp}"
    exported_sessions_dir = destination_root / "sessions"
    exported_sessions_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "exported_at": datetime.now().isoformat(),
        "source_sessions_dir": str(SESSIONS_DIR),
        "items": [],
        "errors": [],
    }

    if source_paths is None:
        jsonl_files = sorted(SESSIONS_DIR.rglob("*.jsonl"))
        selection_label = selection_label or "全部会话"
    else:
        seen: set[str] = set()
        jsonl_files = []
        for source_path in source_paths:
            normalized = normalize_display_path(source_path)
            key = str(normalized).lower()
            if key in seen:
                continue
            seen.add(key)
            jsonl_files.append(normalized)
        selection_label = selection_label or "选中会话"

    exported_count = 0
    for source_path in jsonl_files:
        item: dict[str, Any] = {"source_path": str(source_path)}
        if not source_path.exists():
            manifest["errors"].append({"path": str(source_path), "error": "文件不存在"})
            manifest["items"].append(item)
            continue

        try:
            relative_path = source_path.relative_to(SESSIONS_DIR)
        except ValueError:
            relative_path = Path(source_path.name)

        destination_path = exported_sessions_dir / relative_path
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination_path)
        exported_count += 1

        item["exported_path"] = str(destination_path)
        try:
            context = build_context(source_path)
            item.update(
                {
                    "thread_id": context.thread_id,
                    "title": context.title,
                    "created_at": context.created_at_utc.isoformat(),
                    "updated_at": context.updated_at_utc.isoformat(),
                }
            )
        except Exception as exc:
            manifest["errors"].append({"path": str(source_path), "error": str(exc)})
        manifest["items"].append(item)

    manifest_path = destination_root / EXPORT_MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    return [
        f"导出范围：{selection_label}",
        f"导出目录：{destination_root}",
        f"已导出 {exported_count} 个会话文件。",
        f"清单文件：{manifest_path}",
        "导出的文件夹可以直接用“导入会话”导回 Codex，导入时会自动修复索引。",
    ]


def find_codex_executable() -> Path | None:
    config_hint = None
    if CONFIG_PATH.exists():
        try:
            config = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            config_hint = (
                config.get("mcp_servers", {})
                .get("node_repl", {})
                .get("env", {})
                .get("CODEX_CLI_PATH")
            )
        except Exception:
            config_hint = None

    candidates: list[Path] = []
    if config_hint:
        candidates.append(Path(str(config_hint)))
    bin_root = Path.home() / "AppData" / "Local" / "OpenAI" / "Codex" / "bin"
    if bin_root.exists():
        candidates.extend(sorted(bin_root.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True))

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def ensure_toml_table(text: str, table_name: str) -> tuple[str, bool]:
    if f"[{table_name}]" in text:
        return text, False
    if text and not text.endswith("\n"):
        text += "\n"
    return text + f"\n[{table_name}]\n", True


def ensure_toml_key(text: str, table_name: str, key: str, value: str) -> tuple[str, bool]:
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines) if line.strip() == f"[{table_name}]"), None)
    if start is None:
        if text and not text.endswith("\n"):
            text += "\n"
        text += f"\n[{table_name}]\n{key} = {value}\n"
        return text, True

    end = len(lines)
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            end = index
            break

    for index in range(start + 1, end):
        stripped = lines[index].strip()
        if stripped.startswith(f"{key} ") or stripped.startswith(f"{key}="):
            desired = f"{key} = {value}"
            if lines[index].strip() == desired:
                return text, False
            lines[index] = desired
            return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), True

    lines.insert(end, f"{key} = {value}")
    return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), True


def toml_literal_path(path: Path) -> str:
    return "'" + "\\\\?\\" + str(path) + "'"


def ensure_config_entries(logs: list[str]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    original_text = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else ""
    text = original_text

    def add_marketplace(name: str, source: Path) -> None:
        nonlocal text
        table = f"marketplaces.{name}"
        text, _ = ensure_toml_table(text, table)
        text, _ = ensure_toml_key(text, table, "source_type", '"local"')
        text, _ = ensure_toml_key(text, table, "source", toml_literal_path(source))

    add_marketplace("openai-curated", CURATED_MARKETPLACE_ROOT)
    if PRIMARY_RUNTIME_ROOT.exists():
        add_marketplace("openai-primary-runtime", PRIMARY_RUNTIME_ROOT)

    plugin_names: list[str] = []
    if CURATED_CACHE_DIR.exists():
        plugin_names = sorted(path.name for path in CURATED_CACHE_DIR.iterdir() if path.is_dir())
    else:
        marketplace_plugin_root = CURATED_MARKETPLACE_ROOT / "plugins"
        if marketplace_plugin_root.exists():
            plugin_names = sorted(path.name for path in marketplace_plugin_root.iterdir() if path.is_dir())

    for plugin_name in plugin_names:
        table = f'plugins."{plugin_name}@openai-curated"'
        text, _ = ensure_toml_table(text, table)
        text, _ = ensure_toml_key(text, table, "enabled", "true")

    for plugin_name in ("documents", "spreadsheets", "presentations"):
        runtime_plugin = PRIMARY_RUNTIME_ROOT / "plugins" / plugin_name
        if runtime_plugin.exists():
            table = f'plugins."{plugin_name}@openai-primary-runtime"'
            text, _ = ensure_toml_table(text, table)
            text, _ = ensure_toml_key(text, table, "enabled", "true")

    if text != original_text:
        if CONFIG_PATH.exists():
            backup_path = make_backup(CONFIG_PATH, "plugin-skill-repair")
            logs.append(f"已备份配置：{backup_path}")
        CONFIG_PATH.write_text(text, encoding="utf-8")
        logs.append(f"已更新配置：{CONFIG_PATH}")
    else:
        logs.append("配置已包含必要 marketplace 和插件启用项。")

    try:
        tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        logs.append("配置 TOML 语法检查通过。")
    except Exception as exc:
        raise SessionRepairError(f"配置写入后 TOML 语法检查失败：{exc}") from exc


def collect_plugin_skill_status(logs: list[str]) -> dict[str, Any]:
    status: dict[str, Any] = {
        "curated_marketplace_exists": CURATED_MARKETPLACE_ROOT.exists(),
        "curated_cache_exists": CURATED_CACHE_DIR.exists(),
        "curated_plugins": [],
        "skill_files": 0,
        "missing_skill_plugins": [],
    }

    marketplace_plugin_root = CURATED_MARKETPLACE_ROOT / "plugins"
    if CURATED_CACHE_DIR.exists():
        status["curated_plugins"] = sorted(path.name for path in CURATED_CACHE_DIR.iterdir() if path.is_dir())
    elif marketplace_plugin_root.exists():
        status["curated_plugins"] = sorted(path.name for path in marketplace_plugin_root.iterdir() if path.is_dir())

    if CURATED_CACHE_DIR.exists():
        status["skill_files"] = sum(1 for _ in CURATED_CACHE_DIR.rglob("SKILL.md"))

    missing_skill_plugins = []
    for plugin_name in status["curated_plugins"]:
        cache_dir = CURATED_CACHE_DIR / plugin_name
        marketplace_dir = marketplace_plugin_root / plugin_name
        has_skills = any(cache_dir.rglob("SKILL.md")) if cache_dir.exists() else False
        if not has_skills and marketplace_dir.exists():
            has_skills = any(marketplace_dir.rglob("SKILL.md"))
        if not has_skills:
            missing_skill_plugins.append(plugin_name)
    status["missing_skill_plugins"] = missing_skill_plugins

    logs.append(f"检测到 curated 插件目录：{len(status['curated_plugins'])} 个")
    logs.append(f"检测到 curated SKILL.md：{status['skill_files']} 个")
    if missing_skill_plugins:
        logs.append(f"这些插件未发现 SKILL.md，可能只提供 app/MCP 能力：{', '.join(missing_skill_plugins)}")
    return status


def run_codex_plugin_verification(logs: list[str]) -> None:
    codex_exe = find_codex_executable()
    if codex_exe is None:
        logs.append("未找到 codex.exe，已跳过 CLI 验证。")
        return

    logs.append(f"使用 Codex CLI 验证：{codex_exe}")
    try:
        result = subprocess.run(
            [str(codex_exe), "plugin", "list", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
        )
    except Exception as exc:
        logs.append(f"Codex CLI 验证失败：{exc}")
        return

    if result.returncode != 0:
        logs.append(f"Codex CLI 返回错误：{result.stderr.strip() or result.stdout.strip()}")
        return

    try:
        data = json.loads(result.stdout)
        installed = data.get("installed", [])
        enabled = [
            item.get("pluginId", item.get("name"))
            for item in installed
            if item.get("enabled") and item.get("installed")
        ]
        logs.append(f"Codex CLI 已识别 installed/enabled 插件：{len(enabled)} 个")
    except Exception as exc:
        logs.append(f"Codex CLI 输出解析失败：{exc}")


def write_plugin_skill_repair_report(logs: list[str], status: dict[str, Any]) -> Path:
    report_path = Path(__file__).resolve().parent / REPAIR_REPORT_NAME
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# 修复丢失插件技能记录",
        "",
        f"## {now}",
        f"- Codex 配置：`{CONFIG_PATH}`",
        f"- curated marketplace：`{CURATED_MARKETPLACE_ROOT}`",
        f"- curated cache：`{CURATED_CACHE_DIR}`",
        f"- 检测到 curated 插件：{len(status.get('curated_plugins', []))} 个",
        f"- 检测到 `SKILL.md`：{status.get('skill_files', 0)} 个",
        "",
        "## 执行日志",
    ]
    lines.extend(f"- {line}" for line in logs)
    if status.get("missing_skill_plugins"):
        lines.extend(["", "## 说明"])
        lines.append("- 部分插件没有 `SKILL.md` 并不一定是异常，它们可能只提供 app、MCP 或远程连接能力。")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def repair_missing_plugin_skills() -> list[str]:
    logs: list[str] = ["开始修复丢失插件技能。"]
    if not CURATED_MARKETPLACE_ROOT.exists() and not CURATED_CACHE_DIR.exists():
        raise SessionRepairError(
            f"未找到 openai-curated marketplace 或缓存目录：{CURATED_MARKETPLACE_ROOT} / {CURATED_CACHE_DIR}"
        )

    ensure_config_entries(logs)
    status = collect_plugin_skill_status(logs)
    run_codex_plugin_verification(logs)
    report_path = Path(__file__).resolve().parent / REPAIR_REPORT_NAME
    logs.append(f"已写入修复记录：{report_path}")
    logs.append("插件技能修复完成。建议重启 Codex 以刷新插件和技能列表。")
    write_plugin_skill_repair_report(logs, status)
    return logs


def log_unhandled_error(exc: BaseException) -> None:
    ERROR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ERROR_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"[{datetime.now().isoformat()}] {type(exc).__name__}: {exc}\n")


class _LegacyAppUnused:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Codex 会话修复工具")
        self.root.geometry("880x620")

        self.path_var = tk.StringVar()
        self.status_var = tk.StringVar(value="选择单个会话文件、导入文件夹，或导出当前会话库。")

        frame = ttk.Frame(root, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="会话文件或目录").pack(anchor="w")
        path_row = ttk.Frame(frame)
        path_row.pack(fill="x", pady=(6, 12))
        ttk.Entry(path_row, textvariable=self.path_var).pack(side="left", fill="x", expand=True)
        ttk.Button(path_row, text="选文件", command=self.pick_file).pack(side="left", padx=(8, 0))
        ttk.Button(path_row, text="选目录", command=self.pick_directory).pack(side="left", padx=(8, 0))
        ttk.Button(path_row, text="开始修复", command=self.run_repair).pack(side="left", padx=(8, 0))

        action_row = ttk.Frame(frame)
        action_row.pack(fill="x", pady=(0, 12))
        ttk.Button(action_row, text="导入文件夹", command=self.import_directory).pack(side="left")
        ttk.Button(action_row, text="导出会话", command=self.export_current_sessions).pack(side="left", padx=(8, 0))

        ttk.Label(
            frame,
            text="功能：支持单文件修复、递归导入文件夹、导出当前会话库；导入时会自动检测子文件夹中的 jsonl，复制到标准 rollout 路径，修复 session_index、state_5.sqlite 和全局状态。",
            wraplength=820,
        ).pack(anchor="w", pady=(0, 10))

        self.output = tk.Text(frame, wrap="word", height=28)
        self.output.pack(fill="both", expand=True)
        self.output.configure(state="disabled")

        ttk.Label(frame, textvariable=self.status_var).pack(anchor="w", pady=(10, 0))

    def append_output(self, text: str) -> None:
        self.output.configure(state="normal")
        self.output.insert("end", text + "\n")
        self.output.see("end")
        self.output.configure(state="disabled")
        self.root.update_idletasks()

    def pick_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 Codex 会话文件",
            initialdir=str(SESSIONS_DIR if SESSIONS_DIR.exists() else Path.home()),
            filetypes=[("JSONL", "*.jsonl"), ("All files", "*.*")],
        )
        if path:
            self.path_var.set(path)

    def pick_directory(self) -> None:
        path = filedialog.askdirectory(
            title="选择包含会话文件的目录",
            initialdir=str(SESSIONS_DIR if SESSIONS_DIR.exists() else Path.home()),
        )
        if path:
            self.path_var.set(path)

    def run_repair(self) -> None:
        chosen = self.path_var.get().strip()
        if not chosen:
            messagebox.showerror("错误", "请先选择会话文件或目录。")
            return

        self.run_target(Path(chosen))

    def run_target(self, target: Path) -> None:
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.configure(state="disabled")

        try:
            if target.is_dir():
                logs = repair_directory(target)
                status = "导入/批量修复完成。"
                done_message = "文件夹导入和批量修复完成。"
            else:
                context, logs = repair_session(target)
                status = f"修复完成：{context.thread_id}"
                done_message = f"线程 {context.thread_id} 修复完成。"
            for line in logs:
                self.append_output(line)
            self.status_var.set(status)
            messagebox.showinfo("完成", done_message)
        except Exception as exc:
            self.append_output(f"错误：{exc}")
            self.status_var.set("修复失败。")
            messagebox.showerror("修复失败", str(exc))

    def import_directory(self) -> None:
        path = filedialog.askdirectory(
            title="选择要导入的会话文件夹",
            initialdir=str(SESSIONS_DIR if SESSIONS_DIR.exists() else Path.home()),
        )
        if path:
            self.path_var.set(path)
            self.run_target(Path(path))

    def export_current_sessions(self) -> None:
        path = filedialog.askdirectory(
            title="选择导出保存位置",
            initialdir=str(Path.home() / "Desktop"),
        )
        if not path:
            return

        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.configure(state="disabled")

        try:
            logs = export_sessions(Path(path))
            for line in logs:
                self.append_output(line)
            self.status_var.set("导出完成。")
            messagebox.showinfo("完成", "会话导出完成。")
        except Exception as exc:
            self.append_output(f"错误：{exc}")
            self.status_var.set("导出失败。")
            messagebox.showerror("导出失败", str(exc))


class _HaloAppDraftUnused:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("codex会话同步工具")
        self.root.geometry("1120x720")
        self.root.minsize(920, 620)

        self.sessions: list[SessionListItem] = []
        self.item_by_iid: dict[str, SessionListItem] = {}
        self.path_var = tk.StringVar(value=str(SESSIONS_DIR))
        self.status_var = tk.StringVar(value="就绪")
        self.count_var = tk.StringVar(value="共 0 个会话")

        self.configure_style()
        self.build_ui()
        self.refresh_sessions()

    def configure_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 14, "bold"))
        style.configure("Card.TLabelframe", padding=12)
        style.configure("Card.TLabelframe.Label", font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 11, "bold"), padding=(16, 8))
        style.configure("Treeview", rowheight=34, font=("Microsoft YaHei UI", 10))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"))

    def build_ui(self) -> None:
        shell = ttk.Frame(self.root, padding=14)
        shell.pack(fill="both", expand=True)

        path_card = ttk.LabelFrame(shell, text="会话路径", style="Card.TLabelframe")
        path_card.pack(fill="x")
        path_row = ttk.Frame(path_card)
        path_row.pack(fill="x")
        ttk.Entry(path_row, textvariable=self.path_var).pack(side="left", fill="x", expand=True)
        ttk.Button(path_row, text="浏览...", command=self.browse_session_dir).pack(side="left", padx=(10, 0))

        list_card = ttk.LabelFrame(shell, text="对话列表", style="Card.TLabelframe")
        list_card.pack(fill="both", expand=True, pady=(12, 0))
        tree_wrap = ttk.Frame(list_card)
        tree_wrap.pack(fill="both", expand=True)

        self.tree = ttk.Treeview(
            tree_wrap,
            columns=("title", "thread_id"),
            show="headings",
            selectmode="extended",
            height=10,
        )
        self.tree.heading("title", text="会话名称")
        self.tree.heading("thread_id", text="会话 ID")
        self.tree.column("title", minwidth=260, width=620, stretch=True, anchor="w")
        self.tree.column("thread_id", minwidth=260, width=340, stretch=False, anchor="e")
        self.tree.tag_configure("missing", foreground="#9a3412")

        y_scroll = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=y_scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        y_scroll.pack(side="right", fill="y")

        self.tree.bind("<Button-3>", self.show_session_menu)
        self.session_menu = tk.Menu(self.root, tearoff=0)
        self.session_menu.add_command(label="打开所在文件夹", command=self.open_selected_in_explorer)

        action_row = ttk.Frame(list_card)
        action_row.pack(fill="x", pady=(12, 0))
        ttk.Button(action_row, text="导出会话", style="Primary.TButton", command=self.export_current_sessions).pack(
            side="right", padx=(10, 0)
        )
        ttk.Button(action_row, text="导入会话", style="Primary.TButton", command=self.import_directory).pack(side="right")

        log_card = ttk.LabelFrame(shell, text="日志", style="Card.TLabelframe")
        log_card.pack(fill="both", expand=False, pady=(12, 0))
        log_top = ttk.Frame(log_card)
        log_top.pack(fill="x")
        ttk.Button(log_top, text="清空日志", command=self.clear_output).pack(side="right")

        log_wrap = ttk.Frame(log_card)
        log_wrap.pack(fill="both", expand=True, pady=(8, 0))
        self.output = tk.Text(log_wrap, wrap="word", height=7, borderwidth=1, relief="solid")
        log_scroll = ttk.Scrollbar(log_wrap, orient="vertical", command=self.output.yview)
        self.output.configure(yscrollcommand=log_scroll.set, state="disabled")
        self.output.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

        status_bar = ttk.Frame(self.root, padding=(14, 8))
        status_bar.pack(fill="x", side="bottom")
        ttk.Label(status_bar, textvariable=self.status_var).pack(side="left")
        ttk.Label(status_bar, textvariable=self.count_var, foreground="#1f5fa8").pack(side="right")

    def append_output(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.output.configure(state="normal")
        self.output.insert("end", f"[{stamp}] {text}\n")
        self.output.see("end")
        self.output.configure(state="disabled")
        self.root.update_idletasks()

    def clear_output(self) -> None:
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.configure(state="disabled")

    def browse_session_dir(self) -> None:
        path = filedialog.askdirectory(
            title="请选择会话目录",
            initialdir=str(SESSIONS_DIR if SESSIONS_DIR.exists() else Path.home()),
        )
        if path:
            self.path_var.set(path)
            self.refresh_sessions()

    def refresh_sessions(self) -> None:
        root = Path(self.path_var.get().strip() or SESSIONS_DIR)
        self.tree.delete(*self.tree.get_children())
        self.item_by_iid.clear()

        self.sessions = load_session_items(root)
        for index, item in enumerate(self.sessions):
            iid = f"{index}:{item.thread_id}"
            tags = ("missing",) if not item.source_path.exists() else ()
            self.tree.insert("", "end", iid=iid, values=(item.title, item.thread_id), tags=tags)
            self.item_by_iid[iid] = item

        self.count_var.set(f"共 {len(self.sessions)} 个会话")
        self.status_var.set("就绪")
        self.append_output(f"已加载会话列表：{len(self.sessions)} 个")

    def selected_session_items(self) -> list[SessionListItem]:
        selected_iids = list(self.tree.selection())
        if not selected_iids:
            return self.sessions
        return [self.item_by_iid[iid] for iid in selected_iids if iid in self.item_by_iid]

    def import_directory(self) -> None:
        path = filedialog.askdirectory(
            title="请选择要导入的会话文件夹",
            initialdir=str(Path.home() / "Desktop"),
        )
        if not path:
            return

        self.clear_output()
        try:
            logs = repair_directory(Path(path))
            for line in logs:
                self.append_output(line)
            self.path_var.set(str(SESSIONS_DIR))
            self.refresh_sessions()
            self.status_var.set("导入并修复完成")
            messagebox.showinfo("完成", "会话导入并自动修复完成。")
        except Exception as exc:
            self.append_output(f"错误：{exc}")
            self.status_var.set("导入失败")
            messagebox.showerror("导入失败", str(exc))

    def export_current_sessions(self) -> None:
        items = self.selected_session_items()
        if not items:
            messagebox.showerror("导出失败", "当前没有可导出的会话。")
            return

        path = filedialog.askdirectory(
            title="请选择导出保存位置",
            initialdir=str(Path.home() / "Desktop"),
        )
        if not path:
            return

        selected_count = len(self.tree.selection())
        source_paths = [item.source_path for item in items]
        self.clear_output()
        try:
            scope_label = "选中会话" if selected_count else "全部会话"
            logs = export_sessions(Path(path), source_paths, scope_label)
            for line in logs:
                self.append_output(line)
            if selected_count:
                self.status_var.set(f"已导出选中会话：{len(items)} 个")
                messagebox.showinfo("完成", f"已导出选中会话 {len(items)} 个。")
            else:
                self.status_var.set(f"已导出全部会话：{len(items)} 个")
                messagebox.showinfo("完成", f"已导出全部会话 {len(items)} 个。")
        except Exception as exc:
            self.append_output(f"错误：{exc}")
            self.status_var.set("导出失败")
            messagebox.showerror("导出失败", str(exc))

    def show_session_menu(self, event: tk.Event) -> None:
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        self.tree.focus(iid)
        self.session_menu.tk_popup(event.x_root, event.y_root)

    def open_selected_in_explorer(self) -> None:
        iid = self.tree.focus()
        if not iid and self.tree.selection():
            iid = self.tree.selection()[0]
        item = self.item_by_iid.get(iid)
        if not item:
            return

        path = normalize_display_path(item.source_path)
        if not path.exists():
            messagebox.showerror("文件不存在", f"找不到会话文件：{path}")
            return
        subprocess.Popen(["explorer.exe", f"/select,{path}"])


class _GlassAppDraftUnused:
    BG = "#0A0B0F"
    SURFACE = "#14151C"
    ELEVATED = "#1E2029"
    BORDER = "#2A2D38"
    BORDER_STRONG = "#3A3D4A"
    TEXT = "#F2F4F8"
    MUTED = "#9AA0AE"
    FAINT = "#5C6170"
    PRIMARY = "#5B6BFF"
    PRIMARY_HOVER = "#7886FF"
    CYAN = "#3DD7E5"
    SUCCESS = "#2BE08C"
    ERROR = "#FF3A5C"

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("会话备份工具")
        self.root.geometry("1180x760")
        self.root.minsize(980, 640)
        self.root.configure(bg=self.BG)

        self.sessions: list[SessionListItem] = []
        self.item_by_iid: dict[str, SessionListItem] = {}
        self.path_var = tk.StringVar(value=str(SESSIONS_DIR))
        self.status_var = tk.StringVar(value="就绪")
        self.count_var = tk.StringVar(value="共 0 个会话")

        self.configure_style()
        self.build_ui()
        self.refresh_sessions(log=True)

    def configure_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Halo.Treeview",
            background=self.SURFACE,
            fieldbackground=self.SURFACE,
            foreground=self.TEXT,
            borderwidth=0,
            rowheight=38,
            font=("Microsoft YaHei UI", 10),
        )
        style.configure(
            "Halo.Treeview.Heading",
            background=self.ELEVATED,
            foreground=self.MUTED,
            relief="flat",
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        style.map(
            "Halo.Treeview",
            background=[("selected", self.PRIMARY)],
            foreground=[("selected", "#FFFFFF")],
        )
        style.configure(
            "Halo.Vertical.TScrollbar",
            background=self.ELEVATED,
            troughcolor=self.SURFACE,
            bordercolor=self.BORDER,
            arrowcolor=self.MUTED,
        )

    def card(self, parent: tk.Misc) -> tk.Frame:
        frame = tk.Frame(
            parent,
            bg=self.SURFACE,
            highlightthickness=1,
            highlightbackground=self.BORDER,
            highlightcolor=self.PRIMARY,
        )
        return frame

    def label(self, parent: tk.Misc, text: str, *, size: int = 10, color: str | None = None, bold: bool = False) -> tk.Label:
        weight = "bold" if bold else "normal"
        return tk.Label(
            parent,
            text=text,
            bg=self.SURFACE,
            fg=color or self.TEXT,
            font=("Microsoft YaHei UI", size, weight),
        )

    def button(
        self,
        parent: tk.Misc,
        text: str,
        command: Any,
        *,
        primary: bool = False,
        width: int | None = None,
    ) -> tk.Button:
        bg = self.PRIMARY if primary else self.ELEVATED
        active = self.PRIMARY_HOVER if primary else self.BORDER_STRONG
        return tk.Button(
            parent,
            text=text,
            command=command,
            width=width,
            bg=bg,
            activebackground=active,
            fg="#FFFFFF" if primary else self.TEXT,
            activeforeground="#FFFFFF",
            relief="flat",
            borderwidth=0,
            padx=16,
            pady=8,
            cursor="hand2",
            font=("Microsoft YaHei UI", 10, "bold" if primary else "normal"),
        )

    def build_ui(self) -> None:
        shell = tk.Frame(self.root, bg=self.BG)
        shell.pack(fill="both", expand=True, padx=18, pady=16)

        header = tk.Frame(shell, bg=self.BG)
        header.pack(fill="x", pady=(0, 14))
        tk.Label(
            header,
            text="会话备份工具",
            bg=self.BG,
            fg=self.TEXT,
            font=("Microsoft YaHei UI", 22, "bold"),
        ).pack(side="left")
        tk.Label(
            header,
            text="Halo console",
            bg=self.BG,
            fg=self.CYAN,
            font=("Consolas", 10, "bold"),
        ).pack(side="left", padx=(12, 0), pady=(8, 0))

        path_card = self.card(shell)
        path_card.pack(fill="x")
        path_card.columnconfigure(1, weight=1)
        self.label(path_card, "会话路径", size=12, bold=True).grid(row=0, column=0, sticky="w", padx=18, pady=16)
        path_entry = tk.Entry(
            path_card,
            textvariable=self.path_var,
            bg=self.ELEVATED,
            fg=self.TEXT,
            insertbackground=self.TEXT,
            relief="flat",
            highlightthickness=1,
            highlightbackground=self.BORDER_STRONG,
            highlightcolor=self.PRIMARY,
            font=("Microsoft YaHei UI", 10),
        )
        path_entry.grid(row=0, column=1, sticky="ew", padx=(0, 12), ipady=9)
        self.button(path_card, "浏览...", self.browse_session_dir, width=10).grid(row=0, column=2, padx=(0, 18), pady=16)

        list_card = self.card(shell)
        list_card.pack(fill="both", expand=True, pady=(14, 0))
        list_card.rowconfigure(1, weight=1)
        list_card.columnconfigure(0, weight=1)
        list_header = tk.Frame(list_card, bg=self.SURFACE)
        list_header.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 10))
        self.label(list_header, "对话列表", size=13, bold=True).pack(side="left")
        self.label(list_header, "Shift/Ctrl 多选；点击空白处取消选择", size=9, color=self.MUTED).pack(
            side="left", padx=(12, 0)
        )

        tree_wrap = tk.Frame(list_card, bg=self.SURFACE)
        tree_wrap.grid(row=1, column=0, sticky="nsew", padx=18)
        tree_wrap.rowconfigure(0, weight=1)
        tree_wrap.columnconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            tree_wrap,
            columns=("seq", "title", "thread_id"),
            show="headings",
            selectmode="extended",
            style="Halo.Treeview",
        )
        self.tree.heading("seq", text="序号")
        self.tree.heading("title", text="会话名称")
        self.tree.heading("thread_id", text="会话 ID")
        self.tree.column("seq", minwidth=56, width=68, stretch=False, anchor="center")
        self.tree.column("title", minwidth=320, width=620, stretch=True, anchor="w")
        self.tree.column("thread_id", minwidth=280, width=340, stretch=False, anchor="e")
        self.tree.tag_configure("missing", foreground="#F5D547")
        self.tree.grid(row=0, column=0, sticky="nsew")

        y_scroll = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview, style="Halo.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=y_scroll.set)
        y_scroll.grid(row=0, column=1, sticky="ns")

        self.tree.bind("<Button-1>", self.clear_selection_on_blank)
        self.tree.bind("<Button-3>", self.show_session_menu)
        self.session_menu = tk.Menu(self.root, tearoff=0, bg=self.ELEVATED, fg=self.TEXT, activebackground=self.PRIMARY)
        self.session_menu.add_command(label="打开所在文件夹", command=self.open_selected_in_explorer)

        action_row = tk.Frame(list_card, bg=self.SURFACE)
        action_row.grid(row=2, column=0, sticky="ew", padx=18, pady=16)
        action_row.columnconfigure(0, weight=1)
        self.button(action_row, "导出会话", self.export_current_sessions, primary=True, width=14).grid(
            row=0, column=1, padx=(0, 10)
        )
        self.button(action_row, "导入会话", self.import_directory, primary=True, width=14).grid(row=0, column=2)

        log_card = self.card(shell)
        log_card.pack(fill="both", pady=(14, 0))
        log_card.columnconfigure(0, weight=1)
        log_top = tk.Frame(log_card, bg=self.SURFACE)
        log_top.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 8))
        self.label(log_top, "日志", size=13, bold=True).pack(side="left")
        self.button(log_top, "清空日志", self.clear_output, width=10).pack(side="right")

        log_wrap = tk.Frame(log_card, bg=self.SURFACE)
        log_wrap.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 18))
        log_wrap.columnconfigure(0, weight=1)
        self.output = tk.Text(
            log_wrap,
            wrap="word",
            height=8,
            bg="#0F1117",
            fg=self.TEXT,
            insertbackground=self.TEXT,
            selectbackground=self.PRIMARY,
            relief="flat",
            borderwidth=0,
            font=("Consolas", 10),
        )
        log_scroll = ttk.Scrollbar(log_wrap, orient="vertical", command=self.output.yview, style="Halo.Vertical.TScrollbar")
        self.output.configure(yscrollcommand=log_scroll.set, state="disabled")
        self.output.grid(row=0, column=0, sticky="ew")
        log_scroll.grid(row=0, column=1, sticky="ns")

        status_bar = tk.Frame(self.root, bg=self.BG)
        status_bar.pack(fill="x", side="bottom", padx=18, pady=(0, 12))
        tk.Label(status_bar, textvariable=self.status_var, bg=self.BG, fg=self.SUCCESS, font=("Microsoft YaHei UI", 10)).pack(
            side="left"
        )
        tk.Label(status_bar, textvariable=self.count_var, bg=self.BG, fg=self.CYAN, font=("Microsoft YaHei UI", 10)).pack(
            side="right"
        )

    def append_output(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.output.configure(state="normal")
        self.output.insert("end", f"[{stamp}] {text}\n")
        self.output.see("end")
        self.output.configure(state="disabled")
        self.output.update_idletasks()

    def clear_output(self) -> None:
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.configure(state="disabled")

    def browse_session_dir(self) -> None:
        path = filedialog.askdirectory(
            title="请选择会话目录",
            initialdir=str(SESSIONS_DIR if SESSIONS_DIR.exists() else Path.home()),
        )
        if path:
            self.path_var.set(path)
            self.refresh_sessions(log=True)

    def repair_current_path(self) -> None:
        chosen = self.path_var.get().strip()
        if not chosen:
            messagebox.showerror("修复失败", "请先选择会话文件或会话目录。")
            return

        target = Path(chosen)
        self.clear_output()
        self.append_output(f"开始修复：{target}")
        try:
            if target.is_dir():
                logs = repair_directory(target)
            else:
                _context, logs = repair_session(target)
            for line in logs:
                self.append_output(line)
            self.refresh_sessions(log=False)
            self.append_output("修复完成，列表已刷新。")
            self.status_var.set("修复完成")
            messagebox.showinfo("完成", "会话修复完成。")
        except Exception as exc:
            self.append_output(f"错误：{exc}")
            self.status_var.set("修复失败")
            messagebox.showerror("修复失败", str(exc))

    def refresh_sessions(self, *, log: bool = False) -> None:
        root = Path(self.path_var.get().strip() or SESSIONS_DIR)
        self.tree.delete(*self.tree.get_children())
        self.item_by_iid.clear()

        self.sessions = load_session_items(root)
        for index, item in enumerate(self.sessions, 1):
            iid = f"{index}:{item.thread_id}"
            tags = ("missing",) if not item.source_path.exists() else ()
            self.tree.insert("", "end", iid=iid, values=(index, item.title, item.thread_id), tags=tags)
            self.item_by_iid[iid] = item

        self.count_var.set(f"共 {len(self.sessions)} 个会话")
        self.status_var.set("就绪")
        if log:
            self.append_output(f"已加载会话列表：{len(self.sessions)} 个")

    def selected_session_items(self) -> list[SessionListItem]:
        selected_iids = list(self.tree.selection())
        if not selected_iids:
            return self.sessions
        return [self.item_by_iid[iid] for iid in selected_iids if iid in self.item_by_iid]

    def clear_selection_on_blank(self, event: tk.Event) -> str | None:
        row = self.tree.identify_row(event.y)
        region = self.tree.identify_region(event.x, event.y)
        if not row and region not in ("heading", "separator"):
            self.tree.selection_remove(self.tree.selection())
            self.tree.focus("")
            self.status_var.set("已取消选择")
            return "break"
        return None

    def import_directory(self) -> None:
        path = filedialog.askdirectory(
            title="请选择要导入的会话文件夹",
            initialdir=str(Path.home() / "Desktop"),
        )
        if not path:
            return

        import_root = Path(path)
        duplicate_mode = self.choose_duplicate_import_mode(import_root)
        if duplicate_mode is None:
            return

        self.clear_output()
        self.append_output(f"开始导入并自动修复：{path}")
        try:
            logs = repair_directory(import_root, duplicate_mode=duplicate_mode)
            for line in logs:
                self.append_output(line)
            self.path_var.set(str(SESSIONS_DIR))
            self.refresh_sessions(log=False)
            self.append_output("导入完成，列表已刷新。")
            self.status_var.set("导入并修复完成")
            messagebox.showinfo("完成", "会话导入并自动修复完成。")
        except Exception as exc:
            self.append_output(f"错误：{exc}")
            self.status_var.set("导入失败")
            messagebox.showerror("导入失败", str(exc))

    def choose_duplicate_import_mode(self, import_root: Path) -> str | None:
        duplicate_ids = find_duplicate_thread_ids_for_import(import_root)
        if not duplicate_ids:
            return "replace"

        dialog = tk.Toplevel(self.root)
        dialog.title("会话 ID 重复")
        dialog.configure(bg=self.BG)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        result = tk.StringVar(value="")
        shown_ids = "\n".join(sorted(duplicate_ids)[:8])
        if len(duplicate_ids) > 8:
            shown_ids += f"\n...另有 {len(duplicate_ids) - 8} 个重复 ID"

        frame = tk.Frame(dialog, bg=self.BG, padx=22, pady=18)
        frame.pack(fill="both", expand=True)
        tk.Label(
            frame,
            text="检测到导入会话 ID 已存在",
            bg=self.BG,
            fg=self.TEXT,
            font=("Microsoft YaHei UI", 13, "bold"),
        ).pack(anchor="w")
        tk.Label(
            frame,
            text=f"重复 ID 数量：{len(duplicate_ids)}",
            bg=self.BG,
            fg=self.MUTED,
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(anchor="w", pady=(8, 0))
        tk.Label(
            frame,
            text=shown_ids,
            bg=self.BG,
            fg=self.TEXT,
            justify="left",
            font=("Consolas", 9, "bold"),
        ).pack(anchor="w", pady=(10, 12))
        tk.Label(
            frame,
            text="替换：使用导入会话覆盖本机同 ID 会话。\n增量：保留本机会话，并给导入会话生成新 ID，列表中会出现两条内容相同但 ID 不同的会话。",
            bg=self.BG,
            fg=self.MUTED,
            justify="left",
            wraplength=520,
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(anchor="w", pady=(0, 16))

        button_row = tk.Frame(frame, bg=self.BG)
        button_row.pack(anchor="e")

        def finish(value: str) -> None:
            result.set(value)
            dialog.destroy()

        tk.Button(
            button_row,
            text="替换",
            command=lambda: finish("replace"),
            bg=self.PRIMARY,
            activebackground=self.PRIMARY_HOVER,
            fg="#FFFFFF",
            activeforeground="#FFFFFF",
            relief="flat",
            bd=0,
            padx=18,
            pady=8,
            cursor="hand2",
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(side="left", padx=(0, 10))
        tk.Button(
            button_row,
            text="增量",
            command=lambda: finish("incremental"),
            bg=self.PRIMARY,
            activebackground=self.PRIMARY_HOVER,
            fg="#FFFFFF",
            activeforeground="#FFFFFF",
            relief="flat",
            bd=0,
            padx=18,
            pady=8,
            cursor="hand2",
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(side="left")

        dialog.update_idletasks()
        x = self.root.winfo_rootx() + max((self.root.winfo_width() - dialog.winfo_width()) // 2, 0)
        y = self.root.winfo_rooty() + max((self.root.winfo_height() - dialog.winfo_height()) // 2, 0)
        dialog.geometry(f"+{x}+{y}")
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        self.root.wait_window(dialog)
        return result.get() or None

    def export_current_sessions(self) -> None:
        items = self.selected_session_items()
        if not items:
            messagebox.showerror("导出失败", "当前没有可导出的会话。")
            return

        path = filedialog.askdirectory(
            title="请选择导出保存位置",
            initialdir=str(Path.home() / "Desktop"),
        )
        if not path:
            return

        selected_count = len(self.tree.selection())
        source_paths = [item.source_path for item in items]
        scope_label = "选中会话" if selected_count else "全部会话"
        self.clear_output()
        self.append_output(f"开始导出{scope_label}：{len(items)} 个")
        try:
            logs = export_sessions(Path(path), source_paths, scope_label)
            for line in logs:
                self.append_output(line)
            self.append_output("导出完成。")
            if selected_count:
                self.status_var.set(f"已导出选中会话：{len(items)} 个")
                messagebox.showinfo("完成", f"已导出选中会话 {len(items)} 个。")
            else:
                self.status_var.set(f"已导出全部会话：{len(items)} 个")
                messagebox.showinfo("完成", f"已导出全部会话 {len(items)} 个。")
        except Exception as exc:
            self.append_output(f"错误：{exc}")
            self.status_var.set("导出失败")
            messagebox.showerror("导出失败", str(exc))

    def show_session_menu(self, event: tk.Event) -> None:
        iid = self.tree.identify_row(event.y)
        if not iid:
            self.tree.selection_remove(self.tree.selection())
            self.tree.focus("")
            self.status_var.set("已取消选择")
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        self.tree.focus(iid)
        self.session_menu.tk_popup(event.x_root, event.y_root)

    def open_selected_in_explorer(self) -> None:
        iid = self.tree.focus()
        if not iid and self.tree.selection():
            iid = self.tree.selection()[0]
        item = self.item_by_iid.get(iid)
        if not item:
            return

        path = normalize_display_path(item.source_path)
        if not path.exists():
            messagebox.showerror("文件不存在", f"找不到会话文件：{path}")
            return
        subprocess.Popen(["explorer.exe", f"/select,{path}"])


class _ResizableGlassAppDraftUnused:
    WINDOW_W = 1180
    WINDOW_H = 760
    BG = "#05070D"
    GLASS = "#10131B"
    GLASS_DARK = "#0A0D14"
    GLASS_LIGHT = "#171B26"
    BLACK_BORDER = "#000000"
    BORDER_HIGHLIGHT = "#2B3345"
    TEXT = "#F4F7FB"
    MUTED = "#A7B0C2"
    FAINT = "#6D7688"
    PRIMARY = "#7B83FF"
    PRIMARY_HOVER = "#969DFF"
    CYAN = "#67E8F9"
    SUCCESS = "#35F29A"
    ERROR = "#FF5277"

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("会话备份工具")
        self.root.geometry(f"{self.WINDOW_W}x{self.WINDOW_H}")
        self.root.minsize(self.WINDOW_W, self.WINDOW_H)
        self.root.resizable(False, False)
        self.root.configure(bg=self.BG)

        self.sessions: list[SessionListItem] = []
        self.item_by_iid: dict[str, SessionListItem] = {}
        self.path_var = tk.StringVar(value=str(SESSIONS_DIR))
        self.status_var = tk.StringVar(value="就绪")
        self.count_var = tk.StringVar(value="共 0 个会话")
        self.background_image: tk.PhotoImage | None = None

        self.configure_window_assets()
        self.configure_style()
        self.build_ui()
        self.refresh_sessions(log=True)

    def configure_window_assets(self) -> None:
        icon_path = resource_path("assets/CodexSessionRepair.ico")
        if icon_path.exists():
            try:
                self.root.iconbitmap(str(icon_path))
            except tk.TclError:
                pass

    def configure_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Glass.Treeview",
            background=self.GLASS_DARK,
            fieldbackground=self.GLASS_DARK,
            foreground=self.TEXT,
            borderwidth=0,
            rowheight=26,
            font=("Microsoft YaHei UI", 8),
        )
        style.configure(
            "Glass.Treeview.Heading",
            background=self.GLASS_LIGHT,
            foreground=self.MUTED,
            relief="flat",
            font=("Microsoft YaHei UI", 8, "bold"),
        )
        style.map(
            "Glass.Treeview",
            background=[("selected", self.PRIMARY)],
            foreground=[("selected", "#FFFFFF")],
        )
        style.configure(
            "Glass.Vertical.TScrollbar",
            background=self.GLASS_LIGHT,
            troughcolor=self.GLASS_DARK,
            bordercolor=self.BLACK_BORDER,
            arrowcolor=self.MUTED,
        )

    def install_background(self) -> None:
        bg_path = resource_path("assets/halo_background.png")
        if not bg_path.exists():
            return
        self.background_image = tk.PhotoImage(file=str(bg_path))
        bg_label = tk.Label(self.root, image=self.background_image, borderwidth=0)
        bg_label.place(x=0, y=0, relwidth=1, relheight=1)
        bg_label.lower()

    def glass_frame(self, parent: tk.Misc) -> tk.Frame:
        return tk.Frame(
            parent,
            bg=self.GLASS,
            highlightthickness=1,
            highlightbackground=self.BLACK_BORDER,
            highlightcolor=self.BORDER_HIGHLIGHT,
            bd=0,
        )

    def label(
        self,
        parent: tk.Misc,
        text: str,
        *,
        bg: str | None = None,
        color: str | None = None,
        size: int = 10,
        bold: bool = False,
        mono: bool = False,
    ) -> tk.Label:
        family = "Consolas" if mono else "Microsoft YaHei UI"
        weight = "bold" if bold else "normal"
        return tk.Label(
            parent,
            text=text,
            bg=bg or self.GLASS,
            fg=color or self.TEXT,
            font=(family, size, weight),
        )

    def button(self, parent: tk.Misc, text: str, command: Any, *, primary: bool = False, width: int = 12) -> tk.Button:
        bg = self.PRIMARY if primary else self.GLASS_LIGHT
        active_bg = self.PRIMARY_HOVER if primary else "#232A3A"
        return tk.Button(
            parent,
            text=text,
            command=command,
            width=width,
            bg=bg,
            activebackground=active_bg,
            fg="#FFFFFF",
            activeforeground="#FFFFFF",
            relief="flat",
            bd=0,
            padx=12,
            pady=7,
            cursor="hand2",
            font=("Microsoft YaHei UI", 9, "bold" if primary else "normal"),
        )

    def build_ui(self) -> None:
        self.install_background()

        header = self.glass_frame(self.root)
        header.pack(fill="x", padx=18, pady=(16, 10))
        header.columnconfigure(1, weight=1)
        self.label(header, "会话备份工具", size=18, bold=True).grid(row=0, column=0, sticky="w", padx=16, pady=(12, 2))
        self.label(header, "作者：铃音奈绪", color=self.CYAN, size=10, bold=True).grid(
            row=0, column=2, sticky="e", padx=16, pady=(12, 2)
        )
        self.label(header, "glass console / Codex session backup", color=self.MUTED, size=8, mono=True).grid(
            row=1, column=0, columnspan=3, sticky="w", padx=16, pady=(0, 12)
        )

        path_card = self.glass_frame(self.root)
        path_card.pack(fill="x", padx=18, pady=(0, 10))
        path_card.columnconfigure(1, weight=1)
        self.label(path_card, "会话路径", size=10, bold=True).grid(row=0, column=0, sticky="w", padx=16, pady=12)
        path_entry = tk.Entry(
            path_card,
            textvariable=self.path_var,
            bg=self.GLASS_DARK,
            fg=self.TEXT,
            insertbackground=self.TEXT,
            relief="flat",
            highlightthickness=1,
            highlightbackground=self.BLACK_BORDER,
            highlightcolor=self.BORDER_HIGHLIGHT,
            font=("Microsoft YaHei UI", 8),
        )
        path_entry.grid(row=0, column=1, sticky="ew", padx=(0, 10), ipady=7)
        self.button(path_card, "浏览...", self.browse_session_dir, width=9).grid(row=0, column=2, padx=(0, 16), pady=12)

        list_card = self.glass_frame(self.root)
        list_card.pack(fill="both", expand=True, padx=18, pady=(0, 10))
        list_card.rowconfigure(1, weight=1)
        list_card.columnconfigure(0, weight=1)
        list_header = tk.Frame(list_card, bg=self.GLASS)
        list_header.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 8))
        self.label(list_header, "对话列表", size=11, bold=True).pack(side="left")
        self.label(list_header, "Shift/Ctrl 多选，点击空白处取消选择", color=self.MUTED, size=8).pack(
            side="left", padx=(10, 0)
        )

        tree_wrap = tk.Frame(list_card, bg=self.GLASS, highlightthickness=1, highlightbackground=self.BLACK_BORDER)
        tree_wrap.grid(row=1, column=0, sticky="nsew", padx=16)
        tree_wrap.rowconfigure(0, weight=1)
        tree_wrap.columnconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            tree_wrap,
            columns=("seq", "title", "thread_id"),
            show="headings",
            selectmode="extended",
            style="Glass.Treeview",
        )
        self.tree.heading("seq", text="序号")
        self.tree.heading("title", text="会话名称")
        self.tree.heading("thread_id", text="会话 ID")
        self.tree.column("seq", minwidth=42, width=50, stretch=False, anchor="center")
        self.tree.column("title", minwidth=300, width=650, stretch=True, anchor="w")
        self.tree.column("thread_id", minwidth=250, width=300, stretch=False, anchor="e")
        self.tree.tag_configure("missing", foreground="#F5D547")
        self.tree.grid(row=0, column=0, sticky="nsew")
        y_scroll = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview, style="Glass.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=y_scroll.set)
        y_scroll.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<Button-1>", self.clear_selection_on_blank)
        self.tree.bind("<Button-3>", self.show_session_menu)

        self.session_menu = tk.Menu(self.root, tearoff=0, bg=self.GLASS_LIGHT, fg=self.TEXT, activebackground=self.PRIMARY)
        self.session_menu.add_command(label="打开所在文件夹", command=self.open_selected_in_explorer)

        action_row = tk.Frame(list_card, bg=self.GLASS)
        action_row.grid(row=2, column=0, sticky="ew", padx=16, pady=12)
        action_row.columnconfigure(0, weight=1)
        self.button(action_row, "导出会话", self.export_current_sessions, primary=True, width=13).grid(
            row=0, column=1, padx=(0, 10)
        )
        self.button(action_row, "导入会话", self.import_directory, primary=True, width=13).grid(row=0, column=2)

        log_card = self.glass_frame(self.root)
        log_card.pack(fill="x", side="bottom", padx=18, pady=(0, 10))
        log_card.columnconfigure(0, weight=1)
        log_top = tk.Frame(log_card, bg=self.GLASS)
        log_top.grid(row=0, column=0, sticky="ew", padx=16, pady=(10, 6))
        self.label(log_top, "日志", size=11, bold=True).pack(side="left")
        self.button(log_top, "清空日志", self.clear_output, width=9).pack(side="right")

        log_wrap = tk.Frame(log_card, bg=self.GLASS, highlightthickness=1, highlightbackground=self.BLACK_BORDER)
        log_wrap.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 10))
        log_wrap.columnconfigure(0, weight=1)
        self.output = tk.Text(
            log_wrap,
            wrap="word",
            height=5,
            bg="#070A10",
            fg=self.TEXT,
            insertbackground=self.TEXT,
            selectbackground=self.PRIMARY,
            relief="flat",
            bd=0,
            font=("Consolas", 8),
        )
        log_scroll = ttk.Scrollbar(log_wrap, orient="vertical", command=self.output.yview, style="Glass.Vertical.TScrollbar")
        self.output.configure(yscrollcommand=log_scroll.set, state="disabled")
        self.output.grid(row=0, column=0, sticky="ew")
        log_scroll.grid(row=0, column=1, sticky="ns")

        status_bar = tk.Frame(self.root, bg=self.GLASS_DARK, highlightthickness=1, highlightbackground=self.BLACK_BORDER)
        status_bar.pack(fill="x", side="bottom", padx=18, pady=(0, 10))
        tk.Label(status_bar, textvariable=self.status_var, bg=self.GLASS_DARK, fg=self.SUCCESS, font=("Microsoft YaHei UI", 8)).pack(
            side="left", padx=10, pady=6
        )
        tk.Label(status_bar, textvariable=self.count_var, bg=self.GLASS_DARK, fg=self.CYAN, font=("Microsoft YaHei UI", 8)).pack(
            side="right", padx=10, pady=6
        )

    def append_output(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.output.configure(state="normal")
        self.output.insert("end", f"[{stamp}] {text}\n")
        self.output.see("end")
        self.output.configure(state="disabled")
        self.output.update_idletasks()

    def clear_output(self) -> None:
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.configure(state="disabled")

    def browse_session_dir(self) -> None:
        path = filedialog.askdirectory(
            title="请选择会话目录",
            initialdir=str(SESSIONS_DIR if SESSIONS_DIR.exists() else Path.home()),
        )
        if path:
            self.path_var.set(path)
            self.refresh_sessions(log=True)

    def repair_current_path(self) -> None:
        chosen = self.path_var.get().strip()
        if not chosen:
            messagebox.showerror("修复失败", "请先选择会话文件或会话目录。")
            return

        target = Path(chosen)
        self.clear_output()
        self.append_output(f"开始修复：{target}")
        try:
            if target.is_dir():
                logs = repair_directory(target)
            else:
                _context, logs = repair_session(target)
            for line in logs:
                self.append_output(line)
            self.refresh_sessions(log=False)
            self.append_output("修复完成，列表已刷新。")
            self.status_var.set("修复完成")
            messagebox.showinfo("完成", "会话修复完成。")
        except Exception as exc:
            self.append_output(f"错误：{exc}")
            self.status_var.set("修复失败")
            messagebox.showerror("修复失败", str(exc))

    def refresh_sessions(self, *, log: bool = False) -> None:
        root = Path(self.path_var.get().strip() or SESSIONS_DIR)
        self.tree.delete(*self.tree.get_children())
        self.item_by_iid.clear()

        self.sessions = load_session_items(root)
        for index, item in enumerate(self.sessions, 1):
            iid = f"{index}:{item.thread_id}"
            tags = ("missing",) if not item.source_path.exists() else ()
            self.tree.insert("", "end", iid=iid, values=(index, item.title, item.thread_id), tags=tags)
            self.item_by_iid[iid] = item

        self.count_var.set(f"共 {len(self.sessions)} 个会话")
        self.status_var.set("就绪")
        if log:
            self.append_output(f"已加载会话列表：{len(self.sessions)} 个")

    def selected_session_items(self) -> list[SessionListItem]:
        selected_iids = list(self.tree.selection())
        if not selected_iids:
            return self.sessions
        return [self.item_by_iid[iid] for iid in selected_iids if iid in self.item_by_iid]

    def clear_selection_on_blank(self, event: tk.Event) -> str | None:
        row = self.tree.identify_row(event.y)
        region = self.tree.identify_region(event.x, event.y)
        if not row and region not in ("heading", "separator"):
            self.tree.selection_remove(self.tree.selection())
            self.tree.focus("")
            self.status_var.set("已取消选择")
            return "break"
        return None

    def import_directory(self) -> None:
        path = filedialog.askdirectory(
            title="请选择要导入的会话文件夹",
            initialdir=str(Path.home() / "Desktop"),
        )
        if not path:
            return

        import_root = Path(path)
        duplicate_mode = self.choose_duplicate_import_mode(import_root)
        if duplicate_mode is None:
            return

        self.clear_output()
        self.append_output(f"开始导入并自动修复：{path}")
        try:
            logs = repair_directory(import_root, duplicate_mode=duplicate_mode)
            for line in logs:
                self.append_output(line)
            self.path_var.set(str(SESSIONS_DIR))
            self.refresh_sessions(log=False)
            self.append_output("导入完成，列表已刷新。")
            self.status_var.set("导入并修复完成")
            messagebox.showinfo("完成", "会话导入并自动修复完成。")
        except Exception as exc:
            self.append_output(f"错误：{exc}")
            self.status_var.set("导入失败")
            messagebox.showerror("导入失败", str(exc))

    def choose_duplicate_import_mode(self, import_root: Path) -> str | None:
        duplicate_ids = find_duplicate_thread_ids_for_import(import_root)
        if not duplicate_ids:
            return "replace"

        dialog = tk.Toplevel(self.root)
        dialog.title("会话 ID 重复")
        dialog.configure(bg=self.BG)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        result = tk.StringVar(value="")
        shown_ids = "\n".join(sorted(duplicate_ids)[:8])
        if len(duplicate_ids) > 8:
            shown_ids += f"\n...另有 {len(duplicate_ids) - 8} 个重复 ID"

        frame = tk.Frame(dialog, bg=self.BG, padx=22, pady=18)
        frame.pack(fill="both", expand=True)
        tk.Label(
            frame,
            text="检测到导入会话 ID 已存在",
            bg=self.BG,
            fg=self.TEXT,
            font=("Microsoft YaHei UI", 13, "bold"),
        ).pack(anchor="w")
        tk.Label(
            frame,
            text=f"重复 ID 数量：{len(duplicate_ids)}",
            bg=self.BG,
            fg=self.MUTED,
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(anchor="w", pady=(8, 0))
        tk.Label(
            frame,
            text=shown_ids,
            bg=self.BG,
            fg=self.TEXT,
            justify="left",
            font=("Consolas", 9, "bold"),
        ).pack(anchor="w", pady=(10, 12))
        tk.Label(
            frame,
            text="替换：使用导入会话覆盖本机同 ID 会话。\n增量：保留本机会话，并给导入会话生成新 ID，列表中会出现两条内容相同但 ID 不同的会话。",
            bg=self.BG,
            fg=self.MUTED,
            justify="left",
            wraplength=520,
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(anchor="w", pady=(0, 16))

        button_row = tk.Frame(frame, bg=self.BG)
        button_row.pack(anchor="e")

        def finish(value: str) -> None:
            result.set(value)
            dialog.destroy()

        for text, value in (("替换", "replace"), ("增量", "incremental")):
            tk.Button(
                button_row,
                text=text,
                command=lambda selected=value: finish(selected),
                bg=self.PRIMARY,
                activebackground=self.PRIMARY_HOVER,
                fg="#FFFFFF",
                activeforeground="#FFFFFF",
                relief="flat",
                bd=0,
                padx=18,
                pady=8,
                cursor="hand2",
                font=("Microsoft YaHei UI", 10, "bold"),
            ).pack(side="left", padx=(0, 10))

        dialog.update_idletasks()
        x = self.root.winfo_rootx() + max((self.root.winfo_width() - dialog.winfo_width()) // 2, 0)
        y = self.root.winfo_rooty() + max((self.root.winfo_height() - dialog.winfo_height()) // 2, 0)
        dialog.geometry(f"+{x}+{y}")
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        self.root.wait_window(dialog)
        return result.get() or None

    def export_current_sessions(self) -> None:
        items = self.selected_session_items()
        if not items:
            messagebox.showerror("导出失败", "当前没有可导出的会话。")
            return

        path = filedialog.askdirectory(
            title="请选择导出保存位置",
            initialdir=str(Path.home() / "Desktop"),
        )
        if not path:
            return

        selected_count = len(self.tree.selection())
        source_paths = [item.source_path for item in items]
        scope_label = "选中会话" if selected_count else "全部会话"
        self.clear_output()
        self.append_output(f"开始导出{scope_label}：{len(items)} 个")
        try:
            logs = export_sessions(Path(path), source_paths, scope_label)
            for line in logs:
                self.append_output(line)
            self.append_output("导出完成。")
            if selected_count:
                self.status_var.set(f"已导出选中会话：{len(items)} 个")
                messagebox.showinfo("完成", f"已导出选中会话 {len(items)} 个。")
            else:
                self.status_var.set(f"已导出全部会话：{len(items)} 个")
                messagebox.showinfo("完成", f"已导出全部会话 {len(items)} 个。")
        except Exception as exc:
            self.append_output(f"错误：{exc}")
            self.status_var.set("导出失败")
            messagebox.showerror("导出失败", str(exc))

    def show_session_menu(self, event: tk.Event) -> None:
        iid = self.tree.identify_row(event.y)
        if not iid:
            self.tree.selection_remove(self.tree.selection())
            self.tree.focus("")
            self.status_var.set("已取消选择")
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        self.tree.focus(iid)
        self.session_menu.tk_popup(event.x_root, event.y_root)

    def open_selected_in_explorer(self) -> None:
        iid = self.tree.focus()
        if not iid and self.tree.selection():
            iid = self.tree.selection()[0]
        item = self.item_by_iid.get(iid)
        if not item:
            return

        path = normalize_display_path(item.source_path)
        if not path.exists():
            messagebox.showerror("文件不存在", f"找不到会话文件：{path}")
            return
        subprocess.Popen(["explorer.exe", f"/select,{path}"])


class _CanvasGlassAppDraftUnused:
    BG = "#07101B"
    PANEL = "#0B1421"
    PANEL_SOFT = "#111B2A"
    FIELD = "#07101A"
    BORDER = "#C9D7FF"
    BORDER_BRIGHT = "#7B83FF"
    TEXT = "#F4F7FB"
    MUTED = "#B8C2D8"
    PRIMARY = "#7B83FF"
    PRIMARY_HOVER = "#98A0FF"
    CYAN = "#67E8F9"
    SUCCESS = "#35F29A"

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("会话备份工具")
        self.root.geometry("1180x820")
        self.root.minsize(960, 680)
        self.root.configure(bg=self.BG)

        self.sessions: list[SessionListItem] = []
        self.item_by_iid: dict[str, SessionListItem] = {}
        self.path_var = tk.StringVar(value=str(SESSIONS_DIR))
        self.status_var = tk.StringVar(value="就绪")
        self.count_var = tk.StringVar(value="共 0 个会话")

        self.background_original: Image.Image | None = None
        self.background_photo: ImageTk.PhotoImage | None = None
        self.background_label: tk.Label | None = None
        self.resize_after_id: str | None = None
        self.last_background_size: tuple[int, int] = (0, 0)

        self.configure_window_assets()
        self.configure_style()
        self.build_ui()
        self.refresh_sessions(log=True)
        self.root.bind("<Configure>", self.schedule_background_resize)

    def configure_window_assets(self) -> None:
        icon_path = resource_path("assets/CodexSessionRepair.ico")
        if icon_path.exists():
            try:
                self.root.iconbitmap(str(icon_path))
            except tk.TclError:
                pass

        bg_path = resource_path("assets/halo_background.png")
        if bg_path.exists():
            self.background_original = Image.open(bg_path).convert("RGB")

    def configure_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Glass.Treeview",
            background=self.FIELD,
            fieldbackground=self.FIELD,
            foreground=self.TEXT,
            borderwidth=0,
            rowheight=34,
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.configure(
            "Glass.Treeview.Heading",
            background=self.PANEL_SOFT,
            foreground=self.MUTED,
            relief="flat",
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.map(
            "Glass.Treeview",
            background=[("selected", self.PRIMARY)],
            foreground=[("selected", "#FFFFFF")],
        )
        style.configure(
            "Glass.Vertical.TScrollbar",
            background=self.PANEL_SOFT,
            troughcolor=self.FIELD,
            bordercolor=self.BORDER,
            arrowcolor=self.MUTED,
        )

    def schedule_background_resize(self, event: tk.Event | None = None) -> None:
        if event is not None and event.widget is not self.root:
            return
        if self.resize_after_id:
            self.root.after_cancel(self.resize_after_id)
        self.resize_after_id = self.root.after(80, self.resize_background)

    def resize_background(self) -> None:
        self.resize_after_id = None
        width = max(self.root.winfo_width(), 1)
        height = max(self.root.winfo_height(), 1)
        if (width, height) == self.last_background_size:
            return
        self.last_background_size = (width, height)

        if self.background_original is None:
            return

        src_w, src_h = self.background_original.size
        scale = max(width / src_w, height / src_h)
        new_w = max(int(src_w * scale), 1)
        new_h = max(int(src_h * scale), 1)
        resized = self.background_original.resize((new_w, new_h), Image.Resampling.LANCZOS)
        left = max((new_w - width) // 2, 0)
        top = max((new_h - height) // 2, 0)
        cropped = resized.crop((left, top, left + width, top + height)).convert("RGBA")

        veil = Image.new("RGBA", (width, height), (3, 8, 16, 18))
        composed = Image.alpha_composite(cropped, veil)
        draw = ImageDraw.Draw(composed)
        draw.rectangle((14, 14, width - 15, height - 15), outline=(255, 255, 255, 45), width=1)

        self.background_photo = ImageTk.PhotoImage(composed)
        if self.background_label is None:
            self.background_label = tk.Label(self.root, image=self.background_photo, bd=0)
            self.background_label.place(x=0, y=0, relwidth=1, relheight=1)
            self.background_label.lower()
        else:
            self.background_label.configure(image=self.background_photo)

    def frame(self, parent: tk.Misc, *, pad: int = 0) -> tk.Frame:
        frame = tk.Frame(
            parent,
            bg=self.PANEL,
            highlightthickness=1,
            highlightbackground=self.BORDER,
            highlightcolor=self.BORDER_BRIGHT,
            bd=0,
        )
        if pad:
            frame.configure(padx=pad, pady=pad)
        return frame

    def label(
        self,
        parent: tk.Misc,
        text: str,
        *,
        size: int = 10,
        color: str | None = None,
        bold: bool = True,
        bg: str | None = None,
    ) -> tk.Label:
        return tk.Label(
            parent,
            text=text,
            bg=bg or self.PANEL,
            fg=color or self.TEXT,
            font=("Microsoft YaHei UI", size, "bold" if bold else "normal"),
        )

    def button(self, parent: tk.Misc, text: str, command: Any, *, primary: bool = False, width: int = 10) -> tk.Button:
        bg = self.PRIMARY if primary else self.PANEL_SOFT
        active_bg = self.PRIMARY_HOVER if primary else "#16243A"
        return tk.Button(
            parent,
            text=text,
            command=command,
            width=width,
            bg=bg,
            activebackground=active_bg,
            fg="#FFFFFF",
            activeforeground="#FFFFFF",
            relief="flat",
            bd=0,
            padx=10,
            pady=7,
            cursor="hand2",
            font=("Microsoft YaHei UI", 10, "bold"),
        )

    def build_ui(self) -> None:
        self.resize_background()

        path_card = self.frame(self.root)
        path_card.pack(fill="x", padx=18, pady=(18, 10))
        path_card.columnconfigure(1, weight=1)
        self.label(path_card, "会话路径", size=15).grid(row=0, column=0, sticky="w", padx=18, pady=16)
        path_entry = tk.Entry(
            path_card,
            textvariable=self.path_var,
            bg=self.FIELD,
            fg=self.TEXT,
            insertbackground=self.TEXT,
            relief="flat",
            highlightthickness=1,
            highlightbackground=self.BORDER,
            highlightcolor=self.BORDER_BRIGHT,
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        path_entry.grid(row=0, column=1, sticky="ew", padx=(10, 10), ipady=8)
        self.button(path_card, "浏览...", self.browse_session_dir, width=9).grid(row=0, column=2, padx=(0, 18), pady=14)
        self.label(path_card, "作者：铃音奈绪", size=16, color=self.PRIMARY).grid(
            row=0, column=3, sticky="e", padx=(0, 18)
        )

        list_card = self.frame(self.root)
        list_card.pack(fill="both", expand=True, padx=18, pady=(0, 10))
        list_card.rowconfigure(1, weight=1)
        list_card.columnconfigure(0, weight=1)

        list_header = tk.Frame(list_card, bg=self.PANEL)
        list_header.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 10))
        list_header.columnconfigure(1, weight=1)
        self.label(list_header, "对话列表", size=15).grid(row=0, column=0, sticky="w")
        self.label(list_header, "Shift/Ctrl 多选，点击空白处取消选择", size=9, color=self.MUTED, bold=True).grid(
            row=0, column=1, sticky="w", padx=(14, 0)
        )
        self.button(list_header, "刷新", self.refresh_button, primary=True, width=8).grid(row=0, column=2, padx=(10, 10))
        self.button(list_header, "导出会话", self.export_current_sessions, primary=True, width=12).grid(
            row=0, column=3, padx=(0, 10)
        )
        self.button(list_header, "导入会话", self.import_directory, primary=True, width=12).grid(row=0, column=4)

        tree_wrap = tk.Frame(list_card, bg=self.PANEL, highlightthickness=1, highlightbackground=self.BORDER)
        tree_wrap.grid(row=1, column=0, sticky="nsew", padx=18, pady=(0, 14))
        tree_wrap.rowconfigure(0, weight=1)
        tree_wrap.columnconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            tree_wrap,
            columns=("seq", "title", "thread_id"),
            show="headings",
            selectmode="extended",
            style="Glass.Treeview",
        )
        self.tree.heading("seq", text="序号")
        self.tree.heading("title", text="会话名称")
        self.tree.heading("thread_id", text="会话 ID")
        self.tree.column("seq", minwidth=54, width=66, stretch=False, anchor="center")
        self.tree.column("title", minwidth=320, width=700, stretch=True, anchor="w")
        self.tree.column("thread_id", minwidth=280, width=340, stretch=False, anchor="e")
        self.tree.tag_configure("missing", foreground="#F5D547")
        self.tree.grid(row=0, column=0, sticky="nsew")

        y_scroll = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview, style="Glass.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=y_scroll.set)
        y_scroll.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<Button-1>", self.clear_selection_on_blank)
        self.tree.bind("<Button-3>", self.show_session_menu)

        self.session_menu = tk.Menu(self.root, tearoff=0, bg=self.PANEL_SOFT, fg=self.TEXT, activebackground=self.PRIMARY)
        self.session_menu.add_command(label="打开所在文件夹", command=self.open_selected_in_explorer)

        log_card = self.frame(self.root)
        log_card.pack(fill="both", expand=False, padx=18, pady=(0, 12))
        log_card.columnconfigure(0, weight=1)
        log_top = tk.Frame(log_card, bg=self.PANEL)
        log_top.grid(row=0, column=0, sticky="ew", padx=18, pady=(12, 8))
        self.label(log_top, "日志", size=15).pack(side="left")
        self.button(log_top, "清空日志", self.clear_output, width=9).pack(side="right")

        log_wrap = tk.Frame(log_card, bg=self.PANEL, highlightthickness=1, highlightbackground=self.BORDER)
        log_wrap.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 12))
        log_wrap.columnconfigure(0, weight=1)
        self.output = tk.Text(
            log_wrap,
            wrap="word",
            height=8,
            bg=self.FIELD,
            fg=self.TEXT,
            insertbackground=self.TEXT,
            selectbackground=self.PRIMARY,
            relief="flat",
            bd=0,
            font=("Consolas", 10, "bold"),
        )
        log_scroll = ttk.Scrollbar(log_wrap, orient="vertical", command=self.output.yview, style="Glass.Vertical.TScrollbar")
        self.output.configure(yscrollcommand=log_scroll.set, state="disabled")
        self.output.grid(row=0, column=0, sticky="ew")
        log_scroll.grid(row=0, column=1, sticky="ns")

        status_bar = tk.Frame(self.root, bg=self.PANEL, highlightthickness=1, highlightbackground=self.BORDER)
        status_bar.pack(fill="x", side="bottom", padx=18, pady=(0, 12))
        tk.Label(status_bar, textvariable=self.status_var, bg=self.PANEL, fg=self.SUCCESS, font=("Microsoft YaHei UI", 10, "bold")).pack(
            side="left", padx=12, pady=7
        )
        tk.Label(status_bar, textvariable=self.count_var, bg=self.PANEL, fg=self.CYAN, font=("Microsoft YaHei UI", 10, "bold")).pack(
            side="right", padx=12, pady=7
        )

    def refresh_button(self) -> None:
        self.refresh_sessions(log=True)

    def append_output(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.output.configure(state="normal")
        self.output.insert("end", f"[{stamp}] {text}\n")
        self.output.see("end")
        self.output.configure(state="disabled")
        self.output.update_idletasks()

    def clear_output(self) -> None:
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.configure(state="disabled")

    def browse_session_dir(self) -> None:
        path = filedialog.askdirectory(
            title="请选择会话目录",
            initialdir=str(SESSIONS_DIR if SESSIONS_DIR.exists() else Path.home()),
        )
        if path:
            self.path_var.set(path)
            self.refresh_sessions(log=True)

    def refresh_sessions(self, *, log: bool = False) -> None:
        root = Path(self.path_var.get().strip() or SESSIONS_DIR)
        self.tree.delete(*self.tree.get_children())
        self.item_by_iid.clear()

        self.sessions = load_session_items(root)
        for index, item in enumerate(self.sessions, 1):
            iid = f"{index}:{item.thread_id}"
            tags = ("missing",) if not item.source_path.exists() else ()
            self.tree.insert("", "end", iid=iid, values=(index, item.title, item.thread_id), tags=tags)
            self.item_by_iid[iid] = item

        self.count_var.set(f"共 {len(self.sessions)} 个会话")
        self.status_var.set("就绪")
        if log:
            self.append_output(f"已加载会话列表：{len(self.sessions)} 个")

    def selected_session_items(self) -> list[SessionListItem]:
        selected_iids = list(self.tree.selection())
        if not selected_iids:
            return self.sessions
        return [self.item_by_iid[iid] for iid in selected_iids if iid in self.item_by_iid]

    def clear_selection_on_blank(self, event: tk.Event) -> str | None:
        row = self.tree.identify_row(event.y)
        region = self.tree.identify_region(event.x, event.y)
        if not row and region not in ("heading", "separator"):
            self.tree.selection_remove(self.tree.selection())
            self.tree.focus("")
            self.status_var.set("已取消选择")
            return "break"
        return None

    def import_directory(self) -> None:
        path = filedialog.askdirectory(
            title="请选择要导入的会话文件夹",
            initialdir=str(Path.home() / "Desktop"),
        )
        if not path:
            return

        import_root = Path(path)
        duplicate_mode = self.choose_duplicate_import_mode(import_root)
        if duplicate_mode is None:
            return

        self.clear_output()
        self.append_output(f"开始导入并自动修复：{path}")
        try:
            logs = repair_directory(import_root, duplicate_mode=duplicate_mode)
            for line in logs:
                self.append_output(line)
            self.path_var.set(str(SESSIONS_DIR))
            self.refresh_sessions(log=False)
            self.append_output("导入完成，列表已刷新。")
            self.status_var.set("导入并修复完成")
            messagebox.showinfo("完成", "会话导入并自动修复完成。")
        except Exception as exc:
            self.append_output(f"错误：{exc}")
            self.status_var.set("导入失败")
            messagebox.showerror("导入失败", str(exc))

    def choose_duplicate_import_mode(self, import_root: Path) -> str | None:
        duplicate_ids = find_duplicate_thread_ids_for_import(import_root)
        if not duplicate_ids:
            return "replace"

        dialog = tk.Toplevel(self.root)
        dialog.title("会话 ID 重复")
        dialog.configure(bg=self.BG)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        result = tk.StringVar(value="")
        shown_ids = "\n".join(sorted(duplicate_ids)[:8])
        if len(duplicate_ids) > 8:
            shown_ids += f"\n...另有 {len(duplicate_ids) - 8} 个重复 ID"

        frame = tk.Frame(dialog, bg=self.BG, padx=22, pady=18)
        frame.pack(fill="both", expand=True)
        tk.Label(
            frame,
            text="检测到导入会话 ID 已存在",
            bg=self.BG,
            fg=self.TEXT,
            font=("Microsoft YaHei UI", 13, "bold"),
        ).pack(anchor="w")
        tk.Label(
            frame,
            text=f"重复 ID 数量：{len(duplicate_ids)}",
            bg=self.BG,
            fg=self.MUTED,
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(anchor="w", pady=(8, 0))
        tk.Label(
            frame,
            text=shown_ids,
            bg=self.BG,
            fg=self.TEXT,
            justify="left",
            font=("Consolas", 9, "bold"),
        ).pack(anchor="w", pady=(10, 12))
        tk.Label(
            frame,
            text="替换：使用导入会话覆盖本机同 ID 会话。\n增量：保留本机会话，并给导入会话生成新 ID，列表中会出现两条内容相同但 ID 不同的会话。",
            bg=self.BG,
            fg=self.MUTED,
            justify="left",
            wraplength=520,
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(anchor="w", pady=(0, 16))

        button_row = tk.Frame(frame, bg=self.BG)
        button_row.pack(anchor="e")

        def finish(value: str) -> None:
            result.set(value)
            dialog.destroy()

        for text, value in (("替换", "replace"), ("增量", "incremental")):
            tk.Button(
                button_row,
                text=text,
                command=lambda selected=value: finish(selected),
                bg=self.PRIMARY,
                activebackground=self.PRIMARY_HOVER,
                fg="#FFFFFF",
                activeforeground="#FFFFFF",
                relief="flat",
                bd=0,
                padx=18,
                pady=8,
                cursor="hand2",
                font=("Microsoft YaHei UI", 10, "bold"),
            ).pack(side="left", padx=(0, 10))

        dialog.update_idletasks()
        x = self.root.winfo_rootx() + max((self.root.winfo_width() - dialog.winfo_width()) // 2, 0)
        y = self.root.winfo_rooty() + max((self.root.winfo_height() - dialog.winfo_height()) // 2, 0)
        dialog.geometry(f"+{x}+{y}")
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        self.root.wait_window(dialog)
        return result.get() or None

    def export_current_sessions(self) -> None:
        items = self.selected_session_items()
        if not items:
            messagebox.showerror("导出失败", "当前没有可导出的会话。")
            return

        path = filedialog.askdirectory(
            title="请选择导出保存位置",
            initialdir=str(Path.home() / "Desktop"),
        )
        if not path:
            return

        selected_count = len(self.tree.selection())
        source_paths = [item.source_path for item in items]
        scope_label = "选中会话" if selected_count else "全部会话"
        self.clear_output()
        self.append_output(f"开始导出{scope_label}：{len(items)} 个")
        try:
            logs = export_sessions(Path(path), source_paths, scope_label)
            for line in logs:
                self.append_output(line)
            self.append_output("导出完成。")
            if selected_count:
                self.status_var.set(f"已导出选中会话：{len(items)} 个")
                messagebox.showinfo("完成", f"已导出选中会话 {len(items)} 个。")
            else:
                self.status_var.set(f"已导出全部会话：{len(items)} 个")
                messagebox.showinfo("完成", f"已导出全部会话 {len(items)} 个。")
        except Exception as exc:
            self.append_output(f"错误：{exc}")
            self.status_var.set("导出失败")
            messagebox.showerror("导出失败", str(exc))

    def show_session_menu(self, event: tk.Event) -> None:
        iid = self.tree.identify_row(event.y)
        if not iid:
            self.tree.selection_remove(self.tree.selection())
            self.tree.focus("")
            self.status_var.set("已取消选择")
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        self.tree.focus(iid)
        self.session_menu.tk_popup(event.x_root, event.y_root)

    def open_selected_in_explorer(self) -> None:
        iid = self.tree.focus()
        if not iid and self.tree.selection():
            iid = self.tree.selection()[0]
        item = self.item_by_iid.get(iid)
        if not item:
            return

        path = normalize_display_path(item.source_path)
        if not path.exists():
            messagebox.showerror("文件不存在", f"找不到会话文件：{path}")
            return
        subprocess.Popen(["explorer.exe", f"/select,{path}"])


class _TextSelectableAppDraftUnused:
    BG = "#050913"
    LINE = "#C9D7FF"
    LINE_DIM = "#526783"
    TEXT = "#F4F7FB"
    MUTED = "#B8C2D8"
    PRIMARY = "#7B83FF"
    PRIMARY_HOVER = "#98A0FF"
    SUCCESS = "#35F29A"
    BUTTON_BG = "#111A2A"

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("会话备份工具")
        self.root.geometry("1180x820")
        self.root.minsize(960, 680)
        self.root.configure(bg=self.BG)

        self.sessions: list[SessionListItem] = []
        self.item_by_iid: dict[str, SessionListItem] = {}
        self.selected_iids: set[str] = set()
        self.anchor_iid: str | None = None
        self.row_boxes: list[tuple[str, int, int, int, int, int]] = []
        self.logs: list[str] = []
        self.list_scroll = 0
        self.column_split = 0.66
        self.path_var = tk.StringVar(value=str(SESSIONS_DIR))
        self.status_var = tk.StringVar(value="就绪")
        self.count_var = tk.StringVar(value="共 0 个会话")

        self.background_original: Image.Image | None = None
        self.background_photo: ImageTk.PhotoImage | None = None
        self.background_item: int | None = None
        self.resize_after_id: str | None = None
        self.last_background_size: tuple[int, int] = (0, 0)

        self.title_font = tkfont.Font(family="Microsoft YaHei UI", size=17, weight="bold")
        self.author_font = tkfont.Font(family="Microsoft YaHei UI", size=24, weight="bold")
        self.notice_font = tkfont.Font(family="Microsoft YaHei UI", size=11, weight="bold")
        self.header_font = tkfont.Font(family="Microsoft YaHei UI", size=15, weight="bold")
        self.body_font = tkfont.Font(family="Microsoft YaHei UI", size=11, weight="bold")
        self.small_font = tkfont.Font(family="Microsoft YaHei UI", size=9, weight="bold")
        self.mono_font = tkfont.Font(family="Consolas", size=10, weight="bold")

        self.configure_window_assets()
        self.canvas = tk.Canvas(root, highlightthickness=0, bd=0, bg=self.BG)
        self.canvas.pack(fill="both", expand=True)
        self.create_buttons()
        self.create_text_widgets()
        self.canvas.bind("<Button-1>", self.on_canvas_click)
        self.canvas.bind("<Button-3>", self.on_canvas_right_click)
        self.canvas.bind("<MouseWheel>", self.on_mouse_wheel)
        self.root.bind("<Configure>", self.schedule_redraw)

        self.session_menu = tk.Menu(self.root, tearoff=0, bg="#111A2A", fg=self.TEXT, activebackground=self.PRIMARY)
        self.session_menu.add_command(label="打开所在文件夹", command=self.open_selected_in_explorer)

        self.refresh_sessions(log=True)

    def configure_window_assets(self) -> None:
        icon_path = resource_path("assets/CodexSessionRepair.ico")
        if icon_path.exists():
            try:
                self.root.iconbitmap(str(icon_path))
            except tk.TclError:
                pass

        bg_path = resource_path("assets/halo_background.png")
        if bg_path.exists():
            self.background_original = Image.open(bg_path).convert("RGB")

    def make_button(self, text: str, command: Any, *, primary: bool = False) -> tk.Button:
        bg = self.PRIMARY if primary else self.BUTTON_BG
        active = self.PRIMARY_HOVER if primary else "#1B2A43"
        return tk.Button(
            self.root,
            text=text,
            command=command,
            bg=bg,
            activebackground=active,
            fg="#FFFFFF",
            activeforeground="#FFFFFF",
            relief="flat",
            bd=0,
            padx=14,
            pady=8,
            cursor="hand2",
            font=("Microsoft YaHei UI", 10, "bold"),
        )

    def create_buttons(self) -> None:
        self.browse_button = self.make_button("浏览...", self.browse_session_dir, primary=True)
        self.repair_button = self.make_button("修复会话", self.repair_current_path, primary=True)
        self.repair_plugins_button = self.make_button("修复丢失插件技能", self.repair_missing_plugin_skills, primary=True)
        self.refresh_button_widget = self.make_button("刷新", self.refresh_button, primary=True)
        self.export_button = self.make_button("导出会话", self.export_current_sessions, primary=True)
        self.import_button = self.make_button("导入会话", self.import_directory, primary=True)
        self.clear_button = self.make_button("清空日志", self.clear_output, primary=True)

    def create_text_widgets(self) -> None:
        self.path_entry = tk.Entry(
            self.root,
            textvariable=self.path_var,
            bg=self.BG,
            fg=self.TEXT,
            readonlybackground=self.BG,
            insertbackground=self.TEXT,
            relief="flat",
            bd=0,
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        self.path_entry.configure(state="readonly")

        self.list_text = tk.Text(
            self.root,
            bg=self.BG,
            fg=self.TEXT,
            insertbackground=self.TEXT,
            selectbackground=self.PRIMARY,
            selectforeground="#FFFFFF",
            relief="flat",
            bd=0,
            wrap="none",
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        self.list_text.tag_configure("muted", foreground=self.MUTED)
        self.list_text.tag_configure("selected_row", background="#1B2A43")
        self.list_text.bind("<ButtonRelease-1>", self.on_list_text_click)
        self.list_text.bind("<Button-3>", self.on_list_text_right_click)
        self.list_text.bind("<MouseWheel>", self.on_mouse_wheel)
        self.list_text.bind("<Key>", lambda _event: "break")

        self.log_text = tk.Text(
            self.root,
            bg=self.BG,
            fg=self.TEXT,
            insertbackground=self.TEXT,
            selectbackground=self.PRIMARY,
            selectforeground="#FFFFFF",
            relief="flat",
            bd=0,
            wrap="word",
            font=("Consolas", 10, "bold"),
        )
        self.log_text.bind("<Key>", lambda _event: "break")

        self.column_divider = tk.Frame(self.root, bg=self.LINE, cursor="sb_h_double_arrow", width=3)
        self.column_divider.bind("<B1-Motion>", self.on_column_drag)

    def schedule_redraw(self, event: tk.Event | None = None) -> None:
        if event is not None and event.widget is not self.root:
            return
        if self.resize_after_id:
            self.root.after_cancel(self.resize_after_id)
        self.resize_after_id = self.root.after(60, self.redraw)

    def redraw(self) -> None:
        self.resize_after_id = None
        self.draw_background()
        self.draw_layout()

    def draw_background(self) -> None:
        width = max(self.root.winfo_width(), 1)
        height = max(self.root.winfo_height(), 1)
        if self.background_original is None:
            return
        if (width, height) != self.last_background_size:
            self.last_background_size = (width, height)
            src_w, src_h = self.background_original.size
            scale = max(width / src_w, height / src_h)
            new_w = max(int(src_w * scale), 1)
            new_h = max(int(src_h * scale), 1)
            resized = self.background_original.resize((new_w, new_h), Image.Resampling.LANCZOS)
            left = max((new_w - width) // 2, 0)
            top = max((new_h - height) // 2, 0)
            cropped = resized.crop((left, top, left + width, top + height)).convert("RGBA")
            veil = Image.new("RGBA", (width, height), (3, 8, 16, 8))
            self.background_photo = ImageTk.PhotoImage(Image.alpha_composite(cropped, veil))

        if self.background_item is None:
            self.background_item = self.canvas.create_image(0, 0, image=self.background_photo, anchor="nw")
        else:
            self.canvas.itemconfigure(self.background_item, image=self.background_photo)
        self.canvas.tag_lower(self.background_item)

    def draw_layout(self) -> None:
        self.canvas.delete("ui")
        width = max(self.root.winfo_width(), 960)
        height = max(self.root.winfo_height(), 680)
        margin = 20
        path_top = 78
        path_h = 76
        log_h = max(240, int(height * 0.30))
        gap = 16
        log_top = height - margin - log_h
        list_top = path_top + path_h + gap
        list_bottom = max(list_top + 260, log_top - gap)
        right = width - margin

        self.path_box = (margin, path_top, right, path_top + path_h)
        self.list_box = (margin, list_top, right, list_bottom)
        self.log_box = (margin, log_top, right, log_top + log_h)

        for box in (self.path_box, self.list_box, self.log_box):
            self.canvas.create_rectangle(*box, outline=self.LINE, width=1, tags="ui")

        self.draw_path_section()
        self.draw_list_section()
        self.draw_log_section()
        self.draw_top_brand()

    def draw_top_brand(self) -> None:
        width = max(self.root.winfo_width(), 960)
        self.canvas.create_text(
            width // 2,
            38,
            text="制作者：铃音奈绪",
            fill=self.TEXT,
            font=self.author_font,
            anchor="center",
            tags="ui",
        )
        self.canvas.create_text(
            width - 28,
            39,
            text="本软件纯免费，发现收费立即举报",
            fill=self.LINE,
            font=self.notice_font,
            anchor="e",
            tags="ui",
        )

    def draw_path_section(self) -> None:
        x1, y1, x2, y2 = self.path_box
        self.canvas.create_text(x1 + 22, y1 + 38, text="会话路径", fill=self.TEXT, font=self.header_font, anchor="w", tags="ui")
        path_left = x1 + 145
        path_right = max(path_left + 260, x2 - 490)
        self.canvas.create_rectangle(path_left, y1 + 18, path_right, y2 - 18, outline=self.LINE_DIM, width=1, tags="ui")
        self.path_entry.place(x=path_left + 12, y=y1 + 24, width=path_right - path_left - 24, height=28)
        self.browse_button.place(x=path_right + 14, y=y1 + 18, width=100, height=40)
        self.repair_button.place(x=path_right + 124, y=y1 + 18, width=112, height=40)

    def draw_list_section(self) -> None:
        x1, y1, x2, y2 = self.list_box
        header_y = y1 + 34
        self.canvas.create_text(x1 + 22, header_y, text="对话列表", fill=self.TEXT, font=self.header_font, anchor="w", tags="ui")
        self.canvas.create_text(
            x1 + 138,
            header_y,
            text="Shift/Ctrl 多选，点击空白处取消选择",
            fill=self.MUTED,
            font=self.small_font,
            anchor="w",
            tags="ui",
        )
        button_y = y1 + 17
        self.import_button.place(x=x2 - 126, y=button_y, width=106, height=38)
        self.export_button.place(x=x2 - 246, y=button_y, width=106, height=38)
        self.refresh_button_widget.place(x=x2 - 336, y=button_y, width=76, height=38)

        table_top = y1 + 78
        table_bottom = y2 - 22
        self.canvas.create_rectangle(x1 + 20, table_top, x2 - 20, table_bottom, outline=self.LINE_DIM, width=1, tags="ui")
        self.list_text.place(x=x1 + 26, y=table_top + 8, width=x2 - x1 - 52, height=table_bottom - table_top - 16)
        divider_x = int((x1 + 26) + (x2 - x1 - 52) * self.column_split)
        self.column_divider.place(x=divider_x, y=table_top + 8, width=3, height=table_bottom - table_top - 16)
        self.update_list_text(x2 - x1 - 52)

    def draw_log_section(self) -> None:
        x1, y1, x2, y2 = self.log_box
        self.canvas.create_text(x1 + 22, y1 + 34, text="日志", fill=self.TEXT, font=self.header_font, anchor="w", tags="ui")
        self.clear_button.place(x=x2 - 118, y=y1 + 16, width=98, height=36)
        body_top = y1 + 66
        self.canvas.create_rectangle(x1 + 20, body_top, x2 - 20, y2 - 16, outline=self.LINE_DIM, width=1, tags="ui")
        self.log_text.place(x=x1 + 28, y=body_top + 8, width=x2 - x1 - 56, height=y2 - body_top - 32)
        self.update_log_text()

    def elide(self, text: str, max_width: int, font: tkfont.Font) -> str:
        if font.measure(text) <= max_width:
            return text
        ellipsis = "..."
        while text and font.measure(text + ellipsis) > max_width:
            text = text[:-1]
        return text + ellipsis

    def update_list_text(self, total_width: int | None = None) -> None:
        if total_width is None:
            total_width = max(self.list_text.winfo_width(), 600)
        seq_width = 70
        gap_width = 24
        name_width = max(int((total_width - seq_width - gap_width) * self.column_split), 180)
        id_width = max(total_width - seq_width - gap_width - name_width, 180)
        seq_chars = 6
        name_chars = max(name_width // max(self.body_font.measure("汉"), 10), 12)
        id_chars = max(id_width // max(self.body_font.measure("0"), 8), 18)

        self.list_text.configure(state="normal")
        self.list_text.delete("1.0", "end")
        header = f"{'序号':<{seq_chars}} {'会话名称':<{name_chars}} {'会话 ID':<{id_chars}}\n"
        self.list_text.insert("end", header, ("muted",))
        self.list_text.insert("end", "-" * max(seq_chars + name_chars + id_chars + 4, 80) + "\n", ("muted",))
        for index, item in enumerate(self.sessions, 1):
            iid = f"{index}:{item.thread_id}"
            title = self.truncate_chars(item.title, name_chars)
            thread_id = self.truncate_chars(item.thread_id, id_chars)
            line_start = self.list_text.index("end")
            self.list_text.insert("end", f"{str(index):<{seq_chars}} {title:<{name_chars}} {thread_id:<{id_chars}}\n")
            line_no = int(float(line_start))
            if iid in self.selected_iids:
                self.list_text.tag_add("selected_row", f"{line_no}.0", f"{line_no}.end")
        self.list_text.configure(state="normal")

    def update_log_text(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        if self.logs:
            self.log_text.insert("end", "\n".join(self.logs) + "\n")
        self.log_text.configure(state="normal")
        self.log_text.see("end")

    def truncate_chars(self, text: str, max_chars: int) -> str:
        text = " ".join(str(text).split())
        if len(text) <= max_chars:
            return text
        return text[: max(max_chars - 3, 1)] + "..."

    def on_column_drag(self, event: tk.Event) -> None:
        if not hasattr(self, "list_box"):
            return
        x1, y1, x2, y2 = self.list_box
        total_width = max(x2 - x1 - 52, 1)
        absolute_x = self.column_divider.winfo_x() + event.x
        relative = (absolute_x - (x1 + 26)) / total_width
        self.column_split = min(max(relative, 0.34), 0.78)
        self.draw_layout()

    def on_list_text_click(self, event: tk.Event) -> None:
        line_no = int(self.list_text.index(f"@{event.x},{event.y}").split(".", 1)[0])
        index = line_no - 2
        if index < 1 or index > len(self.sessions):
            return
        iid = f"{index}:{self.sessions[index - 1].thread_id}"
        if event.state & 0x0001 and self.anchor_iid in self.item_by_iid:
            start = self.index_from_iid(self.anchor_iid)
            end = index
            if start is not None:
                low, high = sorted((start, end))
                self.selected_iids = {f"{idx}:{self.sessions[idx - 1].thread_id}" for idx in range(low, high + 1)}
        elif event.state & 0x0004:
            if iid in self.selected_iids:
                self.selected_iids.remove(iid)
            else:
                self.selected_iids.add(iid)
            self.anchor_iid = iid
        else:
            self.selected_iids = {iid}
            self.anchor_iid = iid
        self.update_list_text()

    def on_list_text_right_click(self, event: tk.Event) -> None:
        line_no = int(self.list_text.index(f"@{event.x},{event.y}").split(".", 1)[0])
        index = line_no - 2
        if 1 <= index <= len(self.sessions):
            iid = f"{index}:{self.sessions[index - 1].thread_id}"
            self.selected_iids = {iid}
            self.anchor_iid = iid
            self.update_list_text()
            self.session_menu.tk_popup(event.x_root, event.y_root)

    def refresh_button(self) -> None:
        self.refresh_sessions(log=True)

    def append_output(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.logs.append(f"[{stamp}] {text}")
        self.draw_layout()
        self.root.update_idletasks()

    def clear_output(self) -> None:
        self.logs.clear()
        self.draw_layout()

    def browse_session_dir(self) -> None:
        path = filedialog.askdirectory(
            title="请选择会话目录",
            initialdir=str(SESSIONS_DIR if SESSIONS_DIR.exists() else Path.home()),
        )
        if path:
            self.path_var.set(path)
            self.refresh_sessions(log=True)

    def refresh_sessions(self, *, log: bool = False) -> None:
        root = Path(self.path_var.get().strip() or SESSIONS_DIR)
        self.sessions = load_session_items(root)
        self.item_by_iid.clear()
        for index, item in enumerate(self.sessions, 1):
            self.item_by_iid[f"{index}:{item.thread_id}"] = item
        self.selected_iids.intersection_update(self.item_by_iid.keys())
        self.count_var.set(f"共 {len(self.sessions)} 个会话")
        self.status_var.set("就绪")
        self.draw_background()
        self.draw_layout()
        if log:
            self.append_output(f"已加载会话列表：{len(self.sessions)} 个")

    def selected_session_items(self) -> list[SessionListItem]:
        if not self.selected_iids:
            return self.sessions
        return [self.item_by_iid[iid] for iid in self.selected_iids if iid in self.item_by_iid]

    def on_canvas_click(self, event: tk.Event) -> None:
        iid = self.iid_at(event.x, event.y)
        if not iid:
            self.selected_iids.clear()
            self.anchor_iid = None
            self.status_var.set("已取消选择")
            self.draw_layout()
            return

        if event.state & 0x0001 and self.anchor_iid in self.item_by_iid:
            start = self.index_from_iid(self.anchor_iid)
            end = self.index_from_iid(iid)
            if start is not None and end is not None:
                low, high = sorted((start, end))
                self.selected_iids = {f"{idx}:{self.sessions[idx - 1].thread_id}" for idx in range(low, high + 1)}
        elif event.state & 0x0004:
            if iid in self.selected_iids:
                self.selected_iids.remove(iid)
            else:
                self.selected_iids.add(iid)
            self.anchor_iid = iid
        else:
            self.selected_iids = {iid}
            self.anchor_iid = iid
        self.draw_layout()

    def on_canvas_right_click(self, event: tk.Event) -> None:
        iid = self.iid_at(event.x, event.y)
        if not iid:
            self.selected_iids.clear()
            self.anchor_iid = None
            self.status_var.set("已取消选择")
            self.draw_layout()
            return
        if iid not in self.selected_iids:
            self.selected_iids = {iid}
        self.anchor_iid = iid
        self.draw_layout()
        self.session_menu.tk_popup(event.x_root, event.y_root)

    def on_mouse_wheel(self, event: tk.Event) -> None:
        if not hasattr(self, "list_box"):
            return
        x1, y1, x2, y2 = self.list_box
        if not (x1 <= event.x <= x2 and y1 <= event.y <= y2):
            return
        direction = -1 if event.delta > 0 else 1
        self.list_scroll = max(0, min(self.list_scroll + direction, max(len(self.sessions) - 1, 0)))
        self.draw_layout()

    def iid_at(self, x: int, y: int) -> str | None:
        for iid, x1, y1, x2, y2, _ in self.row_boxes:
            if x1 <= x <= x2 and y1 <= y <= y2:
                return iid
        return None

    def index_from_iid(self, iid: str | None) -> int | None:
        if not iid:
            return None
        try:
            return int(iid.split(":", 1)[0])
        except ValueError:
            return None

    def import_directory(self) -> None:
        path = filedialog.askdirectory(
            title="请选择要导入的会话文件夹",
            initialdir=str(Path.home() / "Desktop"),
        )
        if not path:
            return

        self.clear_output()
        self.append_output(f"开始导入并自动修复：{path}")
        try:
            logs = repair_directory(Path(path))
            for line in logs:
                self.append_output(line)
            self.path_var.set(str(SESSIONS_DIR))
            self.refresh_sessions(log=False)
            self.append_output("导入完成，列表已刷新。")
            self.status_var.set("导入并修复完成")
            messagebox.showinfo("完成", "会话导入并自动修复完成。")
        except Exception as exc:
            self.append_output(f"错误：{exc}")
            self.status_var.set("导入失败")
            messagebox.showerror("导入失败", str(exc))

    def export_current_sessions(self) -> None:
        items = self.selected_session_items()
        if not items:
            messagebox.showerror("导出失败", "当前没有可导出的会话。")
            return

        path = filedialog.askdirectory(
            title="请选择导出保存位置",
            initialdir=str(Path.home() / "Desktop"),
        )
        if not path:
            return

        selected_count = len(self.selected_iids)
        source_paths = [item.source_path for item in items]
        scope_label = "选中会话" if selected_count else "全部会话"
        self.clear_output()
        self.append_output(f"开始导出{scope_label}：{len(items)} 个")
        try:
            logs = export_sessions(Path(path), source_paths, scope_label)
            for line in logs:
                self.append_output(line)
            self.append_output("导出完成。")
            if selected_count:
                self.status_var.set(f"已导出选中会话：{len(items)} 个")
                messagebox.showinfo("完成", f"已导出选中会话 {len(items)} 个。")
            else:
                self.status_var.set(f"已导出全部会话：{len(items)} 个")
                messagebox.showinfo("完成", f"已导出全部会话 {len(items)} 个。")
        except Exception as exc:
            self.append_output(f"错误：{exc}")
            self.status_var.set("导出失败")
            messagebox.showerror("导出失败", str(exc))

    def open_selected_in_explorer(self) -> None:
        iid = next(iter(self.selected_iids), None)
        item = self.item_by_iid.get(iid) if iid else None
        if not item:
            return

        path = normalize_display_path(item.source_path)
        if not path.exists():
            messagebox.showerror("文件不存在", f"找不到会话文件：{path}")
            return
        subprocess.Popen(["explorer.exe", f"/select,{path}"])


class App:
    BG = "#050913"
    LINE = "#C9D7FF"
    LINE_DIM = "#526783"
    TEXT = "#F4F7FB"
    MUTED = "#B8C2D8"
    PRIMARY = "#7B83FF"
    PRIMARY_HOVER = "#98A0FF"
    SUCCESS = "#35F29A"
    BUTTON_BG = "#111A2A"

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("codex会话同步工具")
        self.root.geometry("1180x820")
        self.root.minsize(960, 680)
        self.root.configure(bg=self.BG)

        self.sessions: list[SessionListItem] = []
        self.item_by_iid: dict[str, SessionListItem] = {}
        self.selected_iids: set[str] = set()
        self.anchor_iid: str | None = None
        self.row_boxes: list[tuple[str, int, int, int, int, int]] = []
        self.logs: list[str] = []
        self.list_scroll = 0
        self.path_var = tk.StringVar(value=str(SESSIONS_DIR))
        self.status_var = tk.StringVar(value="就绪")
        self.count_var = tk.StringVar(value="共 0 个会话")

        self.background_original: Image.Image | None = None
        self.background_photo: ImageTk.PhotoImage | None = None
        self.background_item: int | None = None
        self.resize_after_id: str | None = None
        self.last_background_size: tuple[int, int] = (0, 0)

        self.title_font = tkfont.Font(family="Microsoft YaHei UI", size=17, weight="bold")
        self.author_font = tkfont.Font(family="Microsoft YaHei UI", size=24, weight="bold")
        self.notice_font = tkfont.Font(family="Microsoft YaHei UI", size=11, weight="bold")
        self.header_font = tkfont.Font(family="Microsoft YaHei UI", size=15, weight="bold")
        self.body_font = tkfont.Font(family="Microsoft YaHei UI", size=11, weight="bold")
        self.small_font = tkfont.Font(family="Microsoft YaHei UI", size=9, weight="bold")
        self.mono_font = tkfont.Font(family="Consolas", size=10, weight="bold")

        self.configure_window_assets()
        self.canvas = tk.Canvas(root, highlightthickness=0, bd=0, bg=self.BG)
        self.canvas.pack(fill="both", expand=True)
        self.create_buttons()
        self.canvas.bind("<Button-1>", self.on_canvas_click)
        self.canvas.bind("<Button-3>", self.on_canvas_right_click)
        self.canvas.bind("<MouseWheel>", self.on_mouse_wheel)
        self.root.bind("<Configure>", self.schedule_redraw)

        self.session_menu = tk.Menu(self.root, tearoff=0, bg="#111A2A", fg=self.TEXT, activebackground=self.PRIMARY)
        self.session_menu.add_command(label="打开所在文件夹", command=self.open_selected_in_explorer)

        self.refresh_sessions(log=True)

    def configure_window_assets(self) -> None:
        icon_path = resource_path("assets/CodexSessionRepair.ico")
        if icon_path.exists():
            try:
                self.root.iconbitmap(str(icon_path))
            except tk.TclError:
                pass

        bg_path = resource_path("assets/halo_background.png")
        if bg_path.exists():
            self.background_original = Image.open(bg_path).convert("RGB")

    def make_button(self, text: str, command: Any, *, primary: bool = False) -> tk.Button:
        bg = self.PRIMARY if primary else self.BUTTON_BG
        active = self.PRIMARY_HOVER if primary else "#1B2A43"
        return tk.Button(
            self.root,
            text=text,
            command=command,
            bg=bg,
            activebackground=active,
            fg="#FFFFFF",
            activeforeground="#FFFFFF",
            relief="flat",
            bd=0,
            padx=14,
            pady=8,
            cursor="hand2",
            font=("Microsoft YaHei UI", 10, "bold"),
        )

    def create_buttons(self) -> None:
        self.browse_button = self.make_button("浏览...", self.browse_session_dir, primary=True)
        self.repair_button = self.make_button("修复会话", self.repair_current_path, primary=True)
        self.repair_plugins_button = self.make_button("修复丢失插件技能", self.repair_missing_plugin_skills, primary=True)
        self.refresh_button_widget = self.make_button("刷新", self.refresh_button, primary=True)
        self.export_button = self.make_button("导出会话", self.export_current_sessions, primary=True)
        self.import_button = self.make_button("导入会话", self.import_directory, primary=True)
        self.clear_button = self.make_button("清空日志", self.clear_output, primary=True)

    def schedule_redraw(self, event: tk.Event | None = None) -> None:
        if event is not None and event.widget is not self.root:
            return
        if self.resize_after_id:
            self.root.after_cancel(self.resize_after_id)
        self.resize_after_id = self.root.after(60, self.redraw)

    def redraw(self) -> None:
        self.resize_after_id = None
        self.draw_background()
        self.draw_layout()

    def draw_background(self) -> None:
        width = max(self.root.winfo_width(), 1)
        height = max(self.root.winfo_height(), 1)
        if self.background_original is None:
            return
        if (width, height) != self.last_background_size:
            self.last_background_size = (width, height)
            src_w, src_h = self.background_original.size
            scale = max(width / src_w, height / src_h)
            new_w = max(int(src_w * scale), 1)
            new_h = max(int(src_h * scale), 1)
            resized = self.background_original.resize((new_w, new_h), Image.Resampling.LANCZOS)
            left = max((new_w - width) // 2, 0)
            top = max((new_h - height) // 2, 0)
            cropped = resized.crop((left, top, left + width, top + height)).convert("RGBA")
            veil = Image.new("RGBA", (width, height), (3, 8, 16, 8))
            self.background_photo = ImageTk.PhotoImage(Image.alpha_composite(cropped, veil))

        if self.background_item is None:
            self.background_item = self.canvas.create_image(0, 0, image=self.background_photo, anchor="nw")
        else:
            self.canvas.itemconfigure(self.background_item, image=self.background_photo)
        self.canvas.tag_lower(self.background_item)

    def draw_layout(self) -> None:
        self.canvas.delete("ui")
        width = max(self.root.winfo_width(), 960)
        height = max(self.root.winfo_height(), 680)
        margin = 20
        path_top = 78
        path_h = 76
        gap = 16
        log_h = max(250, int(height * 0.32))
        log_top = height - margin - log_h
        list_top = path_top + path_h + gap
        list_bottom = max(list_top + 260, log_top - gap)
        right = width - margin

        self.path_box = (margin, path_top, right, path_top + path_h)
        self.list_box = (margin, list_top, right, list_bottom)
        self.log_box = (margin, log_top, right, log_top + log_h)

        for box in (self.path_box, self.list_box, self.log_box):
            self.canvas.create_rectangle(*box, outline=self.LINE, width=1, tags="ui")

        self.draw_path_section()
        self.draw_list_section()
        self.draw_log_section()
        self.draw_top_brand()

    def draw_top_brand(self) -> None:
        width = max(self.root.winfo_width(), 960)
        self.canvas.create_text(
            width // 2,
            38,
            text="制作者：铃音奈绪",
            fill=self.TEXT,
            font=self.author_font,
            anchor="center",
            tags="ui",
        )
        self.canvas.create_text(
            width - 28,
            39,
            text="本软件纯免费，发现收费立即举报",
            fill=self.LINE,
            font=self.notice_font,
            anchor="e",
            tags="ui",
        )

    def draw_path_section(self) -> None:
        x1, y1, x2, y2 = self.path_box
        self.canvas.create_text(x1 + 22, y1 + 38, text="会话路径", fill=self.TEXT, font=self.header_font, anchor="w", tags="ui")
        path_left = x1 + 145
        path_right = max(path_left + 260, x2 - 560)
        self.canvas.create_rectangle(path_left, y1 + 18, path_right, y2 - 18, outline=self.LINE_DIM, width=1, tags="ui")
        path_text = self.elide(self.path_var.get(), path_right - path_left - 34, self.body_font)
        self.canvas.create_text(path_left + 18, y1 + 38, text=path_text, fill=self.TEXT, font=self.body_font, anchor="w", tags="ui")
        self.browse_button.place(x=path_right + 14, y=y1 + 18, width=100, height=40)
        self.repair_button.place(x=path_right + 124, y=y1 + 18, width=112, height=40)
        self.repair_plugins_button.place(x=path_right + 250, y=y1 + 18, width=170, height=40)

    def draw_list_section(self) -> None:
        x1, y1, x2, y2 = self.list_box
        header_y = y1 + 34
        self.canvas.create_text(x1 + 22, header_y, text="对话列表", fill=self.TEXT, font=self.header_font, anchor="w", tags="ui")
        self.canvas.create_text(
            x1 + 138,
            header_y,
            text="Shift/Ctrl 多选，点击空白处取消选择",
            fill=self.MUTED,
            font=self.small_font,
            anchor="w",
            tags="ui",
        )
        button_y = y1 + 17
        self.import_button.place(x=x2 - 126, y=button_y, width=106, height=38)
        self.export_button.place(x=x2 - 246, y=button_y, width=106, height=38)
        self.refresh_button_widget.place(x=x2 - 336, y=button_y, width=76, height=38)

        table_top = y1 + 78
        table_bottom = y2 - 22
        self.canvas.create_rectangle(x1 + 20, table_top, x2 - 20, table_bottom, outline=self.LINE_DIM, width=1, tags="ui")
        self.canvas.create_line(x1 + 20, table_top + 42, x2 - 20, table_top + 42, fill=self.LINE_DIM, tags="ui")
        seq_x = x1 + 58
        title_x = x1 + 118
        id_x = x2 - 270
        self.canvas.create_text(seq_x, table_top + 21, text="序号", fill=self.MUTED, font=self.body_font, anchor="center", tags="ui")
        self.canvas.create_text(title_x, table_top + 21, text="会话名称", fill=self.MUTED, font=self.body_font, anchor="w", tags="ui")
        self.canvas.create_text(id_x, table_top + 21, text="会话 ID", fill=self.MUTED, font=self.body_font, anchor="w", tags="ui")

        row_h = 42
        visible = max((table_bottom - table_top - 42) // row_h, 1)
        self.list_scroll = max(0, min(self.list_scroll, max(len(self.sessions) - visible, 0)))
        visible_sessions = self.sessions[self.list_scroll : self.list_scroll + visible]
        self.row_boxes = []
        for offset, item in enumerate(visible_sessions):
            absolute_index = self.list_scroll + offset + 1
            iid = f"{absolute_index}:{item.thread_id}"
            row_y1 = table_top + 42 + offset * row_h
            row_y2 = row_y1 + row_h
            self.row_boxes.append((iid, x1 + 20, row_y1, x2 - 20, row_y2, absolute_index))
            self.canvas.create_line(x1 + 20, row_y2, x2 - 20, row_y2, fill="#314059", tags="ui")
            if iid in self.selected_iids:
                self.canvas.create_rectangle(x1 + 24, row_y1 + 4, x2 - 24, row_y2 - 4, outline=self.PRIMARY_HOVER, width=2, tags="ui")
            title = self.elide(item.title, id_x - title_x - 24, self.body_font)
            thread_id = self.elide(item.thread_id, x2 - id_x - 42, self.body_font)
            self.canvas.create_text(seq_x, row_y1 + row_h // 2, text=str(absolute_index), fill=self.TEXT, font=self.body_font, anchor="center", tags="ui")
            self.canvas.create_text(title_x, row_y1 + row_h // 2, text=title, fill=self.TEXT, font=self.body_font, anchor="w", tags="ui")
            self.canvas.create_text(id_x, row_y1 + row_h // 2, text=thread_id, fill=self.MUTED, font=self.body_font, anchor="w", tags="ui")

    def draw_log_section(self) -> None:
        x1, y1, x2, y2 = self.log_box
        self.canvas.create_text(x1 + 22, y1 + 34, text="日志", fill=self.TEXT, font=self.header_font, anchor="w", tags="ui")
        self.clear_button.place(x=x2 - 118, y=y1 + 16, width=98, height=36)
        body_top = y1 + 66
        self.canvas.create_rectangle(x1 + 20, body_top, x2 - 20, y2 - 16, outline=self.LINE_DIM, width=1, tags="ui")
        line_h = 22
        max_lines = max((y2 - 20 - body_top) // line_h, 1)
        for idx, line in enumerate(self.logs[-max_lines:]):
            self.canvas.create_text(
                x1 + 34,
                body_top + 14 + idx * line_h,
                text=self.elide(line, x2 - x1 - 90, self.mono_font),
                fill=self.TEXT,
                font=self.mono_font,
                anchor="w",
                tags="ui",
            )

    def elide(self, text: str, max_width: int, font: tkfont.Font) -> str:
        if font.measure(text) <= max_width:
            return text
        ellipsis = "..."
        while text and font.measure(text + ellipsis) > max_width:
            text = text[:-1]
        return text + ellipsis

    def refresh_button(self) -> None:
        self.refresh_sessions(log=True)

    def append_output(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.logs.append(f"[{stamp}] {text}")
        self.draw_layout()
        self.root.update_idletasks()

    def clear_output(self) -> None:
        self.logs.clear()
        self.draw_layout()

    def browse_session_dir(self) -> None:
        path = filedialog.askdirectory(
            title="请选择会话目录",
            initialdir=str(SESSIONS_DIR if SESSIONS_DIR.exists() else Path.home()),
        )
        if path:
            self.path_var.set(path)
            self.refresh_sessions(log=True)

    def refresh_sessions(self, *, log: bool = False) -> None:
        root = Path(self.path_var.get().strip() or SESSIONS_DIR)
        self.sessions = load_session_items(root)
        self.item_by_iid.clear()
        for index, item in enumerate(self.sessions, 1):
            self.item_by_iid[f"{index}:{item.thread_id}"] = item
        self.selected_iids.intersection_update(self.item_by_iid.keys())
        self.count_var.set(f"共 {len(self.sessions)} 个会话")
        self.status_var.set("就绪")
        self.draw_background()
        self.draw_layout()
        if log:
            self.append_output(f"已加载会话列表：{len(self.sessions)} 个")

    def selected_session_items(self) -> list[SessionListItem]:
        if not self.selected_iids:
            return self.sessions
        return [self.item_by_iid[iid] for iid in self.selected_iids if iid in self.item_by_iid]

    def on_canvas_click(self, event: tk.Event) -> None:
        iid = self.iid_at(event.x, event.y)
        if not iid:
            self.selected_iids.clear()
            self.anchor_iid = None
            self.status_var.set("已取消选择")
            self.draw_layout()
            return

        if event.state & 0x0001 and self.anchor_iid in self.item_by_iid:
            start = self.index_from_iid(self.anchor_iid)
            end = self.index_from_iid(iid)
            if start is not None and end is not None:
                low, high = sorted((start, end))
                self.selected_iids = {f"{idx}:{self.sessions[idx - 1].thread_id}" for idx in range(low, high + 1)}
        elif event.state & 0x0004:
            if iid in self.selected_iids:
                self.selected_iids.remove(iid)
            else:
                self.selected_iids.add(iid)
            self.anchor_iid = iid
        else:
            self.selected_iids = {iid}
            self.anchor_iid = iid
        self.draw_layout()

    def on_canvas_right_click(self, event: tk.Event) -> None:
        iid = self.iid_at(event.x, event.y)
        if not iid:
            self.selected_iids.clear()
            self.anchor_iid = None
            self.status_var.set("已取消选择")
            self.draw_layout()
            return
        if iid not in self.selected_iids:
            self.selected_iids = {iid}
        self.anchor_iid = iid
        self.draw_layout()
        self.session_menu.tk_popup(event.x_root, event.y_root)

    def on_mouse_wheel(self, event: tk.Event) -> None:
        if not hasattr(self, "list_box"):
            return
        x1, y1, x2, y2 = self.list_box
        if not (x1 <= event.x <= x2 and y1 <= event.y <= y2):
            return
        direction = -1 if event.delta > 0 else 1
        self.list_scroll = max(0, min(self.list_scroll + direction, max(len(self.sessions) - 1, 0)))
        self.draw_layout()

    def iid_at(self, x: int, y: int) -> str | None:
        for iid, x1, y1, x2, y2, _ in self.row_boxes:
            if x1 <= x <= x2 and y1 <= y <= y2:
                return iid
        return None

    def index_from_iid(self, iid: str | None) -> int | None:
        if not iid:
            return None
        try:
            return int(iid.split(":", 1)[0])
        except ValueError:
            return None

    def import_directory(self) -> None:
        path = filedialog.askdirectory(
            title="请选择要导入的会话文件夹",
            initialdir=str(Path.home() / "Desktop"),
        )
        if not path:
            return

        self.clear_output()
        self.append_output(f"开始导入并自动修复：{path}")
        try:
            logs = repair_directory(Path(path))
            for line in logs:
                self.append_output(line)
            self.path_var.set(str(SESSIONS_DIR))
            self.refresh_sessions(log=False)
            self.append_output("导入完成，列表已刷新。")
            self.status_var.set("导入并修复完成")
            messagebox.showinfo("完成", "会话导入并自动修复完成。")
        except Exception as exc:
            self.append_output(f"错误：{exc}")
            self.status_var.set("导入失败")
            messagebox.showerror("导入失败", str(exc))

    def export_current_sessions(self) -> None:
        items = self.selected_session_items()
        if not items:
            messagebox.showerror("导出失败", "当前没有可导出的会话。")
            return

        path = filedialog.askdirectory(
            title="请选择导出保存位置",
            initialdir=str(Path.home() / "Desktop"),
        )
        if not path:
            return

        selected_count = len(self.selected_iids)
        source_paths = [item.source_path for item in items]
        scope_label = "选中会话" if selected_count else "全部会话"
        self.clear_output()
        self.append_output(f"开始导出{scope_label}：{len(items)} 个")
        try:
            logs = export_sessions(Path(path), source_paths, scope_label)
            for line in logs:
                self.append_output(line)
            self.append_output("导出完成。")
            if selected_count:
                self.status_var.set(f"已导出选中会话：{len(items)} 个")
                messagebox.showinfo("完成", f"已导出选中会话 {len(items)} 个。")
            else:
                self.status_var.set(f"已导出全部会话：{len(items)} 个")
                messagebox.showinfo("完成", f"已导出全部会话 {len(items)} 个。")
        except Exception as exc:
            self.append_output(f"错误：{exc}")
            self.status_var.set("导出失败")
            messagebox.showerror("导出失败", str(exc))

    def open_selected_in_explorer(self) -> None:
        iid = next(iter(self.selected_iids), None)
        item = self.item_by_iid.get(iid) if iid else None
        if not item:
            return

        path = normalize_display_path(item.source_path)
        if not path.exists():
            messagebox.showerror("文件不存在", f"找不到会话文件：{path}")
            return
        subprocess.Popen(["explorer.exe", f"/select,{path}"])


def _app_choose_duplicate_import_mode(self: App, import_root: Path) -> str | None:
    duplicate_ids = find_duplicate_thread_ids_for_import(import_root)
    if not duplicate_ids:
        return "replace"

    dialog = tk.Toplevel(self.root)
    dialog.title("会话 ID 重复")
    dialog.configure(bg=self.BG)
    dialog.resizable(False, False)
    dialog.transient(self.root)
    dialog.grab_set()

    result = tk.StringVar(value="")
    shown_ids = "\n".join(sorted(duplicate_ids)[:8])
    if len(duplicate_ids) > 8:
        shown_ids += f"\n...另有 {len(duplicate_ids) - 8} 个重复 ID"

    frame = tk.Frame(dialog, bg=self.BG, padx=22, pady=18)
    frame.pack(fill="both", expand=True)
    tk.Label(
        frame,
        text="检测到导入会话 ID 已存在",
        bg=self.BG,
        fg=self.TEXT,
        font=("Microsoft YaHei UI", 13, "bold"),
    ).pack(anchor="w")
    tk.Label(
        frame,
        text=f"重复 ID 数量：{len(duplicate_ids)}",
        bg=self.BG,
        fg=self.MUTED,
        font=("Microsoft YaHei UI", 10, "bold"),
    ).pack(anchor="w", pady=(8, 0))
    tk.Label(
        frame,
        text=shown_ids,
        bg=self.BG,
        fg=self.TEXT,
        justify="left",
        font=("Consolas", 9, "bold"),
    ).pack(anchor="w", pady=(10, 12))
    tk.Label(
        frame,
        text="替换：使用导入会话覆盖本机同 ID 会话。\n增量：保留本机会话，并给导入会话生成新 ID，列表中会出现两条内容相同但 ID 不同的会话。",
        bg=self.BG,
        fg=self.MUTED,
        justify="left",
        wraplength=520,
        font=("Microsoft YaHei UI", 10, "bold"),
    ).pack(anchor="w", pady=(0, 16))

    button_row = tk.Frame(frame, bg=self.BG)
    button_row.pack(anchor="e")

    def finish(value: str) -> None:
        result.set(value)
        dialog.destroy()

    for text, value in (("替换", "replace"), ("增量", "incremental")):
        tk.Button(
            button_row,
            text=text,
            command=lambda selected=value: finish(selected),
            bg=self.PRIMARY,
            activebackground=self.PRIMARY_HOVER,
            fg="#FFFFFF",
            activeforeground="#FFFFFF",
            relief="flat",
            bd=0,
            padx=18,
            pady=8,
            cursor="hand2",
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(side="left", padx=(0, 10))

    dialog.update_idletasks()
    x = self.root.winfo_rootx() + max((self.root.winfo_width() - dialog.winfo_width()) // 2, 0)
    y = self.root.winfo_rooty() + max((self.root.winfo_height() - dialog.winfo_height()) // 2, 0)
    dialog.geometry(f"+{x}+{y}")
    dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
    self.root.wait_window(dialog)
    return result.get() or None


def _app_import_directory(self: App) -> None:
    path = filedialog.askdirectory(
        title="请选择要导入的会话文件夹",
        initialdir=str(Path.home() / "Desktop"),
    )
    if not path:
        return

    import_root = Path(path)
    duplicate_mode = self.choose_duplicate_import_mode(import_root)
    if duplicate_mode is None:
        return

    self.clear_output()
    self.append_output(f"开始导入并自动修复：{path}")
    try:
        logs = repair_directory(import_root, duplicate_mode=duplicate_mode)
        for line in logs:
            self.append_output(line)
        self.path_var.set(str(SESSIONS_DIR))
        self.refresh_sessions(log=False)
        self.append_output("导入完成，列表已刷新。")
        self.status_var.set("导入并修复完成")
        messagebox.showinfo("完成", "会话导入并自动修复完成。")
    except Exception as exc:
        self.append_output(f"错误：{exc}")
        self.status_var.set("导入失败")
        messagebox.showerror("导入失败", str(exc))


App.choose_duplicate_import_mode = _app_choose_duplicate_import_mode
App.import_directory = _app_import_directory


def _app_repair_missing_plugin_skills(self: App) -> None:
    self.clear_output()
    self.append_output("开始修复丢失插件技能。")
    try:
        logs = repair_missing_plugin_skills()
        for line in logs:
            self.append_output(line)
        self.status_var.set("插件技能修复完成")
        messagebox.showinfo("完成", "丢失插件技能修复完成。建议重启 Codex 以刷新列表。")
    except Exception as exc:
        self.append_output(f"错误：{exc}")
        self.status_var.set("插件技能修复失败")
        messagebox.showerror("插件技能修复失败", str(exc))


App.repair_missing_plugin_skills = _app_repair_missing_plugin_skills


def _app_repair_current_path(self: App) -> None:
    chosen = self.path_var.get().strip()
    if not chosen:
        messagebox.showerror("修复失败", "请先选择会话文件或会话目录。")
        return

    target = Path(chosen)
    self.clear_output()
    self.append_output(f"开始修复：{target}")
    try:
        if target.is_dir():
            logs = repair_directory(target)
        else:
            _context, logs = repair_session(target)
        for line in logs:
            self.append_output(line)
        self.refresh_sessions(log=False)
        self.append_output("修复完成，列表已刷新。")
        self.status_var.set("修复完成")
        messagebox.showinfo("完成", "会话修复完成。")
    except Exception as exc:
        self.append_output(f"错误：{exc}")
        self.status_var.set("修复失败")
        messagebox.showerror("修复失败", str(exc))


App.repair_current_path = _app_repair_current_path


def run_gui() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    try:
        if len(sys.argv) > 1:
            command = sys.argv[1]
            if command == "--export" and len(sys.argv) > 2:
                for line in export_sessions(Path(sys.argv[2])):
                    print(line)
            elif command == "--import" and len(sys.argv) > 2:
                for line in repair_directory(Path(sys.argv[2])):
                    print(line)
            elif command == "--repair-plugins":
                for line in repair_missing_plugin_skills():
                    print(line)
            else:
                target = Path(command)
                if target.is_dir():
                    for line in repair_directory(target):
                        print(line)
                else:
                    ctx, log_lines = repair_session(target)
                    print(f"修复线程：{ctx.thread_id}")
                    for line in log_lines:
                        print(line)
        else:
            run_gui()
    except Exception as exc:
        log_unhandled_error(exc)
        raise
