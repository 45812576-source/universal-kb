import { test, expect } from "./fixtures";

test.describe("对话聊天", () => {
  test("主页显示选择对话提示", async ({ authedPage: page }) => {
    await page.goto("/");
    // Main chat index shows placeholder to select or create a conversation
    await expect(page.getByText(/选择一个对话|新建对话/)).toBeVisible({ timeout: 5000 });
  });

  test("对话列表侧边栏可见", async ({ authedPage: page }) => {
    await page.goto("/");
    await expect(page.locator("aside").first()).toBeVisible({ timeout: 3000 });
  });

  test("可以通过侧边栏导航到对话入口", async ({ authedPage: page }) => {
    await page.goto("/");
    // Check that the sidebar link to chat exists
    const chatLink = page.getByRole("link", { name: /对话/ }).first();
    await expect(chatLink).toBeVisible();
  });
});
