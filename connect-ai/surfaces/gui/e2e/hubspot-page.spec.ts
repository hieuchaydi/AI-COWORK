// The HubSpot detail page (M3.6 Step 4, UX-DECISIONS §21): multi-portal with
// Default/Sandbox/access tags, the add-modal's private-app token form (cloud
// one-click removed — token is the only connect path), and the hidden-fields denylist.
import { expect } from "@playwright/test";
import { test } from "./fixtures";

async function openConnectors(page) {
  await page.goto("/");
  await page.getByTestId("account-row").click();
  await page.getByRole("button", { name: "Connectors", exact: true }).click();
}

async function connectFirstPortal(page) {
  await page.getByTestId("connector-hubspot").getByRole("button", { name: "Connect" }).click();
  const modal = page.getByTestId("add-connection-modal");
  await modal.getByPlaceholder("pat-…").fill("pat-test");
  await modal.getByRole("button", { name: "Connect", exact: true }).click();
  await expect(page.getByTestId("connector-hubspot")).toContainText("Acme Inc", {
    timeout: 10_000,
  });
}

test("connect via the token form; the portal lands as default read-only", async ({
  page,
}) => {
  await openConnectors(page);
  await connectFirstPortal(page);
  await page.getByTestId("connector-hubspot").click();
  const row = page.getByTestId("hubspot-portal-111");
  await expect(row).toContainText("Default");
  await expect(page.getByTestId("hubspot-access-tag-111")).toContainText("read-only");
});

test("the modal is the private-app token form — no cloud one-click", async ({
  page,
}) => {
  await openConnectors(page);
  await page.getByTestId("connector-hubspot").getByRole("button", { name: "Connect" }).click();
  const modal = page.getByTestId("add-connection-modal");
  await expect(modal.getByPlaceholder("pat-…")).toBeVisible();
  await expect(modal.getByTestId("modal-connect-hubspot")).toHaveCount(0);
  await expect(modal.getByTestId("hubspot-access-read")).toHaveCount(0);
});

test("second portal: sandbox tag, make-default, disconnect repoints", async ({ page }) => {
  await openConnectors(page);
  await connectFirstPortal(page);
  await page.getByTestId("connector-hubspot").click();

  // add the sandbox portal from the page's header button (reveals the token form)
  await page.getByTestId("add-portal-btn").click();
  const modal = page.getByTestId("add-connection-modal");
  await modal.getByPlaceholder("pat-…").fill("pat-sandbox");
  await modal.getByRole("button", { name: "Connect", exact: true }).click();
  const sandbox = page.getByTestId("hubspot-portal-222");
  await expect(sandbox).toContainText("Sandbox", { timeout: 10_000 });

  await page.getByTestId("hubspot-make-default-222").click();
  await expect(sandbox).toContainText("Default");
  await page.getByTestId("hubspot-disconnect-222").click();
  await expect(page.getByTestId("hubspot-portal-222")).toHaveCount(0);
  await expect(page.getByTestId("hubspot-portal-111")).toContainText("Default");
});

test("hidden fields round-trip and read back normalized", async ({ page }) => {
  await openConnectors(page);
  await connectFirstPortal(page);
  await page.getByTestId("connector-hubspot").click();

  const row = page.getByTestId("hubspot-hidden-fields");
  await row.getByRole("textbox").fill("Salary");
  await row.getByRole("textbox").press("Enter");
  await expect(row).toContainText("salary"); // normalized lowercase from the PATCH echo
  await row.getByTitle("remove").click();
  await expect(row).not.toContainText("salary");
});
