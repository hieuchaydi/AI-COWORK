'''deepseek_client.py

"""Utility module for accessing DeepSeek V4.1-Flash model via OpenAI‑compatible SDK.

Cài đặt yêu cầu:
    pip install openai

Cấu hình:
    - Đặt biến môi trường ``DEEPSEEK_API_KEY`` trong file ``.env`` hoặc hệ thống.
    - API endpoint: ``https://api.deepseek.com`` (OpenAI compatible).

Sử dụng:
    >>> from deepseek_client import get_deepseek_client, ask_deepseek
    >>> client = get_deepseek_client()
    >>> response = ask_deepseek(client, "Hello!")
    >>> print(response)
"""

import os
from typing import List, Dict, Any

from openai import OpenAI

def get_deepseek_client() -> OpenAI:
    """Create and return an ``OpenAI`` client configured for DeepSeek Flash.

    Reads ``DEEPSEEK_API_KEY`` from environment; raises ``RuntimeError`` if missing.
    """
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError(
            "DeepSeek API key not found. Set DEEPSEEK_API_KEY in the environment."
        )
    return OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

def ask_deepseek(
    client: OpenAI,
    user_message: str,
    system_prompt: str = "You are a helpful assistant.",
    model: str = "deepseek-flash",
    thinking: bool = True,
    reasoning_effort: str = "high",
) -> str:
    """Send a chat message to DeepSeek Flash and return the assistant reply.

    Parameters
    ----------
    client: OpenAI
        Client from :func:`get_deepseek_client`.
    user_message: str
        Message from the user.
    system_prompt: str, optional
        System instruction for the model.
    model: str, optional
        Model identifier – default ``deepseek-flash``.
    thinking: bool, optional
        Enable the *thinking* mode.
    reasoning_effort: str, optional
        One of ``"none"``, ``"low"``, ``"high"``, ``"max"``. Default ``"high"``.

    Returns
    -------
    str
        Assistant's response content.
    """
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        stream=False,
        extra_body={
            "thinking": {"type": "enabled" if thinking else "disabled"},
            "reasoning_effort": reasoning_effort,
        },
    )
    return response.choices[0].message.content

def ask_deepseek_vision(
    client: OpenAI,
    messages: List[Dict[str, Any]],
    model: str = "deepseek-flash",
    thinking: bool = True,
    reasoning_effort: str = "high",
) -> str:
    """Chat with DeepSeek using a list of messages that may include images.
    The ``messages`` argument follows the OpenAI multimodal format.
    """
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        stream=False,
        extra_body={
            "thinking": {"type": "enabled" if thinking else "disabled"},
            "reasoning_effort": reasoning_effort,
        },
    )
    return response.choices[0].message.content

if __name__ == "__main__":
    try:
        client = get_deepseek_client()
        print(ask_deepseek(client, "Xin chào!"))
    except Exception as e:
        print(f"Error: {e}")
'''

