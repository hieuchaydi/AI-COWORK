"""
Connector Registry and Factory for Commerce Connectors.
"""

from urllib.parse import urlparse

from ..interfaces import CommerceConnector
from .queue_bridge import ExtensionQueueCommerceConnector
from .tiki import TikiCommerceConnector


def get_connector_for_url(url: str) -> CommerceConnector:
    """
    Returns the appropriate CommerceConnector implementation based on target URL domain.
    """
    domain = urlparse(url).netloc.lower()

    if "tiki.vn" in domain:
        return TikiCommerceConnector()
    elif "shopee.vn" in domain or "shopee." in domain:
        return ExtensionQueueCommerceConnector(platform="shopee")
    elif "lazada.vn" in domain or "lazada." in domain:
        return ExtensionQueueCommerceConnector(platform="lazada")
    elif "tiktok.com" in domain or "shop.tiktok" in domain:
        return ExtensionQueueCommerceConnector(platform="tiktok_shop")
    else:
        # Default to Tiki for testing or generic public connector
        if "tiki" in url:
            return TikiCommerceConnector()
        raise ValueError(f"No compliant connector registered for domain: {domain}")
