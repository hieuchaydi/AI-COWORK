import asyncio
import sys
from pathlib import Path
from uuid import uuid4

import pytest

# Add browser-control-plane to sys.path to resolve imports
bcp_dir = Path(__file__).resolve().parent.parent.parent
if str(bcp_dir) not in sys.path:
    sys.path.insert(0, str(bcp_dir))

from agent.main import AgentServer
from client.transport.socket_transport import SocketTransport
from client.transport.itransport import BcpError


@pytest.mark.asyncio
async def test_pipe_transport_hello_and_ping(tmp_path: Path):
    if sys.platform != "win32":
        pytest.skip("Named pipes are only supported on Windows")

    token_file = tmp_path / "token.txt"
    token_file.write_text("pipe_secret_token_123")

    profile_id = f"test_{uuid4().hex[:8]}"
    server = AgentServer(profile_id, str(token_file))
    transport = SocketTransport()

    try:
        await server.start_listening(use_tcp=False)
        assert server.pipe_name is not None
        assert f"bcp-{profile_id}" in server.pipe_name

        # Connect client to the named pipe
        await transport.connect_pipe(server.pipe_name)

        # Perform handshake
        hello_res = await transport.send_hello("pipe_secret_token_123")
        assert hello_res.get("protocolVersion") == "1.0"
        assert hello_res.get("backend") == "cdp"
        assert "network.interception" in hello_res.get("capabilities", [])

        # Send ping
        ping_res = await transport.send_request("agent.ping")
        assert ping_res == {"pong": True}

        # Send stats
        stats_res = await transport.send_request("agent.stats")
        assert "requests_total" in stats_res
        assert stats_res["requests_total"] >= 1

    finally:
        await transport.close()
        await server.close()


@pytest.mark.asyncio
async def test_pipe_transport_invalid_token_rejects(tmp_path: Path):
    if sys.platform != "win32":
        pytest.skip("Named pipes are only supported on Windows")

    token_file = tmp_path / "token.txt"
    token_file.write_text("pipe_secret_token_123")

    profile_id = f"test_{uuid4().hex[:8]}"
    server = AgentServer(profile_id, str(token_file))
    transport = SocketTransport()

    try:
        await server.start_listening(use_tcp=False)
        assert server.pipe_name is not None

        await transport.connect_pipe(server.pipe_name)

        with pytest.raises(Exception) as exc_info:
            await transport.send_hello("wrong_token")

        assert getattr(exc_info.value, "kind", None) == "UNAUTHENTICATED" or getattr(exc_info.value, "code", None) == 4001 or "token" in str(exc_info.value).lower()

    finally:
        await transport.close()
        await server.close()


@pytest.mark.asyncio
async def test_pipe_transport_itransport_interface(tmp_path: Path):
    if sys.platform != "win32":
        pytest.skip("Named pipes are only supported on Windows")

    token_file = tmp_path / "token.txt"
    token_file.write_text("pipe_secret_token_123")

    profile_id = f"test_{uuid4().hex[:8]}"
    server = AgentServer(profile_id, str(token_file))
    transport = SocketTransport()

    try:
        await server.start_listening(use_tcp=False)
        assert server.pipe_name is not None

        # Test higher-level connect() method
        res = await transport.connect(server.pipe_name, token="pipe_secret_token_123")
        assert res.get("protocolVersion") == "1.0"
        assert "network.interception" in transport.capabilities

        # Test call() method
        call_res = await transport.call("agent.ping")
        assert call_res == {"pong": True}

    finally:
        await transport.close()
        await server.close()
