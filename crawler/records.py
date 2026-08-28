"""Record schemas, validation, and quarantine logic.

Each Record type has a schema with required fields, types and ranges.
A record failing validation goes to quarantine with the raw HTML reference —
never silently dropped, never written half-populated.
"""

from pydantic import BaseModel, Field, field_validator, ValidationError
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime

class ArticleRecord(BaseModel):
    article_id: int  # from URL numeric ID
    canonical_url: str
    title: str
    lead: str = ""
    body_html: str
    body_text: str
    published_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    authors: List[str] = []
    category_path: List[str] = []
    tags: List[str] = []
    images: List[Dict[str, str]] = []  # [{url, caption}]
    source_id: str = ""
    fetched_at: datetime
    extractor_version: int = 1
    content_hash: str = ""

    # Validators for required fields
    @field_validator('title')
    @classmethod
    def title_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError('title must not be empty')
        return v.strip()
    
    @field_validator('body_text')
    @classmethod
    def body_not_empty(cls, v: str) -> str:
        if not v or len(v.strip()) < 50:
            raise ValueError('body_text too short (< 50 chars)')
        return v

class ShopProductRecord(BaseModel):
    item_id: int
    shop_id: int
    name: str
    price: float
    stock: int = 0
    brand: Optional[str] = None
    historical_sold: int = 0
    rating_star: float = 0.0
    images: List[str] = []
    fetched_at: datetime
    content_hash: str = ""
    extractor_version: int = 1

class ShopReviewRecord(BaseModel):
    review_id: str
    shop_id: int
    item_id: int
    model_id: Optional[str] = None
    rating_star: int = Field(ge=1, le=5)
    comment_text: str = ""
    media: List[Dict[str, Any]] = []
    buyer_ref: str  # salted hash
    create_time: datetime
    edit_time: Optional[datetime] = None
    reply_text: str = ""
    reply_time: Optional[datetime] = None
    reply_by: str = ""
    status: str = "visible"  # visible | hidden | removed
    fetched_at: datetime
    content_hash: str = ""
    extractor_version: int = 1

class QuarantineEntry(BaseModel):
    task_id: str
    reason: str
    validation_errors: List[str]
    response_id: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

def validate_record(data: Dict[str, Any], record_type: str) -> Tuple[Optional[BaseModel], List[str]]:
    """Validate extracted data against the record schema.
    Returns (validated_record, errors). If errors, record is None."""
    type_map = {
        'Article': ArticleRecord,
        'ShopProduct': ShopProductRecord,
        'ShopReview': ShopReviewRecord,
    }
    
    model_cls = type_map.get(record_type)
    if not model_cls:
        return None, [f"Unknown record type: {record_type}"]
        
    try:
        record = model_cls(**data)
        return record, []
    except ValidationError as e:
        errors = [f"{err['loc'][0]}: {err['msg']}" for err in e.errors()]
        return None, errors
