'use strict';
const form = document.getElementById('login-form');
const error = document.getElementById('login-error');
const submit = document.getElementById('submit');
let csrf;
async function prepare() {
  const response = await fetch('/api/session');
  if (!response.ok) throw new Error('Сервер недоступен. Обнови страницу.');
  const data = await response.json();
  csrf = data.csrf;
  if (data.authenticated) location.replace('/');
  if (data.setup_required) {
    submit.disabled = true;
    throw new Error(
      'Администратор ещё не создал пользователя. Выполни настройку по README на сервере.'
    );
  }
}
function showError(message) {
  error.textContent = message;
  error.hidden = false;
}
submit.disabled = true;
prepare()
  .then(() => {
    submit.disabled = false;
  })
  .catch((e) => showError(e.message));
document.getElementById('show-password').addEventListener('click', (event) => {
  const input = document.getElementById('password');
  input.type = input.type === 'password' ? 'text' : 'password';
  event.target.textContent = input.type === 'password' ? 'Показать' : 'Скрыть';
  event.target.setAttribute('aria-pressed', String(input.type === 'text'));
});
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  error.hidden = true;
  submit.disabled = true;
  try {
    if (!csrf) await prepare();
    const response = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
      body: JSON.stringify({ username: form.username.value, password: form.password.value }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Не удалось войти');
    location.replace('/');
  } catch (e) {
    showError(e.message);
    submit.disabled = false;
  }
});
