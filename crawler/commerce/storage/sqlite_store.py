"""
SQLite storage engine for monitored products, price snapshots, alert history,
and Profit Guard pricing recommendations, approvals, and audit logs.
Safe, local-first database implementation with CWD-independent path resolution.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from ..interfaces import ProductIdentity, ProductSnapshot
from ..profit_guard.models import (
    ApprovalStatus,
    AuditAction,
    CostProfile,
    PriceRecommendation,
    PricingAuditLogEntry,
)


class CommerceSqliteStore:
    """Thread-safe SQLite store for product snapshots, alerts, and Profit Guard."""

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            env_path = os.getenv("COMMERCE_DB_PATH")
            if env_path:
                self.db_path = str(Path(env_path).resolve())
            else:
                # Resolve relative to project root regardless of CWD
                project_root = Path(__file__).resolve().parents[3]
                default_dir = project_root / "outputs" / "commerce"
                default_dir.mkdir(parents=True, exist_ok=True)
                self.db_path = str(default_dir / "commerce_monitor.db")
        else:
            p = Path(db_path).resolve()
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
                CREATE TABLE IF NOT EXISTS platform_backoffs (
                    platform TEXT PRIMARY KEY,
                    backoff_until TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
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

            # ── Profit Guard Tables ──────────────────────────────────────
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS profit_guard_profiles (
                    sku_id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    cost_price TEXT NOT NULL,
                    platform_fee_pct TEXT NOT NULL,
                    platform_fee_fixed TEXT NOT NULL DEFAULT '0',
                    tax_pct TEXT NOT NULL DEFAULT '0',
                    additional_cost TEXT NOT NULL DEFAULT '0',
                    min_margin_pct TEXT NOT NULL,
                    max_price_change_pct TEXT NOT NULL DEFAULT '0.10',
                    cooldown_seconds INTEGER NOT NULL DEFAULT 3600,
                    authorization_ref TEXT,
                    seller_provider TEXT,
                    is_active INTEGER DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS price_recommendations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sku_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    current_price TEXT NOT NULL,
                    market_price TEXT,
                    breakeven_price TEXT NOT NULL,
                    floor_price TEXT NOT NULL,
                    recommended_price TEXT NOT NULL,
                    profit_amount TEXT NOT NULL,
                    profit_margin_pct TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    expires_at TEXT NOT NULL,
                    approver TEXT,
                    rejection_reason TEXT,
                    applied_at TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_rec_sku_status
                ON price_recommendations (sku_id, status)
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS pricing_audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recommendation_id INTEGER,
                    sku_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    input_snapshot_json TEXT NOT NULL,
                    formula_metadata_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_audit_sku
                ON pricing_audit_logs (sku_id, created_at)
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS seller_provider_backoff (
                    provider_name TEXT PRIMARY KEY,
                    backoff_until REAL NOT NULL,
                    failure_count INTEGER DEFAULT 0,
                    last_error TEXT
                )
                """
            )
            conn.commit()

    # ── Monitored Products & Snapshots ──────────────────────────────────

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

    # ── Profit Guard Store Operations ───────────────────────────────────

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

    def upsert_cost_profile(self, profile: CostProfile):
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO profit_guard_profiles (
                    sku_id, platform, product_id, cost_price, platform_fee_pct,
                    platform_fee_fixed, tax_pct, additional_cost, min_margin_pct,
                    max_price_change_pct, cooldown_seconds, authorization_ref,
                    seller_provider, is_active, updated_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sku_id) DO UPDATE SET
                    platform = excluded.platform,
                    product_id = excluded.product_id,
                    cost_price = excluded.cost_price,
                    platform_fee_pct = excluded.platform_fee_pct,
                    platform_fee_fixed = excluded.platform_fee_fixed,
                    tax_pct = excluded.tax_pct,
                    additional_cost = excluded.additional_cost,
                    min_margin_pct = excluded.min_margin_pct,
                    max_price_change_pct = excluded.max_price_change_pct,
                    cooldown_seconds = excluded.cooldown_seconds,
                    authorization_ref = excluded.authorization_ref,
                    seller_provider = excluded.seller_provider,
                    is_active = excluded.is_active,
                    updated_at = excluded.updated_at
                """,
                (
                    profile.sku_id,
                    profile.platform,
                    profile.product_id,
                    str(profile.cost_price),
                    str(profile.platform_fee_pct),
                    str(profile.platform_fee_fixed),
                    str(profile.tax_pct),
                    str(profile.additional_cost),
                    str(profile.min_margin_pct),
                    str(profile.max_price_change_pct),
                    profile.cooldown_seconds,
                    profile.authorization_ref,
                    profile.seller_provider,
                    1 if profile.is_active else 0,
                    now_iso,
                    profile.created_at.isoformat(),
                ),
            )
            conn.commit()

    def get_cost_profile(self, sku_id: str) -> Optional[CostProfile]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM profit_guard_profiles WHERE sku_id = ?", (sku_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return CostProfile(
                sku_id=row["sku_id"],
                platform=row["platform"],
                product_id=row["product_id"],
                cost_price=Decimal(row["cost_price"]),
                platform_fee_pct=Decimal(row["platform_fee_pct"]),
                platform_fee_fixed=Decimal(row["platform_fee_fixed"]),
                tax_pct=Decimal(row["tax_pct"]),
                additional_cost=Decimal(row["additional_cost"]),
                min_margin_pct=Decimal(row["min_margin_pct"]),
                max_price_change_pct=Decimal(row["max_price_change_pct"]),
                cooldown_seconds=row["cooldown_seconds"],
                authorization_ref=row["authorization_ref"],
                seller_provider=row["seller_provider"],
                is_active=bool(row["is_active"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
                created_at=datetime.fromisoformat(row["created_at"]),
            )

    def list_cost_profiles(self, active_only: bool = True) -> List[CostProfile]:
        query = "SELECT * FROM profit_guard_profiles"
        if active_only:
            query += " WHERE is_active = 1"
        query += " ORDER BY sku_id ASC"

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query)
            rows = cursor.fetchall()
            return [
                CostProfile(
                    sku_id=r["sku_id"],
                    platform=r["platform"],
                    product_id=r["product_id"],
                    cost_price=Decimal(r["cost_price"]),
                    platform_fee_pct=Decimal(r["platform_fee_pct"]),
                    platform_fee_fixed=Decimal(r["platform_fee_fixed"]),
                    tax_pct=Decimal(r["tax_pct"]),
                    additional_cost=Decimal(r["additional_cost"]),
                    min_margin_pct=Decimal(r["min_margin_pct"]),
                    max_price_change_pct=Decimal(r["max_price_change_pct"]),
                    cooldown_seconds=r["cooldown_seconds"],
                    authorization_ref=r["authorization_ref"],
                    seller_provider=r["seller_provider"],
                    is_active=bool(r["is_active"]),
                    updated_at=datetime.fromisoformat(r["updated_at"]),
                    created_at=datetime.fromisoformat(r["created_at"]),
                )
                for r in rows
            ]

    def save_recommendation(self, rec: PriceRecommendation) -> int:
        now_iso = rec.created_at.isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO price_recommendations (
                    sku_id, platform, current_price, market_price,
                    breakeven_price, floor_price, recommended_price,
                    profit_amount, profit_margin_pct, reason, status,
                    expires_at, approver, rejection_reason, applied_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rec.sku_id,
                    rec.platform,
                    str(rec.current_price),
                    str(rec.market_price) if rec.market_price is not None else None,
                    str(rec.breakeven_price),
                    str(rec.floor_price),
                    str(rec.recommended_price),
                    str(rec.profit_amount),
                    str(rec.profit_margin_pct),
                    rec.reason,
                    rec.status.value,
                    rec.expires_at.isoformat(),
                    rec.approver,
                    rec.rejection_reason,
                    rec.applied_at.isoformat() if rec.applied_at else None,
                    now_iso,
                ),
            )
            conn.commit()
            return cursor.lastrowid or 0

    def get_recommendation(self, rec_id: int) -> Optional[PriceRecommendation]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM price_recommendations WHERE id = ?", (rec_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return self._row_to_recommendation(row)

    def get_latest_recommendation(self, sku_id: str) -> Optional[PriceRecommendation]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM price_recommendations WHERE sku_id = ? ORDER BY created_at DESC LIMIT 1",
                (sku_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return self._row_to_recommendation(row)

    def list_recommendations(
        self,
        status: Optional[ApprovalStatus] = None,
        sku_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[PriceRecommendation]:
        query = "SELECT * FROM price_recommendations WHERE 1=1"
        params: List[Any] = []

        if status:
            query += " AND status = ?"
            params.append(status.value)
        if sku_id:
            query += " AND sku_id = ?"
            params.append(sku_id)

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [self._row_to_recommendation(r) for r in rows]

    def update_recommendation_status(
        self,
        rec_id: int,
        status: ApprovalStatus,
        approver: Optional[str] = None,
        rejection_reason: Optional[str] = None,
        applied_at: Optional[datetime] = None,
        expected_status: Optional[ApprovalStatus] = None,
    ) -> bool:
        """
        Atomically updates recommendation status with optimistic concurrency control.
        If expected_status is provided, update only succeeds if current status matches expected_status.
        """
        query = "UPDATE price_recommendations SET status = ?"
        params: List[Any] = [status.value]

        if approver is not None:
            query += ", approver = ?"
            params.append(approver)
        if rejection_reason is not None:
            query += ", rejection_reason = ?"
            params.append(rejection_reason)
        if applied_at is not None:
            query += ", applied_at = ?"
            params.append(applied_at.isoformat())

        query += " WHERE id = ?"
        params.append(rec_id)

        if expected_status is not None:
            query += " AND status = ?"
            params.append(expected_status.value)

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            conn.commit()
            return cursor.rowcount > 0

    def record_audit_log(self, entry: PricingAuditLogEntry) -> int:
        now_iso = entry.created_at.isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO pricing_audit_logs (
                    recommendation_id, sku_id, action, actor,
                    input_snapshot_json, formula_metadata_json, result_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.recommendation_id,
                    entry.sku_id,
                    entry.action.value,
                    entry.actor,
                    json.dumps(entry.input_snapshot, ensure_ascii=False),
                    json.dumps(entry.formula_metadata, ensure_ascii=False),
                    json.dumps(entry.result, ensure_ascii=False),
                    now_iso,
                ),
            )
            conn.commit()
            return cursor.lastrowid or 0

    def get_audit_history(self, sku_id: Optional[str] = None, limit: int = 50) -> List[PricingAuditLogEntry]:
        query = "SELECT * FROM pricing_audit_logs"
        params: List[Any] = []
        if sku_id:
            query += " WHERE sku_id = ?"
            params.append(sku_id)

        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [
                PricingAuditLogEntry(
                    id=r["id"],
                    recommendation_id=r["recommendation_id"],
                    sku_id=r["sku_id"],
                    action=AuditAction(r["action"]),
                    actor=r["actor"],
                    input_snapshot=json.loads(r["input_snapshot_json"]),
                    formula_metadata=json.loads(r["formula_metadata_json"]),
                    result=json.loads(r["result_json"]),
                    created_at=datetime.fromisoformat(r["created_at"]),
                )
                for r in rows
            ]

    def get_provider_backoff(self, provider_name: str) -> Tuple[float, int]:
        """Returns (backoff_until_timestamp, failure_count)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT backoff_until, failure_count FROM seller_provider_backoff WHERE provider_name = ?",
                (provider_name.lower(),),
            )
            row = cursor.fetchone()
            if not row:
                return (0.0, 0)
            return (float(row["backoff_until"]), int(row["failure_count"]))

    def record_provider_backoff(self, provider_name: str, backoff_until: float, failure_count: int, last_error: str):
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO seller_provider_backoff (provider_name, backoff_until, failure_count, last_error)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(provider_name) DO UPDATE SET
                    backoff_until = excluded.backoff_until,
                    failure_count = excluded.failure_count,
                    last_error = excluded.last_error
                """,
                (provider_name.lower(), backoff_until, failure_count, last_error),
            )
            conn.commit()

    def clear_provider_backoff(self, provider_name: str):
        with self._get_connection() as conn:
            conn.execute(
                "DELETE FROM seller_provider_backoff WHERE provider_name = ?",
                (provider_name.lower(),),
            )
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

    @staticmethod
    def _row_to_recommendation(row: sqlite3.Row) -> PriceRecommendation:
        return PriceRecommendation(
            id=row["id"],
            sku_id=row["sku_id"],
            platform=row["platform"],
            current_price=Decimal(row["current_price"]),
            market_price=Decimal(row["market_price"]) if row["market_price"] is not None else None,
            breakeven_price=Decimal(row["breakeven_price"]),
            floor_price=Decimal(row["floor_price"]),
            recommended_price=Decimal(row["recommended_price"]),
            profit_amount=Decimal(row["profit_amount"]),
            profit_margin_pct=Decimal(row["profit_margin_pct"]),
            reason=row["reason"],
            status=ApprovalStatus(row["status"]),
            expires_at=datetime.fromisoformat(row["expires_at"]),
            approver=row["approver"],
            rejection_reason=row["rejection_reason"],
            applied_at=datetime.fromisoformat(row["applied_at"]) if row["applied_at"] else None,
            created_at=datetime.fromisoformat(row["created_at"]),
        )
