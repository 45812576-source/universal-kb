import { test, expect } from "./fixtures";

test.describe("业务数据表", () => {
  test("可以访问业务数据表列表", async ({ authedPage: page }) => {
    await page.goto("/admin/business-tables");
    await expect(page.getByRole("heading", { name: /业务数据表/ })).toBeVisible();
  });

  test("生成页面可以直接访问", async ({ authedPage: page }) => {
    await page.goto("/admin/business-tables/generate");
    await expect(page.getByRole("heading", { name: /生成新数据表/ })).toBeVisible();
  });

  test("生成页显示两个模式切换按钮", async ({ authedPage: page }) => {
    await page.goto("/admin/business-tables/generate");
    await expect(page.getByRole("button", { name: /方向A/ })).toBeVisible();
    await expect(page.getByRole("button", { name: /方向B/ })).toBeVisible();
  });

  test("空描述不能提交", async ({ authedPage: page }) => {
    await page.goto("/admin/business-tables/generate");
    const btn = page.getByRole("button", { name: "生成预览" });
    await expect(btn).toBeDisabled();
  });
});
