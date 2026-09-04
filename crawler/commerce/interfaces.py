"""
Core interfaces and data models for Commerce Connectors.
Follows clean, sustainable architecture without bot-bypass or evasive hacks.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


class CommerceException(Exception):
    """Base exception for all commerce connector operations."""


class CaptchaChallengeException(CommerceException):
    """
    Raised when a target site presents a CAPTCHA or Cloudflare Turnstile challenge.
    Explicitly triggers Human-in-the-loop workflow instead of illegal bypass attempts.
    """

    def __init__(self, platform: str, url: str, message: str = "Bot protection challenge detected. Human verification required."):
        super().__init__(f"[{platform}] {message} (URL: {url})")
        self.platform = platform
        self.url = url


class SessionExpiredException(CommerceException):
    """Raised when an authenticated session has expired and requires user to log in again."""

    def __init__(self, platform: str, message: str = "Session expired. Please log in via browser."):
        super().__init__(f"[{platform}] {message}")
        self.platform = platform


class RateLimitException(CommerceException):
    """Raised when target endpoint returns HTTP 429 Too Many Requests."""

    def __init__(self, platform: str, retry_after: float = 60.0):
        super().__init__(f"[{platform}] Rate limited. Back off for {retry_after}s.")
        self.platform = platform
        self.retry_after = retry_after


class ProductNotFoundException(CommerceException):
    """Raised when the product does not exist or has been removed."""


class AccessDeniedException(CommerceException):
    """Raised when permission is denied or credentials are invalid."""


class CommerceModel(BaseModel):
    """Strict base model for persisted commerce data."""

    model_config = ConfigDict(extra="forbid")


class ProductIdentity(CommerceModel):
    """Normalized identifier for a commerce product across platforms."""

    platform: str
    product_id: str
    sku_id: Optional[str] = None
    canonical_url: str
    shop_id: Optional[str] = None
    title: Optional[str] = None
    extra: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("canonical_url")
    @classmethod
    def validate_canonical_url(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("canonical_url must use HTTPS")
        return value


class ProductSnapshot(CommerceModel):
    """A point-in-time snapshot of product details, pricing, inventory, and ratings."""

    product_id: str
    platform: str
    title: str
    current_price: float = Field(gt=0)
    original_price: Optional[float] = Field(default=None, gt=0)
    discount_rate: Optional[float] = Field(default=None, ge=0, le=100)
    currency: str = "VND"
    in_stock: bool = True
    stock_quantity: Optional[int] = Field(default=None, ge=0)
    rating_score: Optional[float] = Field(default=None, ge=0, le=5)
    review_count: Optional[int] = Field(default=None, ge=0)
    seller_name: Optional[str] = None
    image_url: Optional[str] = None
    url: str
    raw_attributes: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("snapshot URL must use HTTPS")
        return value

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("snapshot timestamp must be timezone-aware")
        return value

    @property
    def computed_discount_pct(self) -> float:
        """Returns the discount percentage (0.0 to 1.0)."""
        if self.discount_rate is not None and self.discount_rate > 0:
            return self.discount_rate if self.discount_rate <= 1.0 else self.discount_rate / 100.0
        if self.original_price and self.original_price > self.current_price:
            return (self.original_price - self.current_price) / self.original_price
        return 0.0


class ReviewItem(CommerceModel):
    """A single product review."""

    review_id: str
    author: Optional[str] = None
    rating: int = Field(default=5, ge=1, le=5)
    content: Optional[str] = None
    created_at: Optional[datetime] = None
    media_urls: List[str] = Field(default_factory=list)


class ReviewPage(CommerceModel):
    """Paginated collection of reviews."""

    product_id: str
    reviews: List[ReviewItem] = Field(default_factory=list)
    next_cursor: Optional[str] = None
    has_next: bool = False


class CommerceConnector(ABC):
    """
    Abstract Base Class for compliant commerce connectors.
    Every connector implements this interface without bypass techniques.
    """

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """Unique identifier of the platform (e.g., 'tiki', 'shopee', 'lazada')."""
        pass

    @abstractmethod
    async def resolve_product(self, url: str) -> ProductIdentity:
        """Extracts and normalizes product ID and canonical URL from a raw link."""
        pass

    @abstractmethod
    async def fetch_product(self, identity: ProductIdentity) -> ProductSnapshot:
        """Fetches current product snapshot (price, stock, rating, title)."""
        pass

    @abstractmethod
    async def fetch_reviews(self, identity: ProductIdentity, cursor: Optional[str] = None) -> ReviewPage:
        """Fetches paginated reviews for the product."""
        pass
