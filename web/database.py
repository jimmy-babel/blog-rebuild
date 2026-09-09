from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    image_mode TEXT NOT NULL,
    caption_mode TEXT NOT NULL,
    status TEXT NOT NULL,
    title TEXT,
    progress TEXT,
    error TEXT,
    result_dir TEXT,
    html_file TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def project_relative_path(value: object, project_root: Path) -> object:
    """Store project files independently of the absolute checkout location."""
    if not isinstance(value, str) or not value:
        return value

    path = Path(value)
    if not path.is_absolute():
        return path.as_posix()

    root = project_root.resolve()
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        # Migrate legacy absolute paths even when the old checkout no longer exists.
        parts = path.parts
        for index, part in enumerate(parts):
            if part.lower() == "result":
                return Path(*parts[index:]).as_posix()
        return value


class TaskStore:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.init()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def init(self) -> None:
        with self.lock, self.connect() as connection:
            connection.executescript(SCHEMA)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)").fetchall()}
            if "brand_reference" not in columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN brand_reference TEXT NOT NULL DEFAULT 'on'")

    def create(self, task_id: str, url: str, image_mode: str, caption_mode: str, brand_reference: str = "on") -> dict:
        created_at = now_iso()
        with self.lock, self.connect() as connection:
            connection.execute(
                "INSERT INTO tasks (id,url,image_mode,caption_mode,brand_reference,status,progress,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (task_id, url, image_mode, caption_mode, brand_reference, "queued", "等待执行", created_at),
            )
        return self.get(task_id)

    def get(self, task_id: str) -> dict | None:
        with self.lock, self.connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def list(self, query: str = "", status: str = "", page: int = 1, page_size: int = 20) -> tuple[list[dict], int]:
        page = max(1, page)
        page_size = min(100, max(1, page_size))
        clauses: list[str] = []
        params: list[object] = []
        if query:
            clauses.append("(url LIKE ? OR title LIKE ? OR id LIKE ?)")
            needle = f"%{query}%"
            params.extend([needle, needle, needle])
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.lock, self.connect() as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM tasks {where}", params).fetchone()[0]
            rows = connection.execute(
                f"SELECT * FROM tasks {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
        return [dict(row) for row in rows], int(total)

    def update(self, task_id: str, **fields: object) -> dict | None:
        allowed = {
            "status", "title", "progress", "error", "result_dir", "html_file",
            "started_at", "finished_at",
        }
        clean = {key: value for key, value in fields.items() if key in allowed}
        if not clean:
            return self.get(task_id)
        assignments = ", ".join(f"{key} = ?" for key in clean)
        values = [clean[key] for key in clean]
        values.append(task_id)
        with self.lock, self.connect() as connection:
            connection.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", values)
        return self.get(task_id)

    def delete(self, task_id: str) -> dict | None:
        task = self.get(task_id)
        if not task:
            return None
        with self.lock, self.connect() as connection:
            connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return task

    def recover_on_start(self) -> None:
        with self.lock, self.connect() as connection:
            connection.execute(
                "UPDATE tasks SET status='failed', error='服务重启，任务未完成', finished_at=? "
                "WHERE status='processing'",
                (now_iso(),),
            )

    def migrate_result_paths(self, project_root: Path) -> int:
        """Convert legacy absolute result paths to paths relative to project_root."""
        changed = 0
        with self.lock, self.connect() as connection:
            rows = connection.execute(
                "SELECT id, result_dir, html_file FROM tasks "
                "WHERE result_dir IS NOT NULL OR html_file IS NOT NULL"
            ).fetchall()
            for row in rows:
                result_dir = project_relative_path(row["result_dir"], project_root)
                html_file = project_relative_path(row["html_file"], project_root)
                if result_dir != row["result_dir"] or html_file != row["html_file"]:
                    connection.execute(
                        "UPDATE tasks SET result_dir = ?, html_file = ? WHERE id = ?",
                        (result_dir, html_file, row["id"]),
                    )
                    changed += 1
        return changed
