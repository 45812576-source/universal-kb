// frontend/e2e/fixtures.ts
import { test as base } from "@playwright/test";
import { login } from "./helpers";

export const test = base.extend<{ authedPage: any }>({
  authedPage: async ({ page }: { page: any }, use: (page: any) => Promise<void>) => {
    await login(page);
    await use(page);
  },
});

export { expect } from "@playwright/test";
