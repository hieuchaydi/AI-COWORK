"""
Browser Control Plane - Transport Interface
Defines the IBrowserTransport ABC and BcpError.
"""

from abc import ABC, abstractmethod
from typing import Any, Callable

class BcpError(Exception):
    """Base exception for BCP errors."""
    def __init__(self, message: str, kind: str = "UnknownError", code: int = 500, retryable: bool = False, data: Any = None):
        super().__init__(message)
        self.kind = kind
        self.code = code
        self.retryable = retryable
        self.data = data

class IBrowserTransport(ABC):
    """Abstract Base Class for Browser Transport."""

    @abstractmethod
    async def connect(self, endpoint: str = "", token: str = "") -> dict:
        """Connect to the browser endpoint."""
        pass

    @abstractmethod
    async def call(self, method: str, params: dict, timeout_ms: int = 30000) -> dict:
        """Call a BCP method."""
        pass

    @abstractmethod
    def on(self, event: str, handler: Callable[[dict], Any]) -> Callable[[], None]:
        """Subscribe to an event."""
        pass

    @property
    @abstractmethod
    def capabilities(self) -> set[str]:
        """Get capabilities supported by this transport."""
        pass

    @abstractmethod
    async def close(self):
        """Close the connection."""
        pass
