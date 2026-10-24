# Setup

You'll build **one project, `allocation/`, across all chapters**. Each chapter's "Touchdown: code along" starts from the finished project of the previous chapter and tells you exactly which files to create or change. Commit and tag after each chapter.

## Create the project

```bash
mkdir allocation && cd allocation
git init
python3 -m venv .venv && source .venv/bin/activate
pip install pytest
```

Ch01 walks you through the first files. By the end the layout is:

```
allocation/
  pytest.ini                      # [pytest] pythonpath = src ; testpaths = tests
  src/allocation/
    config.py  bootstrap.py  views.py
    domain/          model.py  events.py  commands.py
    adapters/        orm.py  repository.py  notifications.py  redis_eventpublisher.py
    service_layer/   handlers.py  messagebus.py  unit_of_work.py
    entrypoints/     flask_app.py  redis_eventconsumer.py
  tests/
    conftest.py
    unit/  integration/  e2e/
```

(Ch03 is a small standalone exercise in its own folder, `sync-exercise/`.)

## Dependencies, added as you need them

| From chapter | Install | Why |
|---|---|---|
| 1 | `pytest` | tests |
| 2 | `sqlalchemy` | ORM (SQLite works for everything until ch07's concurrency test) |
| 4 | `flask` | the web entrypoint |
| 7 | `psycopg2-binary` + Postgres | only for the concurrency test and "real" runs |
| 11 | `redis`, `requests` + Redis | pub/sub integration and its e2e test |

## Things you'll run into (this differs from the book)

The book's code was written for SQLAlchemy 1.x and an older Python. This guide's code has been run on **Python 3.14, SQLAlchemy 2.1, Flask 3.1, pytest 9**. The differences you'll meet, each explained where it comes up:

| Book | This guide | Chapter |
|---|---|---|
| `@dataclass(frozen=True)` for `OrderLine` | `@dataclass(unsafe_hash=True)`; SQLAlchemy 2.x can't instrument a frozen dataclass | 1 |
| `mapper(Class, table)` | `registry().map_imperatively(Class, table)` | 2 |
| `session.execute("raw sql")` | `session.execute(text("raw sql"))` | 2 |
| Postgres from ch04 on | SQLite by default (`DB_URI` env var switches to Postgres) | 4 |
| `flask_app.py` with a global `app` | `create_app(...)` factory, easier to test | 4 |

## Running the app

```bash
export PYTHONPATH=src
flask --app "allocation.entrypoints.flask_app:create_app" run -p 5055
```

By default it uses a SQLite file (`allocation.db`). To use Postgres: `export DB_URI=postgresql://allocation:abc123@localhost:5432/allocation`.

## Docker (only needed from ch07 and ch11)

```yaml
# docker-compose.yml
services:
  postgres:
    image: postgres:16
    environment: {POSTGRES_USER: allocation, POSTGRES_PASSWORD: abc123, POSTGRES_DB: allocation}
    ports: ["5432:5432"]
  redis:
    image: redis:7
    ports: ["6379:6379"]
```

```bash
docker compose up -d
```

## Habit

```bash
git add -A && git commit -m "ch01 done" && git tag ch01
```

If you get stuck, diff against the book's own repo, which has a branch per chapter: <https://github.com/cosmicpython/code>. Expect differences in the places listed above.
