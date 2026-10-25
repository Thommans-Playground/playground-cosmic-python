# Chapter 4: Our First Use Case: A Web API and the Service Layer (FastAPI)

## ✈️ 30,000 ft
You now have a model and storage, but nobody can use them. Add a thin web endpoint, but put the *orchestration* (fetch → validate → call model → save) in a **service layer**, not in FastAPI. Then the same use case can be driven by an API, a CLI or a message queue.

## 🛫 10,000 ft
- **Entrypoint / adapter** (FastAPI): translate HTTP → function call → HTTP. No business logic.
- **Service layer / application layer**: one function per *use case* (`allocate`, `add_batch`). Does the plumbing: repo + session + domain.
- **Domain model**: does the business rules.
- Services depend on the **abstract** repo, so tests can inject a fake.
- Trade-off: a "pointless" extra layer for trivial apps; essential once there are multiple entrypoints.

### 🗺️ Diagram: layers and one request

```mermaid
flowchart TB
    Client([HTTP client]) --> FastAPI["FastAPI entrypoint<br/>HTTP in, HTTP out"]
    FastAPI --> Svc["Service layer<br/>orchestrates the use case"]
    Svc --> Repo[AbstractRepository]
    Svc --> Model["Domain model<br/>business rules"]
    Repo --> SQL[SqlAlchemyRepository] --> DB[(Postgres)]
    Repo -.-> Fake[FakeRepository<br/>in tests]
```

```mermaid
sequenceDiagram
    participant C as Client
    participant F as FastAPI
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

## 🛫 5,000 ft: the conventional way vs. the better way

> Evidence tags: 🧪 I ran it · 🧮 arithmetic · 📖 documentation or reasoning (not run).

### Addition 1: where the use case lives

**The conventional way:** everything in the endpoint function.

```python
@app.post("/allocate", status_code=201)
def allocate_endpoint(body: AllocateRequest):
    session = get_session()
    batches = session.query(Batch).all()
    if body.sku not in {b.sku for b in batches}:
        raise HTTPException(400, "Invalid sku")
    line = OrderLine(body.orderid, body.sku, body.qty)
    try:
        batchref = allocate(line, batches)
    except OutOfStock as e:
        raise HTTPException(400, str(e))
    session.commit()
    return {"batchref": batchref}
```

**Where it breaks**
- Next quarter a queue consumer or CLI needs the same use case. The sensible shortcut is copy-paste: now SKU validation and the commit exist twice.
- Someone fixes a bug in the web copy ("also reject qty <= 0") and the consumer keeps accepting bad data.
- Every business-rule test needs a test client and a database, because the rules are fused with HTTP.

**The better way** (Steps 3-4): a **service layer** function for the use case; FastAPI only translates (JSON in, call, status code out).

**Why it's right:** one definition of "allocate an order" that every entrypoint calls, and it can be tested with a fake repository, no HTTP.
**What it costs:** an extra layer and a function signature to maintain. With exactly one entrypoint and little logic it is overhead (the book is explicit about this).

### Addition 2: what the service accepts and returns

**The conventional way:** pass the framework through.

```python
def allocate(request: Request):             # a Starlette/FastAPI Request
    ...
    return JSONResponse({"batchref": ref}, status_code=201)   # a web response
```

**Where it breaks:** a CLI has no `request` to hand over, so you fake one or duplicate the function. Status codes now sit inside business logic, and you can't change frameworks without rewriting the use cases.

**The better way** (Step 3): primitives in (`orderid: str, sku: str, qty: int`), a plain value or a named exception out (`InvalidSku`). The entrypoint maps exceptions to status codes.

**Why it's right:** callers need to know nothing about the domain classes or the web.
**What it costs:** a little mapping code at each entrypoint (here, one `except` and two status codes).

## 🛬 1,000 ft: design decisions
- `services.allocate(orderid, sku, qty, repo, session)` takes primitives, not domain objects. The API layer needn't know about the model.
- It raises `InvalidSku` for bad input, and the FastAPI handler maps exceptions to 400/404/201.
- Service tests use `FakeRepository` and a `FakeSession` with `committed = False`. (Ch06 will replace the fake session. Notice the smell.)
- The e2e test posts to `/allocate` against a real database (ch05 adds `/add_batch` so the test can seed data through the API).
- Smell the authors acknowledge: the service still knows about `session.commit()`, and the API still creates the repo.

## 🛞 Touchdown: code along

> Starts from your finished ch02 project. New: `repository.list()`, `service_layer/services.py`, `config.py`, `entrypoints/fastapi_app.py`, unit tests with fakes, one end-to-end test. All code below was run and passes (26 tests).

```bash
uv add fastapi uvicorn pydantic-settings
uv add --dev httpx2
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

**Why:** the point of the service layer is that the use case can be tested **without FastAPI and without a database**. Notice the smell: the test has to know about a `session` and `commit`. Chapter 6 removes that.

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
- It takes **primitives** (`orderid: str`, `sku: str`, `qty: int`), not `OrderLine`. Callers (FastAPI, CLI, queue consumers) shouldn't have to know the domain classes.
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


def allocate(orderid: str, sku: str, qty: int, repo: repository.AbstractRepository, session) -> str:
    line = model.OrderLine(orderid, sku, qty)
    batches = repo.list()
    if not is_valid_sku(line.sku, batches):
        raise InvalidSku(f"Invalid sku {line.sku}")
    batchref = model.allocate(line, batches)
    session.commit()
    return batchref
```

### Step 4: Typed settings with pydantic-settings

**Do:** create `config.py`.

**Why:** configuration read with `os.environ.get(...)` is untyped strings scattered through the code; a typo, or `REDIS_PORT=banana`, only surfaces when something finally tries to connect. `pydantic-settings` gives one typed `Settings` class: values are validated when it's created, defaults live in one place, and a local `.env` file works too. Setting `DB_URI=...` in the environment overrides the field `db_uri`.

**📄 `src/allocation/config.py`**

```python
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed settings, read from environment variables (DB_URI=...) or a .env file.

    Invalid values (REDIS_PORT=banana) fail at startup with a clear error,
    not later when something first tries to connect.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    db_uri: str = "sqlite:///allocation.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

SQLite by default means the app runs with zero setup. To use Postgres later: `uv add "psycopg[binary]"` and `DB_URI=postgresql+psycopg://allocation:abc123@localhost:5432/allocation`.

### Step 5: The thin FastAPI entrypoint, with pydantic at the edge

**Do:** create `entrypoints/fastapi_app.py`.

**Why each part is there:**
- **`AllocateRequest` / `AllocateResponse` are pydantic models.** They are the API's contract: FastAPI parses and validates the JSON body *before your code runs* (`qty` must be greater than 0, otherwise the client gets a 422 with a precise message), and it generates the OpenAPI docs from them.
- **Pydantic stays at the edge.** The domain model stays plain Python. If `model.py` imported pydantic or FastAPI, the dependency arrow from ch01-02 would point outward again. The endpoint turns the validated request into primitives and calls the service.
- **`HTTPException`** is how this layer turns a domain/service error into a status code. The service layer knows nothing about HTTP.
- **`get_session` is a `yield` dependency.** FastAPI runs the code after the `yield` once the response is done, so the session is always closed. (Ch06's Unit of Work replaces this.)
- **The endpoints are plain `def`, not `async def`.** Our SQLAlchemy session is synchronous, and FastAPI runs sync endpoints in a thread pool. An `async def` endpoint making blocking database calls would block the whole event loop.
- **`create_app(session_factory)` is a factory** so tests can hand it a throwaway database.

**📄 `src/allocation/entrypoints/fastapi_app.py`**

```python
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from allocation import config
from allocation.adapters import orm, repository
from allocation.domain import model
from allocation.service_layer import services


class AllocateRequest(BaseModel):
    orderid: str
    sku: str
    qty: int = Field(gt=0)


class AllocateResponse(BaseModel):
    batchref: str


def create_app(session_factory=None) -> FastAPI:
    if session_factory is None:
        engine = create_engine(config.get_settings().db_uri)
        orm.metadata.create_all(engine)
        orm.start_mappers()
        session_factory = sessionmaker(bind=engine)

    app = FastAPI(title="Allocation service")

    def get_session() -> Iterator[Session]:
        # A "yield" dependency: FastAPI runs the code after the yield once the
        # response is done, so the session is always closed.
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    DbSession = Annotated[Session, Depends(get_session)]

    @app.post("/allocate", status_code=201)
    def allocate_endpoint(body: AllocateRequest, session: DbSession) -> AllocateResponse:
        repo = repository.SqlAlchemyRepository(session)
        try:
            batchref = services.allocate(body.orderid, body.sku, body.qty, repo, session)
        except (model.OutOfStock, services.InvalidSku) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return AllocateResponse(batchref=batchref)

    return app
```

### Step 5b: End-to-end tests with `TestClient`

**Do:** add the file-based `session_factory` fixture to `tests/conftest.py` (full file shown) and write `tests/e2e/test_api.py`.

**Why:** `TestClient` runs the app in-process: no server, no port. (It needs `httpx2`; Starlette now prefers it over `httpx` and warns if you use the old one.) These go through the real HTTP layer and a real database, so they're slower than the unit tests; keep few of them. The `422` test shows pydantic rejecting bad input **before any of our code ran**.

> Seeding data: at this point the API has no way to add a batch, so the test inserts rows directly with the ORM. Chapter 5 adds an `add_batch` endpoint and removes this.

**📄 `tests/conftest.py`**

```python
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import clear_mappers, sessionmaker

from allocation.adapters.orm import metadata, start_mappers


@pytest.fixture
def in_memory_db():
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(in_memory_db):
    start_mappers()
    session = sessionmaker(bind=in_memory_db)()
    yield session
    session.close()
    clear_mappers()


@pytest.fixture
def session_factory(tmp_path):
    """A file-based SQLite DB, so separate sessions behave like separate connections."""
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    metadata.create_all(engine)
    start_mappers()
    factory = sessionmaker(bind=engine)
    opened = []

    def tracked():
        session = factory()
        opened.append(session)
        return session

    yield tracked
    for session in opened:  # close everything the test (or the app under test) opened
        session.close()
    clear_mappers()
    engine.dispose()
```

**📄 `tests/e2e/test_api.py`**

```python
import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient

from allocation.domain import model
from allocation.entrypoints.fastapi_app import create_app


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
    return TestClient(create_app(session_factory))


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
    assert r.json()["batchref"] == earlybatch


def test_unhappy_path_returns_400_and_error_message(client):
    unknown_sku, orderid = random_sku(), random_orderid()
    data = {"orderid": orderid, "sku": unknown_sku, "qty": 20}

    r = client.post("/allocate", json=data)

    assert r.status_code == 400
    assert r.json()["detail"] == f"Invalid sku {unknown_sku}"


@pytest.mark.parametrize("qty", [0, -3])
def test_non_positive_quantity_is_rejected_by_validation(client, qty):
    r = client.post("/allocate", json={"orderid": "o1", "sku": "ANY-SKU", "qty": qty})

    assert r.status_code == 422  # rejected by pydantic before any of our code ran
```

### Step 6: Run everything, then the real app

```bash
uv run pytest -q
# 26 passed

uv run uvicorn --factory allocation.entrypoints.fastapi_app:create_app --port 5055
```

(`--factory` because `create_app` is a function that returns the app.) In another terminal:

```bash
curl -s -X POST localhost:5055/allocate -H 'content-type: application/json' \
     -d '{"orderid":"o1","sku":"LAMP","qty":2}'
# {"detail":"Invalid sku LAMP"}
```

Open <http://localhost:5055/docs>: FastAPI built interactive API docs from your pydantic models. Try `qty: 0` there and read the 422. (There is no `add_batch` yet, hence "Invalid sku" until ch05.)

### Stretch

- Add a test showing `OutOfStock` also becomes a 400 at the API.
- Look at `services.allocate`: which two lines would you still be nervous about if a second entrypoint (a CLI) called it? (Answer: `session.commit()` and where `repo` comes from, which is ch06's problem.)

## Check yourself
- Why should FastAPI code not call `model.allocate` directly?
- What does the service layer know that the domain doesn't, and vice versa?
- Why do service functions accept primitives?
- What two smells remain (hint: `session`, repo creation)?
