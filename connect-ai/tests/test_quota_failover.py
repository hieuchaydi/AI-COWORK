"""Quota recovery (connect-AI patch): a model hitting its limit must not end the turn.

Two ladders, in order:
  1. fail over to the next configured model in the same provider (instant), then
  2. park the turn and retry when that provider is walled (free tiers reset per minute).
Cross-provider switches require an explicit manual override.
Stop cancels a parked turn; an unrecoverable error still ends it the old way.
"""

from __future__ import annotations

import asyncio

import aisuite as ai
import pytest
from coworker.engine import TurnEngine
from coworker.events import EventType
from coworker.permissions import PermissionEngine
from coworker.providers import (
    AssistantTurn,
    ModelCapabilities,
    ProviderClient,
)
from coworker.tools import ToolRegistry

QUOTA_EXC = RuntimeError(
    "429 RESOURCE_EXHAUSTED: You exceeded your current quota, please check your plan"
)


class FlakyProvider(ProviderClient):
    """Raises per-model errors until a model is marked healthy."""

    def __init__(self, failures: dict[str, Exception], text: str = "done"):
        self.failures = dict(failures)
        self.text = text
        self.models_called: list[str] = []

    def complete(self, *, model, messages, tools=None, **settings):
        self.models_called.append(model)
        exc = self.failures.get(model)
        if exc is not None:
            raise exc
        return AssistantTurn(text=self.text, finish_reason="stop")

    def capabilities(self, model):
        return ModelCapabilities()


def _engine(tmp_path, provider, *, model="gemini:gemini-2.5-flash", fallbacks=None):
    registry = ToolRegistry()
    registry.register_all(ai.toolkits.files(root=str(tmp_path), allow_write=True))
    return TurnEngine(
        provider=provider,
        registry=registry,
        permissions=PermissionEngine(workspace_root=tmp_path),
        model=model,
        fallback_models=(lambda: list(fallbacks)) if fallbacks is not None else None,
    )


def _collect(engine, text="hi"):
    async def _run():
        return [ev async for ev in engine.run(text)]

    return asyncio.run(_run())


def _types(events):
    return [ev.type for ev in events]


def test_quota_fails_over_to_next_model(tmp_path):
    provider = FlakyProvider({"gemini:gemini-3.7-flash": QUOTA_EXC})
    engine = _engine(
        tmp_path,
        provider,
        model="gemini:gemini-3.7-flash",
        fallbacks=["gemini:gemini-3.7-flash", "gemini:gemini-3.5-flash-lite"],
    )
    events = _collect(engine)

    assert EventType.ERROR not in _types(events)
    assert EventType.MODEL_FAILOVER in _types(events)
    failover = next(e for e in events if e.type == EventType.MODEL_FAILOVER)
    assert failover.data["from"] == "gemini:gemini-3.7-flash"
    assert failover.data["to"] == "gemini:gemini-3.5-flash-lite"
    # The turn finished on the healthy model, and the answer is the real one.
    assert engine.model == "gemini:gemini-3.5-flash-lite"
    assert provider.models_called == [
        "gemini:gemini-3.7-flash",
        "gemini:gemini-3.5-flash-lite",
    ]
    assert events[-1].data["status"] == "completed"


def test_quota_uses_same_provider_even_when_cross_provider_is_listed_first(tmp_path):
    provider = FlakyProvider({"gemini:first": QUOTA_EXC})
    engine = _engine(
        tmp_path,
        provider,
        model="gemini:first",
        fallbacks=["gemini:first", "groq:fast", "gemini:second"],
    )

    events = _collect(engine)

    assert EventType.ERROR not in _types(events)
    assert provider.models_called == ["gemini:first", "gemini:second"]
    assert engine.model == "gemini:second"


def test_quota_never_crosses_provider_without_manual_override(tmp_path, monkeypatch):
    monkeypatch.setenv("COWORKER_QUOTA_WAIT_SECONDS", "0")
    provider = FlakyProvider({"gemini:first": QUOTA_EXC})
    engine = _engine(
        tmp_path,
        provider,
        model="gemini:first",
        fallbacks=["gemini:first", "groq:fast"],
    )

    events = _collect(engine)

    assert EventType.MODEL_FAILOVER not in _types(events)
    assert events[-1].type == EventType.ERROR
    assert provider.models_called == ["gemini:first"]


def test_bare_openai_models_share_the_openai_provider(tmp_path):
    provider = FlakyProvider({"gpt-5.6-sol": QUOTA_EXC})
    engine = _engine(
        tmp_path,
        provider,
        model="gpt-5.6-sol",
        fallbacks=["gpt-5.6-sol", "gemini:fast", "gpt-5.6-terra"],
    )

    events = _collect(engine)

    assert EventType.ERROR not in _types(events)
    assert provider.models_called == ["gpt-5.6-sol", "gpt-5.6-terra"]


def test_manual_override_is_applied_at_next_request_boundary(tmp_path):
    provider = FlakyProvider({})
    engine = _engine(
        tmp_path,
        provider,
        model="gemini:first",
        fallbacks=["gemini:first", "gemini:second"],
    )
    assert engine.request_model_switch("groq:user-choice") is True

    events = _collect(engine)

    assert EventType.MODEL_CHANGED in _types(events)
    assert provider.models_called == ["groq:user-choice"]
    assert engine.model == "groq:user-choice"


def test_manual_override_wakes_a_quota_parked_turn(tmp_path, monkeypatch):
    monkeypatch.setattr("coworker.engine._QUOTA_BACKOFF_SEC", (30,))
    provider = FlakyProvider({"gemini:first": QUOTA_EXC})
    engine = _engine(
        tmp_path,
        provider,
        model="gemini:first",
        fallbacks=["gemini:first"],
    )

    async def _run():
        events = []
        async for event in engine.run("hi"):
            events.append(event)
            if event.type == EventType.MODEL_WAITING:
                assert engine.request_model_switch("groq:user-choice") is True
        return events

    events = asyncio.run(asyncio.wait_for(_run(), timeout=2))

    assert EventType.ERROR not in _types(events)
    assert EventType.MODEL_CHANGED in _types(events)
    assert provider.models_called == ["gemini:first", "groq:user-choice"]


def test_failover_walks_the_whole_list(tmp_path):
    provider = FlakyProvider({"a:one": QUOTA_EXC, "a:two": QUOTA_EXC})
    engine = _engine(
        tmp_path, provider, model="a:one", fallbacks=["a:one", "a:two", "a:three"]
    )
    events = _collect(engine)

    assert _types(events).count(EventType.MODEL_FAILOVER) == 2
    assert provider.models_called == ["a:one", "a:two", "a:three"]
    assert engine.model == "a:three"


def test_failover_does_not_consume_the_iteration_budget(tmp_path):
    provider = FlakyProvider({"a:one": QUOTA_EXC})
    engine = _engine(tmp_path, provider, model="a:one", fallbacks=["a:one", "a:two"])
    engine.max_iterations = 1  # one real model call is all the budget allows
    events = _collect(engine)

    assert EventType.ERROR not in _types(events)
    assert events[-1].data["status"] == "completed"


def test_waits_and_retries_when_every_model_is_walled(tmp_path, monkeypatch):
    # One model, quota-blocked on the first call and healthy after: the turn parks
    # instead of failing, then completes on its own.
    monkeypatch.setattr("coworker.engine._QUOTA_BACKOFF_SEC", (0.01,))
    provider = FlakyProvider({"a:one": QUOTA_EXC})

    original = provider.complete

    def heal_after_first(**kwargs):
        try:
            return original(**kwargs)
        finally:
            provider.failures.pop("a:one", None)  # limit lifts during the wait

    provider.complete = heal_after_first  # type: ignore[method-assign]

    engine = _engine(tmp_path, provider, model="a:one", fallbacks=["a:one"])
    events = _collect(engine)

    assert EventType.MODEL_WAITING in _types(events)
    assert EventType.ERROR not in _types(events)
    assert events[-1].data["status"] == "completed"
    waiting = next(e for e in events if e.type == EventType.MODEL_WAITING)
    assert waiting.data["model"] == "a:one"
    assert waiting.data["retry_in"] > 0


def test_wait_budget_bounds_the_hold(tmp_path, monkeypatch):
    monkeypatch.setattr("coworker.engine._QUOTA_BACKOFF_SEC", (0.01,))
    monkeypatch.setenv("COWORKER_QUOTA_WAIT_SECONDS", "0.02")
    provider = FlakyProvider({"a:one": QUOTA_EXC})  # never recovers
    engine = _engine(tmp_path, provider, model="a:one", fallbacks=["a:one"])
    events = _collect(engine)

    # It waited, retried, ran out of budget, and ended with the honest error.
    assert EventType.MODEL_WAITING in _types(events)
    assert events[-1].type == EventType.ERROR
    assert "out of quota" in events[-1].data["error"]


def test_waiting_disabled_by_zero_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("COWORKER_QUOTA_WAIT_SECONDS", "0")
    provider = FlakyProvider({"a:one": QUOTA_EXC})
    engine = _engine(tmp_path, provider, model="a:one", fallbacks=["a:one"])
    events = _collect(engine)

    assert EventType.MODEL_WAITING not in _types(events)
    assert events[-1].type == EventType.ERROR


def test_stop_cancels_a_parked_turn(tmp_path, monkeypatch):
    monkeypatch.setattr("coworker.engine._QUOTA_BACKOFF_SEC", (30,))  # long park
    provider = FlakyProvider({"a:one": QUOTA_EXC})
    engine = _engine(tmp_path, provider, model="a:one", fallbacks=["a:one"])

    async def _run():
        events = []
        async for ev in engine.run("hi"):
            events.append(ev)
            if ev.type == EventType.MODEL_WAITING:
                engine.request_interrupt()
        return events

    events = asyncio.run(asyncio.wait_for(_run(), timeout=10))
    assert events[-1].type == EventType.INTERRUPTED


def test_no_access_never_waits(tmp_path, monkeypatch):
    # Waiting cannot grant access, so an access wall with no fallback errors at once.
    monkeypatch.setattr("coworker.engine._QUOTA_BACKOFF_SEC", (30,))
    provider = FlakyProvider(
        {"a:one": RuntimeError("404 model_not_found: no access to a:one")}
    )
    engine = _engine(tmp_path, provider, model="a:one", fallbacks=["a:one"])
    events = asyncio.run(asyncio.wait_for(_collect_async(engine), timeout=10))

    assert EventType.MODEL_WAITING not in _types(events)
    assert events[-1].type == EventType.ERROR
    assert "doesn't have access" in events[-1].data["error"]


async def _collect_async(engine, text="hi"):
    return [ev async for ev in engine.run(text)]


def test_ordinary_errors_still_end_the_turn(tmp_path):
    provider = FlakyProvider({"a:one": RuntimeError("connection reset by peer")})
    engine = _engine(tmp_path, provider, model="a:one", fallbacks=["a:one", "a:two"])
    events = _collect(engine)

    assert EventType.MODEL_FAILOVER not in _types(events)
    assert events[-1].type == EventType.ERROR
    assert "connection reset" in events[-1].data["error"]


def test_no_fallback_resolver_still_waits(tmp_path, monkeypatch):
    monkeypatch.setattr("coworker.engine._QUOTA_BACKOFF_SEC", (0.01,))
    monkeypatch.setenv("COWORKER_QUOTA_WAIT_SECONDS", "0.02")
    provider = FlakyProvider({"a:one": QUOTA_EXC})
    engine = _engine(tmp_path, provider, model="a:one")  # fallbacks=None
    events = _collect(engine)

    assert EventType.MODEL_WAITING in _types(events)
    assert events[-1].type == EventType.ERROR


@pytest.mark.parametrize(
    "message",
    [
        "429 RESOURCE_EXHAUSTED: quota",
        "Error code: 429 - {'code': 'insufficient_quota'}",
        "Your credit balance is too low to access the Anthropic API",
    ],
)
def test_quota_shapes_all_trigger_failover(tmp_path, message):
    provider = FlakyProvider({"a:one": RuntimeError(message)})
    engine = _engine(tmp_path, provider, model="a:one", fallbacks=["a:one", "a:two"])
    events = _collect(engine)

    assert EventType.MODEL_FAILOVER in _types(events)
    assert engine.model == "a:two"
