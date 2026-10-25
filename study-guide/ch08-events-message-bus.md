# Chapter 8: Events and the Message Bus

## ✈️ 30,000 ft
New requirement: "when a SKU is out of stock, email the purchasing team." Bolting that into `allocate` (or the service, or the web handler) tangles unrelated concerns. Instead, the model **records an event** and a **message bus** routes it to handlers.

## 🛫 10,000 ft
- **Single Responsibility Principle**: "allocate" should not know about email.
- **Domain event**: a fact that happened, named in past tense (`OutOfStock(sku)`), as a dataclass.
- **Message bus**: maps event types → list of handlers.
- The aggregate collects events in `product.events`; the service layer / UoW publishes them after the work is done.
- Option progression the book walks through:
  1. Put email in the web controller ✗
  2. Put it in the model ✗ (model shouldn't do I/O)
  3. Put it in the service ✗ (muddy)
  4. **Events + bus** ✓
- Trade-off: control flow becomes indirect: harder to follow with "who handles this?" questions.

### 🗺️ Diagram: events and the bus

```mermaid
sequenceDiagram
    participant API as FastAPI
    participant H as Service
    participant P as Product (model)
    participant U as UoW
    participant B as MessageBus
    participant E as send_out_of_stock_notification
    API->>H: allocate(...)
    H->>U: with uow
    H->>P: product.allocate(line)
    P->>P: no stock, append OutOfStock to events
    H->>U: uow.commit()
    U->>B: publish_events() then handle(event)
    B->>E: OutOfStock(sku)
    E->>E: email.send(purchasing team)
```

```mermaid
flowchart LR
    M["Model<br/>records events"] -->|events list| U["UoW<br/>collects them"]
    U --> Bus{{MessageBus}}
    Bus --> H1[Email handler]
    Bus --> H2[Other handlers]
```

## 🛫 5,000 ft: the conventional way vs. the better way

> Evidence tags: 🧪 I ran it · 🧮 arithmetic · 📖 documentation or reasoning (not run).

### Addition 1: "email purchasing when we run out of stock"

**The conventional way:** just send it where the failure is detected.

```python
def allocate(orderid, sku, qty, uow):
    with uow:
        product = uow.products.get(sku=sku)
        batchref = product.allocate(OrderLine(orderid, sku, qty))
        if batchref is None:
            email.send("stock@made.com", f"Out of stock for {sku}")   # I/O inline
        uow.commit()
    return batchref
```

**Where it breaks**
- Ordering has no good answer. Send *before* the commit and you can email about a transaction that later rolls back. Send *after* and a failing mail server turns a finished piece of work into an exception. 🧪 (the same effect as ch06's demo: the work was persisted, the caller saw an error, and a retry duplicates it.)
- Every test of `allocate` now needs to stub out email.
- The next request ("also post to Slack, also update the dashboard") edits the same function. It grows into a god-function that knows about every consumer of its results.

**The better way** (Steps 3-5): the model *records a fact* (`OutOfStock(sku)`) and a **handler** reacts to it, wired through a small message bus.

**Why it's right:** `allocate` stays about allocation. Adding "also post to Slack" is appending one function to a list; nothing existing changes.
**What it costs:** the control flow is now indirect. Reading `allocate` you can't see that an email goes out; you have to look up `OutOfStock` in the handler table. Debugging means following events.

### Addition 2: where does the model hand off the side-effect?

**The conventional way:** the domain model does the I/O itself.

```python
class Product:
    def allocate(self, line):
        ...
        except StopIteration:
            email.send("stock@made.com", f"Out of stock for {line.sku}")
```

**Where it breaks:** the model imports infrastructure, so every domain test needs a mail stub, and a script that merely *uses* the model (a simulation, a data fix) can send real emails by accident. It also undoes the whole of ch01-ch02: the pure model is gone.

**The better way** (Step 3): `Product` appends an event to `self.events`; it never learns who listens.

**Why it's right:** the domain stays free of I/O, and recording an event is a plain assertion in a unit test (`product.events[-1] == OutOfStock(...)`).
**What it costs:** one more concept (events) and a small ORM gotcha: SQLAlchemy skips `__init__` when loading, so you need the `load` listener from Step 3.

### Addition 3: who publishes the events?

**The conventional way:** each service remembers to call `messagebus.handle(...)` after committing.

**Where it breaks:** forgetting it in one of the services gives you a use case that silently never notifies. No test fails; the email just never arrives.

**The better way** (Step 5): the repository records every product it hands out (`seen`), and the Unit of Work publishes their events right after a successful commit, for every use case.

**Why it's right:** it can't be forgotten, because it isn't the service's job.
**What it costs:** the UoW now depends on the message bus. Ch09 reverses that so the bus pulls events from the UoW instead.

## 🛬 1,000 ft: design decisions
- `events.py`: `Event` base + `OutOfStock`.
- `Product.allocate` now appends `events.OutOfStock(sku)` and returns `None` instead of raising.
- `messagebus.handle(event)` looks up `HANDLERS[type(event)]`.
- Naive approach: every service calls `messagebus.handle(...)` for each event in `product.events` after committing. Smelly: each service has to remember to do it.
- Better: the repository records every product it hands out (`seen`), and the **UoW** publishes their events in `commit()` (via `publish_events()`). Ch09 flips the direction: the bus *pulls* with `collect_new_events()`.
- Handler example: `send_out_of_stock_notification` (use a patched `email.send` in the tests, one of the few justified mocks).

## 🛞 Touchdown: code along

> Starts from your finished ch07 project. New requirement: **email the purchasing team when a SKU is out of stock.** New files: `domain/events.py`, `adapters/email.py`, `service_layer/handlers.py`, `service_layer/messagebus.py`. 37 tests pass, 1 skipped (the Postgres one).

### Why not just call `email.send()` where the failure happens?

| Place | Problem |
|---|---|
| In the FastAPI handler | The web layer now makes business decisions. |
| In `Product.allocate` | The domain model does I/O; it can't be tested without mocking email. |
| In the service | Allocation logic gets entangled with notification. Next requirement ("also text the warehouse") makes it worse. |

The pattern: the model **records a fact** (`OutOfStock`), something else **reacts** to it.

### Step 1: Define the event

**Do:** create `domain/events.py`. Events are plain dataclasses named in the **past tense**: they report something that already happened.

**📄 `src/allocation/domain/events.py`**

```python
from dataclasses import dataclass


class Event:
    pass


@dataclass
class OutOfStock(Event):
    sku: str
```

### Step 2: Test that the model records the event (red → green)

**Do:** in `tests/unit/test_product.py`, replace `test_raises_out_of_stock_exception_if_cannot_allocate` with this test:

```python
from allocation.domain import events

def test_records_out_of_stock_event_if_cannot_allocate():
    batch = Batch("batch1", "SMALL-FORK", 10, eta=today)
    product = Product(sku="SMALL-FORK", batches=[batch])
    product.allocate(OrderLine("order1", "SMALL-FORK", 10))

    allocation = product.allocate(OrderLine("order2", "SMALL-FORK", 1))

    assert product.events[-1] == events.OutOfStock(sku="SMALL-FORK")
    assert allocation is None
```

### Step 3: Make `Product` record events

**Do:** in `model.py`, import `events`, give `Product` an `events` list, and replace the `raise OutOfStock` with recording an event and returning `None`.

```python
from allocation.domain import events          # new import at the top

class Product:
    def __init__(self, sku: str, batches: list[Batch], version_number: int = 0):
        self.sku = sku
        self.batches = batches
        self.version_number = version_number
        self.events: list[events.Event] = []      # new

    def allocate(self, line: OrderLine) -> str | None:
        try:
            batch = next(b for b in sorted(self.batches) if b.can_allocate(line))
            batch.allocate(line)
            self.version_number += 1
            return batch.reference
        except StopIteration:
            self.events.append(events.OutOfStock(line.sku))   # record, don't raise
            return None
```

**Why:** the model stays pure. It doesn't know who is listening or what they'll do. (You can now delete the unused `OutOfStock` exception from the model, or keep it for now.)

> **A gotcha you must handle.** SQLAlchemy doesn't call `__init__` when it *loads* an object from the database, so a loaded `Product` would have no `events` attribute. Add this listener at the bottom of `orm.py`:

```python
from sqlalchemy import event           # add to the imports

@event.listens_for(model.Product, "load")
def receive_load(product, _):
    product.events = []
```

### Step 4: The handler and the message bus

**Do:** create `adapters/email.py` (a stand-in that just prints), `service_layer/handlers.py`, and `service_layer/messagebus.py`.

**Why:** the bus is only a dictionary from event type to a list of handler functions, plus a loop. Adding "also text the warehouse" later means appending another handler to the list; no existing code changes.

**📄 `src/allocation/adapters/email.py`**

```python
def send(*args):
    print("SENDING EMAIL:", *args)
```

**📄 `src/allocation/service_layer/handlers.py`**

```python
from allocation.adapters import email
from allocation.domain import events


def send_out_of_stock_notification(event: events.OutOfStock):
    email.send(
        "stock@made.com",
        f"Out of stock for {event.sku}",
    )
```

**📄 `src/allocation/service_layer/messagebus.py`**

```python
from collections.abc import Callable

from allocation.domain import events
from allocation.service_layer import handlers


def handle(event: events.Event):
    for handler in HANDLERS[type(event)]:
        handler(event)


HANDLERS: dict[type[events.Event], list[Callable]] = {
    events.OutOfStock: [handlers.send_out_of_stock_notification],
}
```

### Step 5: Who publishes the events? The Unit of Work.

You *could* have each service call `messagebus.handle(...)` after committing, but then every service must remember to do it. Instead, the repository remembers every product it handed out (`seen`), and the UoW publishes their events right after a successful commit.

**Do:**
1. In `repository.py`, make `add`/`get` concrete methods that record `seen` and delegate to abstract `_add`/`_get`.
2. In `unit_of_work.py`, make `commit()` call `_commit()` then `publish_events()`.
3. Update your `FakeRepository` (`_add`/`_get`, call `super().__init__()`) and `FakeUnitOfWork` (`_commit`).

**📄 `src/allocation/adapters/repository.py`**

```python
import abc

from allocation.domain import model


class AbstractRepository(abc.ABC):
    def __init__(self):
        # every aggregate that passes through the repo; the UoW reads its events
        self.seen: set[model.Product] = set()

    def add(self, product: model.Product):
        self._add(product)
        self.seen.add(product)

    def get(self, sku) -> model.Product:
        product = self._get(sku)
        if product:
            self.seen.add(product)
        return product

    @abc.abstractmethod
    def _add(self, product: model.Product):
        raise NotImplementedError

    @abc.abstractmethod
    def _get(self, sku) -> model.Product:
        raise NotImplementedError


class SqlAlchemyRepository(AbstractRepository):
    def __init__(self, session):
        super().__init__()
        self.session = session

    def _add(self, product):
        self.session.add(product)

    def _get(self, sku):
        return self.session.query(model.Product).filter_by(sku=sku).first()
```

**📄 `src/allocation/service_layer/unit_of_work.py`**

```python
# pylint: disable=attribute-defined-outside-init
from __future__ import annotations

import abc

from allocation.adapters import repository
from allocation.service_layer import messagebus


class AbstractUnitOfWork(abc.ABC):
    products: repository.AbstractRepository

    def __enter__(self) -> AbstractUnitOfWork:
        return self

    def __exit__(self, *args):
        # Safe by default: anything not explicitly committed is thrown away.
        self.rollback()

    def commit(self):
        self._commit()
        self.publish_events()

    def publish_events(self):
        for product in self.products.seen:
            while product.events:
                event = product.events.pop(0)
                messagebus.handle(event)

    @abc.abstractmethod
    def _commit(self):
        raise NotImplementedError

    @abc.abstractmethod
    def rollback(self):
        raise NotImplementedError


class SqlAlchemyUnitOfWork(AbstractUnitOfWork):
    def __init__(self, session_factory):
        self.session_factory = session_factory

    def __enter__(self):
        self.session = self.session_factory()  # a fresh session per unit of work
        self.products = repository.SqlAlchemyRepository(self.session)
        return super().__enter__()

    def __exit__(self, *args):
        super().__exit__(*args)
        self.session.close()

    def _commit(self):
        self.session.commit()

    def rollback(self):
        self.session.rollback()
```

### Step 6: Tell the API about "out of stock"

`allocate` now returns `None` instead of raising, so the endpoint must translate that into the same 400 as before. Change the service's return type to `str | None`, drop the now-unused `OutOfStock` from the `except`, and add the `None` check:

```python
        except services.InvalidSku as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if batchref is None:
            raise HTTPException(status_code=400, detail=f"Out of stock for sku {body.sku}")
        return AllocateResponse(batchref=batchref)
```

Full file after this change (it also uses `orm.make_session_factory` from ch07):

**📄 `src/allocation/entrypoints/fastapi_app.py`**

```python
from datetime import date

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from allocation import config
from allocation.adapters import orm
from allocation.service_layer import services, unit_of_work


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


def create_app(session_factory=None) -> FastAPI:
    if session_factory is None:
        orm.start_mappers()
        session_factory = orm.make_session_factory(config.get_settings().db_uri)

    app = FastAPI(title="Allocation service")

    @app.post("/add_batch", status_code=201)
    def add_batch_endpoint(body: AddBatchRequest) -> None:
        uow = unit_of_work.SqlAlchemyUnitOfWork(session_factory)
        services.add_batch(body.ref, body.sku, body.qty, body.eta, uow)

    @app.post("/allocate", status_code=201)
    def allocate_endpoint(body: AllocateRequest) -> AllocateResponse:
        uow = unit_of_work.SqlAlchemyUnitOfWork(session_factory)
        try:
            batchref = services.allocate(body.orderid, body.sku, body.qty, uow)
        except services.InvalidSku as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if batchref is None:
            raise HTTPException(status_code=400, detail=f"Out of stock for sku {body.sku}")
        return AllocateResponse(batchref=batchref)

    return app
```

Add an e2e test `test_out_of_stock_returns_400`.

### Step 7: Prove the email is sent

Update the fakes in `tests/unit/test_services.py` and add this test. `mock.patch` is justified here: email is a true external side-effect you don't own.

```python
from unittest import mock

class FakeRepository(repository.AbstractRepository):
    def __init__(self, products):
        super().__init__()
        self._products = set(products)

    def _add(self, product):
        self._products.add(product)

    def _get(self, sku):
        return next((p for p in self._products if p.sku == sku), None)


class FakeUnitOfWork(unit_of_work.AbstractUnitOfWork):
    def __init__(self):
        self.products = FakeRepository([])
        self.committed = False

    def _commit(self):
        self.committed = True

    def rollback(self):
        pass


def test_sends_email_on_out_of_stock_error():
    uow = FakeUnitOfWork()
    services.add_batch("b1", "POPULAR-CURTAINS", 9, None, uow)

    with mock.patch("allocation.adapters.email.send") as mock_send_mail:
        services.allocate("o1", "POPULAR-CURTAINS", 10, uow)
        assert mock_send_mail.call_args == mock.call(
            "stock@made.com", "Out of stock for POPULAR-CURTAINS"
        )
```

```bash
uv run pytest -q
# 37 passed, 1 skipped
```

### Trade-off to write in your notes

Control flow is now **indirect**: reading `services.allocate`, you can't see that an email gets sent. To find out, you look up `OutOfStock` in `messagebus.HANDLERS`. That's the price of decoupling.

### Stretch

- Add a second handler to the `OutOfStock` list that appends the SKU to a `reorder.log` file. Which existing files did you have to touch?
- What happens if the email handler raises an exception here? (We'll deal with that in ch10.)

## Check yourself
- Why is "past tense" important in event names?
- Why does the model record events instead of publishing them?
- Who should be responsible for publishing: service or UoW? Why did the book move it?
- What gets harder to debug with a message bus?
