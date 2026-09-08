import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ModelPicker, groupModelChoices } from "./ModelPicker";

describe("ModelPicker", () => {
  afterEach(() => {
    cleanup();
  });
  const models = [
    "gemini:gemini-3.1-flash-lite", "groq:openai/gpt-oss-120b",
    "cohere:command-a-03-2025", "cohere:command-a",
  ];
  const labels = {
    "gemini:gemini-3.1-flash-lite": "Gemini 3.1 Flash Lite · Google",
    "groq:openai/gpt-oss-120b": "GPT OSS 120B · Groq",
    "cohere:command-a-03-2025": "Command A (111B) · Cohere",
    "cohere:command-a": "Command A (111B) · Cohere",
  };

  it("groups models by provider and collapses duplicate aliases", () => {
    const groups = groupModelChoices(models, labels, models[0]);
    expect(Array.from(groups.keys())).toEqual(["gemini", "groq", "cohere"]);
    expect(groups.get("cohere")).toHaveLength(1);
  });

  it("shows one provider at a time and selects a child model", () => {
    const onChange = vi.fn();
    render(<ModelPicker value={models[0]} models={models} modelLabels={labels} onChange={onChange} />);
    fireEvent.click(screen.getByRole("button", { name: "Model" }));
    expect(screen.getByLabelText("Gemini models")).toBeTruthy();
    expect(screen.queryByText("GPT OSS 120B · Groq")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Groq/ }));
    fireEvent.click(screen.getByText("GPT OSS 120B · Groq"));
    expect(onChange).toHaveBeenCalledWith("groq:openai/gpt-oss-120b");
  });

  it("filters models dynamically via search input and displays 1M badges", () => {
    const onChange = vi.fn();
    const extendedModels = [
      ...models,
      "nvidia:nvidia/nemotron-3.5-lightning-30b-a3b",
    ];
    const extendedLabels = {
      ...labels,
      "nvidia:nvidia/nemotron-3.5-lightning-30b-a3b": "Nemotron 3.5 Lightning 30B · NVIDIA NIM",
    };
    render(<ModelPicker value={models[0]} models={extendedModels} modelLabels={extendedLabels} onChange={onChange} />);
    fireEvent.click(screen.getByRole("button", { name: "Model" }));
    const searchInput = screen.getByPlaceholderText(/Search models or providers/i);
    fireEvent.change(searchInput, { target: { value: "nemotron" } });
    expect(screen.getByText("Nemotron 3.5 Lightning 30B · NVIDIA NIM")).toBeTruthy();
    expect(screen.getByText("1M Context")).toBeTruthy();
    fireEvent.click(screen.getByText("Nemotron 3.5 Lightning 30B · NVIDIA NIM"));
    expect(onChange).toHaveBeenCalledWith("nvidia:nvidia/nemotron-3.5-lightning-30b-a3b");
  });

  it("renders rate limit badge and searches by rate limit terms", () => {
    const onChange = vi.fn();
    const extendedModels = [...models, "gpt-5-mini"];
    const extendedLabels = {
      ...labels,
      "gpt-5-mini": "GPT-5 Mini · OpenAI",
    };
    const rateLimits = {
      "gpt-5-mini": { tpm: 500000, rpm: 500 },
    };
    render(
      <ModelPicker
        value={models[0]}
        models={extendedModels}
        modelLabels={extendedLabels}
        modelRateLimits={rateLimits}
        onChange={onChange}
      />
    );
    fireEvent.click(screen.getByRole("button", { name: "Model" }));
    fireEvent.click(screen.getByRole("button", { name: /OpenAI/ }));
    expect(screen.getByText("500K TPM · 500 RPM")).toBeTruthy();

    const searchInput = screen.getByPlaceholderText(/Search models or providers/i);
    fireEvent.change(searchInput, { target: { value: "500k" } });
    expect(screen.getByText("GPT-5 Mini · OpenAI")).toBeTruthy();
    expect(screen.getByText("500K TPM · 500 RPM")).toBeTruthy();
  });
});

