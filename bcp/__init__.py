"""
BCP (Browser Control Plane) top-level package.
"""
import sys
from pathlib import Path

# Add browser-control-plane to sys.path so internal relative imports resolve
_bcp_root = Path(__file__).resolve().parent.parent / "browser-control-plane"
if str(_bcp_root) not in sys.path:
    sys.path.insert(0, str(_bcp_root))

from client.api.browser import Browser
from client.api.page import Page
from client.api.frame import Frame
from client.api.locator import Locator
from client.api.network import Network
from client.api.storage import Storage
from client.transport.itransport import IBrowserTransport, BcpError
from client.transport.cdp_transport import CdpTransport

__all__ = [
    "Browser",
    "Page",
    "Frame",
    "Locator",
    "Network",
    "Storage",
    "IBrowserTransport",
    "BcpError",
    "CdpTransport",
]
