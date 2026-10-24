# Chapter 4: Our First Use Case: Flask API and Service Layer

## ✈️ 30,000 ft
You now have a model and storage, but nobody can use them. Add a thin web endpoint, but put the *orchestration* (fetch → validate → call model → save) in a **service layer**, not in Flask. Then the same use case can be driven by an API, a CLI or a message queue.

## 🛫 10,000 ft
- **Entrypoint / adapter** (Flask): translate HTTP → function call → HTTP. No business logic.
- **Service layer / application layer**: one function per *use case* (`allocate`, `add_batch`). Does the plumbing: repo + session + domain.
- **Domain model**: does the business rules.
- Services depend on the **abstract** repo, so tests can inject a fake.
- Trade-off: a "pointless" extra layer for trivial apps; essential once there are multiple entrypoints.

### 🗺️ Diagram: layers and one request

```mermaid
flowchart TB
    Client([HTTP client]) --> Flask["Flask entrypoint<br/>HTTP in, HTTP out"]
    Flask --> Svc["Service layer<br/>orchestrates the use case"]
    Svc --> Repo[AbstractRepository]
    Svc --> Model["Domain model<br/>business rules"]
    Repo --> SQL[SqlAlchemyRepository] --> DB[(Postgres)]
    Repo -.-> Fake[FakeRepository<br/>in tests]
```

```mermaid
sequenceDiagram
    participant C as Client
    participant F as Flask
    participant S as services.allocate
    participant R as Repository
    participant M as model.allocate
    C->>F: POST /allocate
    F->>S: allocate(orderid, sku, qty, repo, session)
    S->>R: list()
    R-->>S: batches
    S->>M: allocate(line, batches)
    M-->>S: batchref
    S->>S: session.commit()
    S-->>F: batchref
    F-->>C: 201 + batchref
```

## 🛬 1,000 ft: design decisions
- `services.allocate(orderid, sku, qty, repo, session)` takes primitives, not domain objects. The API layer needn't know about the model.
- It raises `InvalidSku` for bad input, and the Flask handler maps exceptions to 400/404/201.
- Service tests use `FakeRepository` and a `FakeSession` with `committed = False`. (Ch06 will replace the fake session. Notice the smell.)
- The e2e test posts to `/allocate` against a real database (ch05 adds `/add_batch` so the test can seed data through the API).
- Smell the authors acknowledge: the service still knows about `session.commit()`, and the API still creates the repo.

## 🛞 Touchdown: code along

> Starts from your finished ch02 project. New: `repository.list()`, `service_layer/services.py`, `config.py`, `entrypoints/flask_app.py`, unit tests with fakes, one end-to-end test. All code below was run and passes (24 tests).

```bash
pip install flask
mkdir -p src/allocation/service_layer src/allocation/entrypoints tests/e2e
touch src/allocation/service_layer/__init__.py src/allocation/entrypoints/__init__.py
```

### Step 1: Teach the repository to `list()`

**Do:** add an abstract `list()` to `AbstractRepository` (and the SQLAlchemy implementation already has it from ch02).

**Why:** `allocate` needs *all* batches so the model can pick the best one. The abstract class is the contract that the real repo and the fake both promise to honour.

**📄 `src/allocation/adapters/repository.py`**

```python
import abc

from allocation.domain import model


class AbstractRepository(abc.ABC):
    @abc.abstractmethod
    def add(self, batch: model.Batch):
        raise NotImplementedError

    @abc.abstractmethod
    def get(self, reference) -> model.Batch:
        raise NotImplementedError

    @abc.abstractmethod
    def list(self):
        raise NotImplementedError


class SqlAlchemyRepository(AbstractRepository):
    def __init__(self, session):
        self.session = session

    def add(self, batch):
        self.session.add(batch)

    def get(self, reference):
        return self.session.query(model.Batch).filter_by(reference=reference).one()

    def list(self):
        return self.session.query(model.Batch).all()
```

### Step 2: Write the service-layer tests first (with fakes)

**Do:** create `tests/unit/test_services.py`. The `FakeRepository` is just a `set` wrapped in the repository interface; `FakeSession` only remembers whether `commit()` was called.

**Why:** the point of the service layer is that the use case can be tested **without Flask and without a database**. Notice the smell: the test has to know about a `session` and `commit`. Chapter 6 removes that.

**📄 `tests/unit/test_services.py`**

```python
import pytest

from allocation.adapters import repository
from allocation.domain import model
from allocation.service_layer import services


class FakeRepository(repository.AbstractRepository):
    def __init__(self, batches):
        self._batches = set(batches)

    def add(self, batch):
        self._batches.add(batch)

    def get(self, reference):
        return next(b for b in self._batches if b.reference == reference)

    def list(self):
        return list(self._batches)


class FakeSession:
    committed = False

    def commit(self):
        self.committed = True


def test_returns_allocation():
    batch = model.Batch("b1", "COMPLICATED-LAMP", 100, eta=None)
    repo = FakeRepository([batch])

    result = services.allocate("o1", "COMPLICATED-LAMP", 10, repo, FakeSession())
    assert result == "b1"


def test_error_for_invalid_sku():
    batch = model.Batch("b1", "AREALSKU", 100, eta=None)
    repo = FakeRepository([batch])

    with pytest.raises(services.InvalidSku, match="Invalid sku NONEXISTENTSKU"):
        services.allocate("o1", "NONEXISTENTSKU", 10, repo, FakeSession())


def test_commits():
    batch = model.Batch("b1", "OMINOUS-MIRROR", 100, eta=None)
    repo = FakeRepository([batch])
    session = FakeSession()

    services.allocate("o1", "OMINOUS-MIRROR", 10, repo, session)
    assert session.committed is True
```

### Step 3: Write the service

**Do:** create `service_layer/services.py`.

**Why each line is there:**
- It takes **primitives** (`orderid: str`, `sku: str`, `qty: int`), not `OrderLine`. Callers (Flask, CLI, queue consumers) shouldn't have to know the domain classes.
- It does the **orchestration**: fetch batches → validate the SKU → call the domain function → commit. The domain model contains the business *rules*; the service contains the *steps*.
- `InvalidSku` is a service-level error: checking that a SKU exists needs the repository, so it isn't a pure domain rule.

**📄 `src/allocation/service_layer/services.py`**

```python
from allocation.adapters import repository
from allocation.domain import model


class InvalidSku(Exception):
    pass


def is_valid_sku(sku, batches):
    return sku in {b.sku for b in batches}


def allocate(
    orderid: str, sku: str, qty: int, repo: repository.AbstractRepository, session
) -> str:
    line = model.OrderLine(orderid, sku, qty)
    batches = repo.list()
    if not is_valid_sku(line.sku, batches):
        raise InvalidSku(f"Invalid sku {line.sku}")
    batchref = model.allocate(line, batches)
    session.commit()
    return batchref
```

### Step 4: Add the thin Flask entrypoint

**Do:** create `config.py` and `entrypoints/flask_app.py`.

**Why:** Flask's whole job is to translate: JSON in → function call → JSON/status out. Note that it maps domain/service errors to HTTP 400 and success to 201, and holds **no business logic**.

`create_app(session_factory)` is a factory so tests can give it a throwaway database. By default it uses a SQLite file, so you can run it with no setup. To use Postgres (as the book does), set `DB_URI=postgresql://user:pw@localhost:5432/allocation` and `pip install psycopg2-binary`.

**📄 `src/allocation/config.py`**

```python
import os


def get_db_uri():
    # SQLite file by default so the app runs with zero setup.
    # Point DB_URI at Postgres when you want the real thing, e.g.
    #   postgresql://allocation:abc123@localhost:5432/allocation
    return os.environ.get("DB_URI", "sqlite:///allocation.db")
```

**📄 `src/allocation/entrypoints/flask_app.py`**

```python
from flask import Flask, request
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from allocation import config
from allocation.adapters import orm, repository
from allocation.domain import model
from allocation.service_layer import services


def create_app(session_factory=None):
    if session_factory is None:
        engine = create_engine(config.get_db_uri())
        orm.metadata.create_all(engine)
        orm.start_mappers()
        session_factory = sessionmaker(bind=engine)

    app = Flask(__name__)

    @app.route("/allocate", methods=["POST"])
    def allocate_endpoint():
        session = session_factory()
        repo = repository.SqlAlchemyRepository(session)
        try:
            batchref = services.allocate(
                request.json["orderid"],
                request.json["sku"],
                request.json["qty"],
                repo,
                session,
            )
        except (model.OutOfStock, services.InvalidSku) as e:
            return {"message": str(e)}, 400

        return {"batchref": batchref}, 201

    return app
```

### Step 5: End-to-end test

**Do:** add the file-based `session_factory` fixture to `tests/conftest.py` (full file shown) and write `tests/e2e/test_api.py`.

**Why:** these tests go through the real HTTP layer and a real database, so they're slow compared to the unit tests. Keep very few of them, one happy path and one error path, since the services tests already cover the logic.

> Seeding data: at this point the API has no way to add a batch, so the test inserts rows directly with the ORM. Chapter 5 adds an `add_batch` endpoint and removes this.

**📄 `tests/conftest.py`**

```python
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, clear_mappers

from allocation.adapters.orm import metadata, start_mappers


@pytest.fixture
def in_memory_db():
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    return engine


@pytest.fixture
def session(in_memory_db):
    start_mappers()
    yield sessionmaker(bind=in_memory_db)()
    clear_mappers()


@pytest.fixture
def session_factory(tmp_path):
    """A file-based SQLite DB, so separate sessions behave like separate connections."""
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    metadata.create_all(engine)
    start_mappers()
    yield sessionmaker(bind=engine)
    clear_mappers()
    engine.dispose()
```

**📄 `tests/e2e/test_api.py`**

```python
import uuid
from datetime import date
import pytest

from allocation.domain import model
from allocation.entrypoints.flask_app import create_app


def random_suffix():
    return uuid.uuid4().hex[:6]


def random_sku(name=""):
    return f"sku-{name}-{random_suffix()}"


def random_batchref(name=""):
    return f"batch-{name}-{random_suffix()}"


def random_orderid(name=""):
    return f"order-{name}-{random_suffix()}"


@pytest.fixture
def client(session_factory):
    return create_app(session_factory).test_client()


def add_stock(session_factory, batches):
    session = session_factory()
    for ref, sku, qty, eta in batches:
        eta = date.fromisoformat(eta) if eta else None
        session.add(model.Batch(ref, sku, qty, eta))
    session.commit()


def test_happy_path_returns_201_and_allocated_batch(client, session_factory):
    sku, othersku = random_sku(), random_sku("other")
    earlybatch = random_batchref(1)
    laterbatch = random_batchref(2)
    otherbatch = random_batchref(3)
    add_stock(
        session_factory,
        [
            (laterbatch, sku, 100, "2011-01-02"),
            (earlybatch, sku, 100, "2011-01-01"),
            (otherbatch, othersku, 100, None),
        ],
    )
    data = {"orderid": random_orderid(), "sku": sku, "qty": 3}

    r = client.post("/allocate", json=data)

    assert r.status_code == 201
    assert r.json["batchref"] == earlybatch


def test_unhappy_path_returns_400_and_error_message(client):
    unknown_sku, orderid = random_sku(), random_orderid()
    data = {"orderid": orderid, "sku": unknown_sku, "qty": 20}

    r = client.post("/allocate", json=data)

    assert r.status_code == 400
    assert r.json["message"] == f"Invalid sku {unknown_sku}"
```

### Step 6: Run everything, and try the real app

```bash
pytest -q
# 24 passed

PYTHONPATH=src flask --app "allocation.entrypoints.flask_app:create_app" run
# (in another terminal; the app has no add_batch yet, so you'll get "Invalid sku" until ch05)
curl -s -X POST localhost:5000/allocate -H 'content-type: application/json' \
     -d '{"orderid":"o1","sku":"LAMP","qty":2}'
# {"message":"Invalid sku LAMP"}
```

### Stretch

- Add a test showing `OutOfStock` also becomes a 400 at the API.
- Look at `services.allocate`: which two lines would you still be nervous about if a second entrypoint (a CLI) called it? (Answer: `session.commit()` and where `repo` comes from, which is ch06's problem.)

## Check yourself
- Why should Flask code not call `model.allocate` directly?
- What does the service layer know that the domain doesn't, and vice versa?
- Why do service functions accept primitives?
- What two smells remain (hint: `session`, repo creation)?
