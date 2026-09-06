"""Acknowledged, chunked ingestion over the extension's WebSocket connection."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor


class IngestRPC:
    """Deduplicate retries and keep file/media work off the socket receive loop."""

    def __init__(self, send, store, progress):
        self.send = send
        self.store = store
        self.progress = progress
        self.lock = threading.RLock()
        self.requests = {}
        self.uploads = {}
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ingest-ws")

    def submit(self, message):
        request_id = message.get("id")
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            return
        with self.lock:
            now = time.monotonic()
            self.requests = {key: value for key, value in self.requests.items()
                             if not value[1].done() or now - value[0] < 600}
            previous = self.requests.get(request_id)
            if previous:
                future = previous[1]
            else:
                if len(self.requests) >= 512:
                    oldest_done = next((key for key, value in self.requests.items() if value[1].done()), None)
                    if oldest_done is None:
                        self.send({"type": "ingest.reply", "id": request_id,
                                   "ok": False, "error": "Ingest queue is full"})
                        return
                    del self.requests[oldest_done]
                future = self.executor.submit(self._execute, message.get("params", {}))
                self.requests[request_id] = (now, future)
        future.add_done_callback(lambda done: self._reply(request_id, done))

    def _reply(self, request_id, future):
        try:
            result = future.result()
            reply = {"ok": True, "result": result}
        except Exception as exc:
            reply = {"ok": False, "error": str(exc)}
        self.send({"v": 1, "type": "ingest.reply", "id": request_id, **reply})

    def _execute(self, params):
        operation = params.get("operation")
        if operation == "progress":
            job = params.get("job")
            patch = params.get("progress")
            if not isinstance(job, str) or not job or not isinstance(patch, dict):
                raise ValueError("job and progress are required")
            return self.progress(job, patch)
        if operation == "chunk":
            upload_id, index, chunk = params.get("uploadId"), params.get("index"), params.get("chunk")
            if (not isinstance(upload_id, str) or not upload_id or len(upload_id) > 128
                    or type(index) is not int or index < 0
                    or not isinstance(chunk, str) or len(chunk) > 262144):
                raise ValueError("Invalid upload chunk")
            with self.lock:
                now = time.monotonic()
                self.uploads = {key: value for key, value in self.uploads.items() if now - value[0] < 600}
                if upload_id not in self.uploads:
                    if len(self.uploads) >= 8:
                        raise ValueError("Too many pending uploads")
                    self.uploads[upload_id] = [now, [], 0]
                upload = self.uploads[upload_id]
                chunks = upload[1]
                if index < len(chunks) and chunks[index] == chunk:
                    return {"index": index}
                if index != len(chunks):
                    raise ValueError("Upload chunks must arrive in order")
                size = len(chunk.encode("utf-8"))
                if upload[2] + size > 64 * 1024 * 1024:
                    del self.uploads[upload_id]
                    raise ValueError("Upload exceeds 64 MiB")
                chunks.append(chunk)
                upload[0], upload[2] = now, upload[2] + size
                return {"index": index}
        if operation == "complete":
            with self.lock:
                upload = self.uploads.pop(params.get("uploadId"), None)
            if not upload:
                raise ValueError("Upload missing or expired")
            body = json.loads("".join(upload[1]))
            if not isinstance(body, dict):
                raise ValueError("Expected result object")
            status, result = self.store(body)
            if status >= 400:
                raise ValueError(result.get("error", "Unable to save result"))
            return result
        raise ValueError("Unknown ingest operation")
