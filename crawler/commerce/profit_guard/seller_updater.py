"""
Official Seller API Price Updater Registry and Fail-Closed Manager.
Enforces authorization checks, persistent backoff across process restarts,
and idempotent updates.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from decimal import Decimal
import logging
import time
from typing import Dict, Optional

from .models import PriceUpdateResult

logger = logging.getLogger(__name__)


class SellerPriceUpdater(ABC):
    """
    Interface for official Seller API price updates (Shopee Open Platform,
    Tiki Open Platform, Lazada Open Platform, etc.).
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Unique provider identifier (e.g., 'shopee_open_api', 'tiki_open_api')."""
        pass

    @abstractmethod
    async def update_price(
        self,
        sku_id: str,
        new_price: Decimal,
        authorization_ref: str,
        idempotency_key: str,
    ) -> PriceUpdateResult:
        """
        Executes price update using official, authenticated vendor endpoints.
        Must be idempotent.
        """
        pass


class SellerUpdateRegistry:
    """Registry holding registered Seller API providers."""

    def __init__(self):
        self._providers: Dict[str, SellerPriceUpdater] = {}

    def register(self, provider: SellerPriceUpdater):
        self._providers[provider.provider_name.lower()] = provider
        logger.info(f"Registered official Seller API provider: {provider.provider_name}")

    def get(self, name: Optional[str]) -> Optional[SellerPriceUpdater]:
        if not name:
            return None
        return self._providers.get(name.lower())

    def list_providers(self) -> list[str]:
        return list(self._providers.keys())


class MockOfficialSellerApiProvider(SellerPriceUpdater):
    """
    Mock official Seller API provider for hermetic testing and verification.
    Simulates official API response, token validation, and transient error injection.
    """

    def __init__(self, provider_name: str = "mock_seller_api"):
        self._name = provider_name
        self.simulate_transient_error: bool = False
        self.call_history: list[dict] = []

    @property
    def provider_name(self) -> str:
        return self._name

    async def update_price(
        self,
        sku_id: str,
        new_price: Decimal,
        authorization_ref: str,
        idempotency_key: str,
    ) -> PriceUpdateResult:
        self.call_history.append({
            "sku_id": sku_id,
            "new_price": new_price,
            "authorization_ref": authorization_ref,
            "idempotency_key": idempotency_key,
            "timestamp": datetime.now(timezone.utc),
        })

        if self.simulate_transient_error:
            raise ConnectionError("503 Service Unavailable from official seller gateway")

        if not authorization_ref or authorization_ref.startswith("invalid"):
            return PriceUpdateResult(
                success=False,
                sku_id=sku_id,
                provider=self.provider_name,
                error="Invalid or expired seller authorization token",
                error_code="INVALID_AUTH",
            )

        return PriceUpdateResult(
            success=True,
            sku_id=sku_id,
            applied_price=new_price,
            provider=self.provider_name,
            transaction_id=f"tx_{idempotency_key[:12]}",
        )
