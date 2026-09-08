"""Native Cohere v2 Chat provider.

Cohere's OpenAI compatibility endpoint is useful for simple completions, but its
native v2 Chat API is the authoritative path for multi-step tool use.  The runtime
stores OpenAI-shaped history, so this adapter only translates tool-result content;
the remaining message and tool schemas are already compatible with Cohere v2.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from .base import AssistantTurn, ModelCapabilities, ProviderClient, TokenUsage, ToolCall
from .capabilities import capabilities_for


DEFAULT_BASE_URL = "https://api.cohere.com/v2"


def _cohere_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert canonical runtime history to Cohere v2 message objects."""
    converted: list[dict[str, Any]] = []
    for original in messages:
        if original.get("role") == "notice":
            continue
        message = {k: v for k, v in original.items() if not str(k).startswith("_")}
        if message.get("role") == "tool":
            content = message.get("content")
            if not isinstance(content, list):
                if not isinstance(content, str):
                    content = json.dumps(content, ensure_ascii=False)
                message["content"] = [
                    {"type": "document", "document": {"data": content}}
                ]
        converted.append(message)
    return converted


def _tool_calls(raw: Any) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for item in raw or []:
        function = item.get("function") or {}
        arguments = function.get("arguments") or "{}"
        try:
            parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
        except (TypeError, json.JSONDecodeError):
            parsed = {"_raw": arguments}
        if not isinstance(parsed, dict):
            parsed = {"_raw": parsed}
        calls.append(
            ToolCall(
                id=str(item.get("id") or ""),
                name=str(function.get("name") or ""),
                arguments=parsed,
            )
        )
    return calls


def _usage(data: dict[str, Any]) -> Optional[TokenUsage]:
    tokens = ((data.get("usage") or {}).get("tokens") or {})
    if not tokens:
        return None
    return TokenUsage(
        input=int(tokens.get("input_tokens") or 0),
        output=int(tokens.get("output_tokens") or 0),
    )


class CohereProvider(ProviderClient):
    """Cohere Client V2 semantics over the documented ``/v2/chat`` endpoint."""

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        client: Any = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client

    def _ensure_client(self) -> Any:
        if self._client is None:
            import httpx

            key = self._api_key or os.environ.get("COHERE_API_KEY", "").strip()
            if not key:
                raise RuntimeError(
                    "No Cohere API key configured — add it in Settings ▸ Models."
                )
            self._client = httpx.Client(
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "X-Client-Name": "connect-ai",
                },
                timeout=120.0,
            )
        return self._client

    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        **settings: Any,
    ) -> AssistantTurn:
        payload: dict[str, Any] = {
            "model": model.split(":", 1)[-1] if model.startswith("cohere:") else model,
            "messages": _cohere_messages(messages),
        }
        caps = self.capabilities(model)
        if tools and caps.tools:
            payload["tools"] = tools

        # Translate only settings supported by Cohere v2. Provider-specific or
        # OpenAI-only values (reasoning_effort, parallel_tool_calls, etc.) stay out.
        setting_map = {
            "max_tokens": "max_tokens",
            "max_completion_tokens": "max_tokens",
            "temperature": "temperature",
            "top_p": "p",
            "p": "p",
            "seed": "seed",
            "frequency_penalty": "frequency_penalty",
            "presence_penalty": "presence_penalty",
            "stop": "stop_sequences",
            "stop_sequences": "stop_sequences",
            "tool_choice": "tool_choice",
            "strict_tools": "strict_tools",
        }
        for source, target in setting_map.items():
            if source in settings and target not in payload:
                payload[target] = settings[source]

        response = self._ensure_client().post(self._base_url + "/chat", json=payload)
        if response.status_code >= 400:
            detail = response.text[:2000]
            raise RuntimeError(f"Cohere HTTP {response.status_code}: {detail}")
        data = response.json()
        message = data.get("message") or {}
        content = message.get("content") or []
        text_parts = [
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        reasoning_parts = [
            str(block.get("thinking") or block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "thinking"
        ]
        finish = str(data.get("finish_reason") or "").lower() or None
        if finish == "tool_call":
            finish = "tool_calls"
        return AssistantTurn(
            text="".join(text_parts) or None,
            reasoning="".join(reasoning_parts) or None,
            tool_calls=_tool_calls(message.get("tool_calls")),
            finish_reason=finish,
            raw=data,
            usage=_usage(data),
        )

    def capabilities(self, model: str) -> ModelCapabilities:
        qualified = model if model.startswith("cohere:") else f"cohere:{model}"
        return capabilities_for(qualified)
