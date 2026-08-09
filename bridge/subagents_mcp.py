"""MCP stdio server: spawn sub-agents to run parallel tasks.

Each `spawn_agent(prompt, persona)` opens a fresh OpenWorker session, POSTs the
prompt, and returns a task_id. The caller can `await_agent(task_id)` to block for
the result, `list_agents()` to see everything running, or `stop_agent(task_id)`
to cancel.

Uses the OpenWorker REST API on 127.0.0.1:8765 — the same sidecar this bridge is
loaded by. Token comes from COWORKER_API_TOKEN env (set by launch.py) with a
default fallback.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from typing import Any

import httpx

from mcp.server.fastmcp import FastMCP

# connect-AI naming; API_BASE still honoured for older launcher seeds.
API_BASE = os.environ.get("CONNECT_AI_BASE") or os.environ.get(
    "API_BASE", "http://127.0.0.1:8765"
)
OW_TOKEN = os.environ.get("CONNECT_AI_API_TOKEN") or os.environ.get("COWORKER_API_TOKEN", "connect-ai-dev-token")
HEADERS = {"x-connect-ai-token": OW_TOKEN, "Content-Type": "application/json"}

# In-memory task registry: task_id → {session_id, prompt, persona, started_at, status}
_TASKS: dict[str, dict[str, Any]] = {}


def _ow(method: str, path: str, **body) -> dict:
    """Wrapper for OpenWorker REST calls."""
    url = f"{API_BASE}{path}"
    try:
        if method == "GET":
            r = httpx.get(url, headers=HEADERS, timeout=30)
        elif method == "POST":
            r = httpx.post(url, headers=HEADERS, json=body if body else None, timeout=30)
        elif method == "DELETE":
            r = httpx.delete(url, headers=HEADERS, timeout=30)
        elif method == "PATCH":
            r = httpx.patch(url, headers=HEADERS, json=body if body else None, timeout=30)
        else:
            return {"error": f"unsupported method {method}"}
        return r.json() if r.content else {"ok": True}
    except Exception as exc:
        return {"error": str(exc)}


mcp = FastMCP("subagents")


@mcp.tool()
def spawn_agent(prompt: str, persona: str = "cowork") -> dict:
    """Spawn a NEW OpenWorker session with `persona` and post `prompt` as the first
    user message. Returns a `task_id` you can pass to `await_agent` or `stop_agent`.
    Session runs asynchronously in the sidecar; this returns immediately.

    `persona`: "cowork" (default, general knowledge work), "code" (coding-focused),
    "chat" (light chat), "ops" (system ops)."""
    session_id = f"sub-{uuid.uuid4().hex[:10]}"
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    # Post the first message; the sidecar auto-creates the session on first hit.
    resp = _ow("POST", f"/v1/sessions/{session_id}/messages", content=prompt, agent=persona)
    if "error" in resp:
        return {"error": f"spawn failed: {resp['error']}"}
    _TASKS[task_id] = {
        "session_id": session_id,
        "prompt": prompt[:100],
        "persona": persona,
        "started_at": time.time(),
        "status": "running",
    }
    return {"task_id": task_id, "session_id": session_id, "status": "running"}


@mcp.tool()
def list_agents() -> list[dict]:
    """List every spawned agent this bridge is tracking, with status + age."""
    now = time.time()
    out = []
    for tid, info in _TASKS.items():
        out.append(
            {
                "task_id": tid,
                "session_id": info["session_id"],
                "persona": info["persona"],
                "status": info["status"],
                "age_sec": int(now - info["started_at"]),
                "prompt_preview": info["prompt"],
            }
        )
    return out


@mcp.tool()
def await_agent(task_id: str, timeout_sec: int = 60) -> dict:
    """Block until the sub-agent finishes its current turn (or `timeout_sec` elapses).
    Returns the last assistant message text + tool_calls summary. Poll interval 1s."""
    if task_id not in _TASKS:
        return {"error": f"unknown task_id {task_id}"}
    sid = _TASKS[task_id]["session_id"]
    deadline = time.time() + timeout_sec
    last_assistant = None
    while time.time() < deadline:
        # Check if the session is currently mid-turn (busy) via unattended endpoint.
        # If not busy AND has an assistant tail message → done.
        msgs_resp = _ow("GET", f"/v1/sessions/{sid}/messages")
        msgs = msgs_resp.get("messages", [])
        if msgs:
            last = msgs[-1]
            if last.get("role") == "assistant" and last.get("content"):
                last_assistant = last
                break
        time.sleep(1)
    if last_assistant is None:
        return {"task_id": task_id, "status": "timeout", "timeout_sec": timeout_sec}
    _TASKS[task_id]["status"] = "done"
    return {
        "task_id": task_id,
        "status": "done",
        "result": last_assistant.get("content", ""),
        "tool_calls": len(last_assistant.get("tool_calls") or []),
        "total_messages": len(msgs),
    }


@mcp.tool()
def stop_agent(task_id: str) -> dict:
    """Cancel the sub-agent's session (deletes it). No effect if session already ended."""
    if task_id not in _TASKS:
        return {"error": f"unknown task_id {task_id}"}
    sid = _TASKS[task_id]["session_id"]
    resp = _ow("DELETE", f"/v1/sessions/{sid}")
    _TASKS[task_id]["status"] = "stopped"
    return {"task_id": task_id, "status": "stopped", "delete_resp": resp}


@mcp.tool()
def spawn_parallel(prompts: list[str], persona: str = "cowork") -> dict:
    """Fire N sub-agents at once, each with a different prompt. Returns list of
    task_ids. Use with `await_agent` to gather results in a fan-in pattern."""
    tasks = []
    for p in prompts:
        r = spawn_agent(p, persona)
        tasks.append(r)
    return {"spawned": len(tasks), "tasks": tasks}


if __name__ == "__main__":
    print(f"[subagents_mcp] CONNECT_AI_BASE={API_BASE}", file=sys.stderr)
    mcp.run()
