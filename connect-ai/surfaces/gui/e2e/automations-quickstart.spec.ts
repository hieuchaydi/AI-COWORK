// The Automations quickstart (UX-DECISIONS §29): ONE template system — the former onboarding
// recipe (role templates, connect rows, §25 consent) merged into the page's "Start from a
// template" grid. Cards carry §27's connector-dot vocabulary; picking one expands the
// configure card. Non-Google connectors connect via the in-place modal (manual token / local
// MCP OAuth); Google connectors go through the local 1-click login. `ob-*` testids kept.
import { expect } from "@playwright/test";
import { test } from "./fixtures";

async function openAutomations(page) {
  await page.goto("/");
  await page.getByTestId("account-row").click();
  await page.getByTestId("account-menu").getByRole("button", { name: "Automations", exact: true }).click();
  await expect(page.getByText("Recurring tasks Workspace runs on a schedule.")).toBeVisible();
}

// The fixtures seed one task, so the quickstart isn't on the bare list — surface it via the
// "+ New automation" toggle (empty state shows it without the toggle; covered indirectly by
// the delete test in automations-manage.spec.ts).
async function openQuickstart(page) {
  await openAutomations(page);
  await page.getByRole("button", { name: "+ New automation" }).click();
  await expect(page.getByText("Start from a template")).toBeVisible();
}

test("role recipe: connect the missing tool in place, channel by name, consent mints the grant", async ({
  page,
}) => {
  await openQuickstart(page);

  // Pipeline digest: Slack is connected in fixtures, HubSpot isn't. No recipe form yet.
  await page.getByTestId("qs-template-pipeline").click();
  const cfg = page.getByTestId("qs-configure");
  await expect(cfg).toContainText("Set up");
  await expect(cfg).toContainText("Pipeline digest");
  await expect(cfg.getByText("✓ Connected").first()).toBeVisible();
  await expect(page.getByTestId("ob-recipe")).toHaveCount(0);
  await expect(page.getByTestId("ob-create")).toBeDisabled();
  await expect(page.getByTestId("ob-create-hint")).toContainText("Connect HubSpot");

  // Connect HubSpot → the in-place modal opens; the token form connects it, and the
  // poll flips the row so the recipe form appears.
  await page.getByTestId("ob-connect-hubspot").click();
  const modal = page.getByTestId("add-connection-modal");
  await modal.getByPlaceholder("pat-…").fill("pat-test");
  await modal.getByRole("button", { name: "Connect", exact: true }).click();
  await expect(page.getByTestId("ob-recipe")).toBeVisible({ timeout: 15_000 });

  // Connected but no channel → the gate names the missing piece (tester catch 2026-07-12).
  await expect(page.getByTestId("ob-create-hint")).toContainText("Pick a channel");

  // Channel picked BY NAME; §25 consent pre-checked; create lands on the task's detail with
  // the standing grant listed.
  const chan = page.locator('[data-testid="ob-channel"] input');
  await chan.click();
  await page.getByTestId("channel-suggestions").getByText("#ocw-test").click();
  await expect(chan).toHaveValue("#ocw-test");
  await expect(page.getByTestId("ob-consent")).toBeChecked();
  await page.getByTestId("ob-create").click();

  await expect(page.getByRole("button", { name: /Run now/ })).toBeVisible();
  await expect(page.getByText("Pipeline digest").first()).toBeVisible();
  await expect(page.getByTestId("task-grants")).toContainText("send_message");
});

test("Google connect narrates itself: browser handoff strip → Cancel restores the button", async ({
  page,
}) => {
  await openQuickstart(page);

  // Hold the Google login open (don't connect) so the waiting strip owns the gap until Cancel.
  await page.route("**/google/auth-start*", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ auth_url: "https://accounts.google.com/o/oauth2/v2/auth?mock", opened_browser: true }),
    });
  });

  // Brief needs Calendar + Gmail (both Google). Connect Calendar → the handoff strip appears.
  await page.getByTestId("qs-template-brief").click();
  await page.getByTestId("ob-connect-google_calendar").click();
  await expect(page.getByText("Waiting for Google Calendar…")).toBeVisible();
  await expect(page.getByTestId("ob-connect-wait")).toContainText(
    "Finish connecting Google Calendar in your browser",
  );

  // Cancel clears only the LOCAL waiting state — the Connect button returns.
  await page.getByTestId("ob-connect-cancel").click();
  await expect(page.getByTestId("ob-connect-wait")).toHaveCount(0);
  await expect(page.getByTestId("ob-connect-google_calendar")).toBeVisible();
});

test("read-only recipe (Morning brief) carries disclosure, not a grant", async ({ page }) => {
  await openQuickstart(page);
  await page.getByTestId("qs-template-brief").click();

  // Calendar + Gmail rows; no consent checkbox anywhere — reads never gate.
  await expect(page.getByText("Today's meetings and gaps")).toBeVisible();
  await expect(page.getByText("What arrived overnight")).toBeVisible();
  await expect(page.getByTestId("ob-consent")).toHaveCount(0);
});

test("no-connection template: When is editable and create opens the detail", async ({ page }) => {
  await openQuickstart(page);
  // The card says so on its face.
  await expect(page.getByTestId("qs-template-news")).toContainText("No connections needed");
  await page.getByTestId("qs-template-news").click();

  // No connect rows, no consent — just When (day × time) and an enabled Create.
  await expect(page.getByTestId("ob-consent")).toHaveCount(0);
  await expect(
    page.getByTestId("ob-recipe").getByRole("button", { name: "Day" }),
  ).toContainText("Every day");
  await expect(page.getByTestId("ob-create")).toBeEnabled();
  await page.getByTestId("ob-create").click();

  await expect(page.getByRole("button", { name: /Run now/ })).toBeVisible();
  await expect(page.getByText("Morning news briefing").first()).toBeVisible();
});
