// The GitHub detail page (github-relay-spec §8): one group per App INSTALLATION
// with People / Waiting rows and a per-installation disconnect, add-installation
// via the header MODAL (personal access token — cloud one-click removed), and the
// park → allow & deliver flow that admits a new sender login into the allow-list.
import { expect } from "@playwright/test";
import { test } from "./fixtures";

async function openGithubPage(page) {
  await page.goto("/");
  await page.getByTestId("account-row").click();
  await page.getByRole("button", { name: "Connectors", exact: true }).click();
  await page.getByTestId("connector-github").click();
}

async function addInstallationViaToken(page) {
  await page.getByTestId("add-installation-btn").click();
  const modal = page.getByTestId("add-connection-modal");
  await modal.locator("input[type=password]").fill("ghp_test");
  await modal.getByRole("button", { name: "Connect", exact: true }).click();
}

test("lists each installation as its own group with people and waiting rows", async ({
  page,
}) => {
  await openGithubPage(page);
  const group = page.getByTestId("github-install-101");
  await expect(group).toContainText("acme");
  await expect(group).toContainText("selected repos"); // repo consent is GitHub-native
  await expect(group).toContainText("@rohit-dev"); // logins ARE the readable identity
  // the parked mention files under ITS installation, quoting the trigger
  await expect(group).toContainText("@maya-dev");
  await expect(group).toContainText("please take a look");
});

test("allow & deliver admits the sender into that installation's list", async ({
  page,
}) => {
  await openGithubPage(page);
  await page.getByTestId("parked-allow-deliver-gh-pk1").click();
  const group = page.getByTestId("github-install-101");
  await expect(group).toContainText("@maya-dev"); // now a People chip
  await expect(page.getByTestId("waiting-gh-pk1")).toHaveCount(0);
});

test("add installation opens the token modal; connecting installs a second org", async ({
  page,
}) => {
  await openGithubPage(page);
  await page.getByTestId("add-installation-btn").click();
  const modal = page.getByTestId("add-connection-modal");
  // Personal access token is the only connect path now — no cloud one-click pane.
  await expect(modal.locator("input[type=password]")).toBeVisible();
  await expect(modal.getByTestId("modal-install-github-app")).toHaveCount(0);
  await page.keyboard.press("Escape");

  await addInstallationViaToken(page);
  // the mock connects instantly; the page's poll shows the new installation
  await expect(page.getByTestId("github-install-202")).toContainText("hooli", {
    timeout: 10_000,
  });
  await expect(page.getByTestId("github-install-202")).toContainText("all repos");
  await expect(page.getByTestId("github-install-101")).toBeVisible(); // existing stays
});

test("disconnect removes one installation and keeps the rest", async ({ page }) => {
  await openGithubPage(page);
  await addInstallationViaToken(page);
  await expect(page.getByTestId("github-install-202")).toBeVisible({ timeout: 10_000 });
  await page.keyboard.press("Escape"); // the modal never auto-closes (by design)

  await page.getByTestId("disconnect-install-202").click();
  await expect(page.getByTestId("github-install-202")).toHaveCount(0);
  await expect(page.getByTestId("github-install-101")).toBeVisible();
});
