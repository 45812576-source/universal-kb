import { test, expect } from "./fixtures";

test.describe("改进建议", () => {
  test("可以访问我的建议列表", async ({ authedPage: page }) => {
    await page.goto("/suggestions/my");
    await expect(page).not.toHaveURL("/login");
  });

  test("提交建议页面可访问", async ({ authedPage: page }) => {
    await page.goto("/suggestions/new");
    await expect(page).not.toHaveURL("/login");
  });
});
