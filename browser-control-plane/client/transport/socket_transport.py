"""
Client SocketTransport implementation.
"""
import asyncio
import sys
from typing import Any, Callable, Dict, Optional
import struct
import json
import logging

logger = logging.getLogger(__name__)

class SocketTransport:
    def __init__(self):
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.next_id = 1
        self.pending_requests: Dict[int, asyncio.Future] = {}
        self.listeners: Dict[str, Callable] = {}
        
    async def connect_pipe(self, pipe_name: str):
        if sys.platform == "win32":
            self.reader, self.writer = await asyncio.open_connection(pipe=pipe_name)
        else:
            raise NotImplementedError("Named pipes only supported on Windows in this implementation")
            
    async def connect_tcp(self, host: str, port: int):
        self.reader, self.writer = await asyncio.open_connection(host, port)
        
    async def send_hello(self, token: str):
        return await self.send_request("agent.hello", {"token": token})
        
    async def send_request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if params is None:
            params = {}
            
        req_id = self.next_id
        self.next_id += 1
        
        payload = {
            "id": req_id,
            "method": method,
            "params": params
        }
        
        json_bytes = json.dumps(payload).encode("utf-8")
        length = len(json_bytes) + 1
        frame = struct.pack(">I", length) + b"\x00" + json_bytes
        
        future = asyncio.get_event_loop().create_future()
        self.pending_requests[req_id] = future
        
        self.writer.write(frame)
        await self.writer.drain()
        
        return await future
        
    async def receive_loop(self):
        buffer = b""
        while True:
            data = await self.reader.read(4096)
            if not data:
                break
            buffer += data
            
            while True:
                if len(buffer) < 4:
                    break
                length = struct.unpack(">I", buffer[:4])[0]
                if len(buffer) < 4 + length:
                    break
                    
                kind = buffer[4]
                payload_bytes = buffer[5:4+length]
                buffer = buffer[4+length:]
                
                if kind == 0:
                    msg = json.loads(payload_bytes.decode("utf-8"))
                    if "id" in msg and msg["id"] in self.pending_requests:
                        future = self.pending_requests.pop(msg["id"])
                        if msg.get("ok"):
                            future.set_result(msg.get("result"))
                        else:
                            future.set_exception(Exception(msg.get("error")))
                    elif "event" in msg:
                        event = msg["event"]
                        if event in self.listeners:
                            self.listeners[event](msg.get("params"))
                elif kind == 1:
                    logger.info("Received binary frame")
                    
    def on(self, event: str, listener: Callable):
        self.listeners[event] = listener
