# Как работает `agent-connect` и можно ли подключить своего локального агента в AG2 Space

> Исследование по первичным источникам: исходный код репозитория, исходный код
> пакета-транспорта `ag2-sparrow` 0.2.0 (sdist с PyPI), спецификация протокола
> `docs/remote-gateway-protocol.md` из `sonichi/sutando`, git-история и PR-описания
> репозитория, живые ответы шлюза `https://chat.ag2.space/relay`.
>
> Дата: 2026-08-05. Состояние репозитория: ветка `main`, коммит `a3f82ca`.

**О месте этого файла.** В репозитории нет сложившейся конвенции для
исследовательских заметок: `docs/` содержит только `docs/agents/*.md` — конфиг для
agent-скиллов (`docs/agents/issue-tracker.md`, `docs/agents/domain.md`,
`docs/agents/triage-labels.md`), а `CONTEXT.md` и `docs/adr/` заявлены в
`.claude/CLAUDE.md:13-15`, но физически отсутствуют. Рабочие тикеты живут в
gitignore-нутом `.scratch/` (`.gitignore:7`). Поэтому файл положен в
`docs/research/` — как отдельный, явно названный раздел.

---

## Краткий ответ

**(a)** `agent-connect` — это не сервер и не бот. Это ~150 строк Python-воркера,
который крутится **на вашей машине**, следит за директорией `tasks/`, на каждый
файл-задачу запускает локальный CLI-агент (`codex exec`, `cline`, `omnigent run`,
HTTP Ollama) и кладёт stdout в `results/`. Всю сеть делает **отдельный пакет**
`ag2-sparrow` (PyPI, тоже stdlib-only): он long-poll'ит `GET
https://chat.ag2.space/relay/v1/tasks` с Bearer-токеном вашего агента, пишет
задачи в `tasks/`, читает `results/` и POST'ит ответ обратно. Связь между двумя
процессами — **только файловая система**, никакого API между ними нет.

**(b)** Да, подключить своего агента можно, и есть **четыре** разных пути. Самый
дешёвый и не требующий правки репозитория — реализовать у себя HTTP-эндпоинт,
совместимый с `POST /api/generate` Ollama, и запустить адаптер `ollama` с
`OLLAMA_HOST`, указывающим на ваш сервис. Полноценный путь — добавить свой модуль
в `agent_connect/adapters/` (контракт — одна функция `run(task, sandbox, cwd) -> str`).

**Единственное жёсткое внешнее условие**: нужен **relay-токен**, который выдаёт
AG2 Space Agent Portal. Кода, который создаёт агента или выпускает токен, в этом
репозитории **нет** — это серверная часть в закрытых репозиториях
(`ag2-space/ag2space-backend`, `ag2-space/cinny-webclient` — оба `private`).
Без токена ничего не заработает.

---

## 1. Что физически лежит в репозитории

Весь продуктовый код — 8 Python-файлов и 2 shell-скрипта:

| Файл | Строк | Роль |
|---|---|---|
| `agent_connect/worker.py` | 148 | вся логика: парсер задач, маппинг tier→sandbox, цикл опроса |
| `agent_connect/adapters/__init__.py` | 21 | статический реестр адаптеров |
| `agent_connect/adapters/codex.py` | 50 | `codex exec` |
| `agent_connect/adapters/ollama.py` | 41 | HTTP к Ollama |
| `agent_connect/adapters/omnigent.py` | 71 | `omnigent run --harness …` |
| `agent_connect/adapters/cline.py` | 80 | `cline --json -y` |
| `agent_connect/adapters/kilo.py` | 49 | `kilo run --auto` (скаффолд) |
| `install.sh` | 298 | one-line инсталлятор + launchd/systemd юниты |
| `run-agent.sh` | 65 | ручной запуск (relay + worker) |
| `test_*.py`, `install.test.sh` | ~180 | тесты (plain-python, без pytest, кроме одного) |

Зависимостей нет вообще: `pyproject.toml:13` → `dependencies = []`. Точка входа —
консольный скрипт `agent-connect = "agent_connect.worker:main"`
(`pyproject.toml:15-16`).

Важно: **сетевого кода в репозитории нет ни строчки.** Единственный `urllib` —
в `agent_connect/adapters/ollama.py:16-17`, и он ходит на `localhost:11434`.

---

## 2. Архитектура end-to-end

```
Комната AG2 Space (Matrix)            ваша машина
┌──────────────────────────┐
│ пользователь пишет       │
│   "!codex fix the test"  │
│   или @-mention агента   │
└───────────┬──────────────┘
            │  (сервер AG2 Space ставит задачу в очередь
            │   именно вашего агента — по его токену)
            ▼
┌──────────────────────────┐   HTTPS, Bearer, исходящее соединение
│ chat.ag2.space/relay     │◄──────────────── GET /v1/tasks?wait=25  (long-poll)
│  (task gateway)          │◄──────────────── POST /v1/tasks/<id>/ack
│                          │◄──────────────── POST /v1/results
│                          │◄──────────────── POST /v1/heartbeat
└──────────────────────────┘                          │
                                                      │  процесс №1: `ag2-sparrow`
                                    ┌─────────────────┴──────────────────┐
                                    │  ~/.agent-connect/workspace/       │
                                    │    tasks/task-<id>.txt   ◄── пишет │
                                    │    results/task-<id>.txt ──► читает│
                                    │    state/  (inflight, rooms)       │
                                    └─────────────────┬──────────────────┘
                                                      │  процесс №2: `agent-connect`
                                    ┌─────────────────▼──────────────────┐
                                    │ worker: polling каждую 1.0 с       │
                                    │  parse_task() → access_tier, task  │
                                    │  tier_to_sandbox()                 │
                                    │  adapter.run(preamble+task, …)     │
                                    └─────────────────┬──────────────────┘
                                                      ▼
                                       codex exec / cline / omnigent /
                                       HTTP → Ollama
```

Ключевая архитектурная идея сформулирована в `README.md:21-27`: appservice
(Matrix-мост, к которому homeserver ходит сам) не работает для ноутбука за NAT,
поэтому используется **исходящий** long-poll — как self-hosted CI runner.

### Разделение на два процесса

Оба процесса стартуют из одного лаунчера `~/.agent-connect/launch.sh`, который
генерирует `install.sh:186-223`: relay уходит в фон (`&`), воркер запускается
через `exec`. PID-файл `$WS/.worker.pids` привязан к workspace, чтобы перезапуск
не убил соседних агентов (`install.sh:199-205`, аналогично `run-agent.sh:25-31`).

Исторический баг, важный для понимания: до PR #3 launchd/systemd-юниты запускали
**только воркер**, relay не стартовал вовсе, и задачи никогда не приходили —
см. описание PR #3 («Services actually start the relay now»).

---

## 3. Транспорт: `ag2-sparrow` (не в этом репозитории)

Репозиторий **не содержит** транспорта. Он ставится с PyPI:

```sh
RELAY_PIP_SPEC="${RELAY_PIP_SPEC:-ag2-sparrow>=0.2.0}"   # install.sh:49
```

- PyPI: <https://pypi.org/project/ag2-sparrow/> — «Transport client that connects
  a local agent to AG2 Space: long-polls the task gateway for your agent's tasks
  and posts results back.»
- Source в метаданных пакета: <https://github.com/sonichi/sutando> (публичный).
  Модули `remote_gateway_bridge`, `_dirs`, `send_allowlist` каноничны в самом
  пакете; `task_archive`, `local_task_protocol`, `result_markers` синхронизируются
  из `sutando/src/` (README пакета, строка 41).
- Console script: `ag2-sparrow = "ag2_sparrow.remote_gateway_bridge:main"`.

### Wire-протокол

Спецификация: <https://github.com/sonichi/sutando/blob/main/docs/remote-gateway-protocol.md>.
Реализация клиента — `ag2_sparrow/remote_gateway_bridge.py`. Четыре эндпоинта,
все под `Authorization: Bearer <REMOTE_TASK_TOKEN>`, тела — JSON:

| Метод | Путь | Тело / ответ |
|---|---|---|
| `GET` | `/v1/tasks?wait=<sec>` | → `{"tasks": [{"id": "task-123", "task": "...", ...}]}`; `[]` по таймауту |
| `POST` | `/v1/tasks/<id>/ack` | `{"id": "task-123"}` |
| `POST` | `/v1/results` | `{"id": "task-123", "body": "<текст ответа>"}` |
| `POST` | `/v1/heartbeat` | `{"client","protocol_version":1,"provider","tier","inflight","capabilities":[…]}` |
| `POST` | `/v1/rooms/<room>/media` | `{"filename","content_b64"}` — загрузка вложений |

Живая проверка (2026-08-05):

```
$ curl -s -i -H 'Authorization: Bearer invalid' \
       'https://chat.ag2.space/relay/v1/tasks?wait=1'
HTTP/2 401
content-type: application/json
{"error": "unauthorized"}
```

То есть шлюз реально существует, отвечает JSON'ом и требует токен. Клиент при
401/403 **завершается фатально**, а не ретраит: `remote_gateway_bridge.py:881-882`.

Дополнительные детали клиента, важные на практике:
- Long-poll 25 с по умолчанию, HTTP-timeout `wait + 10` (`:109`, `:860`).
- Экспоненциальный backoff 1→60 с на сетевых ошибках (`:884-890`).
- `User-Agent: sutando-gateway-client/1.0` — обход Cloudflare bot-fight (`:238-241`).
- Идемпотентность: множество in-flight персистится в `state/remote-task-inflight.json`
  (`:73`, `:721-741`), повторная доставка уже обработанной задачи отбивается дедупом
  (`:546-569`).
- Токен может быть **комбинированным** `"https://gateway|<secret>"` — тогда URL
  берётся из токена (`:101-107`).
- Атомарная запись задачи через `tmp` + `rename` (`:620-622`) — воркер никогда не
  видит частичный файл.

---

## 4. Формат задачи на диске (граница между relay и воркером)

Это и есть **весь** контракт между двумя половинками. Файл `tasks/task-<id>.txt`,
плоские заголовки `key: value`:

```
id: task-123
timestamp: 2026-07-13T02:21:31Z
task: create a file called write-test.txt with content hi
source: ag2space
channel_id: !room:ag2.space
room_name: qingyun
sender_name: qingyun
user_id: @qingyun:ag2.space
priority: normal
interaction_type: message
access_tier: owner
```

(дословный фикстур из `test_worker_parse.py:12-23`; порядок полей задаёт
`remote_gateway_bridge.py:115-124`).

Две неочевидные, но принципиальные вещи:

1. **`access_tier` пишется ПОСЛЕДНИМ, уже после `task:`** — это анти-forgery
   инвариант (`remote_gateway_bridge.py:616-619`): даже парсер «последнее
   вхождение выигрывает» не даст телу сообщения подделать tier.
2. Все значения прогоняются через `_one_line()` (`remote_gateway_bridge.py:220-225`),
   который вырезает CR/LF — тело сообщения не может сфабриковать лишнюю строку-заголовок.

Из-за (1) в воркере был живой баг: парсер останавливался на `task:` и глотал все
последующие заголовки в тело, поэтому tier всегда падал в `other` и codex всегда
работал в `read-only` (PR #4, комментарий `agent_connect/worker.py:33-36`). Текущий
парсер `parse_task()` (`agent_connect/worker.py:44-74`) читает заголовки по всему
файлу; тело — от `task:` до следующего **известного** заголовка; дубль `access_tier`
fail-closed'ится в `other` (`:71-73`).

Результат воркер кладёт в `results/task-<id>.txt` (`agent_connect/worker.py:103`,
`:114`). Пустая задача → `[no-send] empty task` (`:109`), исключение адаптера →
текст ошибки в тот же файл (`:139-142`) — воркер **никогда не падает** на одной
задаче.

### Маркеры в теле результата

Это скрытая, но реально работающая фича: текст, который вернул ваш агент,
парсится `ag2_sparrow/result_markers.py` (`parse_markers`, `remote_gateway_bridge.py:758-802`):

| Маркер | Где | Эффект |
|---|---|---|
| `[no-send]`, `[REPLIED]`, `[deduped: <id>]` | в начале тела | результат архивируется, в комнату ничего не уходит |
| `[channel: <id>]` | первая непустая строка | ответ уходит в другую комнату |
| `[file: /path]`, `[send: /path]`, `[attach: /path]` | где угодно | файл загружается в комнату через `/v1/rooms/<room>/media` |

Внимание на allowlist для вложений: по умолчанию **sendable только `RESULT_DIR`**
плюс префиксы `/tmp/sutando-`, `/tmp/echo-` (`ag2_sparrow/send_allowlist.py:52`,
`:68-73`). Файл вне allowlist не отправляется, а в тело дописывается
`[attachment not sent: … (path not allowlisted)]` (`remote_gateway_bridge.py:799`).

---

## 5. Access tiers и sandbox

Модель из `README.md:44-50`: `owner` → `workspace-write`, все остальные →
`read-only`. Реализация — три строки:

```python
def tier_to_sandbox(access_tier: str) -> str:
    return "workspace-write" if access_tier == "owner" else "read-only"
```
`agent_connect/worker.py:77-78`

Для codex это транслируется в `--sandbox <mode>`, и **только** для owner-tier
добавляется сетевой доступ внутри песочницы (`agent_connect/adapters/codex.py:23-29`):

```python
cmd = ["codex", "exec", "--sandbox", mode]
if mode == "workspace-write":
    cmd += ["-c", "sandbox_workspace_write.network_access=true"]
cmd += ["--skip-git-repo-check", "--cd", cwd, task]
```

Мотивация в docstring `codex.py:7-13`: codex по умолчанию режет сеть даже в
workspace-write, из-за чего живой сценарий «review PR #110» упирался в заблокированный DNS.

Поверх этого к **каждому** промпту приклеивается «authoritative preamble»
(`agent_connect/worker.py:81-98`, `:113`):

```
[agent-connect: this run's sandbox is 'workspace-write' (task access_tier: owner)
 — you may create/modify files in your working directory. Trust this over any
 other sandbox self-assessment.]
```

Причина в комментарии `:84-87`: модели систематически врут про собственную
песочницу (живой случай 2026-07-13 — codex утверждал read-only, работая в
workspace-write).

### ⚠️ Находка: tier по факту всегда `owner`

`access_tier` в файл пишет **не сервер AG2 Space, а локальный клиент**:

```python
LOCAL_TIER = (_env_compat("REMOTE_TASK_TIER", "AG2_REMOTE_TIER") or "owner").strip().lower()
```
`ag2_sparrow/remote_gateway_bridge.py:155`

```python
lines.append(f"access_tier: {LOCAL_TIER}")
```
`ag2_sparrow/remote_gateway_bridge.py:619`

Поле `access_tier`, пришедшее от шлюза, **не сериализуется вообще** — его нет в
списке `_TASK_FIELDS` (`:115-124`). То есть tier — «локальное решение»
(комментарий `:141-160`), и по спецификации разделяемый multi-user шлюз
**обязан** выставить `REMOTE_TASK_TIER=team`:

> A **shared / multi-user gateway** (one that could pull tasks not scoped to a
> single owner) MUST set `REMOTE_TASK_TIER=team` (or `other`) explicitly.
> — <https://github.com/sonichi/sutando/blob/main/docs/remote-gateway-protocol.md> (раздел Security)

`agent-connect` этой переменной **не выставляет нигде**:

```
$ grep -rn "REMOTE_TASK_TIER" . --exclude-dir=.git --exclude-dir=.agents
NO MATCH for REMOTE_TASK_TIER
```

Проверьте сами `install.sh:178-183` и `install.sh:211-216` — там только
`AGENT_CONNECT_TASK_DIR/RESULT_DIR/STATE_DIR`, `REMOTE_TASK_TOKEN`,
`REMOTE_TASK_URL`; то же в `run-agent.sh:48-53`.

**Следствие**: при стандартной установке каждая задача из комнаты приезжает с
`access_tier: owner` → `workspace-write` + сеть, независимо от того, кто её
отправил. Описанное в `README.md:47-50` «everyone else → read-only» на практике
не срабатывает.

Смягчающие обстоятельства (обе — за пределами этого репозитория, проверить их
изнутри нельзя, серверная часть закрыта):
- шлюз, вероятно, owner-scoped по токену (аргумент в `remote_gateway_bridge.py:145-152`);
- на сервере есть ограничение отправителей: `agent_connect/adapters/cline.py:11-12`
  прямо говорит «safe for an owner-tier, **allowFrom-restricted** agent … today's
  agents are owner-only».

Тем не менее это единственное место, где документированная модель безопасности и
код расходятся, и это стоит проверить перед тем, как пускать агента в общую
комнату. Быстрая проверка на своей машине: посмотреть строку `access_tier:` в
`~/.agent-connect/workspace/tasks/task-*.txt` для сообщения от **другого**
пользователя. Быстрое лечение — добавить `REMOTE_TASK_TIER=team` в окружение
relay в `~/.agent-connect/launch.sh`.

---

## 6. Адаптеры

Контракт адаптера — **duck typing, ни базового класса, ни протокола**:

```python
def run(task: str, sandbox: str, cwd: str, timeout: int = 600) -> str: ...
```

Реестр статический:

```python
ADAPTERS = {"codex": codex, "ollama": ollama, "omnigent": omnigent,
            "cline": cline, "kilo": kilo}
def get(name):
    a = ADAPTERS.get(name)
    if a is None:
        raise KeyError(f"unknown adapter {name!r}; have: {', '.join(sorted(ADAPTERS))}")
    return a
```
`agent_connect/adapters/__init__.py:8-21`

**Механизма плагинов нет** — ни `entry_points`, ни сканирования директории, ни
загрузки по пути. Свой адаптер = правка этого файла.

Статус адаптеров (из `README.md:62-69` + кода):

| Адаптер | Команда / транспорт | Статус | Env |
|---|---|---|---|
| `codex` | `codex exec --sandbox <mode> --skip-git-repo-check --cd <cwd> <task>` | ✅ verified, live | — |
| `ollama` | `POST {OLLAMA_HOST}/api/generate` | ✅ verified, live | `OLLAMA_HOST`, `AGENT_CONNECT_OLLAMA_MODEL` (по умолчанию `qwen2.5:3b`) |
| `omnigent` | `omnigent run --harness <H> [--model M] -p <task>` | ✅ verified (omnigent 0.4.0) | `AGENT_CONNECT_OMNIGENT_HARNESS/MODEL/BIN` |
| `cline` | `cline --json -y [-P provider] [-m model] <task>` | ✅ путь команды и auth проверены; для live нужен логин Cline | `AGENT_CONNECT_CLINE_BIN/PROVIDER/MODEL` |
| `kilo` | `kilo run --auto <task>` | ⚠️ **скаффолд**, захват вывода не проверен | `AGENT_CONNECT_KILO_BIN` |
| ACP | — | ❌ **только план**, кода нет (`README.md:60`) | — |

Про `kilo` честно написано в docstring `agent_connect/adapters/kilo.py:13-19`:
с ненастроенной авторизацией stdout был пустым и на дефолтном выводе, и на
`--format json`, поэтому механизм захвата результата не подтверждён.

Полезная деталь `omnigent`: префикс `[harness]` или `[harness: kimi]` в самом
сообщении выбирает harness на лету (`agent_connect/adapters/omnigent.py:35-44`) —
один агент в комнате маршрутизирует в любой движок.

Общий паттерн всех CLI-адаптеров: `subprocess.run` с `stdin=DEVNULL` (чтобы
CLI ушёл в one-shot режим, а не в REPL), `capture_output=True`, `timeout=600`,
и graceful-сообщение вместо исключения при `FileNotFoundError` / `TimeoutExpired`.

---

## 7. Установка и конфигурация

### Однострочник

```sh
curl -fsSL <installer-url>/install.sh | sh -s -- --token <TOKEN> [--adapter codex]
```
`install.sh:9`

Что делает `install.sh`:
1. Проверяет `--token` (обязателен, `:66-69`), python3 + pip (`:103-107`).
2. Ставит воркер и relay через `pipx` (предпочтительно) или в выделенный venv
   `~/.agent-connect/venv` (`:124-140`). Оба пути PEP-668-safe — голый
   `pip install --user` падает на Homebrew Python и современных Debian/Ubuntu (`:113-116`).
3. Пишет `~/.agent-connect/launch.sh` — единый код-путь для launchd / systemd /
   nohup / ручного запуска (`:157-225`).
4. Стартует персистентно: launchd LaunchAgent `space.ag2.agent-connect.plist`
   на macOS (`:243-269`), systemd `--user` unit на Linux (`:270-288`), иначе
   `nohup` (`:289-295`). Логи: `~/.agent-connect/agent-connect.log`.

### Переменные окружения

| Переменная | Где читается | По умолчанию |
|---|---|---|
| `AGENT_CONNECT_TOKEN` | `install.sh:30`, `run-agent.sh:14` | — (обязательна) |
| `AGENT_CONNECT_ADAPTER` | `worker.py:118` | — (обязательна; иначе `SystemExit`) |
| `AGENT_CONNECT_REPO` | `worker.py:122` | `$HOME/agents` в install.sh:36; `os.getcwd()` в воркере |
| `AGENT_CONNECT_WORKSPACE` | `worker.py:26-30` | `~/.agent-connect/workspace` |
| `AGENT_CONNECT_POLL` | `worker.py:123` | `1.0` с |
| `REMOTE_TASK_URL` | лаунчер, `install.sh:182`/`:215` | `https://chat.ag2.space/relay` |
| `AGENT_CONNECT_TASK_DIR` / `_RESULT_DIR` / `_STATE_DIR` | `ag2_sparrow/_dirs.py:32-41` | `~/.ag2-sparrow/…` |
| `AGENT_CONNECT_PIP_SPEC` | `install.sh:46` | `git+https://github.com/ag2-space/agent-connect.git` |
| `RELAY_PIP_SPEC` | `install.sh:49` | `ag2-sparrow>=0.2.0` |

Шаблон: `examples/config.env.example:1-5`.

Про дефолт `~/agents` (`install.sh:32-36`) — это не косметика: раньше дефолтом
был `pwd`, и установка из `~/Documents` давала агенту рабочую директорию под
macOS TCC, где launchd-процесс получал `operation not permitted` на запись.
Инсталлятор теперь ещё и предупреждает при `~/Documents|Desktop|Downloads`
(`install.sh:97-100`).

### Ручной запуск

```sh
export AGENT_CONNECT_TOKEN=<token from the portal>
export AGENT_CONNECT_ADAPTER=codex
export AGENT_CONNECT_REPO=/path/to/repo
./run-agent.sh
```
`README.md:79-84`

### Режим `--sutando-workspace` (relay-only)

```sh
install.sh --token <TOKEN> --sutando-workspace /path/to/sutando/workspace
```

Ставит **только** `ag2-sparrow` и направляет его `tasks/`/`results/`/`state/`
в workspace уже работающего Sutando — воркер не ставится вообще, потому что
core-сессия Sutando сама является воркером (`install.sh:15-18`, `:159-184`).
Путь канонизируется в абсолютный и валидируется по наличию `tasks/`
(`install.sh:71-88`).

### Публикация

`ag2-agent-connect` **ещё не опубликован на PyPI** — на 2026-08-05
`https://pypi.org/pypi/ag2-agent-connect/json` отдаёт `404`, git-тегов и релизов
в репозитории нет. Поэтому дефолт `AC_PIP_SPEC` осознанно оставлен git-специей
(комментарий `install.sh:41-45`). Workflow релиза готов: `.github/workflows/release.yml`
публикует по тегу `v*` через PyPI Trusted Publishing (OIDC), предварительно
сверяя тег с `agent_connect.__version__` (`release.yml:40-49`). Требуется разовая
настройка pending publisher на PyPI (`release.yml:6-14`).

---

## 8. Реализовано / не реализовано

**Реализовано и проверено вживую:** воркер (парсер, tier→sandbox, preamble,
цикл), адаптеры `codex` / `ollama` / `omnigent`, инсталлятор с launchd/systemd,
relay-only режим для Sutando, транспорт целиком (в отдельном пакете), протокол
маркеров результата, дедуп/idempotency, CI-релиз.

**Не реализовано / заглушки:**
- **ACP-адаптер** — только строчка в README (`README.md:60`), кода нет.
- **`kilo`** — скаффолд, захват вывода не подтверждён (`kilo.py:13-19`).
- **`cline`** — код готов, live-запуск требует авторизации Cline (`README.md:66`);
  для non-owner tier предполагается `CLINE_COMMAND_PERMISSIONS`, но это TODO
  (`cline.py:11-12`).
- **Agent Portal / выпуск токенов / регистрация имени агента (`!codex`)** — вне
  этого репозитория, закрытый код.
- **PyPI-публикация `ag2-agent-connect`** — не сделана (404).
- **`CONTEXT.md` и `docs/adr/`** — заявлены в `.claude/CLAUDE.md:13-15`, отсутствуют.

---

## 9. Ответ на второй вопрос: как подключить СВОЕГО локального агента

### Обязательная предпосылка

Нужен **relay-токен вашего агента** из AG2 Space Agent Portal
(`README.md:77`, `install.sh:67`). Токен — это **идентичность агента в AG2 Space**,
а не ключ модели: «(1) agent identity → AG2 Space (a relay token we issue);
(2) the agent's own tool auth … which AG2 Space never sees» (`README.md:71`).
Токен может быть комбинированным `https://gateway|secret`
(`remote_gateway_bridge.py:101-105`).

Ни выпустить токен, ни зарегистрировать агента из этого репозитория **нельзя**.
Также именно на стороне сервера задаётся, как агента звать в комнате (`!codex`,
@-mention) и кто имеет право ему писать (`allowFrom`, `cline.py:11-12`).

---

### Вариант A — без правки репозитория: HTTP-шим под Ollama API ⭐

Самый быстрый способ. Адаптер `ollama` — это просто HTTP-клиент, и **он не
проверяет, что на том конце действительно Ollama**:

```python
HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
MODEL = os.environ.get("AGENT_CONNECT_OLLAMA_MODEL", "qwen2.5:3b")

def run(task: str, sandbox: str, cwd: str, timeout: int = 600) -> str:
    body = json.dumps({"model": MODEL, "prompt": task, "stream": False}).encode()
    req = urllib.request.Request(HOST + "/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    ...
    out = (data.get("response") or "").strip()
```
`agent_connect/adapters/ollama.py:19-41`

Значит, достаточно поднять свой HTTP-сервер, который:
- принимает `POST /api/generate` с телом `{"model": str, "prompt": str, "stream": false}`;
- отвечает JSON `{"response": "<текст ответа>"}`.

Внутри вы делаете что угодно: свой LangGraph/AG2-агент, RAG, вызовы инструментов.

```sh
# ваш сервис слушает 127.0.0.1:9000
export AGENT_CONNECT_TOKEN=<токен из портала>
export AGENT_CONNECT_ADAPTER=ollama
export OLLAMA_HOST=http://127.0.0.1:9000
export AGENT_CONNECT_OLLAMA_MODEL=my-agent      # уедет в поле "model", можно игнорировать
./run-agent.sh
```

Плюсы: ноль изменений в чужом коде, обновления `agent-connect` не ломают вас.
Минусы: `sandbox` и `cwd` до вас не доезжают — адаптер их не передаёт
(`ollama.py:6-7`: «`sandbox` and `cwd` are unused here»), tier-логика в вашем
шиме недоступна. Если она нужна — вариант B.

---

### Вариант B — свой адаптер (полный контракт)

1. Создать `agent_connect/adapters/mine.py`:

```python
"""Мой локальный агент."""
from __future__ import annotations
import subprocess

def run(task: str, sandbox: str, cwd: str, timeout: int = 600) -> str:
    # task    — текст из комнаты, уже с authoritative-преамбулой воркера
    # sandbox — "workspace-write" | "read-only"
    # cwd     — AGENT_CONNECT_REPO
    proc = subprocess.run(
        ["my-agent", "--mode", sandbox, task],
        stdin=subprocess.DEVNULL,      # без TTY -> one-shot, не REPL
        capture_output=True, text=True, timeout=timeout, cwd=cwd or None,
    )
    return (proc.stdout or "").strip() or "(my-agent produced no output)"
```

Возвращаемая строка — ровно то, что улетит в комнату (плюс обработка маркеров
из §4). Исключения ловить не обязательно: воркер обернёт их сам
(`worker.py:137-142`), но осмысленное сообщение лучше трейсбека.

2. Зарегистрировать в `agent_connect/adapters/__init__.py:8-13`:

```python
from . import mine
ADAPTERS = {..., "mine": mine}
```

3. Запустить с `AGENT_CONNECT_ADAPTER=mine`.

Заметьте: реестр статический, поэтому шаг 2 неизбежен — либо форк, либо PR в
`ag2-space/agent-connect`. Плагинного механизма нет.

---

### Вариант C — через `omnigent`, если ваш агент уже в его каталоге

`omnigent` (<https://github.com/omnigent-ai/omnigent>) оборачивает claude, codex,
cursor, kimi, qwen, goose, hermes, pi, opencode и др. Один адаптер даёт весь
каталог:

```sh
export AGENT_CONNECT_ADAPTER=omnigent
export AGENT_CONNECT_OMNIGENT_HARNESS=claude     # или codex / kimi / …
export AGENT_CONNECT_OMNIGENT_MODEL=...          # опционально
```
`agent_connect/adapters/omnigent.py:17-19`

В самой комнате harness можно переключать префиксом: `[kimi] посмотри логи`
или `[harness: qwen] …` (`omnigent.py:31-44`).

---

### Вариант D — обойти воркер целиком (relay-only)

Если ваш агент уже умеет сам читать директорию задач, поставьте **только**
транспорт и направьте его в вашу очередь:

```sh
pipx install ag2-sparrow
AGENT_CONNECT_TASK_DIR=/my/agent/tasks \
AGENT_CONNECT_RESULT_DIR=/my/agent/results \
AGENT_CONNECT_STATE_DIR=/my/agent/state \
REMOTE_TASK_TOKEN=<токен> \
REMOTE_TASK_URL=https://chat.ag2.space/relay \
REMOTE_TASK_TIER=team \
ag2-sparrow
```

Ваш агент парсит `tasks/task-<id>.txt` (формат §4) и пишет
`results/task-<id>.txt`. `agent-connect` в этом сценарии вообще не нужен —
именно так работает `install.sh --sutando-workspace` (`install.sh:159-184`).
Это самый развязанный вариант: вы зависите только от опубликованного пакета
и документированного файлового контракта.

---

## 10. Ограничения, о которых стоит знать заранее

| # | Ограничение | Доказательство |
|---|---|---|
| 1 | **Агент — не собеседник, а исполнитель one-shot задач.** Никакой истории диалога: воркер берёт только `task` и `access_tier`, всё остальное (`room_name`, `sender_name`, `user_id`, `channel_id`) читается в `fields`, но в адаптер **не передаётся**. | `agent_connect/worker.py:106-113` — в `adapter.run()` уходит только `preamble + task` |
| 2 | **Строго последовательная обработка, без конкурентности.** Один `codex exec` до 600 с блокирует всю очередь. | `worker.py:133-144` — один цикл, `adapter.run` синхронный; `timeout=600` в каждом адаптере |
| 3 | **Polling каждую секунду, никакого inotify/watchdog.** | `worker.py:123`, `:144` |
| 4 | **Множество `seen` живёт только в памяти.** После рестарта воркер пересканирует всё; защита — только проверка «результат уже есть» (`:104-105`). Если relay уже заархивировал результат, а файл задачи почему-то остался — задача выполнится повторно. | `worker.py:132`, `:104-105` |
| 5 | **Имя файла задачи должно начинаться на `task-`.** Воркер глобит `task-*.txt`, а relay пишет `f"{tid}.txt"` из `id` шлюза. Формат id зафиксирован в `TASK_ID_RE = ^task-[A-Za-z0-9][A-Za-z0-9-]{0,120}$`. | `worker.py:134`; `remote_gateway_bridge.py:542`; `local_task_protocol.py:135` |
| 6 | **Расхождение словарей заголовков.** `worker._HEADER_KEYS` не знает `content_modalities` / `media_form` / `attachments` (relay пишет их **сразу после** `task:`), поэтому при задаче с вложением эти три строки попадут **в тело промпта**. `source_message_id` и `platform_card` идут после `source:` и просто молча теряются. | `worker.py:37-41` vs `remote_gateway_bridge.py:115-124`, `:594-600`; `local_task_protocol.py:444-448` |
| 7 | **`access_tier` фактически всегда `owner`** — см. §5. | `remote_gateway_bridge.py:155`, `:619`; `grep REMOTE_TASK_TIER` → нет совпадений |
| 8 | **Вложения ограничены allowlist'ом.** По умолчанию отправить можно только файл из `RESULT_DIR` (или `/tmp/sutando-*`, `/tmp/echo-*`). | `ag2_sparrow/send_allowlist.py:52`, `:68-73` |
| 9 | **`ag2-agent-connect` не на PyPI** — установка идёт из git; `pip install ag2-agent-connect` не сработает. | `https://pypi.org/pypi/ag2-agent-connect/json` → 404; `install.sh:41-46` |
| 10 | **401/403 от шлюза убивают relay-процесс насмерть.** Протухший токен = молчащий агент; под launchd `KeepAlive` он будет перезапускаться в цикле. | `remote_gateway_bridge.py:881-882`; `install.sh:262` |
| 11 | **Нет тестов транспорта в этом репозитории** — тесты покрывают только `parse_task`, преамбулу, сборку команды codex и single-source версии. | `test_worker_parse.py`, `test_worker_preamble.py`, `test_codex_adapter.py`, `test_version_single_source.py` |

---

## 11. Проверочный чек-лист «завести своего агента»

1. Получить relay-токен в AG2 Space Agent Portal (веб). **Блокер, если доступа нет.**
2. Выбрать вариант интеграции (A / B / C / D из §9).
3. Убедиться, что базовый инструмент авторизован **своими** креденшелами
   (логин codex / `ANTHROPIC_API_KEY` для cline / `ollama serve` — ничего из этого
   AG2 Space не видит, `README.md:71`).
4. Установить:
   `curl -fsSL <installer-url>/install.sh | sh -s -- --token <TOKEN> --adapter <name> --repo ~/agents`
   (или `./run-agent.sh` для ручного запуска).
5. Проверить `~/.agent-connect/agent-connect.log`: relay должен напечатать
   `[remote-gateway-bridge] starting — gateway=… tasks=…` (`remote_gateway_bridge.py:854`),
   воркер — `agent-connect worker: adapter=… repo=… ws=…` (`worker.py:131`).
6. Написать в разрешённой комнате `@<ваш-агент> …` и посмотреть, что появилось в
   `~/.agent-connect/workspace/tasks/`.
7. **Отдельно проверить строку `access_tier:`** в этом файле — см. §5.

---

## Источники

**Код репозитория** (`/Users/nikitapastukhov/Desktop/work/agent-connect`, `main`, `a3f82ca`):
`README.md`, `pyproject.toml`, `install.sh`, `run-agent.sh`, `install.test.sh`,
`agent_connect/worker.py`, `agent_connect/adapters/*.py`, `test_*.py`,
`.github/workflows/release.yml`, `examples/config.env.example`,
`.claude/CLAUDE.md`, `docs/agents/*.md`, git log, описания PR #1–#11
(`gh pr list --repo ag2-space/agent-connect --state all`).

**Транспорт (первичный код и метаданные):**
- <https://pypi.org/project/ag2-sparrow/> — метаданные пакета
- sdist `ag2_sparrow-0.2.0.tar.gz`: `ag2_sparrow/remote_gateway_bridge.py`,
  `ag2_sparrow/_dirs.py`, `ag2_sparrow/result_markers.py`,
  `ag2_sparrow/send_allowlist.py`, `ag2_sparrow/local_task_protocol.py`,
  `README.md`, `pyproject.toml`

**Спецификация протокола:**
- <https://github.com/sonichi/sutando/blob/main/docs/remote-gateway-protocol.md>

**Живая проверка шлюза** (2026-08-05):
`GET https://chat.ag2.space/relay/v1/tasks?wait=1` → `HTTP/2 401 {"error": "unauthorized"}`;
`POST /relay/v1/heartbeat` → `401`; `GET /relay/` → `404`; `GET https://chat.ag2.space/` → `200`, `<title>AG2 Space</title>`.

**Прочее:**
- <https://github.com/omnigent-ai/omnigent> — meta-harness, который драйвит адаптер `omnigent`
- `gh api /orgs/ag2-space/repos` — `ag2space-backend` и `cinny-webclient` помечены `private`
  (отсюда вывод, что Agent Portal и очередь на стороне сервера недоступны для инспекции)
