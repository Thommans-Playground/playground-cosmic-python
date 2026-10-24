# Setup

## Layout you'll grow into

```
allocation/              # your working code, built chapter by chapter
  src/allocation/
    domain/  service_layer/  adapters/  entrypoints/
  tests/
    unit/  integration/  e2e/
  pytest.ini  setup.py
```

Start with the flat layout of ch01 (`model.py`, `test_batches.py`) and restructure as the book does. The refactors are part of the lesson.

## Commands

```bash
cd allocation
python3 -m venv .venv && source .venv/bin/activate
pip install pytest sqlalchemy flask psycopg2-binary requests redis
pytest -q          # should say "no tests ran" until ch01
```

Notes
- The book targets Python 3.8+; 3.14 is fine. Some pinned SQLAlchemy 1.x idioms (classical mapping via `mapper(...)`) changed in 2.x: use `registry().map_imperatively(...)`. See the ch02 guide.
- Chapters 2–6 need a database. SQLite in memory works for early tests; Postgres (Docker) is used from the Flask e2e tests onwards.
- Chapter 11 needs Redis; Docker Compose is the easiest way to run Postgres + Redis together.

## Habit

```bash
git init && git add -A && git commit -m "ch01 done" && git tag ch01
```
