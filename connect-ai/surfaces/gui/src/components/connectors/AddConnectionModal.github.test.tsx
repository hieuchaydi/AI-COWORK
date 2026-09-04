import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { Connector } from "../../api";
import { AddConnectionModal } from "./AddConnectionModal";

const github = {
  name: "github",
  title: "GitHub",
  icon: "",
  blurb: "GitHub repositories",
  auth: "token",
  two_way: true,
  channels: false,
  available: true,
  logo: "github",
  brand_color: "#1f2328",
  fields: [{
    key: "token",
    label: "Personal access token",
    required: true,
    secret: true,
    help: "Fine-grained or classic GitHub token.",
    placeholder: "",
  }],
  instructions: ["Create a token."],
  connected: false,
  account: null,
  enabled: false,
  allowed_users: [],
  tools: [],
  managed: true,
  managed_profile: false,
  installations: [],
} satisfies Connector;

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("GitHub connection flow", () => {
  it("signs in, starts the App install, then completes only after a new installation appears", async () => {
    let cloudChecks = 0;
    const calls: Array<{ url: string; method: string; body?: unknown }> = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const method = (init?.method || "GET").toUpperCase();
      calls.push({
        url,
        method,
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
      });
      if (url.includes("/v1/cloud/status")) {
        cloudChecks += 1;
        return { json: async () => ({ signed_in: cloudChecks > 1 }) } as Response;
      }
      if (url.includes("/v1/cloud/login")) {
        return { json: async () => ({ ok: true }) } as Response;
      }
      if (url.includes("/v1/connectors/github/connect-managed")) {
        return { json: async () => ({ ok: true }) } as Response;
      }
      if (url.endsWith("/v1/connectors")) {
        return {
          json: async () => ({
            connectors: [{ ...github, connected: true, mode: "relay", installations: [{ installation_id: "101" }] }],
          }),
        } as Response;
      }
      return { json: async () => ({}) } as Response;
    }));
    const onChanged = vi.fn();
    const onClose = vi.fn();

    render(<AddConnectionModal c={github} onChanged={onChanged} onClose={onClose} />);
    fireEvent.click(screen.getByTestId("github-app-connect"));

    await waitFor(() => expect(onChanged).toHaveBeenCalledOnce());
    expect(onClose).toHaveBeenCalledOnce();
    expect(calls.some((x) => x.method === "POST" && x.url.includes("/v1/cloud/login"))).toBe(true);
    expect(calls).toContainEqual(expect.objectContaining({
      method: "POST",
      body: { flow: "install" },
    }));
  });

  it("keeps manual PAT as an explicit secondary path", () => {
    vi.stubGlobal("fetch", vi.fn());
    render(<AddConnectionModal c={github} onChanged={vi.fn()} onClose={vi.fn()} />);

    fireEvent.click(screen.getByRole("tab", { name: "Manual PAT" }));
    expect(screen.getByText("Personal access token")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Connect" })).toBeTruthy();
  });
});
