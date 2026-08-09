// Settings ▸ Personas ▸ Gallery: the catalog lives in a screen-sized modal opened
// from the Personas page (link → featured carousel + list → in-modal solo page →
// informed install → Done lands back on Personas). Plus the page-level delete
// affordance for non-builtin personas.
import { expect } from "@playwright/test";
import { test } from "./fixtures";

async function openPersonas(page) {
  // Personas is launch-flagged off by default — these suites cover the flagged-on flows.
  await page.addInitScript(() => localStorage.setItem("ocw.flag.personas", "1"));
  await page.goto("/");
  await page.getByTestId("account-row").click();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByRole("button", { name: "Personas", exact: true }).click();
  await expect(page.getByTestId("gallery-link")).toBeVisible();
}

async function openGallery(page) {
  await openPersonas(page);
  await page.getByTestId("gallery-link").click();
  await expect(page.getByTestId("gallery-modal")).toBeVisible();
}

test("slow load: skeleton shows while the gallery loads, never a blank body", async ({
  page,
}) => {
  // The gallery feed can take seconds; delay it to assert the skeleton bridges the gap.
  await page.route("**/v1/cloud/gallery", async (route) => {
    await new Promise((r) => setTimeout(r, 1200));
    await route.fallback(); // reach the fixtures' /v1/** mock, not the network
  });
  await openGallery(page);
  await expect(page.getByTestId("gallery-loading")).toBeVisible();
  await expect(page.getByTestId("gallery-loading")).toContainText("Loading the gallery");
  // Resolves into the real body once the feed answers.
  await expect(page.getByTestId("gallery-featured")).toBeVisible({ timeout: 10_000 });
  await expect(page.getByTestId("gallery-loading")).toHaveCount(0);
});

test("the catalog loads straight in — no sign-in gate; manual install path present", async ({ page }) => {
  await openGallery(page);
  await expect(page.getByTestId("gallery-signin")).toHaveCount(0);
  await expect(page.getByTestId("gallery-featured")).toBeVisible({ timeout: 10_000 });
  // Esc closes; the Personas page (with its dir/Git importer) is still there.
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("gallery-modal")).not.toBeVisible();
  await expect(page.getByRole("button", { name: "Install", exact: true })).toBeVisible();
});

test("featured carousel + list; solo page installs informed; Done returns", async ({
  page,
}) => {
  await openGallery(page);

  // Featured carousel holds the flagged persona; the list holds both.
  const featured = page.getByTestId("gallery-featured");
  await expect(featured).toBeVisible({ timeout: 10_000 });
  await expect(featured).toContainText("Sales Coworker");
  await expect(featured).not.toContainText("Recruiter");
  await expect(page.getByTestId("gallery-recruiter")).toContainText("View & install");
  await expect(page.getByTestId("gallery-team-teaser")).toContainText("coming soon");

  // Search narrows the list.
  await page.getByPlaceholder("Search personas").fill("recruit");
  await expect(page.getByTestId("gallery-sales")).not.toBeVisible();
  await page.getByPlaceholder("Search personas").fill("");

  // Solo page: pitch + manifest-derived capabilities BEFORE install.
  await page.getByTestId("gallery-sales").click();
  const detail = page.getByTestId("gallery-detail");
  await expect(detail).toContainText("Walk into every call already knowing the account");
  const caps = page.getByTestId("gallery-capabilities");
  await expect(caps).toContainText("verified from its manifest");
  await expect(caps).toContainText("files, search, todo");
  await expect(caps).toContainText("hubspot · core");
  await expect(caps).toContainText("read deals and contacts");

  await detail.getByRole("button", { name: "Install" }).click();
  await expect(detail).toContainText("disabled until you approve and enable it");

  // Done closes the modal, landing back on the Personas page.
  await detail.getByRole("button", { name: "Done" }).click();
  await expect(page.getByTestId("gallery-modal")).not.toBeVisible();
  await expect(page.getByTestId("gallery-link")).toBeVisible();
});

test("back link returns from the solo page to the catalog", async ({ page }) => {
  await openGallery(page);
  await page.getByTestId("gallery-sales").click({ timeout: 10_000 });
  await expect(page.getByTestId("gallery-detail")).toBeVisible();
  await page.getByRole("button", { name: "← Gallery" }).click();
  await expect(page.getByTestId("gallery-cards")).toBeVisible();
});

test("delete: non-builtin personas removable after confirm; built-ins are not", async ({
  page,
}) => {
  await openPersonas(page);
  // Built-ins expose no delete affordance.
  await expect(page.getByTestId("persona-delete-cowork")).toHaveCount(0);
  // Non-builtin: trash → inline confirm → row gone (works signed out).
  await expect(page.getByText("Acme Notes")).toBeVisible();
  await page.getByTestId("persona-delete-acme-notes").click();
  await page.getByTestId("persona-delete-confirm-acme-notes").click();
  await expect(page.getByText("Acme Notes")).not.toBeVisible();
});
