# Chapter 13: Dependency Injection (and Bootstrapping)

## ✈️ 30,000 ft
Handlers import `email`, `redis` and `unit_of_work` directly, so tests use `mock.patch`. Instead, **inject** dependencies explicitly and wire everything in one place, the **bootstrap script** (composition root).

## 🛫 10,000 ft
- **Dependency injection** = pass collaborators in, rather than importing them inside.
- **Composition root / bootstrap**: one module that builds the real objects (UoW, notifications, publisher), starts the ORM, and returns a ready-to-use `MessageBus`.
- Handlers receive dependencies by **function parameters**; the bootstrap inspects signatures and injects them (partial application).
- `MessageBus` becomes a **class** holding `uow`, `event_handlers`, `command_handlers`, not a module with globals.
- Fakes replace the real adapters in tests: `FakeNotifications`, `FakeUnitOfWork`, `FakeEventPublisher`. No more `mock.patch`.
- Trade-off: more wiring code, but explicit, testable, no import-time side effects.

### 🗺️ Diagram: composition root

```mermaid
flowchart TB
    subgraph Prod["Production wiring"]
        App[FastAPI app.py] --> Boot1["bootstrap()"]
        Boot1 --> RealUoW[SqlAlchemyUoW]
        Boot1 --> RealN[EmailNotifications]
        Boot1 --> RealP[redis publish]
    end
    subgraph Test["Test wiring"]
        Fix["bootstrap_test_app()"] --> Boot2["bootstrap(start_orm=False, ...)"]
        Boot2 --> FUoW[FakeUnitOfWork]
        Boot2 --> FN[FakeNotifications]
        Boot2 --> FP[fake publish]
    end
    Boot1 --> Bus
    Boot2 --> Bus
    Bus["MessageBus<br/>handlers with deps injected"]
```

```mermaid
sequenceDiagram
    participant B as bootstrap
    participant I as inject_dependencies
    participant H as handler(cmd, uow, notifications)
    B->>I: handler, {uow, notifications, publish}
    I->>I: inspect.signature(handler)
    I-->>B: partial(handler, uow=..., notifications=...)
    Note over B,H: bus calls handler(message) only
```

## 🛫 5,000 ft: the conventional way vs. the better way

> Evidence tags: 🧪 I ran it · 🧮 arithmetic · 📖 documentation or reasoning (not run).

### Addition 1: how handlers get their collaborators (email, Redis, the DB)

**The conventional way:** import them, and patch the import in tests.

```python
from allocation.adapters.email import send              # in the handler module

def send_out_of_stock_notification(event, uow):
    send("stock@made.com", f"Out of stock for {event.sku}")

def test_sends_email():
    with mock.patch("allocation.adapters.email.send") as m:      # looks right...
        ...
        assert m.called
```

**Where it breaks**
- 🧪 This test is wrong, and demonstrably so: `from ... import send` copied the function into the handler's own namespace, so patching `email.send` changes nothing. I ran the equivalent: the mock was **never called** and the **real `send` ran instead**. Patching must target the name *where it is used*, a rule people learn the hard way.
- Tests are welded to import paths: move a function to another module and every patch string is stale.
- Every test needs its ritual of `with mock.patch(...)` blocks.

**The better way** (Steps 2, 4): handlers **declare** what they need as parameters (`notifications`, `publish`, `uow`); `bootstrap.py` passes in real implementations, and tests pass fakes.

**Why it's right:** a test says `bootstrap(notifications=FakeNotifications())` and then asserts on something *you wrote* (`fake.sent[...]`). No path strings, no patch-the-right-name pitfalls.
**What it costs:** a composition-root module to read ("where does `publish` come from?" has the answer "bootstrap"), and parameter *names* now matter because injection matches by name.

### Addition 2: when are the connections made?

**The conventional way:** create clients and engines at import time.

```python
r = redis.Redis(host="redis", port=6379)                # module level
engine = create_engine(os.environ["DB_URI"])            # module level
app = FastAPI()
```

**Where it breaks**
- 🧪 We hit this in ch11: with a module-level Redis client, every test that allocated stock tried to reach a Redis that wasn't there. The errors were swallowed by the event-handler `try/except`, so the suite just got slower (about 0.4 s to 5 s) with no failure to point at; we needed an autouse fixture to stop it.
- Importing a module has side effects, so test order, environment variables and import order all start to matter.

**The better way** (Step 4): nothing is built until `bootstrap()` is called; every external dependency has a real default that tests override.

**Why it's right:** importing is free and predictable, and tests assemble exactly the world they want.
**What it costs:** discipline to keep all wiring in one place. Also: a bus carries per-message state (its queue) and its UoW stores a session on `self`, so one bus must not be shared across threads. FastAPI runs sync endpoints in a thread pool, so this chapter builds a bus **per request**; the book shares a single one.

## 🛬 1,000 ft: design decisions
- Adapters get their own abstract classes (`AbstractNotifications`) in `adapters/notifications.py`.
- `bootstrap(start_orm=True, uow=None, notifications=None, publish=redis_eventpublisher.publish)`; all keyword args with defaults (`None` means "build the real one"), so tests override just what they need.
- `inject_dependencies(handler, dependencies)` uses `inspect.signature` to bind only the parameters each handler declares.
- The bus: `MessageBus(uow, event_handlers, command_handlers).handle(message)`.
- FastAPI's `create_app(bus=None)` calls `bootstrap.bootstrap()` unless a bus is passed in, and the tests build a bus with `bootstrap(start_orm=False, uow=FakeUnitOfWork(), ...)`.
- The authors show alternative: a manual closure/lambda or class-based handlers; pick one, be consistent.

## 🛞 Touchdown: code along

> Starts from your finished ch12 project. Handlers currently `import` the things they use (the email module, the Redis publisher), so tests need `mock.patch`, and wiring is scattered across modules. This chapter **injects** dependencies and wires them in one place. 51 tests pass, 2 skipped (they need Postgres / the full Redis stack). I also ran the finished app for real (uvicorn + SQLite + curl) and it behaved correctly: 201 on `add_batch`, 202 on `allocate`, warehouse stock preferred over a shipment, 400 for an unknown SKU, 422 for invalid input, 404 for an unknown order.

### The target shape

```
bootstrap()  ── builds real uow / notifications / publish
     │          (tests pass fakes instead)
     ▼
inject_dependencies(handler, {...})   ← looks at the handler's parameter NAMES
     ▼
MessageBus(uow, event_handlers, command_handlers)   ← a class, not a module of globals
```

### Step 1: Put the notifications behind an abstraction

**Do:** create `adapters/notifications.py` and delete `adapters/email.py`. Add the two email fields to `Settings` (typed, like the Redis ones):

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
    redis_host: str = "localhost"
    redis_port: int = 6379
    email_host: str = "localhost"
    email_port: int = 1025


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

**Why:** the handler should ask for "something that can send a notification". It shouldn't care that it's SMTP. `EmailNotifications` connects at *send* time, so constructing it never requires a mail server.

**📄 `src/allocation/adapters/notifications.py`**

```python
import abc
import smtplib

from allocation import config


class AbstractNotifications(abc.ABC):
    @abc.abstractmethod
    def send(self, destination, message):
        raise NotImplementedError


class EmailNotifications(AbstractNotifications):
    def __init__(self, smtp_host=None, port=None):
        settings = config.get_settings()
        self.smtp_host = smtp_host or settings.email_host
        self.port = port or settings.email_port

    def send(self, destination, message):
        msg = f"Subject: allocation service notification\n{message}"
        # connect at send time, so constructing this never needs a mail server
        with smtplib.SMTP(self.smtp_host, port=self.port) as server:
            server.sendmail(from_addr="allocations@example.com", to_addrs=[destination], msg=msg)
```

### Step 2: Handlers declare their dependencies as parameters

**Do:** edit `handlers.py`. Where a handler used to import something, it now takes it as a parameter, **named** the same as the dependency:

| Handler | Parameters (after the message) |
|---|---|
| `add_batch`, `allocate`, `change_batch_quantity`, `reallocate`, read-model handlers | `uow` |
| `send_out_of_stock_notification` | `notifications` |
| `publish_allocated_event` | `publish` |

Also move the two registries (`EVENT_HANDLERS`, `COMMAND_HANDLERS`) to the bottom of this file, since the message bus won't import handlers any more.

**📄 `src/allocation/service_layer/handlers.py`**

```python
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import TYPE_CHECKING

from sqlalchemy import text

from allocation.domain import commands, events, model
from allocation.domain.model import OrderLine

if TYPE_CHECKING:
    from allocation.adapters import notifications

    from . import unit_of_work


class InvalidSku(Exception):
    pass


# Each handler declares what it needs as parameters. The bootstrap script
# looks at those names and passes in matching dependencies.
def add_batch(cmd: commands.CreateBatch, uow: unit_of_work.AbstractUnitOfWork):
    with uow:
        product = uow.products.get(sku=cmd.sku)
        if product is None:
            product = model.Product(cmd.sku, batches=[])
            uow.products.add(product)
        product.batches.append(model.Batch(cmd.ref, cmd.sku, cmd.qty, cmd.eta))
        uow.commit()


def allocate(cmd: commands.Allocate, uow: unit_of_work.AbstractUnitOfWork):
    line = OrderLine(cmd.orderid, cmd.sku, cmd.qty)
    with uow:
        product = uow.products.get(sku=line.sku)
        if product is None:
            raise InvalidSku(f"Invalid sku {line.sku}")
        batchref = product.allocate(line)
        uow.commit()
        return batchref


def change_batch_quantity(cmd: commands.ChangeBatchQuantity, uow: unit_of_work.AbstractUnitOfWork):
    with uow:
        product = uow.products.get_by_batchref(batchref=cmd.ref)
        product.change_batch_quantity(ref=cmd.ref, qty=cmd.qty)
        uow.commit()


# --- event handlers ---------------------------------------------------------
def send_out_of_stock_notification(
    event: events.OutOfStock, notifications: notifications.AbstractNotifications
):
    notifications.send("stock@made.com", f"Out of stock for {event.sku}")


def publish_allocated_event(event: events.Allocated, publish: Callable):
    publish("line_allocated", event)


def reallocate(event: events.Deallocated, uow: unit_of_work.AbstractUnitOfWork):
    allocate(commands.Allocate(**asdict(event)), uow=uow)


def add_allocation_to_read_model(event: events.Allocated, uow: unit_of_work.AbstractUnitOfWork):
    with uow:
        uow.session.execute(
            text(
                "INSERT INTO allocations_view (orderid, sku, batchref)"
                " VALUES (:orderid, :sku, :batchref)"
            ),
            dict(orderid=event.orderid, sku=event.sku, batchref=event.batchref),
        )
        uow.commit()


def remove_allocation_from_read_model(
    event: events.Deallocated, uow: unit_of_work.AbstractUnitOfWork
):
    with uow:
        uow.session.execute(
            text("DELETE FROM allocations_view WHERE orderid = :orderid AND sku = :sku"),
            dict(orderid=event.orderid, sku=event.sku),
        )
        uow.commit()


EVENT_HANDLERS: dict[type[events.Event], list[Callable]] = {
    events.Allocated: [publish_allocated_event, add_allocation_to_read_model],
    events.Deallocated: [remove_allocation_from_read_model, reallocate],
    events.OutOfStock: [send_out_of_stock_notification],
}

COMMAND_HANDLERS: dict[type[commands.Command], Callable] = {
    commands.Allocate: allocate,
    commands.CreateBatch: add_batch,
    commands.ChangeBatchQuantity: change_batch_quantity,
}
```

### Step 3: The message bus becomes a class

**Do:** rewrite `messagebus.py`. It no longer imports `handlers`; it is *given* the handlers (already wired) and a UoW. Same logic as ch10, with `self` instead of passing `queue` and `uow` around.

**📄 `src/allocation/service_layer/messagebus.py`**

```python
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from allocation.domain import commands, events

if TYPE_CHECKING:
    from . import unit_of_work

logger = logging.getLogger(__name__)

Message = commands.Command | events.Event


class MessageBus:
    def __init__(
        self,
        uow: unit_of_work.AbstractUnitOfWork,
        event_handlers: dict[type[events.Event], list[Callable]],
        command_handlers: dict[type[commands.Command], Callable],
    ):
        self.uow = uow
        self.event_handlers = event_handlers
        self.command_handlers = command_handlers

    def handle(self, message: Message):
        results = []
        self.queue = [message]
        while self.queue:
            message = self.queue.pop(0)
            if isinstance(message, events.Event):
                self.handle_event(message)
            elif isinstance(message, commands.Command):
                cmd_result = self.handle_command(message)
                results.append(cmd_result)
            else:
                raise Exception(f"{message} was not an Event or Command")
        return results

    def handle_event(self, event: events.Event):
        for handler in self.event_handlers[type(event)]:
            try:
                logger.debug("handling event %s with handler %s", event, handler)
                handler(event)
                self.queue.extend(self.uow.collect_new_events())
            except Exception:
                logger.exception("Exception handling event %s", event)
                continue

    def handle_command(self, command: commands.Command):
        logger.debug("handling command %s", command)
        try:
            handler = self.command_handlers[type(command)]
            result = handler(command)
            self.queue.extend(self.uow.collect_new_events())
            return result
        except Exception:
            logger.exception("Exception handling command %s", command)
            raise
```

### Step 4: The composition root, `bootstrap.py`

**Do:** create `allocation/bootstrap.py`. This is the **only** module that knows which concrete classes to use.

**Read it slowly:**
- Every parameter has a real default, so production calls `bootstrap()` with no arguments, and a test overrides **just what it needs**.
- `inject_dependencies` uses `inspect.signature` to see which of `uow / notifications / publish` a handler asks for, and binds only those. The bus then calls every handler as `handler(message)`.
- `start_orm=False` lets tests skip the global mapper setup.
- `make_bus_factory()` is for servers: it starts the mappers and builds the session factory **once**, and returns a function that builds a fresh bus (with its own UoW) each time it's called. Building a bus is just wiring a few closures, so doing it per request is cheap.

**📄 `src/allocation/bootstrap.py`**

```python
"""The composition root: the ONE place that knows about concrete classes."""

import inspect
from collections.abc import Callable

from allocation import config
from allocation.adapters import orm, redis_eventpublisher
from allocation.adapters.notifications import AbstractNotifications, EmailNotifications
from allocation.service_layer import handlers, messagebus, unit_of_work


def default_uow() -> unit_of_work.SqlAlchemyUnitOfWork:
    session_factory = orm.make_session_factory(config.get_settings().db_uri)
    return unit_of_work.SqlAlchemyUnitOfWork(session_factory)


def bootstrap(
    start_orm: bool = True,
    uow: unit_of_work.AbstractUnitOfWork | None = None,
    notifications: AbstractNotifications | None = None,
    publish: Callable = redis_eventpublisher.publish,
) -> messagebus.MessageBus:
    # Everything has a real default; tests override only what they need.
    if uow is None:
        uow = default_uow()
    if notifications is None:
        notifications = EmailNotifications()

    if start_orm:
        orm.start_mappers()

    dependencies = {"uow": uow, "notifications": notifications, "publish": publish}
    injected_event_handlers = {
        event_type: [inject_dependencies(handler, dependencies) for handler in handlers_]
        for event_type, handlers_ in handlers.EVENT_HANDLERS.items()
    }
    injected_command_handlers = {
        command_type: inject_dependencies(handler, dependencies)
        for command_type, handler in handlers.COMMAND_HANDLERS.items()
    }

    return messagebus.MessageBus(
        uow=uow,
        event_handlers=injected_event_handlers,
        command_handlers=injected_command_handlers,
    )


def make_bus_factory() -> Callable[[], messagebus.MessageBus]:
    """For servers that handle requests concurrently: one bus (and UoW) per request.

    Start the ORM mappers and build the session factory once; building a bus is
    just wiring a few closures, so doing it per request is cheap.
    """
    orm.start_mappers()
    session_factory = orm.make_session_factory(config.get_settings().db_uri)

    def bus_factory() -> messagebus.MessageBus:
        uow = unit_of_work.SqlAlchemyUnitOfWork(session_factory)
        return bootstrap(start_orm=False, uow=uow)

    return bus_factory


def inject_dependencies(handler, dependencies):
    """Return handler(message) with whichever of `dependencies` it asks for, by name."""
    params = inspect.signature(handler).parameters
    deps = {name: dep for name, dep in dependencies.items() if name in params}
    return lambda message: handler(message, **deps)
```

### Step 5: Entrypoints receive a bus (a fresh one per request)

**Do:** `create_app(bus_factory=None)` builds the real factory unless given one. A FastAPI **dependency**, `get_bus`, calls the factory, so every request gets its own bus. The Redis consumer calls `bootstrap.bootstrap()` once in `main()` and passes the bus into its handler function.

**Why a bus per request (this differs from the book):** the book creates one bus at startup and shares it. FastAPI runs our sync endpoints in a **thread pool**, so requests overlap in different threads. A bus holds per-message state (its queue), and the UoW stores its session on `self`; sharing one across threads means two requests can trample each other's session and queue. The consumer is a single loop, so one shared bus is fine there. (This is reasoning about the code, not something I demonstrated with a failing test.)

**📄 `src/allocation/entrypoints/fastapi_app.py`**

```python
from collections.abc import Callable
from datetime import date
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from allocation import bootstrap, views
from allocation.domain import commands
from allocation.service_layer import handlers, messagebus


class AddBatchRequest(BaseModel):
    ref: str
    sku: str
    qty: int = Field(gt=0)
    eta: date | None = None  # pydantic parses "2030-01-01" for us


class AllocateRequest(BaseModel):
    orderid: str
    sku: str
    qty: int = Field(gt=0)


class AllocateResponse(BaseModel):
    batchref: str


class AllocationOut(BaseModel):
    sku: str
    batchref: str


def create_app(bus_factory: Callable[[], messagebus.MessageBus] | None = None) -> FastAPI:
    # Tests pass in a factory wired with fakes; production builds the real one.
    bus_factory = bus_factory or bootstrap.make_bus_factory()
    app = FastAPI(title="Allocation service")

    def get_bus() -> messagebus.MessageBus:
        # A fresh bus (and so a fresh UoW and queue) per request. FastAPI runs
        # these sync endpoints in a thread pool, so sharing one would be a race.
        return bus_factory()

    Bus = Annotated[messagebus.MessageBus, Depends(get_bus)]

    @app.post("/add_batch", status_code=201)
    def add_batch_endpoint(body: AddBatchRequest, bus: Bus) -> None:
        bus.handle(commands.CreateBatch(body.ref, body.sku, body.qty, body.eta))

    @app.post("/allocate", status_code=202)
    def allocate_endpoint(body: AllocateRequest, bus: Bus) -> None:
        try:
            bus.handle(commands.Allocate(body.orderid, body.sku, body.qty))
        except handlers.InvalidSku as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.get("/allocations/{orderid}")
    def allocations_endpoint(orderid: str, bus: Bus) -> list[AllocationOut]:
        result = views.allocations(orderid, bus.uow)
        if not result:
            raise HTTPException(status_code=404, detail="not found")
        return result

    return app
```

**📄 `src/allocation/entrypoints/redis_eventconsumer.py`**

```python
import logging

import redis
from pydantic import BaseModel, Field, ValidationError

from allocation import bootstrap, config
from allocation.domain import commands
from allocation.service_layer import messagebus

logger = logging.getLogger(__name__)


class ChangeBatchQuantityMessage(BaseModel):
    """The other service's wire format, validated at the edge before it becomes a command."""

    batchref: str
    qty: int = Field(ge=0)


def main():
    bus = bootstrap.bootstrap()
    settings = config.get_settings()
    r = redis.Redis(host=settings.redis_host, port=settings.redis_port)
    pubsub = r.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe("change_batch_quantity")

    for m in pubsub.listen():
        handle_change_batch_quantity(m, bus)


def handle_change_batch_quantity(m, bus: messagebus.MessageBus):
    logger.info("handling %s", m)
    try:
        msg = ChangeBatchQuantityMessage.model_validate_json(m["data"])
    except ValidationError:
        logger.exception("dropping malformed message %s", m)
        return
    # The wire format belongs to the OTHER service; translate it into our command.
    bus.handle(commands.ChangeBatchQuantity(ref=msg.batchref, qty=msg.qty))


if __name__ == "__main__":
    main()
```

### Step 6: Rewrite the tests: zero `mock.patch` for handlers

**Do:**
1. `tests/conftest.py`: **delete** the autouse `fake_redis` fixture from ch11 (injection makes it unnecessary) and add a `bus_factory` fixture (plus `sqlite_bus`, one bus from it): a real bus over SQLite with only the outside world faked:

**📄 `tests/conftest.py`**

```python
import os
from collections import defaultdict

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import clear_mappers, sessionmaker

from allocation import bootstrap
from allocation.adapters import notifications
from allocation.adapters.orm import metadata, start_mappers
from allocation.service_layer import unit_of_work


class FakeNotifications(notifications.AbstractNotifications):
    def __init__(self):
        self.sent: defaultdict[str, list[str]] = defaultdict(list)

    def send(self, destination, message):
        self.sent[destination].append(message)


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


@pytest.fixture
def postgres_session_factory():
    """Only used by the concurrency test, which needs a real Postgres.
    Run with:  POSTGRES_URI=postgresql+psycopg://allocation:abc123@localhost:5432/allocation pytest
    """
    uri = os.environ.get("POSTGRES_URI")
    if not uri:
        pytest.skip("set POSTGRES_URI to run tests that need Postgres")
    engine = create_engine(uri, isolation_level="REPEATABLE READ")
    metadata.drop_all(engine)
    metadata.create_all(engine)
    start_mappers()
    yield sessionmaker(bind=engine)
    clear_mappers()
    engine.dispose()


@pytest.fixture
def bus_factory(session_factory):
    """Each call builds a real bus over the test SQLite database, with the outside world faked."""

    def make():
        return bootstrap.bootstrap(
            start_orm=False,  # the session_factory fixture already started the mappers
            uow=unit_of_work.SqlAlchemyUnitOfWork(session_factory),
            notifications=FakeNotifications(),
            publish=lambda channel, event: None,
        )

    return make


@pytest.fixture
def sqlite_bus(bus_factory):
    return bus_factory()
```

2. `tests/unit/test_handlers.py`: build the app with `bootstrap_test_app()`. The email and publish tests now check **fakes you wrote**, not call signatures on a mock:

**📄 `tests/unit/test_handlers.py`**

```python
from collections import defaultdict
from datetime import date

import pytest

from allocation import bootstrap
from allocation.adapters import notifications, repository
from allocation.domain import commands, events
from allocation.service_layer import handlers, unit_of_work


class FakeRepository(repository.AbstractRepository):
    def __init__(self, products):
        super().__init__()
        self._products = set(products)

    def _add(self, product):
        self._products.add(product)

    def _get(self, sku):
        return next((p for p in self._products if p.sku == sku), None)

    def _get_by_batchref(self, batchref):
        return next(
            (p for p in self._products for b in p.batches if b.reference == batchref),
            None,
        )


class FakeSession:
    """Swallows the raw SQL that the read-model handlers run (tested in test_views)."""

    def execute(self, *args, **kwargs):
        pass


class FakeUnitOfWork(unit_of_work.AbstractUnitOfWork):
    def __init__(self):
        self.products = FakeRepository([])
        self.session = FakeSession()
        self.committed = False

    def _commit(self):
        self.committed = True

    def rollback(self):
        pass


class FakeNotifications(notifications.AbstractNotifications):
    def __init__(self):
        self.sent: defaultdict[str, list[str]] = defaultdict(list)

    def send(self, destination, message):
        self.sent[destination].append(message)


def bootstrap_test_app():
    """A real bus, real handlers, real wiring: only the edges are fake."""
    return bootstrap.bootstrap(
        start_orm=False,
        uow=FakeUnitOfWork(),
        notifications=FakeNotifications(),
        publish=lambda *args: None,
    )


class TestAddBatch:
    def test_for_new_product(self):
        bus = bootstrap_test_app()
        bus.handle(commands.CreateBatch("b1", "CRUNCHY-ARMCHAIR", 100, None))
        assert bus.uow.products.get("CRUNCHY-ARMCHAIR") is not None
        assert bus.uow.committed

    def test_for_existing_product(self):
        bus = bootstrap_test_app()
        bus.handle(commands.CreateBatch("b1", "GARISH-RUG", 100, None))
        bus.handle(commands.CreateBatch("b2", "GARISH-RUG", 99, None))
        assert "b2" in [b.reference for b in bus.uow.products.get("GARISH-RUG").batches]


class TestAllocate:
    def test_allocates(self):
        bus = bootstrap_test_app()
        bus.handle(commands.CreateBatch("batch1", "COMPLICATED-LAMP", 100, None))
        results = bus.handle(commands.Allocate("o1", "COMPLICATED-LAMP", 10))
        assert results.pop(0) == "batch1"
        [batch] = bus.uow.products.get("COMPLICATED-LAMP").batches
        assert batch.available_quantity == 90

    def test_errors_for_invalid_sku(self):
        bus = bootstrap_test_app()
        bus.handle(commands.CreateBatch("b1", "AREALSKU", 100, None))

        with pytest.raises(handlers.InvalidSku, match="Invalid sku NONEXISTENTSKU"):
            bus.handle(commands.Allocate("o1", "NONEXISTENTSKU", 10))

    def test_commits(self):
        bus = bootstrap_test_app()
        bus.handle(commands.CreateBatch("b1", "OMINOUS-MIRROR", 100, None))
        bus.uow.committed = False

        bus.handle(commands.Allocate("o1", "OMINOUS-MIRROR", 10))
        assert bus.uow.committed

    def test_sends_email_on_out_of_stock_error(self):
        fake_notifs = FakeNotifications()
        bus = bootstrap.bootstrap(
            start_orm=False,
            uow=FakeUnitOfWork(),
            notifications=fake_notifs,
            publish=lambda *args: None,
        )
        bus.handle(commands.CreateBatch("b1", "POPULAR-CURTAINS", 9, None))

        bus.handle(commands.Allocate("o1", "POPULAR-CURTAINS", 10))

        assert fake_notifs.sent["stock@made.com"] == ["Out of stock for POPULAR-CURTAINS"]

    def test_publishes_an_allocated_event(self):
        published = []
        bus = bootstrap.bootstrap(
            start_orm=False,
            uow=FakeUnitOfWork(),
            notifications=FakeNotifications(),
            publish=lambda channel, event: published.append((channel, event)),
        )
        bus.handle(commands.CreateBatch("b1", "LAMP", 100, None))

        bus.handle(commands.Allocate("o1", "LAMP", 10))

        assert published == [("line_allocated", events.Allocated("o1", "LAMP", 10, "b1"))]


class TestChangeBatchQuantity:
    def test_changes_available_quantity(self):
        bus = bootstrap_test_app()
        bus.handle(commands.CreateBatch("batch1", "ADORABLE-SETTEE", 100, None))
        [batch] = bus.uow.products.get(sku="ADORABLE-SETTEE").batches
        assert batch.available_quantity == 100

        bus.handle(commands.ChangeBatchQuantity("batch1", 50))

        assert batch.available_quantity == 50

    def test_reallocates_if_necessary(self):
        bus = bootstrap_test_app()
        history = [
            commands.CreateBatch("batch1", "INDIFFERENT-TABLE", 50, None),
            commands.CreateBatch("batch2", "INDIFFERENT-TABLE", 50, date.today()),
            commands.Allocate("order1", "INDIFFERENT-TABLE", 20),
            commands.Allocate("order2", "INDIFFERENT-TABLE", 20),
        ]
        for msg in history:
            bus.handle(msg)
        [batch1, batch2] = bus.uow.products.get(sku="INDIFFERENT-TABLE").batches
        assert batch1.available_quantity == 10
        assert batch2.available_quantity == 50

        bus.handle(commands.ChangeBatchQuantity("batch1", 25))

        # order1 or order2 will be deallocated, so we'll have 25 - 20
        assert batch1.available_quantity == 5
        # and 20 will be reallocated to the next batch
        assert batch2.available_quantity == 30


class TestEventErrors:
    def test_a_failing_event_handler_does_not_stop_the_others(self):
        calls = []

        def bad(event):
            raise RuntimeError("boom")

        def good(event):
            calls.append(event)

        bus = bootstrap_test_app()
        bus.event_handlers = {events.OutOfStock: [bad, good]}

        bus.handle(events.OutOfStock("SOME-SKU"))

        assert calls == [events.OutOfStock("SOME-SKU")]

    def test_command_errors_propagate_to_the_caller(self):
        bus = bootstrap_test_app()
        with pytest.raises(handlers.InvalidSku):
            bus.handle(commands.Allocate("o1", "NOPE", 1))
```

3. `tests/unit/test_redis.py`: the consumer now takes a bus. We still fake the Redis *client* for the one test that checks the adapter itself, because that test is *about* the adapter:

**📄 `tests/unit/test_redis.py`**

```python
import json
from unittest import mock

import pytest

from allocation.adapters import redis_eventpublisher
from allocation.domain import commands, events
from allocation.entrypoints import redis_eventconsumer


def test_publish_serialises_the_event_as_json():
    # The one place we still fake a library: this is a test OF the Redis adapter.
    with mock.patch.object(redis_eventpublisher, "r") as fake_client:
        redis_eventpublisher.publish("line_allocated", events.Allocated("o1", "SOME-SKU", 5, "b1"))
    channel, payload = fake_client.publish.call_args.args
    assert channel == "line_allocated"
    assert json.loads(payload) == {
        "orderid": "o1",
        "sku": "SOME-SKU",
        "qty": 5,
        "batchref": "b1",
    }


def test_consumer_turns_a_message_into_a_command():
    message = {"data": json.dumps({"batchref": "b1", "qty": 10})}
    bus = mock.Mock()

    redis_eventconsumer.handle_change_batch_quantity(message, bus)

    bus.handle.assert_called_once_with(commands.ChangeBatchQuantity(ref="b1", qty=10))


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        json.dumps({"batchref": "b1"}),  # qty missing
        json.dumps({"batchref": "b1", "qty": "lots"}),  # wrong type
        json.dumps({"batchref": "b1", "qty": -5}),  # negative
    ],
)
def test_consumer_drops_malformed_messages(payload):
    message = {"data": payload}
    bus = mock.Mock()

    redis_eventconsumer.handle_change_batch_quantity(message, bus)

    bus.handle.assert_not_called()
```

4. `tests/integration/test_views.py` uses `sqlite_bus` (`bus.handle(...)` instead of `messagebus.handle(..., uow)`). The e2e `client` fixture uses `create_app(bus_factory)`, and a new test asserts that **each request gets its own bus**:

**📄 `tests/integration/test_views.py`**

```python
from datetime import date

from allocation import views
from allocation.domain import commands

today = date.today()


def test_allocations_view(sqlite_bus):
    sqlite_bus.handle(commands.CreateBatch("sku1batch", "sku1", 50, None))
    sqlite_bus.handle(commands.CreateBatch("sku2batch", "sku2", 50, today))
    sqlite_bus.handle(commands.Allocate("order1", "sku1", 20))
    sqlite_bus.handle(commands.Allocate("order1", "sku2", 20))
    # add a spurious batch and order to make sure we're getting the right ones
    sqlite_bus.handle(commands.CreateBatch("sku1batch-later", "sku1", 50, today))
    sqlite_bus.handle(commands.Allocate("otherorder", "sku1", 30))
    sqlite_bus.handle(commands.Allocate("otherorder", "sku2", 10))

    assert views.allocations("order1", sqlite_bus.uow) == [
        {"sku": "sku1", "batchref": "sku1batch"},
        {"sku": "sku2", "batchref": "sku2batch"},
    ]


def test_deallocation_updates_the_view(sqlite_bus):
    sqlite_bus.handle(commands.CreateBatch("b1", "sku1", 50, None))
    sqlite_bus.handle(commands.CreateBatch("b2", "sku1", 50, today))
    sqlite_bus.handle(commands.Allocate("o1", "sku1", 40))
    assert views.allocations("o1", sqlite_bus.uow) == [{"sku": "sku1", "batchref": "b1"}]

    sqlite_bus.handle(commands.ChangeBatchQuantity("b1", 10))

    assert views.allocations("o1", sqlite_bus.uow) == [{"sku": "sku1", "batchref": "b2"}]
```

```bash
uv run pytest -q
# 51 passed, 2 skipped
grep -rn "mock.patch" tests/unit/test_handlers.py     # → nothing
```

### Run the finished app

```bash
export DB_URI=sqlite:///demo.db
uv run uvicorn --factory allocation.entrypoints.fastapi_app:create_app --port 5055

curl -X POST localhost:5055/add_batch -H 'content-type: application/json' \
     -d '{"ref":"b-stock","sku":"LAMP","qty":100,"eta":null}'                       # 201
curl -X POST localhost:5055/allocate  -H 'content-type: application/json' \
     -d '{"orderid":"o1","sku":"LAMP","qty":10}'                                    # 202
curl localhost:5055/allocations/o1                                                  # [{"sku":"LAMP","batchref":"b-stock"}]
curl -X POST localhost:5055/allocate  -H 'content-type: application/json' \
     -d '{"orderid":"o2","sku":"LAMP","qty":0}'                                     # 422: pydantic said no
```

Open <http://localhost:5055/docs> for the generated API docs.

(With no Redis running, the `Allocated` → publish handler fails, is logged, and swallowed, and the allocation still succeeds. That is the event-handler policy from ch10 in action.)

### The whole toolchain, once more

```bash
uv run pytest -q          # tests            -> 51 passed, 2 skipped
uv run ruff check         # lint             -> All checks passed!
uv run ruff format        # format
uv run ty check src       # types            -> All checks passed!
```

### Stretch

- **Break it on purpose:** change `get_bus` to return one bus created at startup and hit the API from two threads at once. Predict what goes wrong first (the queue? the session?) before you run it, then see if you were right.
- Add `SmsNotifications` next to `EmailNotifications`, and make `bootstrap` choose via a setting in `Settings`. Which files change? (Only `notifications.py`, `config.py` and `bootstrap.py`.)
- Give the abstract UoW a better read-side story than "it has a `session`": for example a `Protocol` with an `execute_read(sql, params)` method. What would `views.py` and the fake UoW look like?

## Check yourself
- Where is the *only* place that knows the concrete classes?
- Why prefer keyword defaults in `bootstrap` for testing?
- What's the difference between dependency inversion and dependency injection?
- When is a DI container (library) worthwhile, and when is manual wiring better?
