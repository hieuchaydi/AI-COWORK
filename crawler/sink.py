"""
Sink: idempotent upsert with content-change versioning.

Rules:
- Upsert by dedup_key. If content_hash unchanged → update last_seen_at only.
- If content_hash changed → write new article + article_versions row.
- Discovery and extraction never write directly; only Sink writes.
- Records failing validation go to quarantine, never silently dropped.
"""

import json
from dataclasses import dataclass, field
from typing import List, Optional, Any, Dict
from datetime import datetime, timezone

from .storage import CrawlerStorage
from .records import validate_record
from .models import Record, Source


@dataclass
class SinkResult:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    quarantined: int = 0
    errors: List[str] = field(default_factory=list)


class Sink:
    def __init__(self, storage: CrawlerStorage):
        self._storage = storage

    async def write(self, records: List[Record], source: Source, task_id: str = "") -> SinkResult:
        """Write records to storage. Idempotent upsert by dedup_key.
        Returns stats: inserted, updated, unchanged, quarantined."""
        result = SinkResult()

        for record in records:
            raw_data = record.data
            record_type = record.record_type

            # 1. Validate against schema
            validated_record, errors = validate_record(raw_data, record_type)
            if errors or not validated_record:
                await self.quarantine(
                    task_id=task_id or record.dedup_key,
                    reason="Validation failed",
                    errors=errors,
                    response_id=raw_data.get("response_id")
                )
                result.quarantined += 1
                result.errors.extend(errors)
                continue

            record_dict = validated_record.model_dump()
            dedup_key = record.dedup_key
            content_hash = record.content_hash or record_dict.get("content_hash", "")
            now_iso = datetime.now(timezone.utc).isoformat()

            # Ensure necessary fields for Article storage
            if record_type == "Article":
                article_payload = {
                    "article_id": str(record_dict.get("article_id")),
                    "source_id": source.source_id,
                    "dedup_key": dedup_key,
                    "canonical_url": record_dict.get("canonical_url", ""),
                    "title": record_dict.get("title", ""),
                    "lead": record_dict.get("lead", ""),
                    "body_text": record_dict.get("body_text", ""),
                    "body_html_ref": record_dict.get("body_html", ""),
                    "published_at": record_dict.get("published_at").isoformat() if record_dict.get("published_at") else None,
                    "updated_at": record_dict.get("updated_at").isoformat() if record_dict.get("updated_at") else None,
                    "authors_json": json.dumps(record_dict.get("authors", [])),
                    "category_path": json.dumps(record_dict.get("category_path", [])),
                    "tags_json": json.dumps(record_dict.get("tags", [])),
                    "images_json": json.dumps(record_dict.get("images", [])),
                    "content_hash": content_hash,
                    "extractor_version": record.extractor_version,
                    "first_seen_at": now_iso,
                    "last_seen_at": now_iso,
                    "run_id": record.run_id or ""
                }

                # 2. Check existing by dedup_key
                existing = await self._storage.get_article_by_dedup_key(dedup_key)

                if not existing:
                    # not exists → INSERT
                    await self._storage.upsert_article(article_payload)
                    result.inserted += 1
                else:
                    if existing.get("content_hash") == content_hash:
                        # exists, same hash → UPDATE last_seen_at only
                        await self._storage.update_article_last_seen(dedup_key, now_iso)
                        result.unchanged += 1
                    else:
                        # exists, different hash → UPDATE article + INSERT article_versions
                        await self._storage.upsert_article(article_payload)
                        await self._storage.save_version(
                            article_id=str(record_dict.get("article_id")),
                            content_hash=existing.get("content_hash", ""),
                            captured_at=existing.get("last_seen_at") or now_iso,
                            diff_ref=""
                        )
                        result.updated += 1
            else:
                # Other record types
                result.inserted += 1

        return result

    async def quarantine(
        self,
        task_id: str,
        reason: str,
        errors: List[str],
        response_id: Optional[int] = None
    ):
        """Send a failed record to quarantine."""
        await self._storage.quarantine_record(
            task_id=task_id,
            reason=reason,
            validation_errors=errors,
            response_id=response_id,
            created_at=datetime.now(timezone.utc).isoformat()
        )
