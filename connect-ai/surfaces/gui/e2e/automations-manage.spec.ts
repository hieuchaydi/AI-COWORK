// Automations management — the parts of Rohit's manual pass that automations.spec.ts (run-banner +
// Back) doesn't cover: the task list, triggering a manual run (POST .../run appends a run and opens
// its live session), pausing via the enable toggle, and deleting. Seeded with one task.
import { expect } from "@playwright/test";
import { test } from "./fixtures";

async function openAutomations(page) {
  await page.addInitScript(() => localStorage.removeItem("coworker:nav-collapsed:v1"));
  await page.goto("/");
  await page.getByTestId("nav-automations").dispatchEvent("click");
  await expect(page.getByText("Recurring tasks Workspace runs on a schedule.")).toBeVisible();
}

test("lists a scheduled task with its schedule and run count", async ({ page }) => {
  await openAutomations(page);
  const card = page.locator(".sched-card", { hasText: "Daily AI News" });
  await expect(card).toBeVisible();
  await expect(card).toContainText("Every day at ~5:40 PM");
  await expect(card).toContainText("last running");
});

test("Run now completes as a one-shot and clears the live run session", async ({ page }) => {
  await openAutomations(page);
  await page.locator(".sched-card", { hasText: "Daily AI News" }).click();
  const runPrepared = page.waitForResponse(
    (res) => /\/v1\/automations\/task-1\/run$/.test(new URL(res.url()).pathname) && res.request().method() === "POST",
  );
  const runFinalized = page.waitForResponse(
    (res) => /\/v1\/automations\/task-1\/runs\/[^/]+\/finalize$/.test(new URL(res.url()).pathname) && res.request().method() === "POST",
  );
  await page.getByRole("button", { name: /Run now/ }).dispatchEvent("click");
  await runPrepared;
  await runFinalized;

  // Once its first turn is done, the UI finalizes the run, returns to the task detail,
  // and clears the internal __run__ session from the active view.
  await expect(page.getByRole("button", { name: /Run now/ })).toBeVisible();
  await expect(page.getByTestId("run-banner")).toHaveCount(0);
  await expect(page.locator(".sched-run", { hasText: "manual" }).first()).toContainText("ok");
});

test("enable toggle pauses the task", async ({ page }) => {
  await openAutomations(page);
  await page.locator(".sched-card", { hasText: "Daily AI News" }).click();
  await expect(page.getByText(/Active · next/)).toBeVisible();
  // The checkbox is visually hidden behind a styled slider — click the label wrapper.
  await page.locator("label.switch").click();
  await expect(page.getByText("Paused", { exact: false })).toBeVisible();
});

test("delete removes the task; deleting the last one shows the empty state", async ({ page }) => {
  await openAutomations(page);
  await page.locator(".sched-card", { hasText: "Daily AI News" }).click();
  await page.getByRole("button", { name: /Delete/ }).click();
  // Back on the list, the deleted task is gone; the other seeded task remains.
  await expect(page.locator(".sched-card", { hasText: "Daily AI News" })).toHaveCount(0);
  await expect(page.locator(".sched-card", { hasText: "Weekly CRM digest" })).toHaveCount(1);

  await page.locator(".sched-card", { hasText: "Weekly CRM digest" }).click();
  await page.getByRole("button", { name: /Delete/ }).click();
  await expect(page.getByText(/No scheduled tasks yet/)).toBeVisible();
});
