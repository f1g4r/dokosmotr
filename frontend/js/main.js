'use strict';
const $ = (id) => document.getElementById(id);
let auth,
  connection,
  snapshot,
  filter = 'all',
  selectedId,
  logId,
  pendingAction;
let busy = false,
  refreshing = false,
  toastTimer,
  logSequence = 0;
let lastReceived = 0;
const rowCache = new Map();
const stateNames = {
  running: 'Работает',
  exited: 'Остановлен',
  created: 'Создан',
  restarting: 'Перезапуск',
  paused: 'Приостановлен',
  removing: 'Удаляется',
  dead: 'Ошибка',
};
const healthNames = {
  healthy: 'Healthy',
  unhealthy: 'Unhealthy',
  starting: 'Проверка…',
  none: 'Без healthcheck',
  unknown: 'Статус неизвестен',
};
function text(id, value) {
  $(id).textContent = value;
}
function bytes(value) {
  if (value == null || !Number.isFinite(value)) return '—';
  const units = ['Б', 'КиБ', 'МиБ', 'ГиБ', 'ТиБ'];
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index++;
  }
  return `${value.toLocaleString('ru-RU', { maximumFractionDigits: 1 })} ${units[index]}`;
}
function percent(value) {
  return value == null ? '—' : `${value.toLocaleString('ru-RU', { maximumFractionDigits: 1 })}%`;
}
function duration(seconds) {
  if (seconds == null) return '—';
  const days = Math.floor(seconds / 86400),
    hours = Math.floor((seconds % 86400) / 3600);
  return `${days ? days + ' д ' : ''}${hours} ч ${Math.floor((seconds % 3600) / 60)} м`;
}
function attention(container) {
  return container.health === 'unhealthy' || ['restarting', 'dead'].includes(container.state);
}
function stopped(container) {
  return ['exited', 'created', 'dead'].includes(container.state);
}
function badgeClass(container) {
  return attention(container)
    ? 'error'
    : container.running
      ? 'running'
      : ['paused', 'restarting'].includes(container.state)
        ? 'warning'
        : '';
}
function notify(message) {
  text('toast', message);
  $('toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    $('toast').hidden = true;
  }, 5000);
}
async function api(path, options = {}) {
  const response = await fetch('/api' + path, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(options.method ? { 'X-CSRF-Token': auth.csrf } : {}),
      ...options.headers,
    },
    signal: options.signal || AbortSignal.timeout(55000),
  });
  if (response.status === 401) {
    location.replace('/login');
    throw new Error('Сессия закончилась');
  }
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Ошибка сервера (${response.status})`);
  return data;
}
function progress(id, value) {
  if (value == null) $(id).removeAttribute('value');
  else $(id).value = Math.min(100, Math.max(0, value));
}
function renderStats() {
  const all = snapshot.containers,
    running = all.filter((c) => c.running).length;
  text('server-name', snapshot.server);
  text('sidebar-server', snapshot.server);
  text('nav-count', all.length);
  text('metric-total', all.length);
  text('metric-running', `${running} работают`);
  text('metric-stopped', `${all.filter(stopped).length} остановлены`);
  text('filter-all', all.length);
  text('filter-running', running);
  text('filter-stopped', all.filter(stopped).length);
  text('filter-attention', all.filter(attention).length);
  document.querySelector('.health-orbit').textContent = !snapshot.docker_available
    ? '…'
    : all.some(attention)
      ? '!'
      : '✓';
  document
    .querySelector('.health-orbit')
    .classList.toggle('warning', all.some(attention) || !snapshot.docker_available);
  text('unhealthy', all.filter(attention).length);
  text('no-health', all.filter((c) => c.health === 'none').length);
  text(
    'health-subtitle',
    !snapshot.docker_available
      ? 'Проверяем связь с Docker'
      : all.some(attention)
        ? 'Есть контейнеры, требующие внимания'
        : all.length
          ? 'Проблемных контейнеров не обнаружено'
          : 'Контейнеров пока нет'
  );
  const stats = snapshot.stats;
  text('metric-cpu', stats ? percent(stats.cpu) : '—');
  text('cpu-caption', stats ? (stats.cpu > 80 ? 'Высокая' : 'В пределах нормы') : 'Нет данных');
  text('metric-memory', bytes(stats?.memory_used));
  text('metric-disk', bytes(stats?.disk_used));
  text('memory-caption', stats ? `из ${bytes(stats.memory_total)}` : 'Нет данных');
  text('disk-caption', stats ? `из ${bytes(stats.disk_total)}` : 'Нет данных');
  text('memory-percent', percent(stats?.memory_percent));
  text('disk-percent', percent(stats?.disk_percent));
  progress('cpu-progress', stats?.cpu);
  progress('memory-progress', stats?.memory_percent);
  progress('disk-progress', stats?.disk_percent);
  text('uptime', duration(stats?.uptime));
  text('scope', stats?.scope === 'linux-host' ? 'Ресурсы: Linux-сервер' : 'Ресурсы: среда Docker');
  text('version', snapshot.version);
  const history = snapshot.history || [];
  const points = (field) =>
    history
      .map(
        (point, index) =>
          `${((index / Math.max(1, history.length - 1)) * 800).toFixed(2)},${(115 - Math.max(0, Math.min(100, Number(point[field]) || 0)) * 1.1).toFixed(2)}`
      )
      .join(' ');
  $('cpu-line').setAttribute('points', points('cpu'));
  $('memory-line').setAttribute('points', points('memory'));
  $('chart-empty').hidden = history.length > 1;
  if (history.length) text('chart-start', new Date(history[0].time).toLocaleTimeString('ru-RU'));
  $('chart').setAttribute(
    'aria-label',
    `Загрузка системы: CPU ${percent(stats?.cpu)}, память ${percent(stats?.memory_percent)}. История: ${history.length} замеров.`
  );
  status();
}
function status() {
  const maxAge = Math.max(30000, (snapshot?.update_interval || 5) * 3000);
  const sampleTime = snapshot?.timestamp ? Date.parse(snapshot.timestamp) : NaN;
  const stale =
    !lastReceived ||
    Date.now() - lastReceived > maxAge ||
    (Number.isFinite(sampleTime) && Date.now() - sampleTime > maxAge);
  const available = snapshot?.docker_available && !stale;
  text(
    'connection',
    available
      ? connection?.connected
        ? 'Live'
        : 'Резервное обновление'
      : lastReceived
        ? 'Нет свежих данных'
        : 'Подключаемся'
  );
  $('connection').className = `connection ${available ? '' : lastReceived ? 'offline' : 'pending'}`;
  const message =
    snapshot?.error ||
    (stale && lastReceived
      ? 'Связь с сервером потеряна. На экране последние полученные данные.'
      : snapshot?.stats_error);
  text('banner', message || '');
  $('banner').hidden = !message;
  text(
    'updated',
    snapshot?.timestamp
      ? `Обновлено ${new Date(snapshot.timestamp).toLocaleTimeString('ru-RU')}`
      : 'Ожидаем первый снимок'
  );
}
function filteredContainers() {
  const query = $('search').value.trim().toLowerCase(),
    project = $('project').value,
    order = $('sort').value;
  return (snapshot?.containers || [])
    .filter(
      (c) =>
        (!query ||
          [c.name, c.image, c.id, c.project].some((value) =>
            value.toLowerCase().includes(query)
          )) &&
        (!project || c.project === project) &&
        (filter === 'all' ||
          (filter === 'running' && c.running) ||
          (filter === 'stopped' && stopped(c)) ||
          (filter === 'attention' && attention(c)))
    )
    .sort((a, b) => {
      if (order === 'cpu') return (b.cpu ?? -1) - (a.cpu ?? -1) || a.name.localeCompare(b.name);
      if (order === 'memory')
        return b.memory_bytes - a.memory_bytes || a.name.localeCompare(b.name);
      if (order === 'state')
        return (
          Number(attention(b)) - Number(attention(a)) ||
          a.state.localeCompare(b.state) ||
          a.name.localeCompare(b.name)
        );
      return a.name.localeCompare(b.name);
    });
}
function updateProjects() {
  const previous = $('project').value;
  const projects = [...new Set(snapshot.containers.map((c) => c.project))].sort();
  const existing = [...$('project').options].slice(1).map((o) => o.value);
  if (JSON.stringify(projects) === JSON.stringify(existing)) return;
  $('project').replaceChildren(
    new Option('Все проекты', ''),
    ...projects.map((p) => new Option(p, p))
  );
  $('project').value = projects.includes(previous) ? previous : '';
}
function renderRows() {
  const items = filteredContainers(),
    body = $('rows');
  const visibleIds = new Set(items.map((c) => c.id));
  for (const [id, row] of rowCache)
    if (!visibleIds.has(id)) {
      row.remove();
      rowCache.delete(id);
    }
  items.forEach((c, index) => {
    let row = rowCache.get(c.id);
    if (!row) {
      row = $('row-template').content.firstElementChild.cloneNode(true);
      row.dataset.id = c.id;
      rowCache.set(c.id, row);
    }
    const put = (selector, value) => {
      const node = row.querySelector(selector);
      if (node.textContent !== value) node.textContent = value;
    };
    put('.container-name', c.name);
    put('.image-name', c.image);
    put('.project-name', c.project === 'Без проекта' ? '' : c.project);
    row.querySelector('.container-name').title = c.name;
    row.querySelector('.image-name').title = c.image;
    put('.status-badge', stateNames[c.state] || c.state);
    row.querySelector('.status-badge').className = 'status-badge ' + badgeClass(c);
    put('.health-label', healthNames[c.health] || 'Статус неизвестен');
    put('.cpu-cell', percent(c.cpu));
    put('.memory-cell', c.memory ? bytes(c.memory_bytes) : '—');
    put('.ports-cell', c.ports || '—');
    row.querySelector('.ports-cell').title = c.ports || '';
    row.querySelector('[data-open="logs"]').setAttribute('aria-label', `Логи ${c.name}`);
    row
      .querySelector('.row-actions [data-open="details"]')
      .setAttribute('aria-label', `Детали ${c.name}`);
    if (body.children[index] !== row) body.insertBefore(row, body.children[index] || null);
  });
  text('list-count', snapshot?.containers.length || 0);
  text('visible-count', `Показано ${items.length} из ${snapshot?.containers.length || 0}`);
  $('empty').hidden = items.length > 0;
  const filtering = $('search').value || $('project').value || filter !== 'all';
  text(
    'empty-title',
    filtering
      ? 'Ничего не нашлось'
      : snapshot?.timestamp
        ? 'Контейнеров пока нет'
        : 'Подключаемся к Docker'
  );
  text(
    'empty-message',
    filtering
      ? 'Попробуй другой запрос или сбрось фильтры.'
      : snapshot?.timestamp
        ? 'Здесь появятся контейнеры подключённого Docker.'
        : 'Первый снимок появится после сбора метрик.'
  );
  $('reset-filters').hidden = !filtering;
}
function receive(data) {
  snapshot = data;
  lastReceived = Date.now();
  renderStats();
  updateProjects();
  renderRows();
  if ($('details').open) renderDetails();
}
async function refresh(manual = false) {
  if (refreshing) return;
  refreshing = true;
  $('refresh').disabled = true;
  try {
    receive(await api('/snapshot'));
    if (manual) notify('Последний снимок сервера загружен');
  } catch (e) {
    if (manual) notify(e.message);
    status();
  } finally {
    refreshing = false;
    $('refresh').disabled = false;
  }
}
function renderDetails() {
  const c = snapshot.containers.find((item) => item.id === selectedId);
  const enabled =
    c &&
    !c.protected &&
    !auth.read_only &&
    auth.role === 'admin' &&
    snapshot.docker_available &&
    !busy;
  ['start', 'stop', 'restart'].forEach((action) => {
    $(`action-${action}`).disabled = !enabled;
  });
  if (!c) {
    text('action-note', 'Контейнер больше не существует');
    return;
  }
  text('detail-title', c.name);
  const badge = document.createElement('span');
  badge.className = 'status-badge ' + badgeClass(c);
  badge.textContent = stateNames[c.state] || c.state;
  $('detail-status').replaceChildren(badge);
  const details = {
    ID: c.id,
    Образ: c.image,
    Проект: c.project,
    Состояние: c.status,
    Healthcheck: healthNames[c.health],
    Перезапуски: c.restarts ?? '—',
    Порты: c.ports || 'Не опубликованы',
    Создан: c.created || '—',
    CPU: percent(c.cpu),
    Память: c.memory || '—',
    'Сеть, вход / выход': c.network || '—',
    'Диск, чтение / запись': c.disk_io || '—',
    Процессы: c.pids || '—',
  };
  const nodes = [];
  Object.entries(details).forEach(([label, value]) => {
    const dt = document.createElement('dt'),
      dd = document.createElement('dd');
    dt.textContent = label;
    dd.textContent = value;
    nodes.push(dt, dd);
  });
  $('detail-list').replaceChildren(...nodes);
  text(
    'action-note',
    c.protected
      ? 'Собственный контейнер панели защищён от остановки.'
      : auth.read_only
        ? 'Режим наблюдения: управление отключено на сервере.'
        : auth.role !== 'admin'
          ? 'Твоя роль позволяет только просмотр.'
          : 'Действия выполняются на реальном сервере.'
  );
  $('action-start').hidden = c.running || c.state === 'paused';
  $('action-stop').hidden = !c.running;
  $('action-restart').hidden = !c.running;
}
function openDetails(id) {
  selectedId = id;
  renderDetails();
  $('details').showModal();
}
async function loadLogs() {
  const sequence = ++logSequence;
  text('log-content', '');
  text('log-status', 'Загружаем логи…');
  $('download-logs').disabled = true;
  try {
    const result = await api(`/container/${logId}/logs?tail=${$('log-tail').value}`);
    if (sequence !== logSequence) return;
    text('log-content', result.logs || 'В журнале пока нет записей.');
    text(
      'log-status',
      `Снимок журнала · ${new Date().toLocaleTimeString('ru-RU')} · stdout + stderr`
    );
    $('download-logs').disabled = !result.logs;
  } catch (e) {
    if (sequence === logSequence) text('log-status', e.message);
  }
}
function openLogs(id) {
  logId = id;
  text('logs-title', snapshot.containers.find((c) => c.id === id)?.name || id.slice(0, 12));
  if (!$('logs').open) $('logs').showModal();
  loadLogs();
}
function download(content, filename, type) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function askAction(action) {
  const c = snapshot.containers.find((item) => item.id === selectedId);
  if (!c || busy) return;
  pendingAction = { id: c.id, action };
  const names = { start: 'Запустить', stop: 'Остановить', restart: 'Перезапустить' };
  text('confirm-title', `${names[action]} контейнер?`);
  text(
    'confirm-message',
    `${c.name}${action !== 'start' ? ' временно станет недоступен. Убедись, что сейчас это допустимо.' : ' будет запущен на сервере.'}`
  );
  text('confirm-submit', names[action]);
  $('confirm').showModal();
}
$('confirm-submit').addEventListener('click', async () => {
  if (!pendingAction || busy) return;
  if (pendingAction.action === 'logout') {
    try {
      await api('/logout', { method: 'POST' });
      location.replace('/login');
    } catch (e) {
      notify(e.message);
    }
    return;
  }
  const { id, action } = pendingAction;
  busy = true;
  $('confirm-submit').disabled = true;
  try {
    await api(`/container/${id}/${action}`, { method: 'POST' });
    $('confirm').close();
    notify('Команда выполнена. Состояние обновится с ближайшим замером.');
    await refresh();
  } catch (e) {
    text('confirm-message', e.message);
  } finally {
    busy = false;
    $('confirm-submit').disabled = false;
    if ($('details').open) renderDetails();
  }
});
$('account').addEventListener('click', () => {
  pendingAction = { action: 'logout' };
  text('confirm-title', 'Выйти из аккаунта?');
  text('confirm-message', `Текущий пользователь: ${auth.username}`);
  text('confirm-submit', 'Выйти');
  $('confirm').showModal();
});
document
  .querySelectorAll('[data-close]')
  .forEach((button) => button.addEventListener('click', () => $(button.dataset.close).close()));
$('rows').addEventListener('click', (event) => {
  const button = event.target.closest('[data-open]');
  if (!button) return;
  const id = button.closest('tr').dataset.id;
  if (button.dataset.open === 'logs') openLogs(id);
  else openDetails(id);
});
$('detail-logs').addEventListener('click', () => openLogs(selectedId));
['start', 'stop', 'restart'].forEach((action) =>
  $(`action-${action}`).addEventListener('click', () => askAction(action))
);
$('reload-logs').addEventListener('click', loadLogs);
$('log-tail').addEventListener('change', loadLogs);
$('logs').addEventListener('close', () => {
  logSequence++;
});
$('download-logs').addEventListener('click', () =>
  download(
    $('log-content').textContent,
    `container-${logId.slice(0, 12)}.log`,
    'text/plain;charset=utf-8'
  )
);
$('refresh').addEventListener('click', () => refresh(true));
$('search').addEventListener('input', renderRows);
$('project').addEventListener('change', renderRows);
$('sort').addEventListener('change', () => {
  renderRows();
  try {
    localStorage.setItem('dokosmotr-sort', $('sort').value);
  } catch (_) {
    /* Optional preference. */
  }
});
document.querySelectorAll('[data-filter]').forEach((button) =>
  button.addEventListener('click', () => {
    filter = button.dataset.filter;
    document.querySelectorAll('[data-filter]').forEach((b) => {
      b.classList.toggle('active', b === button);
      b.setAttribute('aria-pressed', String(b === button));
    });
    renderRows();
  })
);
$('reset-filters').addEventListener('click', () => {
  $('search').value = '';
  $('project').value = '';
  document.querySelector('[data-filter="all"]').click();
});
$('export').addEventListener('click', () => {
  const safe = (value) =>
    '"' +
    String(value ?? '')
      .replace(/^[=+\-@\t\r]/, (c) => "'" + c)
      .replaceAll('"', '""') +
    '"';
  const rows = [
    ['Имя', 'Образ', 'Состояние', 'Проект', 'CPU %', 'Память', 'Порты'],
    ...filteredContainers().map((c) => [
      c.name,
      c.image,
      c.state,
      c.project,
      c.cpu,
      c.memory,
      c.ports,
    ]),
  ];
  download(
    '\uFEFF' + rows.map((row) => row.map(safe).join(';')).join('\r\n'),
    'dokosmotr-containers.csv',
    'text/csv;charset=utf-8'
  );
});
document.addEventListener('keydown', (event) => {
  if (
    event.key === '/' &&
    !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName) &&
    !document.querySelector('dialog[open]')
  ) {
    event.preventDefault();
    $('search').focus();
  }
});
async function init() {
  auth = await api('/session');
  if (!auth.authenticated) {
    location.replace('/login');
    return;
  }
  text('mode', auth.read_only || auth.role !== 'admin' ? 'Наблюдение' : 'Управление');
  text('account', auth.username.slice(0, 1).toUpperCase());
  $('account').title = `${auth.username} · ${auth.role} · Выйти`;
  try {
    const sort = localStorage.getItem('dokosmotr-sort');
    if (['name', 'cpu', 'memory', 'state'].includes(sort)) $('sort').value = sort;
  } catch (_) {
    /* Optional preference. */
  }
  await refresh();
  // Force WebSocket: no cross-origin credentials or polling POST transport.
  if (typeof io === 'function') {
    connection = io({ transports: ['websocket'], reconnectionDelayMax: 10000 });
    connection.on('update', receive);
    connection.on('connect', status);
    connection.on('disconnect', status);
    connection.on('connect_error', status);
  }
  setInterval(() => {
    status();
    if (!connection?.connected || Date.now() - lastReceived > 15000) refresh();
  }, 10000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refresh();
  });
}
init().catch((e) => {
  text('banner', 'Не удалось загрузить панель: ' + e.message);
  $('banner').hidden = false;
});
