import { expect, test, type Page } from "@playwright/test";

async function signIn(page: Page, user: string, password: string) {
  await page.goto("/");
  await page.getByLabel("Username").fill(user);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByLabel("Main")).toBeVisible();
}

const nav = (page: Page) => page.getByLabel("Main");
const sub = (page: Page, name: string) => page.getByLabel("Administration sections").getByRole("button", { name });

async function adminPage(page: Page) {
  await signIn(page, "root", "root-pass");
  await nav(page).getByRole("button", { name: "Admin" }).click();
}

test("non-admins get no Admin tab and the API refuses them", async ({ page }) => {
  await signIn(page, "ann", "ann-pass");
  await expect(nav(page).getByRole("button", { name: "Admin" })).toHaveCount(0);
  await page.goto("/admin");
  await expect(page.getByText("Administrators only")).toBeVisible();
  const r = await page.request.get("/api/admin/config");
  expect(r.status()).toBe(403);
});

test("a folder change applies to a signed-in user without them signing in again", async ({ page, browser }) => {
  const other = await browser.newContext();
  const ann = await other.newPage();
  await signIn(ann, "ann", "ann-pass");
  await nav(ann).getByRole("button", { name: "Upload" }).click();
  expect(await ann.getByLabel("Folder").locator("option").allTextContents()).toEqual(["delivery", "sales"]);

  await adminPage(page);
  const group = () => page.getByRole("group", { name: /cn=sales/ });
  try {
    await group().getByLabel("delivery").uncheck();
    await group().getByRole("button", { name: "Save" }).click();
    await expect(page.locator(".ok")).toContainText("Saved folders");

    await ann.reload(); // same session cookie: only the page is reloaded
    await nav(ann).getByRole("button", { name: "Upload" }).click();
    expect(await ann.getByLabel("Folder").locator("option").allTextContents()).toEqual(["sales"]);
  } finally {
    // put it back for the other tests, even if the checks above failed
    await group().getByLabel("delivery").check();
    await group().getByRole("button", { name: "Save" }).click();
    await expect(page.locator(".ok")).toContainText("Saved folders");
    await other.close();
  }
});

test("a deny exception hides a folder and can be removed", async ({ page, browser }) => {
  const other = await browser.newContext();
  const ann = await other.newPage();
  await signIn(ann, "ann", "ann-pass");

  await adminPage(page);
  const row = () => page.getByRole("row", { name: /ann.*sales.*deny/ });
  try {
    const form = page.locator("form", { hasText: "Save exception" });
    await form.getByLabel("Username").fill("ann");
    await form.getByLabel("Folder").selectOption("sales");
    await form.getByLabel("Effect").selectOption("deny");
    await form.getByRole("button", { name: "Save exception" }).click();
    await expect(row()).toBeVisible();

    await ann.reload();
    await nav(ann).getByRole("button", { name: "Upload" }).click();
    expect(await ann.getByLabel("Folder").locator("option").allTextContents()).toEqual(["delivery"]);
  } finally {
    if (await row().count()) await row().getByRole("button", { name: "Remove" }).click();
    await expect(row()).toHaveCount(0);
    await other.close();
  }
});

test("document types can be added, are validated, and deleted", async ({ page }) => {
  await adminPage(page);
  await sub(page, "Document types").click();
  await expect(page.getByRole("row", { name: /^SOW.*statement of work/ })).toBeVisible(); // seeded defaults
  await page.getByLabel("Type name").fill("1bad");
  await page.getByLabel("Words and phrases").fill("lease");
  await page.getByRole("button", { name: "Save type" }).click();
  await expect(page.getByRole("alert")).toContainText("type name");

  await page.getByLabel("Type name").fill("lease");
  await page.getByLabel("Words and phrases").fill("Lease, tenancy agreement");
  await page.getByRole("button", { name: "Save type" }).click();
  await expect(page.getByRole("row", { name: /LEASE.*lease, tenancy agreement/ })).toBeVisible();
  await page.getByRole("row", { name: /LEASE/ }).getByRole("button", { name: "Delete" }).click();
  await expect(page.getByRole("row", { name: /LEASE/ })).toHaveCount(0);
  await expect(page.getByRole("row", { name: /^SOW/ })).toBeVisible();
});

test("the index screen shows counts and a manual scan adds a run", async ({ page }) => {
  await adminPage(page);
  await sub(page, "Index").click();
  await expect(page.getByText("documents searchable")).toBeVisible();
  const rows = page.locator("table").first().locator("tbody tr");
  const before = await rows.count();
  await page.getByRole("button", { name: "Scan now" }).click();
  await expect(async () => {
    expect(await rows.count()).toBeGreaterThan(before);
  }).toPass({ timeout: 15_000 });
});

test("questions and the audit log are visible to admins, with thumbs-down export", async ({ page, browser }) => {
  const other = await browser.newContext();
  const ann = await other.newPage();
  await signIn(ann, "ann", "ann-pass");
  await ann.getByLabel("Your question").fill("Orion SOW SR-5000 scope");
  await ann.locator(".composer").getByRole("button", { name: "Ask" }).click();
  await ann.locator(".picker").getByRole("button").first().click();
  await ann.getByRole("button", { name: "Not helpful" }).click();
  await expect(ann.getByRole("button", { name: "Not helpful" })).toHaveAttribute("aria-pressed", "true");

  await adminPage(page);
  await sub(page, "Questions").click();
  await page.getByLabel("Show").selectOption("-1");
  await expect(page.getByText("Orion SOW SR-5000 scope").first()).toBeVisible();
  await expect(page.getByRole("link", { name: /Export thumbs-down/ })).toHaveAttribute(
    "href",
    "/api/admin/qa/export?feedback=-1",
  );

  await sub(page, "Audit log").click();
  await page.getByLabel("Action").selectOption("login.ok");
  await expect(page.getByRole("row", { name: /ann.*login\.ok/ }).first()).toBeVisible();
  await other.close();
});

test("disabling a user ends their session at once", async ({ page, browser }) => {
  const other = await browser.newContext();
  const ann = await other.newPage();
  await signIn(ann, "ann", "ann-pass");

  await adminPage(page);
  await sub(page, "Users").click();
  const row = page.getByRole("row", { name: /^ann/ });
  await expect(row).toContainText("active");
  await row.getByRole("button", { name: "Disable" }).click();
  await expect(row).toContainText("disabled");

  await ann.getByLabel("Your question").fill("Anything at all here");
  await ann.locator(".composer").getByRole("button", { name: "Ask" }).click();
  await expect(ann.getByText("Your session ended")).toBeVisible();

  await row.getByRole("button", { name: "Enable" }).click();
  await expect(row).toContainText("active");
  await other.close();
});
