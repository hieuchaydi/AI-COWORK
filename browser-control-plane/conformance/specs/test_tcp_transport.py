import asyncio
import sys
from pathlib import Path

import pytest

# Add browser-control-plane to sys.path to resolve imports
bcp_dir = Path(__file__).resolve().parent.parent.parent
if str(bcp_dir) not in sys.path:
    sys.path.insert(0, str(bcp_dir))

from agent.main import AgentServer
from agent.framing import RequestEnvelope, encode_json_frame, parse_frame

@pytest.mark.asyncio
async def test_tcp_transport_hello_and_shutdown(tmp_path: Path):
    token_file = tmp_path / "token.txt"
    token_file.write_text("test_token_123")

    server = AgentServer("test_profile", str(token_file))
    writer = None

    try:
        await server.start_listening(use_tcp=True, tcp_host="127.0.0.1", tcp_port=0)
        assert server.tcp_address is not None
        host, port = server.tcp_address

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=2.0
        )

        req = RequestEnvelope(
            id=1,
            method="agent.hello",
            params={
                "token": "test_token_123",
                "protocolVersion": "1.0"
            }
        )
        writer.write(encode_json_frame(req))
        await writer.drain()

        buffer = b""
        payload = None
        while True:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=2.0)
            if not chunk:
                break
            buffer += chunk
            payload, buffer = parse_frame(buffer)
            if payload is not None:
                break

        assert payload is not None
        assert payload["id"] == 1
        assert payload["ok"] is True
        assert payload["result"]["protocolVersion"] == "1.0"
        assert payload["result"]["backend"] == "cdp"
        assert "network.interception" in payload["result"]["capabilities"]

    finally:
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=2.0)
            except Exception:
                pass
        await server.close()

@pytest.mark.asyncio
async def test_tcp_rejects_non_loopback(tmp_path: Path):
    token_file = tmp_path / "token.txt"
    token_file.write_text("test_token_123")

    server = AgentServer("test_profile", str(token_file))

    with pytest.raises(ValueError, match="must bind to a loopback address"):
        await server.start_listening(use_tcp=True, tcp_host="0.0.0.0", tcp_port=0)

    assert server.server is None
    assert server.tcp_address is None

    await server.close()

@pytest.mark.asyncio
async def test_double_start_and_close_idempotent(tmp_path: Path):
    token_file = tmp_path / "token.txt"
    token_file.write_text("test_token_123")

    server = AgentServer("test_profile", str(token_file))

    try:
        await server.start_listening(use_tcp=True, tcp_host="127.0.0.1", tcp_port=0)

        with pytest.raises(RuntimeError, match="Server already started"):
            await server.start_listening(use_tcp=True, tcp_host="127.0.0.1", tcp_port=0)

        await server.close()
        assert server.server is None
        assert server.tcp_address is None

        # Should be idempotent
        await server.close()
    finally:
        await server.close()

@pytest.mark.asyncio
async def test_close_disconnects_active_client(tmp_path: Path):
    token_file = tmp_path / "token.txt"
    token_file.write_text("test_token_123")

    server = AgentServer("test_profile", str(token_file))
    writer = None

    try:
        await server.start_listening(use_tcp=True, tcp_host="127.0.0.1", tcp_port=0)
        assert server.tcp_address is not None
        host, port = server.tcp_address

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=2.0
        )

        req = RequestEnvelope(
            id=1,
            method="agent.hello",
            params={
                "token": "test_token_123",
                "protocolVersion": "1.0"
            }
        )
        writer.write(encode_json_frame(req))
        await writer.drain()

        # Parse response
        buffer = b""
        payload = None
        while True:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=2.0)
            if not chunk:
                break
            buffer += chunk
            payload, buffer = parse_frame(buffer)
            if payload is not None:
                break

        assert payload is not None
        assert payload["id"] == 1
        assert payload["ok"] is True

        # Ensure server tracks the client
        assert len(server._active_writers) == 1

        # Close server, expect it to disconnect active clients
        await asyncio.wait_for(server.close(), timeout=2.0)

        # Client reader should receive EOF
        chunk = await asyncio.wait_for(reader.read(4096), timeout=2.0)
        assert chunk == b""

        # Check properties reset
        assert server.server is None
        assert server.tcp_address is None
        assert len(server._active_writers) == 0

    finally:
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=2.0)
            except Exception:
                pass
        await server.close()
