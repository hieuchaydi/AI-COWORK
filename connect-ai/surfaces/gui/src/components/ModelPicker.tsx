import { useEffect, useMemo, useState } from "react";
import { Icon } from "./Icon";

const PROVIDER_LABELS: Record<string, string> = {
  openai: "OpenAI", anthropic: "Anthropic", gemini: "Gemini", groq: "Groq",
  cohere: "Cohere", vertex: "Vertex AI", bedrock: "Amazon Bedrock",
  openrouter: "OpenRouter", ollama: "Ollama",
};

export interface ModelChoice { value: string; label: string; provider: string; }

export const providerForModel = (model: string) =>
  model.includes(":") ? model.split(":", 1)[0] : "openai";

export const providerLabel = (provider: string) =>
  PROVIDER_LABELS[provider] || provider.charAt(0).toUpperCase() + provider.slice(1);

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
  const groups = useMemo(() => groupModelChoices(models, modelLabels, value), [models, modelLabels, value]);
  const currentProvider = providerForModel(value);
  const [provider, setProvider] = useState(currentProvider);
  useEffect(() => setProvider(currentProvider), [currentProvider]);
  const providers = Array.from(groups.keys());
  const choices = groups.get(provider) || [];
  const current = Array.from(groups.values()).flat().find((choice) => choice.value === value);
  const label = current?.label || value;

  return (
    <div className="dd model-picker">
      <button className="pill" onClick={() => setOpen((shown) => !shown)}
        title={`${providerLabel(currentProvider)} · ${label}`} aria-haspopup="menu"
        aria-expanded={open} aria-label="Model">
        <span className="pill-label">{label}</span>
        <Icon name="chevronDown" size={13} className="caret" />
      </button>
      {open && <>
        <div className="dd-backdrop" onClick={() => setOpen(false)} />
        <div className="model-picker-menu" role="menu" data-testid="model-picker-menu">
          <div className="model-provider-list" aria-label="Providers">
            <div className="model-picker-heading">Provider</div>
            {providers.map((name) => <button key={name}
              className={`model-provider-item${name === provider ? " active" : ""}`}
              onClick={() => setProvider(name)} data-provider={name}>
              <span>{providerLabel(name)}</span>
              <span className="model-provider-count">{groups.get(name)?.length || 0}</span>
            </button>)}
          </div>
          <div className="model-choice-list" aria-label={`${providerLabel(provider)} models`}>
            <div className="model-picker-heading">{providerLabel(provider)} models</div>
            {choices.map((choice) => <button key={choice.value}
              className={`dd-item model-choice${choice.value === value ? " sel" : ""}`}
              onClick={() => { onChange(choice.value); setOpen(false); }} title={choice.value}>
              <span className="dd-label">{choice.label}
                {choice.value === value && <span className="chk">✓</span>}
              </span>
            </button>)}
          </div>
        </div>
      </>}
    </div>
  );
}
