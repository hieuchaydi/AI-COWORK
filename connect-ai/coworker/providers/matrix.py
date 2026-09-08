"""The curated model matrix — the only models we actively suggest, label, and vouch for.

Keyed by the FULL routed id, exactly as the ProviderRouter receives it — including reseller
"ugly names" like ``together:zai-org/GLM-5.2`` (bare ids route to the OpenAI default). Each
entry carries the UI display label and the model's capabilities, making this the single
source of truth the capability probe and the GUI's pickers read from.

Deliberately SMALL (owner call, 2026-07-04): current-generation, agent-capable (tool-calling)
models only. It is not user-editable — users can still add any custom model string, which
falls back to the conservative heuristics in ``capabilities.py`` at their own risk of
degraded results. Ids verified against vendor/reseller catalogs on 2026-07-04; refresh the
reseller rows when catalogs rotate (they rename on every model generation).

Context windows (``context_window``, tokens) feed the GUI's context-fill meter. Entries
where the vendor spec wasn't re-checked stay ``None`` — the meter simply hides rather than
showing a made-up denominator. Values entered 2026-07-28 from vendor docs; verify alongside
the id refresh.

Resellers: Together + Fireworks + OpenRouter. TODO: add Groq entries here AND its
descriptor in ``registry.py`` once the current provider surface is tested — deliberately
deferred to bound how much needs verifying at once.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .base import ModelCapabilities

_AGENTIC = ModelCapabilities(
    tools=True, vision=False, parallel_tool_calls=True, streaming=True
)
_AGENTIC_VISION_SERIAL = ModelCapabilities(
    tools=True,
    vision=True,
    pdf=False,
    parallel_tool_calls=False,
    streaming=True,
)

# The native three (OpenAI, Anthropic, Gemini) all take PDFs directly; every
# OpenAI-compatible vendor and reseller in the matrix does not (their chat APIs have
# no inline file part — checked 2026-07-17), so those fall back via pdf_support.py.
_AGENTIC_VISION = ModelCapabilities(
    tools=True, vision=True, pdf=True, parallel_tool_calls=True, streaming=True
)


@dataclass(frozen=True)
class ModelEntry:
    label: str  # UI display name, e.g. "GLM-5.2 · via Together"
    caps: ModelCapabilities = _AGENTIC
    # Max context length in tokens (prompt side), for the GUI's context-fill meter.
    # None = not verified against the vendor spec yet; the meter hides.
    context_window: Optional[int] = None
    tpm: Optional[int] = None
    rpm: Optional[int] = None


MATRIX: dict[str, ModelEntry] = {
    # -- first-party ------------------------------------------------------------
    # GPT-5.6 (2026-07-09): number = generation, Sol/Terra/Luna = capability tiers.
    # Bare "gpt-5.6" aliases to Sol server-side; we list the explicit tier ids only.
    # Rolling out — accounts without access get a friendly error (providers/errors.py).
    "gpt-5.6-sol": ModelEntry("GPT-5.6 Sol · OpenAI", _AGENTIC_VISION, 400_000),
    "gpt-5.6-terra": ModelEntry("GPT-5.6 Terra · OpenAI", _AGENTIC_VISION, 400_000),
    "gpt-5.6-luna": ModelEntry("GPT-5.6 Luna · OpenAI", _AGENTIC_VISION, 400_000),
    "gpt-5.5": ModelEntry("GPT-5.5 · OpenAI", _AGENTIC_VISION, 400_000),
    "gpt-5-mini": ModelEntry(
        "GPT-5 Mini · OpenAI",
        _AGENTIC_VISION,
        400_000,
        tpm=500_000,
        rpm=500,
    ),
    # Fable 5 (2026-06-09) is GA; its Mythos 5 sibling is approved-orgs-only, so it
    # stays out of a picker meant for the public.
    "anthropic:claude-fable-5": ModelEntry(
        "Claude Fable 5 · Anthropic", _AGENTIC_VISION, 1_000_000
    ),
    "anthropic:claude-opus-4-8": ModelEntry(
        "Claude Opus 4.8 · Anthropic", _AGENTIC_VISION, 200_000
    ),
    "anthropic:claude-sonnet-4-6": ModelEntry(
        "Claude Sonnet 4.6 · Anthropic", _AGENTIC_VISION, 200_000
    ),
    "anthropic:claude-haiku-4-5": ModelEntry(
        "Claude Haiku 4.5 · Anthropic", _AGENTIC_VISION, 200_000
    ),
    # Google AI Studio catalogue checked live 2026-09-07 and cross-checked against the
    # official model pages. Put the high daily-quota Flash-Lite models first, then
    # Gemma's 14.4K/day pools, then the lower-RPD generations. Gemma 4's hosted API
    # documents text/image input and native tools, but not direct PDF input or parallel
    # tool calls, so both capabilities stay conservative.
    "gemini:gemini-3.5-flash-lite": ModelEntry(
        "Gemini 3.5 Flash-Lite · Google", _AGENTIC_VISION, 1_048_576
    ),
    "gemini:gemini-3.1-flash-lite": ModelEntry(
        "Gemini 3.1 Flash-Lite · Google", _AGENTIC_VISION, 1_048_576
    ),
    "gemini:gemini-3.8-flash": ModelEntry(
        "Gemini 3.8 Flash · Google", _AGENTIC_VISION, 1_048_576
    ),
    "gemini:gemini-3.1-pro-preview": ModelEntry(
        "Gemini 3.1 Pro · Google", _AGENTIC_VISION, 1_048_576
    ),
    "gemini:gemini-3.7-flash": ModelEntry(
        "Gemini 3.7 Flash · Google", _AGENTIC_VISION, 1_048_576
    ),
    "gemini:gemini-3.6-flash": ModelEntry(
        "Gemini 3.6 Flash · Google", _AGENTIC_VISION, 1_048_576
    ),
    "gemini:gemini-3.5-flash": ModelEntry(
        "Gemini 3.5 Flash · Google", _AGENTIC_VISION, 1_048_576
    ),
    "gemini:gemini-3-flash-preview": ModelEntry(
        "Gemini 3 Flash (Preview) · Google", _AGENTIC_VISION, 1_048_576
    ),
    "gemini:gemini-2.5-pro": ModelEntry(
        "Gemini 2.5 Pro · Google", _AGENTIC_VISION, 1_048_576
    ),
    "gemini:gemini-2.5-flash": ModelEntry(
        "Gemini 2.5 Flash · Google", _AGENTIC_VISION, 1_048_576
    ),
    "gemini:gemini-2.5-flash-lite": ModelEntry(
        "Gemini 2.5 Flash-Lite · Google", _AGENTIC_VISION, 1_048_576
    ),
    # -- direct OpenAI-compatible vendors ----------------------------------------
    # Muse Spark (Meta Model API, public preview 2026-07-09): multimodal + tools via
    # their OpenAI-compat surface. Vision yes; PDFs unverified over compat — falls
    # back via pdf_support.py like the other compat vendors.
    "meta:muse-spark-1.1": ModelEntry(
        "Muse Spark 1.1 · Meta",
        ModelCapabilities(
            tools=True, vision=True, parallel_tool_calls=True, streaming=True
        ),
    ),
    "zai:glm-5.2": ModelEntry("GLM-5.2 · Z AI", _AGENTIC, 128_000),
    "deepseek:deepseek-v4-flash": ModelEntry(
        "DeepSeek V4 Flash · DeepSeek", _AGENTIC, 128_000
    ),
    "deepseek:deepseek-v4-pro": ModelEntry(
        "DeepSeek V4 Pro · DeepSeek", _AGENTIC, 128_000
    ),
    "kimi:kimi-k2.6": ModelEntry("Kimi K2.6 · Moonshot", _AGENTIC, 256_000),
    "minimax:MiniMax-M2.5": ModelEntry("MiniMax M2.5 · MiniMax"),
    "qwen:qwen3-max": ModelEntry("Qwen3 Max · Alibaba", _AGENTIC, 256_000),
    "xai:grok-4.3": ModelEntry("Grok 4.3 · xAI", _AGENTIC, 256_000),
    "mistral:mistral-large-latest": ModelEntry(
        "Mistral Large · Mistral", _AGENTIC, 128_000
    ),
    # -- resellers (their model namespaces, verbatim) -----------------------------
    "together:thinkingmachines/Inkling": ModelEntry("Inkling · via Together"),
    "together:zai-org/GLM-5.2": ModelEntry("GLM-5.2 · via Together", _AGENTIC, 128_000),
    # Kimi K3 (2026-07-16) is not on Together yet — weights land ~07-27; revisit then.
    "together:moonshotai/Kimi-K2.7-Code": ModelEntry(
        "Kimi K2.7 Code · via Together", _AGENTIC, 256_000
    ),
    "together:moonshotai/Kimi-K2.6": ModelEntry(
        "Kimi K2.6 · via Together", _AGENTIC, 256_000
    ),
    "together:deepseek-ai/DeepSeek-V4-Pro": ModelEntry(
        "DeepSeek V4 Pro · via Together", _AGENTIC, 128_000
    ),
    "together:meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8": ModelEntry(
        "Llama 4 Maverick · via Together", _AGENTIC, 1_000_000
    ),
    "fireworks:accounts/fireworks/models/glm-5p2": ModelEntry(
        "GLM-5.2 · via Fireworks", _AGENTIC, 128_000
    ),
    "fireworks:accounts/fireworks/models/kimi-k2p6": ModelEntry(
        "Kimi K2.6 · via Fireworks", _AGENTIC, 256_000
    ),
    "fireworks:accounts/fireworks/models/deepseek-v4-pro": ModelEntry(
        "DeepSeek V4 Pro · via Fireworks", _AGENTIC, 128_000
    ),
    "fireworks:accounts/fireworks/models/llama4-maverick-instruct-basic": ModelEntry(
        "Llama 4 Maverick · via Fireworks", _AGENTIC, 1_000_000
    ),
    # OpenRouter slugs are lowercase `<lab>/<model>` (checked against their catalog
    # 2026-07-25); same labs as above, one key for all of them.
    "openrouter:z-ai/glm-5.2": ModelEntry("GLM-5.2 · via OpenRouter", _AGENTIC, 128_000),
    "openrouter:moonshotai/kimi-k2.6": ModelEntry(
        "Kimi K2.6 · via OpenRouter", _AGENTIC, 256_000
    ),
    "openrouter:deepseek/deepseek-v4-pro": ModelEntry(
        "DeepSeek V4 Pro · via OpenRouter", _AGENTIC, 128_000
    ),
    "openrouter:meta-llama/llama-4-maverick": ModelEntry(
        "Llama 4 Maverick · via OpenRouter", _AGENTIC, 1_000_000
    ),
    # Groq — LPU inference, free tier. 131K context window across models.
    # Tool support: gpt-oss-120b, gpt-oss-20b, qwen3.6-27b support native tool calling.
    # compound & compound-mini are text-only completions (no function calling parameter).
    "groq:openai/gpt-oss-120b": ModelEntry(
        "GPT-OSS 120B · via Groq", _AGENTIC, 131_072
    ),
    "groq:openai/gpt-oss-20b": ModelEntry(
        "GPT-OSS 20B · via Groq", _AGENTIC, 131_072
    ),
    "groq:groq/compound": ModelEntry(
        "Compound (text only) · via Groq",
        ModelCapabilities(tools=False, vision=False, streaming=True),
        131_072,
    ),
    "groq:groq/compound-mini": ModelEntry(
        "Compound Mini (text only) · via Groq",
        ModelCapabilities(tools=False, vision=False, streaming=True),
        131_072,
    ),
    "groq:qwen/qwen3.6-27b": ModelEntry(
        "Qwen 3.6 27B · via Groq", _AGENTIC, 131_072
    ),
    # NVIDIA hosted NIM catalog, live-checked 2026-09-08. Only free-endpoint chat
    # models whose official NVIDIA page specifies >= 1M context are listed.
    "nvidia:nvidia/nemotron-3.5-lightning-30b-a3b": ModelEntry(
        "Nemotron 3.5 Lightning 30B · NVIDIA NIM", _AGENTIC, 1_048_576
    ),
    "nvidia:nvidia/nemotron-3-super-120b-a12b": ModelEntry(
        "Nemotron 3 Super 120B · NVIDIA NIM", _AGENTIC, 1_048_576
    ),
    "nvidia:nvidia/nemotron-3-ultra-550b-a55b": ModelEntry(
        "Nemotron 3 Ultra 550B · NVIDIA NIM", _AGENTIC, 1_048_576
    ),
    "nvidia:deepseek-ai/deepseek-v4-flash-0731": ModelEntry(
        "DeepSeek V4 Flash 0731 · NVIDIA NIM", _AGENTIC, 1_000_000
    ),
    "nvidia:deepseek-ai/deepseek-v4-pro-0813": ModelEntry(
        "DeepSeek V4 Pro 0813 · NVIDIA NIM", _AGENTIC, 1_000_000
    ),
    "nvidia:minimaxai/minimax-m3": ModelEntry(
        "MiniMax M3 · NVIDIA NIM",
        ModelCapabilities(tools=True, vision=True, parallel_tool_calls=True, streaming=True),
        1_000_000,
    ),
    "nvidia:moonshotai/kimi-k3": ModelEntry(
        "Kimi K3 · NVIDIA NIM",
        ModelCapabilities(tools=True, vision=True, parallel_tool_calls=True, streaming=True),
        1_048_576,
    ),
    # Cohere — Command / Aya family via the native v2 Chat endpoint.
    # Free trial key: 20 RPM, 1,000 calls/month.
    "cohere:command-a-plus-05-2026": ModelEntry(
        "Command A+ (218B) · Cohere", _AGENTIC_VISION, 128_000
    ),
    "cohere:command-a-03-2025": ModelEntry(
        "Command A (111B) · Cohere", _AGENTIC, 256_000
    ),
    "cohere:command-r-plus-08-2024": ModelEntry(
        "Command R+ · Cohere", _AGENTIC, 128_000
    ),
    "cohere:command-r-08-2024": ModelEntry(
        "Command R · Cohere", _AGENTIC, 128_000
    ),
    "cohere:command-r7b-12-2024": ModelEntry(
        "Command R7B · Cohere", _AGENTIC, 128_000
    ),
    "cohere:command-a-reasoning-08-2025": ModelEntry(
        "Command A Reasoning · Cohere", _AGENTIC, 256_000
    ),
    "cohere:command-a-translate-08-2025": ModelEntry(
        "Command A Translate · Cohere", _AGENTIC, 8_000
    ),
    "cohere:command-a-vision-07-2025": ModelEntry(
        "Command A Vision · Cohere",
        ModelCapabilities(tools=False, vision=True, streaming=True),
        128_000,
    ),
    "cohere:command-r7b-arabic-02-2025": ModelEntry(
        "Command R7B Arabic · Cohere", _AGENTIC, 128_000
    ),
    "cohere:c4ai-aya-expanse-32b": ModelEntry(
        "Aya Expanse 32B · Cohere",
        ModelCapabilities(tools=False, vision=False, streaming=True),
        128_000,
    ),
    "cohere:c4ai-aya-vision-32b": ModelEntry(
        "Aya Vision 32B · Cohere",
        ModelCapabilities(tools=False, vision=True, streaming=True),
        16_000,
    ),
    # Cohere unversioned aliases
    "cohere:command-a-plus": ModelEntry(
        "Command A+ (218B) · Cohere", _AGENTIC_VISION, 128_000
    ),
    "cohere:command-a": ModelEntry(
        "Command A (111B) · Cohere", _AGENTIC, 256_000
    ),
    "cohere:command-r-plus": ModelEntry(
        "Command R+ · Cohere", _AGENTIC, 128_000
    ),
    "cohere:command-r": ModelEntry(
        "Command R · Cohere", _AGENTIC, 128_000
    ),
    "cohere:command-r7b": ModelEntry(
        "Command R7B · Cohere", _AGENTIC, 128_000
    ),
    "cohere:command-a-reasoning": ModelEntry(
        "Command A Reasoning · Cohere", _AGENTIC, 256_000
    ),
    "cohere:command-a-translate": ModelEntry(
        "Command A Translate · Cohere", _AGENTIC, 8_000
    ),
    "cohere:command-a-vision": ModelEntry(
        "Command A Vision · Cohere",
        ModelCapabilities(tools=False, vision=True, streaming=True),
        128_000,
    ),
    "cohere:command-r7b-arabic": ModelEntry(
        "Command R7B Arabic · Cohere", _AGENTIC, 128_000
    ),
    "cohere:aya-expanse-32b": ModelEntry(
        "Aya Expanse 32B · Cohere",
        ModelCapabilities(tools=False, vision=False, streaming=True),
        128_000,
    ),
    "cohere:aya-vision-32b": ModelEntry(
        "Aya Vision 32B · Cohere",
        ModelCapabilities(tools=False, vision=True, streaming=True),
        16_000,
    ),
    # -- cloud accounts (models running in the user's own AWS/GCP) ----------------
    # Bedrock ids carry a family segment (claude/ → native Anthropic path, other/ →
    # Converse) plus AWS's own `-v<n>:<m>` version suffix. Some regions require the
    # `us.`/`eu.` cross-region inference-profile prefix — custom add-model accepts those.
    "bedrock:claude/anthropic.claude-sonnet-4-6-v1:0": ModelEntry(
        "Claude Sonnet 4.6 · AWS Bedrock", _AGENTIC_VISION, 200_000
    ),
    "bedrock:claude/anthropic.claude-haiku-4-5-v1:0": ModelEntry(
        "Claude Haiku 4.5 · AWS Bedrock", _AGENTIC_VISION, 200_000
    ),
    "bedrock:other/amazon.nova-2-pro-v1:0": ModelEntry(
        "Nova 2 Pro · AWS Bedrock", _AGENTIC, 300_000
    ),
    "bedrock:other/meta.llama4-maverick-17b-instruct-v1:0": ModelEntry(
        "Llama 4 Maverick · AWS Bedrock", _AGENTIC, 1_000_000
    ),
    "bedrock:other/mistral.mistral-large-3-v1:0": ModelEntry(
        "Mistral Large 3 · AWS Bedrock", _AGENTIC, 128_000
    ),
    # Live-verified on Converse 2026-07-26 (complete/stream/tool round trip); asked for
    # two tool calls it emits them one at a time, so parallel stays off.
    "bedrock:other/nvidia.nemotron-super-3-120b": ModelEntry(
        "Nemotron Super 3 120B · AWS Bedrock",
        ModelCapabilities(
            tools=True, vision=False, parallel_tool_calls=False, streaming=True
        ),
    ),
    # Vertex ids carry a family segment too (gemini/ and claude/ → native paths,
    # openweight/ → the MaaS OpenAI-compat endpoint, keeping the publisher segment).
    "vertex:gemini/gemini-3.1-pro-preview": ModelEntry(
        "Gemini 3.1 Pro · Vertex AI", _AGENTIC_VISION, 1_048_576
    ),
    "vertex:gemini/gemini-3.6-flash": ModelEntry(
        "Gemini 3.6 Flash · Vertex AI", _AGENTIC_VISION, 1_048_576
    ),
    "vertex:claude/claude-sonnet-4-6": ModelEntry(
        "Claude Sonnet 4.6 · Vertex AI", _AGENTIC_VISION, 200_000
    ),
    "vertex:claude/claude-haiku-4-5": ModelEntry(
        "Claude Haiku 4.5 · Vertex AI", _AGENTIC_VISION, 200_000
    ),
    "vertex:openweight/meta/llama-4-maverick-17b-128e-instruct-maas": ModelEntry(
        "Llama 4 Maverick · Vertex AI", _AGENTIC, 1_000_000
    ),
    "vertex:openweight/qwen/qwen3-coder-480b-a35b-instruct-maas": ModelEntry(
        "Qwen3 Coder · Vertex AI", _AGENTIC, 256_000
    ),
}


COHERE_ALIASES: dict[str, str] = {
    "cohere:command-a-plus": "cohere:command-a-plus-05-2026",
    "cohere:command-a": "cohere:command-a-03-2025",
    "cohere:command-r-plus": "cohere:command-r-plus-08-2024",
    "cohere:command-r": "cohere:command-r-08-2024",
    "cohere:command-r7b": "cohere:command-r7b-12-2024",
    "cohere:command-a-reasoning": "cohere:command-a-reasoning-08-2025",
    "cohere:command-a-translate": "cohere:command-a-translate-08-2025",
    "cohere:command-a-vision": "cohere:command-a-vision-07-2025",
    "cohere:command-r7b-arabic": "cohere:command-r7b-arabic-02-2025",
    "cohere:aya-expanse-32b": "cohere:c4ai-aya-expanse-32b",
    "cohere:aya-vision-32b": "cohere:c4ai-aya-vision-32b",
    "command-a-plus": "command-a-plus-05-2026",
    "command-a": "command-a-03-2025",
    "command-r-plus": "command-r-plus-08-2024",
    "command-r": "command-r-08-2024",
    "command-r7b": "command-r7b-12-2024",
    "command-a-reasoning": "command-a-reasoning-08-2025",
    "command-a-translate": "command-a-translate-08-2025",
    "command-a-vision": "command-a-vision-07-2025",
    "command-r7b-arabic": "command-r7b-arabic-02-2025",
    "aya-expanse-32b": "c4ai-aya-expanse-32b",
    "aya-vision-32b": "c4ai-aya-vision-32b",
}


def resolve_model_alias(model: str) -> str:
    """Map human/short aliases to canonical model IDs (e.g. Cohere versioned IDs)."""
    return COHERE_ALIASES.get(model, model)


def entry_for(model: str) -> ModelEntry | None:
    canonical = resolve_model_alias(model)
    return MATRIX.get(canonical) or MATRIX.get(model)


def model_labels() -> dict[str, str]:
    """Full-id → display-label map, shipped to the GUI so every picker shows human names."""
    return {mid: e.label for mid, e in MATRIX.items()}


def model_context_windows() -> dict[str, int]:
    """Full-id → context-window map (verified entries only), for the GUI's fill meter."""
    return {
        mid: e.context_window for mid, e in MATRIX.items() if e.context_window
    }


def models_for_provider(provider: str) -> list[str]:
    """BARE model ids (prefix stripped) the matrix curates for a provider — feeds the
    Settings pane's suggestions and the composer picker so both stay in lockstep with the
    matrix. OpenAI entries are stored without a prefix (bare ids route to the OpenAI
    default), so its list is every un-prefixed id."""
    if provider == "openai":
        return [mid for mid in MATRIX if ":" not in mid]
    prefix = provider + ":"
    aliases = {mid for mid in COHERE_ALIASES if mid.startswith(prefix)}
    return [
        mid[len(prefix) :]
        for mid in MATRIX
        if mid.startswith(prefix) and mid not in aliases
    ]


def model_rate_limits() -> dict[str, dict[str, int]]:
    """Full-id → rate limit specs (tpm/rpm) for entries that specify them."""
    return {
        mid: {k: v for k, v in (("tpm", e.tpm), ("rpm", e.rpm)) if v is not None}
        for mid, e in MATRIX.items()
        if e.tpm is not None or e.rpm is not None
    }

