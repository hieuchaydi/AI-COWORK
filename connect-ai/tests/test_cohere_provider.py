from __future__ import annotations

import json

from coworker.providers.cohere_provider import CohereProvider, _cohere_messages


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    }
]


class _Response:
    status_code = 200
    text = ""

    def json(self):
        return {
            "finish_reason": "TOOL_CALL",
            "message": {
                "content": [{"type": "thinking", "thinking": "Need the file"}],
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "README.md"}),
                        },
                    }
                ],
            },
            "usage": {"tokens": {"input_tokens": 12, "output_tokens": 5}},
        }


class _Client:
    def __init__(self):
        self.url = None
        self.payload = None

    def post(self, url, *, json):
        self.url = url
        self.payload = json
        return _Response()


def test_native_v2_chat_routes_tools_and_parses_tool_call():
    client = _Client()
    provider = CohereProvider(api_key="test", client=client)
    turn = provider.complete(
        model="command-a-03-2025",
        messages=[{"role": "user", "content": "Open README"}],
        tools=TOOLS,
        reasoning_effort="high",
    )

    assert client.url == "https://api.cohere.com/v2/chat"
    assert client.payload["model"] == "command-a-03-2025"
    assert client.payload["tools"] == TOOLS
    assert "reasoning_effort" not in client.payload
    assert turn.finish_reason == "tool_calls"
    assert turn.tool_calls[0].name == "read_file"
    assert turn.tool_calls[0].arguments == {"path": "README.md"}
    assert turn.reasoning == "Need the file"
    assert turn.usage.input == 12 and turn.usage.output == 5


def test_tool_results_are_documents_for_cohere_v2():
    messages = _cohere_messages(
        [
            {"role": "assistant", "content": None, "_gemini": {"signature": "x"}},
            {"role": "tool", "tool_call_id": "call-1", "content": '{"ok":true}'},
        ]
    )
    assert "_gemini" not in messages[0]
    assert messages[1]["content"] == [
        {"type": "document", "document": {"data": '{"ok":true}'}}
    ]
