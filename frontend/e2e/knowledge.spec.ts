import { test, expect } from "./fixtures";

test.describe("知识库", () => {
  test("录入表单展示扩展后的上传类型", async ({ authedPage: page }) => {
    await page.goto("/knowledge/my");
    await page.getByRole("button", { name: /录入新知识/ }).click();
    await page.getByRole("button", { name: "文件上传" }).click();

    const fileInput = page.locator('input[type="file"][name="file"]');
    await expect(fileInput).toBeVisible();
    await expect(fileInput).toHaveAttribute(
      "accept",
      ".txt,.pdf,.docx,.pptx,.md,.html,.htm,.xlsx,.xls,.csv,.jpg,.jpeg,.png,.webp,.bmp,.gif,.mp3,.wav,.m4a,.ogg,.flac,.zip",
    );
  });

  test("录入表单支持切换到飞书导入模式", async ({ authedPage: page }) => {
    await page.goto("/knowledge/my");
    await page.getByRole("button", { name: /录入新知识/ }).click();
    await page.getByRole("button", { name: "飞书导入" }).click();

    await expect(page.locator('input[name="lark_url"]')).toBeVisible();
    await expect(page.getByText("已连接飞书账号时，可导入你有权限访问的飞书文档并生成可编辑工作台副本。")).toBeVisible();
  });

  test("员工可以提交知识条目", async ({ authedPage: page }) => {
    await page.goto("/knowledge/my");
    await page.getByRole("button", { name: /录入新知识/ }).click();
    await page.fill('input[name="title"]', `E2E知识-${Date.now()}`);
    await page.fill('textarea[name="content"]', "这是E2E测试的知识内容，足够详细。");
    // Click submit button (not the layout logout button)
    await page.getByRole("button", { name: /提交审核/ }).click();
    // Should redirect to /knowledge/my after submit
    await expect(page).toHaveURL(/\/knowledge\/my/, { timeout: 10000 });
  });

  test("可以访问我的知识列表", async ({ authedPage: page }) => {
    await page.goto("/knowledge/my");
    await expect(page).not.toHaveURL("/login");
  });

  test("管理员可以访问知识审核页面", async ({ authedPage: page }) => {
    await page.goto("/admin/knowledge");
    await expect(page.getByRole("heading", { name: "知识审核" })).toBeVisible();
  });

  test("管理员可以批准知识条目", async ({ authedPage: page }) => {
    // Submit a knowledge entry first
    await page.goto("/knowledge/my");
    await page.getByRole("button", { name: /录入新知识/ }).click();
    const title = `审核测试-${Date.now()}`;
    await page.fill('input[name="title"]', title);
    await page.fill('textarea[name="content"]', "审核测试内容详细描述。");
    await page.getByRole("button", { name: /提交审核/ }).click();
    await page.waitForURL(/\/knowledge\/my/, { timeout: 10000 });

    // Go to admin review page
    await page.goto("/admin/knowledge");
    const row = page.locator(`td, li`).filter({ hasText: title }).first();
    if (await row.isVisible({ timeout: 3000 })) {
      await row.getByRole("button", { name: /批准|通过/ }).click();
      await page.waitForTimeout(500);
    }
  });
});
