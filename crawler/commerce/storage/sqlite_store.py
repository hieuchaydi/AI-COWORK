"""
SQLite storage engine for monitored products, price snapshots, and alert history.
Safe, local-first database implementation.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..interfaces import ProductIdentity, ProductSnapshot


class CommerceSqliteStore:
    """Thread-safe SQLite store for product snapshots and alerts."""

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            default_dir = Path("outputs/commerce")
            default_dir.mkdir(parents=True, exist_ok=True)
            self.db_path = str(default_dir / "commerce_monitor.db")
        else:
            p = Path(db_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            self.db_path = str(p)

        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS monitored_products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    canonical_url TEXT NOT NULL UNIQUE,
                    title TEXT,
                    target_price REAL,
                    is_active INTEGER DEFAULT 1,
                    last_checked_at TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS product_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    title TEXT NOT NULL,
                    current_price REAL NOT NULL,
                    original_price REAL,
                    discount_rate REAL,
                    in_stock INTEGER NOT NULL,
                    stock_quantity INTEGER,
                    rating_score REAL,
                    review_count INTEGER,
                    seller_name TEXT,
                    image_url TEXT,
                    url TEXT NOT NULL,
                    raw_json TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_snapshots_lookup 
                ON product_snapshots (product_id, platform, created_at)
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS price_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    alert_rule TEXT NOT NULL,
                    previous_value REAL,
                    current_value REAL,
                    message TEXT NOT NULL,
                    delivered INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS platform_backoffs (
                    platform TEXT PRIMARY KEY,
                    backoff_until TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def upsert_monitored_product(
        self,
        identity: ProductIdentity,
        target_price: Optional[float] = None,
    ) -> int:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO monitored_products 
                (platform, product_id, canonical_url, title, target_price, is_active, created_at)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(canonical_url) DO UPDATE SET
                    title = COALESCE(excluded.title, monitored_products.title),
                    target_price = COALESCE(excluded.target_price, monitored_products.target_price),
                    is_active = 1
                """,
                (identity.platform, identity.product_id, identity.canonical_url, identity.title, target_price, now_iso),
            )
            conn.commit()
            return cursor.lastrowid or 0

    def list_monitored_products(self, active_only: bool = True) -> List[Dict[str, Any]]:
        query = "SELECT * FROM monitored_products"
        if active_only:
            query += " WHERE is_active = 1"
        query += " ORDER BY id DESC"

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query)
            return [dict(row) for row in cursor.fetchall()]

    def update_last_checked(self, canonical_url: str):
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE monitored_products SET last_checked_at = ? WHERE canonical_url = ?",
                (now_iso, canonical_url),
            )
            conn.commit()

    def deactivate_product(self, canonical_url: str):
        with self._get_connection() as conn:
            conn.execute("UPDATE monitored_products SET is_active = 0 WHERE canonical_url = ?", (canonical_url,))
            conn.commit()

    def record_snapshot(self, snapshot: ProductSnapshot) -> int:
        now_iso = snapshot.timestamp.isoformat()
        raw_json = json.dumps(snapshot.raw_attributes, ensure_ascii=False)
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO product_snapshots (
                    product_id, platform, title, current_price, original_price,
                    discount_rate, in_stock, stock_quantity, rating_score,
                    review_count, seller_name, image_url, url, raw_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.product_id,
                    snapshot.platform,
                    snapshot.title,
                    snapshot.current_price,
                    snapshot.original_price,
                    snapshot.discount_rate,
                    1 if snapshot.in_stock else 0,
                    snapshot.stock_quantity,
                    snapshot.rating_score,
                    snapshot.review_count,
                    snapshot.seller_name,
                    snapshot.image_url,
                    snapshot.url,
                    raw_json,
                    now_iso,
                ),
            )
            conn.commit()
            return cursor.lastrowid or 0

    def get_latest_snapshot(self, product_id: str, platform: str) -> Optional[ProductSnapshot]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT * FROM product_snapshots 
                WHERE product_id = ? AND platform = ? 
                ORDER BY created_at DESC LIMIT 1
                """,
                (product_id, platform),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return self._row_to_snapshot(row)

    def get_snapshots_history(
        self,
        product_id: str,
        platform: str,
        limit: int = 50,
        days: Optional[int] = None,
    ) -> List[ProductSnapshot]:
        query = "SELECT * FROM product_snapshots WHERE product_id = ? AND platform = ?"
        params: List[Any] = [product_id, platform]

        if days is not None and days > 0:
            since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            query += " AND created_at >= ?"
            params.append(since)

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [self._row_to_snapshot(r) for r in rows]

    def get_lowest_price(self, product_id: str, platform: str, days: int = 30) -> Optional[float]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT MIN(current_price) AS min_price 
                FROM product_snapshots 
                WHERE product_id = ? AND platform = ? AND created_at >= ?
                """,
                (product_id, platform, since),
            )
            row = cursor.fetchone()
            if row and row["min_price"] is not None:
                return float(row["min_price"])
            return None

    def record_alert(
        self,
        product_id: str,
        platform: str,
        rule: str,
        previous_value: Optional[float],
        current_value: float,
        message: str,
    ) -> int:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO price_alerts (
                    product_id, platform, alert_rule, previous_value,
                    current_value, message, delivered, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (product_id, platform, rule, previous_value, current_value, message, now_iso),
            )
            conn.commit()
            return cursor.lastrowid or 0

    def get_undelivered_alerts(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM price_alerts WHERE delivered = 0 ORDER BY created_at ASC")
            return [dict(r) for r in cursor.fetchall()]

    def mark_alert_delivered(self, alert_id: int):
        with self._get_connection() as conn:
            conn.execute("UPDATE price_alerts SET delivered = 1 WHERE id = ?", (alert_id,))
            conn.commit()

    def set_platform_backoff(
        self,
        platform: str,
        backoff_until: datetime,
        reason: str,
    ) -> None:
        if backoff_until.tzinfo is None or backoff_until.utcoffset() is None:
            raise ValueError("backoff_until must be timezone-aware")
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO platform_backoffs (platform, backoff_until, reason, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(platform) DO UPDATE SET
                    backoff_until = excluded.backoff_until,
                    reason = excluded.reason,
                    updated_at = excluded.updated_at
                """,
                (platform, backoff_until.isoformat(), reason, now_iso),
            )
            conn.commit()

    def get_platform_backoff_until(self, platform: str) -> Optional[datetime]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT backoff_until FROM platform_backoffs WHERE platform = ?",
                (platform,),
            ).fetchone()
        if not row:
            return None
        backoff_until = datetime.fromisoformat(row["backoff_until"])
        if backoff_until <= datetime.now(timezone.utc):
            self.clear_platform_backoff(platform)
            return None
        return backoff_until

    def clear_platform_backoff(self, platform: str) -> None:
        with self._get_connection() as conn:
            conn.execute("DELETE FROM platform_backoffs WHERE platform = ?", (platform,))
            conn.commit()

    @staticmethod
    def _row_to_snapshot(row: sqlite3.Row) -> ProductSnapshot:
        raw_dict = {}
        if row["raw_json"]:
            try:
                raw_dict = json.loads(row["raw_json"])
            except Exception:
                pass

        return ProductSnapshot(
            product_id=row["product_id"],
            platform=row["platform"],
            title=row["title"],
            current_price=row["current_price"],
            original_price=row["original_price"],
            discount_rate=row["discount_rate"],
            currency="VND",
            in_stock=bool(row["in_stock"]),
            stock_quantity=row["stock_quantity"],
            rating_score=row["rating_score"],
            review_count=row["review_count"],
            seller_name=row["seller_name"],
            image_url=row["image_url"],
            url=row["url"],
            raw_attributes=raw_dict,
            timestamp=datetime.fromisoformat(row["created_at"]),
        )
