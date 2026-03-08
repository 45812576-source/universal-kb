import { test, expect } from "@playwright/test";

test("debug: click generate link", async ({ browser }) => {
  const context = await browser.newContext({ baseURL: "http://localhost:5173" });
  const page = await context.newPage();

  // Collect all JS errors
  const errors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") errors.push(`[console.error] ${msg.text()}`);
  });
  page.on("pageerror", (err) => errors.push(`[pageerror] ${err.message}`));

  // Login
  await page.goto("/login");
  await page.fill('input[name="username"]', "admin");
  await page.fill('input[name="password"]', "admin123");
  await page.click('button[type="submit"]');
  await page.waitForURL("**/chat**", { timeout: 10000 });

  // Go to business-tables
  await page.goto("/admin/business-tables");
  await page.waitForLoadState("networkidle");
  await page.waitForTimeout(2000); // let hydration complete

  console.log("=== JS errors on business-tables page ===");
  errors.forEach((e) => console.log(e));

  // Click the link
  const link = page.locator('a[href="/admin/business-tables/generate"]').first();
  await expect(link).toBeVisible();

  console.log("=== Clicking link ===");
  const errCountBefore = errors.length;
  await link.click();

  // Wait and check
  await page.waitForTimeout(3000);
  const newErrors = errors.slice(errCountBefore);
  console.log("=== JS errors after click ===");
  newErrors.forEach((e) => console.log(e));
  console.log("=== Current URL ===", page.url());

  expect(page.url()).toContain("/generate");

  await context.close();
});
