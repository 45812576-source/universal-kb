// frontend/e2e/helpers.ts
import { Page } from "@playwright/test";

export async function login(page: Page, username = "admin", password = "admin123") {
  await page.goto("/login");
  await page.fill('input[name="username"]', username);
  await page.fill('input[name="password"]', password);
  await page.click('button[type="submit"]');
  await page.waitForURL("/");
}

export async function logout(page: Page) {
  await page.goto("/logout");
}
