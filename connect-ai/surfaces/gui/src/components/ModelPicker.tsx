import { useEffect, useMemo, useState } from "react";
import { Icon } from "./Icon";

const PROVIDER_LABELS: Record<string, string> = {
  openai: "OpenAI", anthropic: "Anthropic", gemini: "Gemini", groq: "Groq",
  cohere: "Cohere", vertex: "Vertex AI", bedrock: "Amazon Bedrock",
  nvidia: "NVIDIA NIM", openrouter: "OpenRouter", ollama: "Ollama",
  together: "Together AI", fireworks: "Fireworks", cerebras: "Cerebras",
  deepseek: "DeepSeek", zai: "Z AI", kimi: "Kimi", minimax: "MiniMax",
  qwen: "Qwen", xai: "xAI", mistral: "Mistral", meta: "Meta",
};

export interface ModelChoice { value: string; label: string; provider: string; }

export const providerForModel = (model: string) =>
  model.includes(":") ? model.split(":", 1)[0] : "openai";

export const providerLabel = (provider: string) =>
  PROVIDER_LABELS[provider] || provider.charAt(0).toUpperCase() + provider.slice(1);

export const isOneMillionModel = (value: string, label: string = "") => {
  const v = value.toLowerCase();
  const l = label.toLowerCase();
  return (
    v.startsWith("nvidia:") ||
    v.includes("nemotron-3") ||
    v.includes("deepseek-v4") ||
    v.includes("minimax-m3") ||
    v.includes("kimi-k3") ||
    v.includes("fable-5") ||
    v.includes("gemini") ||
    l.includes("1m") ||
    l.includes("1,000,000") ||
    l.includes("1,048,576")
  );
};

/** Build the visible list and collapse aliases which have the same display label. */
export function groupModelChoices(
  models: string[], labels: Record<string, string>, selected: string,
): Map<string, ModelChoice[]> {
  const groups = new Map<string, ModelChoice[]>();
  for (const value of Array.from(new Set([selected, ...models]))) {
    const provider = providerForModel(value);
    const label = labels[value] || (value.includes(":") ? value.split(":").slice(1).join(":") : value);
    const choices = groups.get(provider) || [];
    const duplicate = choices.findIndex((choice) => choice.label.trim().toLowerCase() === label.trim().toLowerCase());
    if (duplicate < 0) choices.push({ value, label, provider });
    else if (value === selected) choices[duplicate] = { value, label, provider };
    groups.set(provider, choices);
  }
  return groups;
}

interface Props {
  value: string;
  models: string[];
  modelLabels?: Record<string, string>;
  onChange: (value: string) => void;
}

export function ModelPicker({ value, models, modelLabels = {}, onChange }: Props) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const groups = useMemo(() => groupModelChoices(models, modelLabels, value), [models, modelLabels, value]);
  const currentProvider = providerForModel(value);
  const [provider, setProvider] = useState(currentProvider);
  useEffect(() => setProvider(currentProvider), [currentProvider]);
  const providers = Array.from(groups.keys());
  const choices = groups.get(provider) || [];
  const allChoices = useMemo(() => Array.from(groups.values()).flat(), [groups]);
  const current = allChoices.find((choice) => choice.value === value);
  const label = current?.label || value;

  const filteredChoices = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return null;
    return allChoices.filter((c) =>
      c.label.toLowerCase().includes(q) ||
      c.value.toLowerCase().includes(q) ||
      providerLabel(c.provider).toLowerCase().includes(q) ||
      (q === "1m" && isOneMillionModel(c.value, c.label))
    );
  }, [allChoices, search]);

  return (
    <div className="dd model-picker">
      <button
        className="pill"
        onClick={() => { setOpen((shown) => !shown); setSearch(""); }}
        title={`${providerLabel(currentProvider)} · ${label}`}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Model"
      >
        <span className="pill-label">{label}</span>
        <Icon name="chevronDown" size={13} className={`caret${open ? " open" : ""}`} />
      </button>

      {open && (
        <>
          <div className="dd-backdrop" onClick={() => setOpen(false)} />
          <div className="model-picker-menu" role="menu" data-testid="model-picker-menu">
            <div className="model-picker-search">
              <Icon name="search" size={14} className="search-ico" />
              <input
                type="text"
                placeholder="Search models or providers…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                autoFocus
              />
              {search && (
                <button
                  className="clear-btn"
                  onClick={() => setSearch("")}
                  aria-label="Clear search"
                >
                  <Icon name="x" size={12} />
                </button>
              )}
            </div>

            {filteredChoices ? (
              <div className="model-search-results">
                {filteredChoices.length === 0 ? (
                  <div className="model-empty-msg">No models matching &ldquo;{search}&rdquo;</div>
                ) : (
                  filteredChoices.map((choice) => {
                    const is1M = isOneMillionModel(choice.value, choice.label);
                    const rawId = choice.value.includes(":") ? choice.value.split(":")[1] : choice.value;
                    return (
                      <button
                        key={choice.value}
                        className={`dd-item model-choice${choice.value === value ? " sel" : ""}`}
                        onClick={() => { onChange(choice.value); setOpen(false); }}
                        title={choice.value}
                      >
                        <span className="dd-label">
                          <span className="model-choice-text">
                            <span className="model-choice-header">
                              <span className="model-choice-title">{choice.label}</span>
                              {is1M && <span className="model-badge-1m">1M Context</span>}
                            </span>
                            <span className="model-choice-id">{providerLabel(choice.provider)} · {rawId}</span>
                          </span>
                          {choice.value === value && (
                            <span className="chk">
                              <Icon name="check" size={13} />
                            </span>
                          )}
                        </span>
                      </button>
                    );
                  })
                )}
              </div>
            ) : (
              <div className="model-picker-columns">
                <div className="model-provider-list" aria-label="Providers">
                  <div className="model-picker-heading">Provider</div>
                  {providers.map((name) => (
                    <button
                      key={name}
                      className={`model-provider-item${name === provider ? " active" : ""}`}
                      onClick={() => setProvider(name)}
                      data-provider={name}
                    >
                      <span>{providerLabel(name)}</span>
                      <span className="model-provider-count">{groups.get(name)?.length || 0}</span>
                    </button>
                  ))}
                </div>
                <div className="model-choice-list" aria-label={`${providerLabel(provider)} models`}>
                  <div className="model-picker-heading">{providerLabel(provider)} models</div>
                  {choices.map((choice) => {
                    const is1M = isOneMillionModel(choice.value, choice.label);
                    const rawId = choice.value.includes(":") ? choice.value.split(":")[1] : choice.value;
                    return (
                      <button
                        key={choice.value}
                        className={`dd-item model-choice${choice.value === value ? " sel" : ""}`}
                        onClick={() => { onChange(choice.value); setOpen(false); }}
                        title={choice.value}
                      >
                        <span className="dd-label">
                          <span className="model-choice-text">
                            <span className="model-choice-header">
                              <span className="model-choice-title">{choice.label}</span>
                              {is1M && <span className="model-badge-1m">1M</span>}
                            </span>
                            <span className="model-choice-id">{rawId}</span>
                          </span>
                          {choice.value === value && (
                            <span className="chk">
                              <Icon name="check" size={13} />
                            </span>
                          )}
                        </span>
                      </button>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
