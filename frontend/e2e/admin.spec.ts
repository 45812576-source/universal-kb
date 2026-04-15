import { test, expect } from "./fixtures";

test.describe("管理后台", () => {
  test("可以访问模型配置页", async ({ authedPage: page }) => {
    await page.goto("/admin/models");
    await expect(page.getByRole("heading", { name: "模型配置" })).toBeVisible();
  });

  test("可以访问审计日志页", async ({ authedPage: page }) => {
    await page.goto("/admin/audit");
    await expect(page.getByRole("heading", { name: /审计日志/ })).toBeVisible();
  });

  test("可以访问贡献统计页", async ({ authedPage: page }) => {
    await page.goto("/admin/contributions");
    await expect(page.getByRole("heading", { name: "贡献统计" })).toBeVisible();
  });

  test("可以访问 Studio 监控页并看到导出入口", async ({ authedPage: page }) => {
    await page.goto("/admin/studio-metrics");
    await expect(page.getByRole("heading", { name: "Skill Studio 监控" })).toBeVisible();
    await expect(page.getByRole("button", { name: "导出 CSV" })).toBeVisible();
    await expect(page.getByText("First_Useful_Response")).toBeVisible();
  });

  test("可以访问情报管理页", async ({ authedPage: page }) => {
    await page.goto("/admin/intel");
    await expect(page).not.toHaveURL("/login");
  });

  test("可以访问工具管理页", async ({ authedPage: page }) => {
    await page.goto("/admin/tools");
    await expect(page).not.toHaveURL("/login");
  });

  test("创建模型配置", async ({ authedPage: page }) => {
    await page.goto("/admin/models");
    const createBtn = page.getByRole("button", { name: /新建|新增|添加/ }).first();
    if (await createBtn.isVisible()) {
      await createBtn.click();
      await expect(page.locator("input[name='name'], input[placeholder*='名称']").first()).toBeVisible();
    }
  });
});
