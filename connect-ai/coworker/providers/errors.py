"""Friendly translation of model access + quota failures.

The picker now defaults to brand-new flagships (GPT-5.6 Sol, Claude Fable 5), and not every
account can use them: OpenAI is still rolling GPT-5.6 out per-organization, and both vendors
reject calls once quota/credits run out. Those failures arrive as terse SDK exceptions
wrapping JSON error bodies; this maps the well-known shapes to one actionable sentence.
Anything unrecognized returns None and the caller surfaces the raw error unchanged.

Matching is on the error BODY text (error codes/types), not just HTTP status — a 404 also
means "wrong base_url" and a 429 also means "slow down", and neither of those should be
dressed up as an access problem.
"""

from __future__ import annotations

from typing import Optional

# Error-body markers, verbatim from the vendors' error codes/messages:
# OpenAI: {"error": {"code": "model_not_found", "message": "The model `X` does not exist or
#   you do not have access to it."}} (404/403) and {"code": "insufficient_quota"} (429).
# Anthropic: {"type": "not_found_error", "message": "model: X"} (404),
#   {"type": "permission_error"} (403), and "credit balance is too low" (400).
_NO_ACCESS = (
    "model_not_found",
    "does not exist or you do not have access",
    "does not have access to model",
    "permission_error",
    "permission denied",
)
_NO_QUOTA = (
    "insufficient_quota",
    "exceeded your current quota",
    "credit balance is too low",
    "billing hard limit",
    # Gemini/Vertex speak gRPC status names for the same thing.
    "resource_exhausted",
    "resource has been exhausted",
)
# A plain rate limit is NOT a quota failure: nothing is wrong with the account, the
# caller just has to wait. Kept separate so callers can retry these instead of
# telling the user to go buy credits.
_RATE_LIMITED = (
    "rate_limit_exceeded",
    "rate limit reached",
    "too many requests",
    "please retry in",
    "overloaded",
)

# What `classify_model_error` returns.
QUOTA = "quota"  # out of credits / over the plan's quota
NO_ACCESS = "no_access"  # model not enabled for this account
RATE_LIMIT = "rate_limit"  # transient; retry the same model shortly


def classify_model_error(model: str, exc: Exception) -> Optional[str]:
    """QUOTA / NO_ACCESS / RATE_LIMIT for the failures a caller can route around,
    else None (an ordinary error the caller should surface as-is).

    Callers use this to fail over to another model — or, for RATE_LIMIT, to wait
    and try the same one again — instead of ending the turn (see TurnEngine).
    """
    text = str(exc).lower()
    if any(marker in text for marker in _NO_QUOTA):
        return QUOTA
    if any(marker in text for marker in _RATE_LIMITED):
        return RATE_LIMIT
    if any(marker in text for marker in _NO_ACCESS):
        return NO_ACCESS
    # Anthropic's 404 body is just "model: <id>" under type not_found_error; require both
    # halves so unrelated 404s (bad base_url, deleted resource) keep their raw message.
    if "not_found_error" in text and f"model: {model.split(':')[-1].lower()}" in text:
        return NO_ACCESS
    return None


def friendly_model_error(model: str, exc: Exception) -> Optional[str]:
    """One actionable sentence for "your account can't use this model" failures, or None.

    A plain rate limit deliberately returns None: nothing is wrong with the account,
    so the raw error stands. `classify_model_error` still reports RATE_LIMIT for
    callers that want to wait it out.
    """
    kind = classify_model_error(model, exc)
    if kind == QUOTA:
        return (
            f"Your account is out of quota for {model} — add credits or raise the limit "
            "in the provider's billing console, or pick a different model."
        )
    if kind == NO_ACCESS:
        return (
            f"Your account doesn't have access to {model} — new models can roll out "
            "gradually or require a plan upgrade. Pick a different model, or check "
            "the provider's console for availability."
        )
    return None
