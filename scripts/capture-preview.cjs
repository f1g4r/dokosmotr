// README illustration uses explicitly synthetic names/metrics, never private server data.
const { chromium } = require('@playwright/test');
const fs = require('node:fs');
(async () => {
  const browser = await chromium.launch({ channel: process.env.TEST_BROWSER_CHANNEL || undefined });
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1100 },
    colorScheme: 'light',
  });
  const base = process.env.TEST_URL || 'http://127.0.0.1:5080';
  const credentials = process.env.TEST_PASSWORD
    ? { username: 'admin', password: process.env.TEST_PASSWORD }
    : JSON.parse(fs.readFileSync('.local-access.json', 'utf8'));
  const session = await (await page.request.get(base + '/api/session')).json();
  const response = await page.request.post(base + '/api/login', {
    data: credentials,
    headers: { 'X-CSRF-Token': session.csrf },
  });
  if (!response.ok()) throw new Error('Preview login failed');
  const now = new Date();
  const names = [
    'api-gateway',
    'background-worker',
    'postgres',
    'redis',
    'web-frontend',
    'backup-job',
  ];
  const images = [
    'nginx:stable-alpine',
    'worker:1.4.0',
    'postgres:17-alpine',
    'redis:7-alpine',
    'frontend:2.6.0',
    'backup:1.0.0',
  ];
  const demo = {
    containers: names.map((name, i) => ({
      id: String(i + 1).repeat(64),
      name,
      image: images[i],
      state: i === 5 ? 'exited' : 'running',
      running: i !== 5,
      status: i === 5 ? 'Exited (0) 2 hours ago' : 'Up 12 days',
      health: i === 5 ? 'none' : 'healthy',
      restarts: 0,
      project: i === 5 ? 'maintenance' : 'production',
      cpu: i === 5 ? null : [3.2, 12.8, 6.1, 0.4, 1.7][i],
      memory: i === 5 ? null : `${[86, 248, 512, 24, 128][i]}MiB / 4GiB`,
      memory_bytes: [86, 248, 512, 24, 128, 0][i] * 1024 ** 2,
      memory_percent: 10,
      ports: ['80:80, 443:443', '', '5432:5432', '6379:6379', '3000:3000', ''][i],
      network: '12MiB / 38MiB',
      disk_io: '8MiB / 24MiB',
      pids: 8,
      created: '2026-10-01',
      protected: false,
    })),
    stats: {
      cpu: 24.2,
      memory_percent: 42,
      memory_used: 6.7 * 1024 ** 3,
      memory_total: 16 * 1024 ** 3,
      disk_percent: 28,
      disk_used: 71 * 1024 ** 3,
      disk_total: 256 * 1024 ** 3,
      uptime: 12 * 86400 + 7 * 3600,
      scope: 'linux-host',
    },
    history: Array.from({ length: 60 }, (_, i) => ({
      time: new Date(now - (59 - i) * 5000).toISOString(),
      cpu: 21 + Math.sin(i / 3) * 7 + Math.sin(i * 1.5) * 4 + (i > 28 && i < 35 ? 15 : 0),
      memory: 38 + i / 15 + Math.sin(i / 8) * 2,
    })),
    timestamp: now.toISOString(),
    docker_available: true,
    error: null,
    stats_error: null,
    server: 'production-01 · demo',
    version: '0.3.0',
  };
  await page.route('**/api/snapshot', (route) => route.fulfill({ json: demo }));
  await page.routeWebSocket('**/socket.io/**', (socket) => {
    socket.send(
      '0' +
        JSON.stringify({
          sid: 'demo',
          upgrades: [],
          pingInterval: 25000,
          pingTimeout: 20000,
          maxPayload: 4096,
        })
    );
    socket.onMessage((message) => {
      if (message === '40') {
        socket.send('40{"sid":"demo"}');
        socket.send('42' + JSON.stringify(['update', demo]));
      }
    });
  });
  await page.goto(base);
  await page.waitForSelector('#rows tr');
  await page.screenshot({ path: 'docs/dashboard.png', fullPage: true });
  await page.getByRole('button', { name: 'Переключить тему' }).click();
  await page.screenshot({ path: 'docs/dashboard-dark.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: 'docs/mobile.png', fullPage: false });
  await page.request.post(base + '/api/logout', {
    headers: {
      'X-CSRF-Token': (await (await page.request.get(base + '/api/session')).json()).csrf,
    },
  });
  await browser.close();
})();
