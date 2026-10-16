const { test, expect } = require('@playwright/test');
const fs = require('node:fs');
const credentials = process.env.TEST_PASSWORD
  ? { username: process.env.TEST_USERNAME || 'admin', password: process.env.TEST_PASSWORD }
  : JSON.parse(fs.readFileSync('.local-access.json', 'utf8'));
async function login(page) {
  await page.goto('/login');
  await page.getByLabel('Имя пользователя').fill(credentials.username);
  await page.getByLabel('Пароль', { exact: true }).fill(credentials.password);
  await page.getByRole('button', { name: 'Войти в Докосмотр' }).click();
  await expect(page.getByRole('heading', { name: 'Всё под контролем.' })).toBeVisible();
}

test('real server: login, metrics, search, details, logs and logout', async ({ page }) => {
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await login(page);
  await expect(page.locator('#connection')).toHaveText('Live', { timeout: 20000 });
  await expect(page.locator('#metric-total')).not.toHaveText('—');
  if (process.env.CI) {
    const snapshot = await (await page.request.get('/api/snapshot')).json();
    expect(snapshot.stats.scope).toBe('linux-host');
    expect(snapshot.stats_error).toBeNull();
    expect(
      snapshot.containers.some((c) => c.name === 'dokosmotr-ci-stopped' && !c.running)
    ).toBeTruthy();
  }
  await page.locator('#search').fill('dokosmotr-preview');
  await expect(page.locator('#rows tr')).toHaveCount(1);
  await page.getByRole('button', { name: 'dokosmotr-preview', exact: true }).click();
  await expect(page.locator('#details')).toBeVisible();
  await expect(page.locator('#action-note')).toContainText('защищён');
  await expect(page.locator('#action-stop')).toBeDisabled();
  await page.getByRole('button', { name: 'Открыть логи', exact: true }).click();
  await expect(page.locator('#log-status')).toContainText('Снимок журнала');
  await page.keyboard.press('Escape');
  await page.keyboard.press('Escape');
  await page.locator('#search').fill('there-is-no-such-container');
  await expect(page.getByRole('heading', { name: 'Ничего не нашлось' })).toBeVisible();
  await page.getByRole('button', { name: 'Сбросить фильтры' }).click();
  await page.getByRole('button', { name: 'Выйти из аккаунта' }).click();
  await page.locator('#confirm-submit').click();
  await expect(page).toHaveURL(/\/login$/);
  expect(errors).toEqual([]);
});

test('mobile layout, theme and accessible controls', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)
  ).toBeTruthy();
  const old = await page.locator('html').getAttribute('data-theme');
  await page.getByRole('button', { name: 'Переключить тему' }).click();
  await expect(page.locator('html')).not.toHaveAttribute('data-theme', old);
  await page.screenshot({ path: 'test-results/mobile.png', fullPage: true });
});

test('untrusted names stay text; filter persists across updates; outage is visible', async ({
  page,
}) => {
  await page.routeWebSocket('**/socket.io/**', (socket) => socket.close());
  await login(page);
  const fixture = await (await page.request.get('/api/snapshot')).json();
  fixture.containers = [
    {
      ...fixture.containers[0],
      id: 'f'.repeat(64),
      name: '<img src=x onerror=alert(1)>',
      image: 'test:latest',
      state: 'running',
      running: true,
      health: 'unhealthy',
      project: 'qa',
      protected: false,
    },
  ];
  await page.route('**/api/snapshot', (route) => route.fulfill({ json: fixture }));
  await page.locator('#refresh').click();
  await expect(page.locator('#rows tr')).toHaveCount(1);
  await expect(page.locator('.container-name')).toHaveText('<img src=x onerror=alert(1)>');
  await expect(page.locator('#rows img')).toHaveCount(0);
  await page.getByRole('button', { name: /Требуют внимания/ }).click();
  await page.locator('#search').fill('test:latest');
  await page.locator('#refresh').click();
  await expect(page.locator('#rows tr')).toHaveCount(1);
  await page.locator('.container-name').click();
  await expect(page.locator('#action-stop')).toBeDisabled();
  await page.keyboard.press('Escape');
  fixture.docker_available = false;
  fixture.error = 'Нет связи с Docker. Показаны последние полученные данные';
  await page.locator('#refresh').click();
  await expect(page.locator('#banner')).toContainText('Нет связи');
  fixture.docker_available = true;
  fixture.error = null;
  fixture.timestamp = new Date(Date.now() - 120000).toISOString();
  await page.locator('#refresh').click();
  await expect(page.locator('#connection')).toHaveText('Нет свежих данных');
});
