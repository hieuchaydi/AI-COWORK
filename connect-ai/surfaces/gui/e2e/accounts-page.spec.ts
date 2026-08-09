// The generic multi-account detail page (AccountsDetail) + the modal's manual
// token form, exercised via Notion — the pattern all batch-2 connectors
// share (accounts.py layer: AccountRow shape, Default badge, per-account ×).
import { expect } from "@playwright/test";
import { test } from "./fixtures";

async function openConnectors(page) {
  await page.goto("/");
  await page.getByTestId("account-row").click();
  await page.getByRole("button", { name: "Connectors", exact: true }).click();
}

async function connectFirstWorkspace(page) {
  await openConnectors(page);
  // Available row → modal → the manual token form (the ONE flow — no cloud)
  await page
    .getByTestId("connector-notion")
    .getByRole("button", { name: "Connect", exact: true })
    .click();
  const modal = page.getByTestId("add-connection-modal");
  await modal.getByPlaceholder("ntn_…").fill("ntn_test");
  await modal.getByRole("button", { name: "Connect", exact: true }).click();
  await expect(page.getByTestId("connector-notion")).toContainText("Rohit's Workspace", {
    timeout: 10_000,
  });
}

test("manual connect, add a second workspace from the page; first stays default", async ({
  page,
}) => {
  await connectFirstWorkspace(page);
  await page.getByTestId("connector-notion").click();
  await expect(page.getByTestId("accounts-detail")).toBeVisible();

  // "+ Add account" reveals the manual form on the page itself
  await page.getByTestId("add-account-btn").click();
  const add = page.getByTestId("accounts-manual-add");
  await add.getByPlaceholder("ntn_…").fill("ntn_more");
  await add.getByRole("button", { name: "Connect", exact: true }).click();
  const first = page.getByTestId("account-ws-1");
  const second = page.getByTestId("account-ws-2");
  await expect(second).toBeVisible({ timeout: 10_000 });
  await expect(first).toContainText("Rohit's Workspace");
  await expect(first).toContainText("Default");
  await expect(second).not.toContainText("Default");
  // list row summarizes the multi-account state
  await page.getByTestId("connectors-breadcrumb").click();
  await expect(page.getByTestId("connector-notion")).toContainText("2 accounts");
});

test("Make default moves the badge; disconnecting the default repoints it", async ({
  page,
}) => {
  await connectFirstWorkspace(page);
  await page.getByTestId("connector-notion").click();
  await page.getByTestId("add-account-btn").click();
  const add2 = page.getByTestId("accounts-manual-add");
  await add2.getByPlaceholder("ntn_…").fill("ntn_more");
  await add2.getByRole("button", { name: "Connect", exact: true }).click();
  await expect(page.getByTestId("account-ws-2")).toBeVisible({ timeout: 10_000 });

  await page.getByTestId("account-make-default-ws-2").click();
  await expect(page.getByTestId("account-ws-2")).toContainText("Default");
  await expect(page.getByTestId("account-ws-1")).not.toContainText("Default");

  await page.getByTestId("account-disconnect-ws-2").click();
  await expect(page.getByTestId("account-ws-2")).toHaveCount(0);
  await expect(page.getByTestId("account-ws-1")).toContainText("Default");
});

test("the modal is the manual token form — no panes, no cloud sign-in", async ({
  page,
}) => {
  await openConnectors(page);
  await page
    .getByTestId("connector-notion")
    .getByRole("button", { name: "Connect", exact: true })
    .click();
  const modal = page.getByTestId("add-connection-modal");
  await expect(modal.getByPlaceholder("ntn_…")).toBeVisible();
  await expect(modal.getByTestId("modal-pane-manual")).toHaveCount(0);
  await expect(page.getByTestId("inline-cloud-sign-in")).toHaveCount(0);
});
