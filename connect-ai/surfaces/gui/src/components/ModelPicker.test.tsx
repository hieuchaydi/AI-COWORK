import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ModelPicker, groupModelChoices } from "./ModelPicker";

describe("ModelPicker", () => {
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
});
