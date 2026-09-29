import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page } from "@playwright/test";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DOCS = path.resolve(HERE, "../../backend/.e2e-docs");
const QUESTION = "Boost Connect SR-1098 SOW — what is the scope of work mainly about?";
const V2 = "Boost Connect SR-1098 SOW v2 FINAL.docx";

async function signIn(page: Page, user: string, password: string) {
  await page.goto("/");
  await page.getByLabel("Username").fill(user);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
}

async function ask(page: Page, question: string) {
  await page.getByLabel("Your question").fill(question);
  await page.locator(".composer").getByRole("button", { name: "Ask" }).click();
}

test("gate: sign in, ask the SR-1098 question, get a cited answer, download the right file", async ({
  page,
}) => {
  await signIn(page, "ann", "ann-pass");
  await expect(page.getByText("Ann Sales")).toBeVisible();
  await ask(page, QUESTION);

  const answer = page.locator(".answer");
  await expect(answer).toBeVisible();
  await expect(answer.locator(".cite").first()).toHaveText("[1]");
  const card = page.locator(".source", { hasText: V2 });
  await expect(card).toBeVisible();
  await expect(card.locator(".badge")).toHaveText("sales");

  const [download] = await Promise.all([
    page.waitForEvent("download"),
    card.getByRole("link", { name: "Download" }).click(),
  ]);
  expect(download.suggestedFilename()).toBe(V2);
  const saved = await download.path();
  const onDisk = readFileSync(path.join(DOCS, "sales", V2));
  expect(createHash("sha256").update(readFileSync(saved)).digest("hex")).toBe(
    createHash("sha256").update(onDisk).digest("hex"),
  );
});

test("a wrong password shows an error and stays on the sign-in page", async ({ page }) => {
  await signIn(page, "ann", "wrong");
  await expect(page.getByRole("alert")).toHaveText(/Invalid username or password/);
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
});

test("a user cannot see or fetch another folder's documents", async ({ page, browser }) => {
  // Ann (sales) finds the SOW and its download link.
  await signIn(page, "ann", "ann-pass");
  await ask(page, QUESTION);
  const href = await page
    .locator(".source", { hasText: V2 })
    .getByRole("link", { name: "Download" })
    .getAttribute("href");
  expect(href).toMatch(/^\/api\/documents\/[0-9a-f-]+\/download$/);

  // Lee (legal only) never sees the sales SOW: whatever the (fake) model answers is drawn from
  // legal documents alone, and the same download link is refused.
  const other = await browser.newContext();
  const lee = await other.newPage();
  await signIn(lee, "lee", "lee-pass");
  await ask(lee, QUESTION);
  await expect(lee.locator(".answer")).toBeVisible();
  await expect(lee.locator(".source", { hasText: V2 })).toHaveCount(0);
  await expect(lee.locator(".answer")).not.toContainText("Boost Connect SR-1098");
  for (const badge of await lee.locator(".source .badge").allTextContents()) {
    expect(badge).toBe("legal");
  }
  const forbidden = await lee.request.get(href as string);
  expect(forbidden.status()).toBe(403);
  await other.close();
});

test("ambiguous names give a picker limited to visible documents", async ({ page }) => {
  await signIn(page, "ann", "ann-pass");
  await ask(page, "Orion SOW SR-5000 scope");
  const picker = page.locator(".picker");
  await expect(picker).toBeVisible();
  await expect(picker.getByRole("button")).toHaveCount(2);
  await picker.getByRole("button").first().click();
  await expect(page.locator(".answer .cite").first()).toBeVisible();
  await expect(page.locator(".chip")).toContainText("Orion SOW SR-5000.docx");
  await page.getByRole("button", { name: "Stop asking about this document" }).click();
  await expect(page.locator(".chip")).toHaveCount(0);
});

test("feedback toggles and the session can be ended", async ({ page }) => {
  await signIn(page, "ann", "ann-pass");
  await ask(page, QUESTION);
  const up = page.getByRole("button", { name: "Helpful", exact: true });
  await up.click();
  await expect(up).toHaveAttribute("aria-pressed", "true");
  await up.click();
  await expect(up).toHaveAttribute("aria-pressed", "false");

  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
});

test("upload a document, then find it by what it says", async ({ page }) => {
  await signIn(page, "ann", "ann-pass");
  await page.getByLabel("Main").getByRole("button", { name: "Upload" }).click();
  await page.getByLabel("Folder").selectOption("sales");
  await page
    .getByLabel(/^File/)
    .setInputFiles(path.resolve(HERE, "fixtures/Harbour Dredging HD-7421 SOW.docx"));
  await page.getByRole("button", { name: "Upload", exact: true }).last().click();
  await expect(page.getByRole("status")).toContainText("It is searchable now");

  await page.getByLabel("Main").getByRole("button", { name: "Ask" }).click();
  await ask(page, "Harbour Dredging HD-7421 SOW - what is the scope of work?");
  await expect(page.locator(".source", { hasText: "Harbour Dredging HD-7421 SOW.docx" })).toBeVisible();
  await expect(page.locator(".answer")).toContainText("dredge the north channel");
});

test("a legal-only user only sees legal as an upload target", async ({ page }) => {
  await signIn(page, "lee", "lee-pass");
  await page.getByLabel("Main").getByRole("button", { name: "Upload" }).click();
  const options = await page.getByLabel("Folder").locator("option").allTextContents();
  expect(options).toEqual(["legal"]);
});
