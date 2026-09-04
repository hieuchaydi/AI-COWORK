"""Contract tests for policy-enforcing commerce connector registration."""

from typing import Optional

import pytest

from crawler.commerce.connectors.registry import (
    CommerceConnectorRegistry,
    ConnectorRegistration,
)
from crawler.commerce.interfaces import (
    AccessDeniedException,
    CommerceConnector,
    ProductIdentity,
    ProductSnapshot,
    ReviewPage,
)
from crawler.models import AccessClass


class AuthorizedApiConnector(CommerceConnector):
    platform_name = "authorized_shop"

    async def resolve_product(self, url: str) -> ProductIdentity:
        return ProductIdentity(
            platform=self.platform_name,
            product_id="42",
            canonical_url="https://api.vendor.example/products/42",
        )

    async def fetch_product(self, identity: ProductIdentity) -> ProductSnapshot:
        return ProductSnapshot(
            product_id=identity.product_id,
            platform=identity.platform,
            title="Product",
            current_price=100.0,
            url=identity.canonical_url,
        )

    async def fetch_reviews(
        self,
        identity: ProductIdentity,
        cursor: Optional[str] = None,
    ) -> ReviewPage:
        return ReviewPage(product_id=identity.product_id)


def api_registration(**overrides) -> ConnectorRegistration:
    values = {
        "platform": "authorized_shop",
        "domains": ("vendor.example",),
        "access_class": AccessClass.API,
        "authorization_ref": "shop-oauth:42",
        "factory": AuthorizedApiConnector,
    }
    values.update(overrides)
    return ConnectorRegistration(**values)


def test_registry_resolves_exact_or_subdomain_for_authorized_api():
    registry = CommerceConnectorRegistry([api_registration()])

    assert registry.resolve("https://vendor.example/products/42").platform_name == "authorized_shop"
    assert registry.resolve("https://api.vendor.example/products/42").platform_name == "authorized_shop"


def test_registry_rejects_lookalike_domain_and_insecure_url():
    registry = CommerceConnectorRegistry([api_registration()])

    with pytest.raises(AccessDeniedException):
        registry.resolve("https://vendor.example.attacker.test/products/42")
    with pytest.raises(ValueError, match="HTTPS"):
        registry.resolve("http://vendor.example/products/42")


def test_registry_requires_api_authorization_reference():
    registry = CommerceConnectorRegistry()

    with pytest.raises(ValueError, match="authorization_ref"):
        registry.register(api_registration(authorization_ref=None))


@pytest.mark.parametrize("access_class", [AccessClass.PUBLIC, AccessClass.RESTRICTED])
def test_registry_rejects_storefront_access_modes(access_class: AccessClass):
    registry = CommerceConnectorRegistry()

    with pytest.raises(ValueError, match="official API or publisher-feed"):
        registry.register(api_registration(access_class=access_class))


def test_registry_has_no_implicit_marketplace_fallback():
    registry = CommerceConnectorRegistry()

    with pytest.raises(AccessDeniedException, match="Storefront scraping"):
        registry.resolve("https://shopee.vn/product/1/2")
