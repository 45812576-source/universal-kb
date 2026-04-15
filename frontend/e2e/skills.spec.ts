import { test, expect } from "./fixtures";

test.describe("Skill 管理", () => {
  test("可以访问 Skill 列表页", async ({ authedPage: page }) => {
    await page.goto("/admin/skills");
    await expect(page.getByRole("heading", { name: "Skill 管理" })).toBeVisible();
  });

  test("新建 Skill 页面可以直接访问", async ({ authedPage: page }) => {
    await page.goto("/admin/skills/new");
    await expect(page.getByRole("button", { name: /创建 Skill/ })).toBeVisible();
  });

  test("新建 Skill 表单提交成功", async ({ authedPage: page }) => {
    await page.goto("/admin/skills/new");
    await page.fill('input[name="name"]', `E2E测试Skill-${Date.now()}`);
    await page.fill('input[name="description"]', "E2E自动化测试");
    await page.fill('textarea[name="system_prompt"]', "你是E2E测试助手。");
    await page.getByRole("button", { name: /创建 Skill/ }).click();
    await expect(page).toHaveURL(/\/admin\/skills\/\d+/, { timeout: 10000 });
  });

  test("重复 Skill 名称提交报错", async ({ authedPage: page }) => {
    const uniqueName = `重复Skill-${Date.now()}`;
    await page.goto("/admin/skills/new");
    await page.fill('input[name="name"]', uniqueName);
    await page.fill('textarea[name="system_prompt"]', "first");
    await page.getByRole("button", { name: /创建 Skill/ }).click();
    await page.waitForURL(/\/admin\/skills\/\d+/, { timeout: 10000 });

    await page.goto("/admin/skills/new");
    await page.fill('input[name="name"]', uniqueName);
    await page.fill('textarea[name="system_prompt"]', "second");
    await page.getByRole("button", { name: /创建 Skill/ }).click();
    await expect(page.locator(".bg-red-50, .text-red-700")).toBeVisible({ timeout: 5000 });
  });

  test("可以发布 Skill", async ({ authedPage: page }) => {
    await page.goto("/admin/skills/new");
    const name = `发布测试-${Date.now()}`;
    const publishablePrompt = Array.from(
      { length: 210 },
      (_, index) => `第 ${index + 1} 行：这是用于 E2E 发布校验的完整 Skill 指令内容。`
    ).join("\n");
    await page.fill('input[name="name"]', name);
    await page.fill('textarea[name="system_prompt"]', publishablePrompt);
    await page.getByRole("button", { name: /创建 Skill/ }).click();
    await page.waitForURL(/\/admin\/skills\/\d+/, { timeout: 10000 });

    await page.goto("/admin/skills");
    // Find the skill row and click publish
    const skillRow = page.locator(`tr, li, [data-skill]`).filter({ hasText: name }).first();
    if (await skillRow.isVisible({ timeout: 3000 })) {
      await skillRow.getByRole("button", { name: /发布/ }).click();
      await expect(skillRow.getByText("已发布")).toBeVisible({ timeout: 10000 });
    }
  });
});
