"""Policy-enforcing registry for commerce data providers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import urlparse

from crawler.models import AccessClass

from ..interfaces import AccessDeniedException, CommerceConnector

ConnectorFactory = Callable[[], CommerceConnector]


@dataclass(frozen=True)
class ConnectorRegistration:
    """A connector whose data access basis has been declared explicitly."""

    platform: str
    domains: tuple[str, ...]
    access_class: AccessClass
    factory: ConnectorFactory
    authorization_ref: str | None = None


class CommerceConnectorRegistry:
    """Resolve URLs only through registered official APIs or publisher feeds.

    Storefront scraping, browser-cookie reuse, and undocumented XHR endpoints are
    deliberately not registration modes. API registrations must point to the
    agreement or shop authorization that permits access.
    """

    _ALLOWED_ACCESS_CLASSES = {AccessClass.API, AccessClass.FEED}

    def __init__(self, registrations: Iterable[ConnectorRegistration] = ()) -> None:
        self._registrations: list[ConnectorRegistration] = []
        for registration in registrations:
            self.register(registration)

    def register(self, registration: ConnectorRegistration) -> None:
        if registration.access_class not in self._ALLOWED_ACCESS_CLASSES:
            raise ValueError(
                "Commerce monitoring accepts only official API or publisher-feed connectors."
            )
        if registration.access_class is AccessClass.API and not registration.authorization_ref:
            raise ValueError("API connectors require a documented authorization_ref.")
        if not registration.domains:
            raise ValueError("Connector registration requires at least one domain.")

        normalized_domains = tuple(self._normalize_domain(domain) for domain in registration.domains)
        normalized = ConnectorRegistration(
            platform=registration.platform.strip().lower(),
            domains=normalized_domains,
            access_class=registration.access_class,
            factory=registration.factory,
            authorization_ref=registration.authorization_ref,
        )
        if not normalized.platform:
            raise ValueError("Connector platform cannot be empty.")

        for existing in self._registrations:
            if set(existing.domains) & set(normalized.domains):
                raise ValueError("A connector is already registered for one of these domains.")
        self._registrations.append(normalized)

    def resolve(self, url: str) -> CommerceConnector:
        parsed = urlparse(url)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise ValueError("Commerce product URLs must use HTTPS and include a hostname.")

        hostname = parsed.hostname.lower().rstrip(".")
        for registration in self._registrations:
            if any(self._domain_matches(hostname, domain) for domain in registration.domains):
                connector = registration.factory()
                if connector.platform_name != registration.platform:
                    raise RuntimeError(
                        "Connector factory platform does not match its registry declaration."
                    )
                return connector

        raise AccessDeniedException(
            "No authorized API/feed connector is registered for "
            f"'{hostname}'. Storefront scraping and browser-session bypass are disabled."
        )

    @staticmethod
    def _normalize_domain(domain: str) -> str:
        normalized = domain.strip().lower().rstrip(".")
        if not normalized or "://" in normalized or "/" in normalized:
            raise ValueError(f"Invalid connector domain: {domain!r}")
        return normalized

    @staticmethod
    def _domain_matches(hostname: str, domain: str) -> bool:
        return hostname == domain or hostname.endswith(f".{domain}")


_default_registry = CommerceConnectorRegistry()


def register_connector(registration: ConnectorRegistration) -> None:
    """Register an authorized connector during application startup."""

    _default_registry.register(registration)


def get_connector_for_url(url: str) -> CommerceConnector:
    """Resolve a product URL using the process-wide authorized registry."""

    return _default_registry.resolve(url)
