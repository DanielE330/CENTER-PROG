// Run from .tooling/frontend (it has @playwright/test):
//   node ../../tests/ui/ui-tour.mjs <screenshots dir> deploy/dev-secrets/demo_password
// Visual smoke test of the running stack: real login through Keycloak, screenshots per screen.
import { chromium } from "@playwright/test";
import { readFileSync } from "node:fs";

const out = process.argv[2];
const password = readFileSync(process.argv[3], "utf8").trim();
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ ignoreHTTPSErrors: true, viewport: { width: 1360, height: 900 } });
const errors = [];
page.on("pageerror", (e) => errors.push(String(e)));
page.on("console", (m) => m.type() === "error" && errors.push(m.text()));

await page.goto("https://localhost:8443/");
await page.waitForSelector("#username", { timeout: 30000 });
await page.fill("#username", "owner@demo-a.ci1.local");
if (!(await page.isVisible("#password"))) await page.click("#kc-login");
await page.waitForSelector("#password", { timeout: 15000 });
await page.fill("#password", password);
await page.click("#kc-login");
await page.waitForSelector("text=Остаток по дням", { timeout: 60000 });
await page.waitForTimeout(1500);
await page.screenshot({ path: `${out}/1-dashboard.png`, fullPage: true });
for (const [hash, name, wait] of [
  ["operations", "2-operations", "text=Категория"],
  ["obligations", "3-obligations", "text=Ожидаются"],
  ["settings", "6-settings", "text=Режим налогообложения"],
]) {
  await page.goto(`https://localhost:8443/#${hash}`);
  await page.waitForSelector(wait, { timeout: 30000 });
  await page.waitForTimeout(800);
  await page.screenshot({ path: `${out}/${name}.png`, fullPage: true });
}
await page.goto("https://localhost:8443/#scenarios");
await page.click("text=Клиенты платят позже");
await page.click("text=Посчитать");
await page.waitForSelector("text=В сценарии", { timeout: 60000 });
await page.waitForTimeout(800);
await page.screenshot({ path: `${out}/4-scenarios.png`, fullPage: true });
console.log(JSON.stringify({ errors }));
await browser.close();
