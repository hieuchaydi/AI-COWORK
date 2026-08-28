"""
SQLite-backed storage for Crawler Layer.
Wraps standard sqlite3 in async methods using asyncio.to_thread.
"""
import sqlite3
import json
import asyncio
from typing import List, Optional, Dict, Any
from pathlib import Path
from datetime import datetime
from .models import Task, TaskState

class CrawlerStorage:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
    
    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    async def init_db(self):
        """Initializes database schema."""
        schema = '''
        CREATE TABLE IF NOT EXISTS sources (
            source_id TEXT PRIMARY KEY,
            name TEXT,
            access_class TEXT,
            authorization_ref TEXT,
            config_hash TEXT,
            enabled BOOLEAN
        );

        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            source_id TEXT,
            dedup_key TEXT,
            kind TEXT,
            url TEXT,
            state TEXT,
            attempt INTEGER DEFAULT 0,
            priority INTEGER DEFAULT 0,
            next_run_at TEXT,
            last_error_kind TEXT,
            run_id TEXT,
            created_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_tasks_source_state ON tasks(source_id, state, priority DESC);
        CREATE INDEX IF NOT EXISTS idx_tasks_dedup ON tasks(source_id, dedup_key);

        CREATE TABLE IF NOT EXISTS raw_responses (
            response_id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT,
            fetched_at TEXT,
            status INTEGER,
            headers_json TEXT,
            body_blob_ref BLOB,
            content_hash TEXT,
            ttl_at TEXT
        );

        CREATE TABLE IF NOT EXISTS articles (
            article_id TEXT PRIMARY KEY,
            source_id TEXT,
            dedup_key TEXT UNIQUE,
            canonical_url TEXT,
            title TEXT,
            lead TEXT,
            body_text TEXT,
            body_html_ref TEXT,
            published_at TEXT,
            updated_at TEXT,
            authors_json TEXT,
            category_path TEXT,
            tags_json TEXT,
            images_json TEXT,
            content_hash TEXT,
            extractor_version INTEGER,
            first_seen_at TEXT,
            last_seen_at TEXT,
            run_id TEXT
        );

        CREATE TABLE IF NOT EXISTS article_versions (
            version_id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id TEXT,
            content_hash TEXT,
            captured_at TEXT,
            diff_ref TEXT
        );

        CREATE TABLE IF NOT EXISTS quarantine (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT,
            reason TEXT,
            validation_errors_json TEXT,
            response_id INTEGER,
            created_at TEXT
        );
        '''
        def _exec():
            with self._get_conn() as conn:
                conn.executescript(schema)
        await asyncio.to_thread(_exec)

    async def save_task(self, task: Task):
        def _exec():
            with self._get_conn() as conn:
                conn.execute(
                    '''INSERT OR REPLACE INTO tasks 
                       (task_id, source_id, dedup_key, kind, url, state, attempt, priority, next_run_at, last_error_kind, run_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (task.task_id, task.source_id, task.dedup_key, task.kind.value, task.url, 
                     task.state.value, task.attempt, task.priority, 
                     task.next_run_at.isoformat() if task.next_run_at else None, 
                     task.last_error_kind, task.run_id, task.created_at.isoformat())
                )
        await asyncio.to_thread(_exec)

    async def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        def _exec():
            with self._get_conn() as conn:
                cursor = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
                row = cursor.fetchone()
                return dict(row) if row else None
        return await asyncio.to_thread(_exec)

    async def get_task_by_dedup_key(self, source_id: str, dedup_key: str) -> Optional[Dict[str, Any]]:
        def _exec():
            with self._get_conn() as conn:
                cursor = conn.execute("SELECT * FROM tasks WHERE source_id = ? AND dedup_key = ?", (source_id, dedup_key))
                row = cursor.fetchone()
                return dict(row) if row else None
        return await asyncio.to_thread(_exec)

    async def update_task_state(self, task_id: str, state: TaskState):
        def _exec():
            with self._get_conn() as conn:
                conn.execute("UPDATE tasks SET state = ? WHERE task_id = ?", (state.value, task_id))
        await asyncio.to_thread(_exec)

    async def update_task_retry(self, task_id: str, attempt: int, state: TaskState, next_run_at: datetime, last_error_kind: str):
        def _exec():
            with self._get_conn() as conn:
                conn.execute(
                    "UPDATE tasks SET attempt = ?, state = ?, next_run_at = ?, last_error_kind = ? WHERE task_id = ?",
                    (attempt, state.value, next_run_at.isoformat(), last_error_kind, task_id)
                )
        await asyncio.to_thread(_exec)

    async def get_pending_tasks(self, source_id: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
        def _exec():
            with self._get_conn() as conn:
                if source_id:
                    cursor = conn.execute(
                        "SELECT * FROM tasks WHERE source_id = ? AND state = ? ORDER BY priority DESC LIMIT ?", 
                        (source_id, TaskState.PENDING.value, limit)
                    )
                else:
                    cursor = conn.execute(
                        "SELECT * FROM tasks WHERE state = ? ORDER BY priority DESC LIMIT ?", 
                        (TaskState.PENDING.value, limit)
                    )
                return [dict(r) for r in cursor.fetchall()]
        return await asyncio.to_thread(_exec)

    async def get_task_count_by_state(self, state: TaskState, source_id: Optional[str] = None) -> int:
        def _exec():
            with self._get_conn() as conn:
                if source_id:
                    cursor = conn.execute("SELECT COUNT(*) FROM tasks WHERE source_id = ? AND state = ?", (source_id, state.value))
                else:
                    cursor = conn.execute("SELECT COUNT(*) FROM tasks WHERE state = ?", (state.value,))
                return cursor.fetchone()[0]
        return await asyncio.to_thread(_exec)

    async def move_to_dead_letter(self, task_id: str, error_kind: str):
        def _exec():
            with self._get_conn() as conn:
                conn.execute(
                    "UPDATE tasks SET state = ?, last_error_kind = ? WHERE task_id = ?",
                    (TaskState.DEAD_LETTER.value, error_kind, task_id)
                )
        await asyncio.to_thread(_exec)

    async def requeue_dead_letters(self, source_id: str) -> int:
        def _exec():
            with self._get_conn() as conn:
                cursor = conn.execute(
                    "UPDATE tasks SET state = ?, attempt = 0 WHERE source_id = ? AND state = ?",
                    (TaskState.PENDING.value, source_id, TaskState.DEAD_LETTER.value)
                )
                return cursor.rowcount
        return await asyncio.to_thread(_exec)

    async def save_raw_response(self, task_id: str, fetched_at: str, status: int, headers: dict, body: bytes, content_hash: str, ttl_at: Optional[str] = None) -> int:
        def _exec():
            with self._get_conn() as conn:
                cursor = conn.execute(
                    '''INSERT INTO raw_responses (task_id, fetched_at, status, headers_json, body_blob_ref, content_hash, ttl_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)''',
                    (task_id, fetched_at, status, json.dumps(headers), body, content_hash, ttl_at)
                )
                return cursor.lastrowid
        return await asyncio.to_thread(_exec)

    async def get_raw_response(self, response_id: int) -> Optional[Dict[str, Any]]:
        def _exec():
            with self._get_conn() as conn:
                cursor = conn.execute("SELECT * FROM raw_responses WHERE response_id = ?", (response_id,))
                row = cursor.fetchone()
                return dict(row) if row else None
        return await asyncio.to_thread(_exec)

    async def upsert_article(self, article: Dict[str, Any]):
        def _exec():
            with self._get_conn() as conn:
                conn.execute(
                    '''INSERT INTO articles 
                       (article_id, source_id, dedup_key, canonical_url, title, lead, body_text, body_html_ref, published_at, updated_at, authors_json, category_path, tags_json, images_json, content_hash, extractor_version, first_seen_at, last_seen_at, run_id)
                       VALUES (:article_id, :source_id, :dedup_key, :canonical_url, :title, :lead, :body_text, :body_html_ref, :published_at, :updated_at, :authors_json, :category_path, :tags_json, :images_json, :content_hash, :extractor_version, :first_seen_at, :last_seen_at, :run_id)
                       ON CONFLICT(dedup_key) DO UPDATE SET
                           title = excluded.title,
                           lead = excluded.lead,
                           body_text = excluded.body_text,
                           body_html_ref = excluded.body_html_ref,
                           updated_at = excluded.updated_at,
                           authors_json = excluded.authors_json,
                           tags_json = excluded.tags_json,
                           images_json = excluded.images_json,
                           content_hash = excluded.content_hash,
                           extractor_version = excluded.extractor_version,
                           last_seen_at = excluded.last_seen_at''',
                    article
                )
        await asyncio.to_thread(_exec)

    async def get_article_by_dedup_key(self, dedup_key: str) -> Optional[Dict[str, Any]]:
        def _exec():
            with self._get_conn() as conn:
                cursor = conn.execute("SELECT * FROM articles WHERE dedup_key = ?", (dedup_key,))
                row = cursor.fetchone()
                return dict(row) if row else None
        return await asyncio.to_thread(_exec)

    async def update_article_last_seen(self, dedup_key: str, last_seen_at: str):
        def _exec():
            with self._get_conn() as conn:
                conn.execute("UPDATE articles SET last_seen_at = ? WHERE dedup_key = ?", (last_seen_at, dedup_key))
        await asyncio.to_thread(_exec)

    async def save_version(self, article_id: str, content_hash: str, captured_at: str, diff_ref: Optional[str] = None):
        def _exec():
            with self._get_conn() as conn:
                conn.execute(
                    '''INSERT INTO article_versions (article_id, content_hash, captured_at, diff_ref)
                       VALUES (?, ?, ?, ?)''',
                    (article_id, content_hash, captured_at, diff_ref or "")
                )
        await asyncio.to_thread(_exec)

    async def quarantine_record(self, task_id: str, reason: str, validation_errors: list, response_id: Optional[int] = None, created_at: Optional[str] = None):
        def _exec():
            with self._get_conn() as conn:
                conn.execute(
                    '''INSERT INTO quarantine (task_id, reason, validation_errors_json, response_id, created_at)
                       VALUES (?, ?, ?, ?, ?)''',
                    (task_id, reason, json.dumps(validation_errors), response_id, created_at or datetime.utcnow().isoformat())
                )
        await asyncio.to_thread(_exec)

    async def get_quarantine(self, task_id: str) -> List[Dict[str, Any]]:
        def _exec():
            with self._get_conn() as conn:
                cursor = conn.execute("SELECT * FROM quarantine WHERE task_id = ?", (task_id,))
                return [dict(r) for r in cursor.fetchall()]
        return await asyncio.to_thread(_exec)
