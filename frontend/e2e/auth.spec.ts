import { test, expect } from "@playwright/test";
import { login, logout } from "./helpers";

test.describe("Auth", () => {
  test("登录成功跳转到主页", async ({ page }) => {
    await login(page);
    await expect(page).toHaveURL("/");
    // 应该能看到侧边栏
    await expect(page.locator("aside").first()).toBeVisible();
  });

  test("密码错误显示报错", async ({ page }) => {
    await page.goto("/login");
    await page.fill('input[name="username"]', "admin");
    await page.fill('input[name="password"]', "wrongpassword");
    await page.click('button[type="submit"]');
    await expect(page.locator("text=用户名或密码错误")).toBeVisible();
    await expect(page).toHaveURL("/login");
  });

  test("未登录访问根路径跳转到 login", async ({ page }) => {
    await page.context().clearCookies();
    await page.goto("/");
    await expect(page).toHaveURL("/login");
  });

  test("已登录访问 /login 跳转到主页", async ({ page }) => {
    await login(page);
    await page.goto("/login");
    await expect(page).toHaveURL("/");
  });

  test("登出后无法访问保护页面", async ({ page }) => {
    await login(page);
    // POST to /logout
    await page.evaluate(async () => {
      const form = document.createElement("form");
      form.method = "POST";
      form.action = "/logout";
      document.body.appendChild(form);
      form.submit();
    });
    await page.waitForURL("/login");
    await page.goto("/admin/skills");
    await expect(page).toHaveURL("/login");
  });
});
