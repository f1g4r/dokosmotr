# Разработка

Сохраняй простую основу Flask / Docker CLI / vanilla JS. Не добавляй Node runtime,
базу данных или фронтенд-фреймворк без конкретной задачи, оправдывающей усложнение.

- API не должен возвращать env контейнеров, произвольные labels, сырой inspect, токены или хеши паролей.
- Для новых mutating endpoints используй authorized(role), CSRF и аудит. Никакого shell=True.
- Пользовательские строки в интерфейсе — только textContent; не собирай HTML из данных Docker.
- Не выполняй операции с реальными контейнерами в тестах, кроме специально созданных test fixtures.
- Дополняй regression tests для изменений авторизации, CLI-параметров и сбора данных.
- Не увеличивай workers Gunicorn: состояние намеренно локально для одного процесса.

Версии прямых зависимостей — requirements.txt, полный lock — requirements.lock.
После осознанного изменения requirements.txt пересоздай lock в Python 3.11:

```bash
docker run --rm -v "$PWD/requirements.txt:/tmp/requirements.txt:ro" python:3.11-slim-trixie \
  sh -c 'pip install -q -r /tmp/requirements.txt && pip freeze' > requirements.lock
```

Фронтенд форматируется Prettier: `npm run format:check`. Форматирование не требуется для запуска приложения.

Перед релизом: собери образ, выполни unittest и Playwright, pip-audit, проверь локальный
diff и отсутствие секретов. Зафиксируй VERSION в app.py и номер в frontend/login.html.
Публикуй тег и архив через `git archive`, чтобы игнорируемые файлы не попали в релиз.

Socket.IO клиент хранится локально. При обновлении проверяй npm dist.integrity и сохраняй
MIT LICENSE из пакета. Для работающего приложения внешние CDN не нужны.
