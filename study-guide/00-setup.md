# Setup

You'll build **one project, `allocation/`, across all chapters**. Each chapter's "Touchdown: code along" starts from the finished project of the previous chapter and says exactly which files to create or change. Commit and tag after each chapter.

## The toolchain

| Tool | Job | Notes |
|---|---|---|
| **uv** | Python versions, virtualenv, dependencies, running commands | replaces pip + venv + pip-tools + pyenv; writes `pyproject.toml` and `uv.lock` |
| **ruff** | linter and formatter | replaces flake8 + isort + black |
| **ty** | type checker | Astral's checker, same team as uv and ruff. **Still beta (pre-1.0).** mypy is a fine substitute, and the final code passes both |
| **pytest** | tests | |
| **FastAPI** | the web entrypoint | replaces the book's Flask |
| **pydantic** v2 | validation of everything that comes **from outside** (HTTP bodies, Redis messages) | used at the edges only; the domain model stays plain Python |
| **pydantic-settings** | typed configuration from environment variables / `.env` | replaces `os.environ.get(...)` calls |
| **SQLAlchemy** 2.x | ORM, with the book's "classical mapping" via `registry().map_imperatively` | |
| **httpx2** | HTTP client used by FastAPI's `TestClient` | Starlette now prefers it over `httpx` and warns if you use the old one |
| **psycopg** 3 | Postgres driver (only for the Postgres-specific parts) | URLs look like `postgresql+psycopg://...` |

Install uv once: `brew install uv` (or see <https://docs.astral.sh/uv/>). uv can also download the Python version you ask for.

## Why pydantic is *not* in the domain model

The whole book is about keeping the model free of infrastructure (ch01-02). Pydantic is a validation library for data crossing a boundary, so it belongs at the boundaries: the API's request/response models (ch04), the Redis message format (ch11) and the settings (ch04). `OrderLine`, `Batch`, `Product`, and the commands and events stay as plain dataclasses and classes, so the dependency arrows keep pointing inward. (It would work to make commands pydantic models, and some teams do. This guide keeps one rule: *validate at the edge, trust inside*.)

## Create the project

```bash
uv init --package allocation --python 3.12
cd allocation
uv add --dev pytest ruff ty
```

Ch01 walks through this in detail. By the end of the book the layout is:

```
allocation/
  pyproject.toml                  # dependencies + pytest + ruff settings   (uv.lock sits beside it)
  src/allocation/
    config.py  bootstrap.py  views.py
    domain/          model.py  events.py  commands.py
    adapters/        orm.py  repository.py  notifications.py  redis_eventpublisher.py
    service_layer/   handlers.py  messagebus.py  unit_of_work.py
    entrypoints/     fastapi_app.py  redis_eventconsumer.py
  tests/
    conftest.py
    unit/  integration/  e2e/
```

(Ch03 is a small standalone exercise in its own folder, `sync-exercise/`.)

### Why a package called `allocation`?

The name comes from the book's domain (allocating orders to stock) and every import in the guide uses it, so keep it while you follow along. `uv init --package` makes your code an **importable, editable-installed package** under `src/allocation/`, so tests, the API, the Redis consumer and the bootstrap all write `from allocation.domain import model` and it resolves identically, with no `PYTHONPATH` tricks. The folders then show the architecture in the import lines (`allocation.domain`, `.adapters`, `.service_layer`, `.entrypoints`), which is how you notice a dependency pointing the wrong way. The `src/` layout also means tests import the *installed* package rather than whatever is in the current directory. A plain `uv init` would give a script-style project with a `main.py` and nothing importable; ch03's exercise uses one on purpose for contrast.

## Dependencies, added as you need them

| From chapter | Command | Why |
|---|---|---|
| 1 | `uv add --dev pytest ruff ty` | tests, lint/format, types |
| 2 | `uv add sqlalchemy` | ORM (SQLite works for everything until ch07's concurrency test) |
| 4 | `uv add fastapi uvicorn pydantic-settings` and `uv add --dev httpx2` | web entrypoint, settings, test client |
| 7 | `uv add --dev "psycopg[binary]"` + Postgres | only for the concurrency test and "real" runs |
| 11 | `uv add redis` + Redis | pub/sub integration |

## The commands you'll use all the time

```bash
uv run pytest -q          # run the tests
uv run ruff check --fix   # lint (and auto-fix what it can)
uv run ruff format        # format
uv run ty check src       # type check
uv sync                   # rebuild the environment from uv.lock on a new machine
```

## Docstrings and comments

The code in the guides uses **Google-style docstrings** (PEP 257 compatible) on every public module, class and function:

```python
def allocate(self, line: OrderLine) -> str | None:
    """Allocate ``line`` to the best available batch of this SKU.

    Args:
        line: The order line to allocate.

    Returns:
        The reference of the chosen batch, or ``None`` if no batch can take the line.
    """
```

A one-line summary that ends with a full stop, then `Args:`, `Returns:`, `Raises:` (and `Attributes:` for classes and dataclasses) where they add something.

- **A docstring says *what* and the contract**: what you may pass in, what comes back, what can go wrong. It is for the caller, who shouldn't have to read the body.
- **A comment says *why***: the reason behind a line that would otherwise look arbitrary, for example `# sorted() uses Batch.__gt__, so the first batch that fits is the preferred one.` Don't write comments that repeat the code.
- **Constructor arguments go on the class docstring**, so `__init__` and the magic methods (`__eq__`, `__hash__`) don't each need their own, unless they carry business meaning, as `Batch.__gt__` does.
- **It is enforced**, not just encouraged: `pyproject.toml` turns on ruff's docstring rules (`D`, `convention = "google"`). `uv run ruff check` lists anything missing. Tests are exempt; their names are the documentation.
- **FastAPI puts endpoint docstrings and pydantic model docstrings into the generated API docs** at `/docs`, so here they are documentation people actually read.

You don't have to type every docstring as you follow along. Writing them as you go (at least the one-line summary) is good practice, and `ruff check` will tell you what you skipped.

## Things you'll run into (this differs from the book)

The book's code was written for Flask, SQLAlchemy 1.x and an older Python. This guide's code has been run on **Python 3.12 and 3.14**. The differences, each explained where it comes up:

| Book | This guide | Chapter |
|---|---|---|
| `pip` + `venv`, `requirements.txt` | uv + `pyproject.toml` + `uv.lock` | 1 |
| `@dataclass(frozen=True)` for `OrderLine` | `@dataclass(unsafe_hash=True)`; SQLAlchemy 2.x can't instrument a frozen dataclass | 1 |
| `mapper(Class, table)` | `registry().map_imperatively(Class, table)` | 2 |
| `session.execute("raw sql")` | `session.execute(text("raw sql"))` | 2 |
| Flask, with a global `app` | FastAPI with pydantic request/response models, built by a `create_app(...)` factory | 4 |
| `os.environ.get(...)` config functions | typed `Settings` class (pydantic-settings) | 4 |
| `Optional[X]`, `List[X]`, `# type:` comments | `X \| None`, `list[X]`, real annotations (ruff's `UP` rules, checked by `ty`) | all |
| Postgres from ch04 on | SQLite by default (`DB_URI` setting switches to Postgres) | 4 |
| One shared message bus | one bus **per request**: FastAPI runs sync endpoints in a thread pool | 13 |

## Running the app

```bash
uv run uvicorn --factory allocation.entrypoints.fastapi_app:create_app --port 5055
```

(`--factory` because `create_app` is a function that returns the app.) Then open <http://localhost:5055/docs> for interactive API docs generated from your pydantic models. By default it uses a SQLite file; set `DB_URI=postgresql+psycopg://allocation:abc123@localhost:5432/allocation` for Postgres.

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
