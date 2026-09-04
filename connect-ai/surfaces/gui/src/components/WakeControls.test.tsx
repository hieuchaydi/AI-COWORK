import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { WakeControls } from "./WakeControls";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("shows pending wakes and lets the user pause auto resume or cancel", async () => {
  const calls: Array<{ url: string; method: string; body?: unknown }> = [];
  let wakes = [{ id: "w1", kind: "timer", state: "pending", note: "check crawl", fire_at: "2026-09-04T10:00:00+07:00", created_at: "" }];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = (init?.method || "GET").toUpperCase();
    calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    if (method === "DELETE") wakes = [];
    return { json: async () => method === "GET" ? { auto: true, wakes } : { ok: true } } as Response;
  }));

  render(<WakeControls sessionId="s1" />);
  expect(await screen.findByText("1 wake đang chờ")).toBeTruthy();
  fireEvent.click(screen.getByRole("switch"));
  await waitFor(() => expect(calls).toContainEqual(expect.objectContaining({ method: "PATCH", body: { auto: false } })));
  fireEvent.click(screen.getByRole("button", { name: "Hủy wake" }));
  await waitFor(() => expect(screen.queryByTestId("wake-controls")).toBeNull());
  expect(calls.some((x) => x.method === "DELETE" && x.url.endsWith("/wakes/w1"))).toBe(true);
});
