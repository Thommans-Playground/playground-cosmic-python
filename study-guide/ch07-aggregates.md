# Chapter 7: Aggregates and Consistency Boundaries

## ✈️ 30,000 ft
Which objects must be consistent *together*, and how do we protect that under concurrent requests? An **aggregate** is a cluster of objects treated as one unit, with a single entry point (the aggregate root) and a single transaction.

## 🛫 10,000 ft
- Problem: `allocate(line, batches)` needs *all* batches of a SKU to decide, and there's no object representing "all stock for this SKU".
- Solution: a new **`Product`** aggregate per SKU, holding its batches. `Product.allocate(line)` replaces the free function.
- **Aggregate root** = `Product`. Repos return only aggregates. Outsiders never reach in and mutate batches.
- Rule of thumb: *one aggregate = one repository = one consistency boundary*.
- **Concurrency**: two requests allocating the same SKU at once can oversell. Fix with **optimistic concurrency** via a `version_number` on `Product`, plus a DB isolation level (`REPEATABLE READ`) or pessimistic `SELECT ... FOR UPDATE`.
- Trade-off: aggregates should be small. Big ones mean contention and slowness.

### 🗺️ Diagram: the Product aggregate

```mermaid
flowchart TB
    subgraph Agg["Product aggregate: one SKU, one consistency boundary"]
        P["Product (aggregate root)<br/>sku, version_number"]
        B1[Batch 1]
        B2[Batch 2]
        L1[OrderLine]
        L2[OrderLine]
        P --> B1
        P --> B2
        B1 --> L1
        B2 --> L2
    end
    Repo["Repository<br/>get(sku) returns Product"] --> P
    Outside[Services] --> Repo
    Outside -. cannot reach in .-x B1
```

```mermaid
sequenceDiagram
    participant T1 as Request 1
    participant T2 as Request 2
    participant DB as DB (Product v3)
    T1->>DB: read Product v3
    T2->>DB: read Product v3
    T1->>DB: write, set v4
    DB-->>T1: OK
    T2->>DB: write, set v4
    DB-->>T2: conflict, serialization failure
    Note over T2: retry or reject, so no overselling
```

## 🛫 5,000 ft: the conventional way vs. the better way

> Evidence tags: 🧪 I ran it · 🧮 arithmetic · 📖 documentation or reasoning (not run).

### Addition 1: two customers, one last sofa

**The conventional way:** read the stock, check it, allocate, commit, with no concurrency story.

```python
batches = uow.batches.list()           # request A and request B both read "10 available"
batchref = model.allocate(line, batches)
uow.commit()                           # both succeed
```

**Where it breaks**
- 🧪 I interleaved two sessions the way two simultaneous requests would: both saw `available: 10`, each allocated 10, each committed. Result: **20 units allocated against 10 in stock, and the batch reports `available_quantity == -10`**. The customer-visible symptom is an oversold sofa.
- It passes every sequential test you will ever write. It only fails under real concurrency, which makes it a production-only bug.

**The better way** (Steps 2, 6, 7): make "all batches of one SKU" an aggregate (`Product`), bump `version_number` on every allocation, and run Postgres at `REPEATABLE READ`. 📖 Two transactions that both try to update the same product row conflict, and the loser fails with a serialization error instead of committing a stale decision. (*I could not run the Postgres test; the guide's Step 7 gives you the Docker commands.*)

**Why it's right:** the invariant ("never allocate more than the SKU has") is protected by the database for you at one well-defined row, not by hoping requests don't overlap.
**What it costs:** the loser must be retried or told to try again, so you now need that handling. And every allocation *writes* the product row, so one very hot SKU becomes a contention point.

### Addition 2: who may touch a batch

**The conventional way:** a repository per table (`BatchRepository`, `OrderLineRepository`), and any code may load a batch and call `batch.allocate(line)`.

**Where it breaks**
- The rules live in `Product.allocate` (preference order, the version bump). Code that loads a `Batch` directly skips all of it: it allocates to the wrong batch and silently disables the concurrency guard.
- Transactions touch arbitrary sets of rows, so lock behaviour is unpredictable.
- 📖 The opposite mistake is one giant aggregate ("Order" containing products, customers, stock): every change locks the world.

**The better way** (Step 4): only **aggregate roots** get repositories. To change a batch you load its `Product` and ask *it*. Keep aggregates small, and refer to other aggregates by id.

**Why it's right:** there is exactly one door to each consistency boundary, so the rules can't be bypassed.
**What it costs:** touching one batch loads the whole product (all its batches). If a SKU had thousands of batches you would feel it, which is why aggregate size is a design decision, not an accident.

## 🛬 1,000 ft: design decisions
- `Product(sku, batches, version_number)`; repo API becomes `get(sku)`.
- Service `allocate` now: `product = uow.products.get(sku=line.sku)`; if `None` → `InvalidSku`.
- `add_batch` creates the Product if missing.
- Concurrency test: two threads, each with a UoW, both allocate; one must fail (`OperationalError`/serialization failure) and the DB should show exactly one allocation.
- Options compared: version column with isolation level vs `with_for_update()`. Know both, and note that one is "optimistic", the other "pessimistic".
- Foreshadow of ch08: `Product` is where events will be raised.

## 🛞 Touchdown: code along

> Starts from your finished ch06 project. This is the biggest refactor of Part 1: `allocate` moves from a free function into a new `Product` class, the repository returns products instead of batches, and a version number guards against concurrent updates. 35 tests pass, 1 skipped; the one Postgres-only concurrency test is **written but I could not run it** (no Postgres in my environment). It skips automatically unless you set `POSTGRES_URI`.

### The problem in one scenario

Two customers order the last 10 units of the same SKU at the same instant. Both requests load the batches, both see 10 available, both allocate, both commit → you've sold 20 units of stock you have 10 of. The rule *"never allocate more than a SKU's stock"* spans **all batches of that SKU**, so that whole group must change under one lock. That group is the **aggregate**; `Product` is its **root**.

### Step 1: Write the `Product` tests first

**Do:** create `tests/unit/test_product.py`. These are the old "allocate across several batches" tests from ch01, now calling `product.allocate(line)`. Add the new `version_number` test. Slim `test_batches.py` down to tests of a *single* batch (delete the `allocate(...)` function tests from it).

**📄 `tests/unit/test_product.py`**

```python
from datetime import date, timedelta

import pytest

from allocation.domain.model import Batch, OrderLine, OutOfStock, Product

today = date.today()
tomorrow = today + timedelta(days=1)
later = tomorrow + timedelta(days=10)


def test_prefers_warehouse_batches_to_shipments():
    in_stock_batch = Batch("in-stock-batch", "RETRO-CLOCK", 100, eta=None)
    shipment_batch = Batch("shipment-batch", "RETRO-CLOCK", 100, eta=tomorrow)
    product = Product(sku="RETRO-CLOCK", batches=[in_stock_batch, shipment_batch])
    line = OrderLine("oref", "RETRO-CLOCK", 10)

    product.allocate(line)

    assert in_stock_batch.available_quantity == 90
    assert shipment_batch.available_quantity == 100


def test_prefers_earlier_batches():
    earliest = Batch("speedy-batch", "MINIMALIST-SPOON", 100, eta=today)
    medium = Batch("normal-batch", "MINIMALIST-SPOON", 100, eta=tomorrow)
    latest = Batch("slow-batch", "MINIMALIST-SPOON", 100, eta=later)
    product = Product(sku="MINIMALIST-SPOON", batches=[medium, earliest, latest])
    line = OrderLine("order1", "MINIMALIST-SPOON", 10)

    product.allocate(line)

    assert earliest.available_quantity == 90
    assert medium.available_quantity == 100
    assert latest.available_quantity == 100


def test_returns_allocated_batch_ref():
    in_stock_batch = Batch("in-stock-batch-ref", "HIGHBROW-POSTER", 100, eta=None)
    shipment_batch = Batch("shipment-batch-ref", "HIGHBROW-POSTER", 100, eta=tomorrow)
    line = OrderLine("oref", "HIGHBROW-POSTER", 10)
    product = Product(sku="HIGHBROW-POSTER", batches=[in_stock_batch, shipment_batch])
    allocation = product.allocate(line)
    assert allocation == in_stock_batch.reference


def test_raises_out_of_stock_exception_if_cannot_allocate():
    batch = Batch("batch1", "SMALL-FORK", 10, eta=today)
    product = Product(sku="SMALL-FORK", batches=[batch])
    product.allocate(OrderLine("order1", "SMALL-FORK", 10))

    with pytest.raises(OutOfStock, match="SMALL-FORK"):
        product.allocate(OrderLine("order2", "SMALL-FORK", 1))


def test_increments_version_number():
    line = OrderLine("oref", "SCANDI-PEN", 10)
    product = Product(sku="SCANDI-PEN", batches=[Batch("b1", "SCANDI-PEN", 100, eta=None)])
    product.version_number = 7
    product.allocate(line)
    assert product.version_number == 8
```

**📄 `tests/unit/test_batches.py`**

```python
from datetime import date

from allocation.domain.model import Batch, OrderLine

today = date.today()


def make_batch_and_line(sku, batch_qty, line_qty):
    return (
        Batch("batch-001", sku, batch_qty, eta=today),
        OrderLine("order-123", sku, line_qty),
    )


def test_allocating_to_a_batch_reduces_the_available_quantity():
    batch = Batch("batch-001", "SMALL-TABLE", qty=20, eta=date.today())
    line = OrderLine("order-ref", "SMALL-TABLE", 2)

    batch.allocate(line)

    assert batch.available_quantity == 18


def test_can_allocate_if_available_greater_than_required():
    large_batch, small_line = make_batch_and_line("ELEGANT-LAMP", 20, 2)
    assert large_batch.can_allocate(small_line)


def test_cannot_allocate_if_available_smaller_than_required():
    small_batch, large_line = make_batch_and_line("ELEGANT-LAMP", 2, 20)
    assert small_batch.can_allocate(large_line) is False


def test_can_allocate_if_available_equal_to_required():
    batch, line = make_batch_and_line("ELEGANT-LAMP", 2, 2)
    assert batch.can_allocate(line)


def test_cannot_allocate_if_skus_do_not_match():
    batch = Batch("batch-001", "UNCOMFORTABLE-CHAIR", 100, eta=None)
    different_sku_line = OrderLine("order-123", "EXPENSIVE-TOASTER", 10)
    assert batch.can_allocate(different_sku_line) is False


def test_can_only_deallocate_allocated_lines():
    batch, unallocated_line = make_batch_and_line("DECORATIVE-TRINKET", 20, 2)
    batch.deallocate(unallocated_line)
    assert batch.available_quantity == 20


def test_allocation_is_idempotent():
    batch, line = make_batch_and_line("ANGULAR-DESK", 20, 2)
    batch.allocate(line)
    batch.allocate(line)
    assert batch.available_quantity == 18
```

### Step 2: Add `Product` to the model

**Do:** add the class and delete the module-level `allocate()` function.

**Why:**
- `Product` is the only way in. Services never grab a `Batch` directly and mutate it.
- `version_number += 1` on every successful allocation is the hook for **optimistic concurrency**: the database can notice that two transactions both tried to change the same product row.
- Rule of thumb: *one aggregate = one repository = one consistency boundary*.

**📄 `src/allocation/domain/model.py`**

```python
"""Domain model for the allocation service.

Plain Python only: nothing in this module imports a web framework, an ORM or a database
driver. That is what lets the business rules be tested in milliseconds and reused unchanged
behind any entrypoint.

The model is organised around one aggregate, ``Product``: every ``Batch`` of a SKU is changed
through it, so the SKU's stock is always updated as one consistent unit.
"""

from dataclasses import dataclass
from datetime import date


class OutOfStock(Exception):
    """Raised when no batch can take an order line."""

    pass


@dataclass(unsafe_hash=True)
class OrderLine:
    """One line of a customer order: a quantity of one SKU.

    A value object: two lines with the same fields are the same line. Treat it as immutable.
    It is hashable so that a ``Batch`` can keep its allocated lines in a ``set``.

    Attributes:
        orderid: Identifier of the customer order this line belongs to.
        sku: Stock-keeping unit: the product being ordered.
        qty: How many units are wanted.
    """

    orderid: str
    sku: str
    qty: int


class Batch:
    """A quantity of one SKU bought from a supplier, either in the warehouse or on its way.

    An entity: identified by its ``reference``, whatever its current quantity or allocations.

    Args:
        ref: Unique reference of this batch.
        sku: The product this batch contains.
        qty: Quantity purchased. The quantity still available is derived, never stored.
        eta: Arrival date for a shipment, or ``None`` for stock already in the warehouse.
    """

    def __init__(self, ref: str, sku: str, qty: int, eta: date | None):
        self.reference = ref
        self.sku = sku
        self.eta = eta
        self._purchased_quantity = qty
        # A set, so allocating an equal line twice is a no-op: allocation is idempotent.
        self._allocations: set[OrderLine] = set()

    def __repr__(self):
        return f"<Batch {self.reference}>"

    def __eq__(self, other):
        """Batches are equal when their reference is equal, regardless of their state."""
        if not isinstance(other, Batch):
            return False
        return other.reference == self.reference

    def __hash__(self):
        """Hash by reference only, so it stays consistent with ``__eq__``."""
        return hash(self.reference)

    def __gt__(self, other):
        """Order batches by how soon their stock can ship.

        Warehouse stock (``eta is None``) sorts first, then the earliest ETA. This is how
        ``sorted(batches)`` expresses the business preference rule.
        """
        if self.eta is None:
            return False
        if other.eta is None:
            return True
        return self.eta > other.eta

    def allocate(self, line: OrderLine):
        """Reserve ``line`` in this batch.

        Does nothing if the line does not fit or is already allocated, so repeating a request
        (a double submit, a retry) is harmless.
        """
        if self.can_allocate(line):
            self._allocations.add(line)

    def deallocate(self, line: OrderLine):
        """Release a previously allocated line. Lines that were never allocated are ignored."""
        if line in self._allocations:
            self._allocations.remove(line)

    @property
    def allocated_quantity(self) -> int:
        """Total quantity of the lines allocated to this batch."""
        return sum(line.qty for line in self._allocations)

    @property
    def available_quantity(self) -> int:
        """Purchased quantity minus allocated quantity.

        Computed on demand, so it can never drift out of sync with the allocations.
        """
        return self._purchased_quantity - self.allocated_quantity

    def can_allocate(self, line: OrderLine) -> bool:
        """Return whether ``line`` is for this batch's SKU and fits in the available quantity."""
        return self.sku == line.sku and self.available_quantity >= line.qty


class Product:
    """Aggregate root: every batch of one SKU, and the only way to change them.

    All allocation decisions for a SKU go through this object, so the rules (preference order,
    never over-allocating) are enforced in one place, and the product is the unit that gets
    loaded, changed and saved in a single transaction.

    Args:
        sku: The product this aggregate represents.
        batches: All batches of that SKU.
        version_number: Incremented on every allocation. The database uses it to detect two
            requests changing the same product at the same time.
    """

    def __init__(self, sku: str, batches: list[Batch], version_number: int = 0):
        self.sku = sku
        self.batches = batches
        self.version_number = version_number

    def allocate(self, line: OrderLine) -> str:
        """Allocate ``line`` to the best available batch of this SKU.

        Args:
            line: The order line to allocate.

        Returns:
            The reference of the chosen batch.

        Raises:
            OutOfStock: If no batch can take the line.
        """
        try:
            # sorted() uses Batch.__gt__, so the first batch that fits is the preferred one.
            batch = next(b for b in sorted(self.batches) if b.can_allocate(line))
            batch.allocate(line)
            # The database uses the version to detect two requests changing this product at once.
            self.version_number += 1
            return batch.reference
        except StopIteration:
            # `from None` hides the internal StopIteration from the traceback.
            raise OutOfStock(f"Out of stock for sku {line.sku}") from None
```

### Step 3: Update the ORM

**Do:** add a `products` table (primary key `sku`, plus `version_number`), make `batches.sku` a foreign key to it, and map `Product.batches` as a relationship.

**📄 `src/allocation/adapters/orm.py`**

```python
"""Database tables and the mapping from the plain domain classes onto them.

The dependency points inwards: this module imports the model, and the model knows nothing
about SQLAlchemy.
"""

from sqlalchemy import Column, Date, ForeignKey, Integer, String, Table, create_engine
from sqlalchemy.orm import registry, relationship, sessionmaker

from allocation.domain import model

mapper_registry = registry()
metadata = mapper_registry.metadata

order_lines = Table(
    "order_lines",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("sku", String(255)),
    Column("qty", Integer, nullable=False),
    Column("orderid", String(255)),
)

products = Table(
    "products",
    metadata,
    Column("sku", String(255), primary_key=True),
    Column("version_number", Integer, nullable=False, server_default="0"),
)

batches = Table(
    "batches",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("reference", String(255)),
    Column("sku", ForeignKey("products.sku")),
    Column("_purchased_quantity", Integer, nullable=False),
    Column("eta", Date, nullable=True),
)

allocations = Table(
    "allocations",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("orderline_id", ForeignKey("order_lines.id")),
    Column("batch_id", ForeignKey("batches.id")),
)


def start_mappers():
    """Map the domain classes onto the tables.

    Call this once per process, before using any session. It is a function, not import-time
    code, so tests that only need the model never touch the ORM.
    """
    lines_mapper = mapper_registry.map_imperatively(model.OrderLine, order_lines)
    batches_mapper = mapper_registry.map_imperatively(
        model.Batch,
        batches,
        properties={
            "_allocations": relationship(
                lines_mapper,
                secondary=allocations,
                collection_class=set,
            )
        },
    )
    mapper_registry.map_imperatively(
        model.Product, products, properties={"batches": relationship(batches_mapper)}
    )


def make_session_factory(uri: str) -> sessionmaker:
    """Create the engine, make sure the tables exist, and return a session factory.

    Postgres runs at ``REPEATABLE READ`` so two requests changing the same product conflict
    instead of both succeeding. SQLite does not accept that setting, hence the check.

    Args:
        uri: SQLAlchemy database URL, for example ``sqlite:///allocation.db``.

    Returns:
        A ``sessionmaker`` bound to the new engine.
    """
    kwargs = {"isolation_level": "REPEATABLE READ"} if uri.startswith("postgresql") else {}
    engine = create_engine(uri, **kwargs)
    metadata.create_all(engine)
    return sessionmaker(bind=engine)
```

### Step 4: The repository now deals in products

**Do:** `add(product)` and `get(sku)`. Delete `list()`. `get` returns `None` when the SKU is unknown (and your service will turn that into `InvalidSku`).

**📄 `src/allocation/adapters/repository.py`**

```python
"""Repositories: the application's view of storage as a collection of aggregates."""

import abc

from allocation.domain import model


class AbstractRepository(abc.ABC):
    """The smallest storage interface the application needs, for products.

    Only aggregate roots get a repository. Implemented by the real SQLAlchemy repository and by
    an in-memory fake in tests.
    """

    @abc.abstractmethod
    def add(self, product: model.Product):
        """Add a product to the collection."""
        raise NotImplementedError

    @abc.abstractmethod
    def get(self, sku) -> model.Product:
        """Return the product for this SKU, or ``None`` if there is none."""
        raise NotImplementedError


class SqlAlchemyRepository(AbstractRepository):
    """Repository backed by a SQLAlchemy session."""

    def __init__(self, session):
        self.session = session

    def add(self, product):
        """Add a product to the session."""
        self.session.add(product)

    def get(self, sku):
        """Return the product for this SKU, or ``None``."""
        return self.session.query(model.Product).filter_by(sku=sku).first()
```

Update `tests/integration/test_repository.py` to save and load a `Product` along with its batches and allocations:

**📄 `tests/integration/test_repository.py`**

```python
from sqlalchemy import text

from allocation.adapters import repository
from allocation.domain import model


def test_repository_can_save_a_product(session):
    product = model.Product("RUSTY-SOAPDISH", batches=[])
    product.batches.append(model.Batch("batch1", "RUSTY-SOAPDISH", 100, eta=None))

    repo = repository.SqlAlchemyRepository(session)
    repo.add(product)
    session.commit()

    products = list(session.execute(text('SELECT sku, version_number FROM "products"')))
    batches = list(session.execute(text('SELECT reference, sku FROM "batches"')))
    assert products == [("RUSTY-SOAPDISH", 0)]
    assert batches == [("batch1", "RUSTY-SOAPDISH")]


def test_repository_can_retrieve_a_product_with_its_batches_and_allocations(session):
    session.execute(text('INSERT INTO products (sku, version_number) VALUES ("GENERIC-SOFA", 3)'))
    session.execute(
        text(
            "INSERT INTO batches (reference, sku, _purchased_quantity, eta)"
            ' VALUES ("batch1", "GENERIC-SOFA", 100, null)'
        )
    )
    session.execute(
        text('INSERT INTO order_lines (orderid, sku, qty) VALUES ("order1", "GENERIC-SOFA", 12)')
    )
    session.execute(text("INSERT INTO allocations (orderline_id, batch_id) VALUES (1, 1)"))

    repo = repository.SqlAlchemyRepository(session)
    product = repo.get("GENERIC-SOFA")

    assert product.version_number == 3
    [batch] = product.batches
    assert batch.reference == "batch1"
    assert batch._allocations == {model.OrderLine("order1", "GENERIC-SOFA", 12)}


def test_get_returns_none_for_unknown_sku(session):
    assert repository.SqlAlchemyRepository(session).get("NOPE") is None
```

`tests/integration/test_orm.py` needs one small change: since `batches.sku` now references `products`, insert a product row before inserting batches (SQLite doesn't enforce foreign keys by default, but Postgres does). Add this to the start of `test_retrieving_batches`, `test_saving_batches`, `test_saving_allocations`, and `test_retrieving_allocations`:

```python
session.execute(text('INSERT INTO products (sku, version_number) VALUES ("sku1", 0)'))
# (test_retrieving_batches also needs a row for "sku2")
```

### Step 5: Unit of Work and services

**Do:** in `unit_of_work.py`, rename `batches` to `products` (two places: the type hint on the abstract class and the assignment in `SqlAlchemyUnitOfWork.__enter__`). Then rewrite the services.

**Why:** `add_batch` now creates the `Product` if this is the first batch for the SKU. `allocate` loads one product, asks it to allocate, and commits. The service no longer contains the "pick a batch" logic at all.

> The `deallocate` service from ch05/06 is dropped here. Reallocation comes back in ch09 as a proper use case (`change_batch_quantity`).

**📄 `src/allocation/service_layer/services.py`**

```python
"""Application services: one function per use case.

Services orchestrate (fetch, call the domain, commit); the business rules stay in the model.
They take plain values, never web objects, so any entrypoint can call them.
"""

from __future__ import annotations

from datetime import date

from allocation.domain import model
from allocation.service_layer import unit_of_work


class InvalidSku(Exception):
    """Raised when an order refers to a SKU we do not stock."""

    pass


def add_batch(
    ref: str,
    sku: str,
    qty: int,
    eta: date | None,
    uow: unit_of_work.AbstractUnitOfWork,
):
    """Add a batch of stock, creating the product the first time its SKU appears.

    Args:
        ref: Unique reference of the new batch.
        sku: The product the batch contains.
        qty: Quantity purchased.
        eta: Arrival date, or ``None`` for warehouse stock.
        uow: The unit of work that wraps the transaction.
    """
    with uow:
        product = uow.products.get(sku=sku)
        if product is None:
            product = model.Product(sku, batches=[])
            uow.products.add(product)
        product.batches.append(model.Batch(ref, sku, qty, eta))
        uow.commit()


def allocate(orderid: str, sku: str, qty: int, uow: unit_of_work.AbstractUnitOfWork) -> str:
    """Allocate an order line to a batch, inside one unit of work.

    Args:
        orderid: The customer order the line belongs to.
        sku: The product ordered.
        qty: How many units.
        uow: The unit of work that wraps the transaction.

    Returns:
        The reference of the batch that was allocated.

    Raises:
        InvalidSku: If the SKU is not one we stock.
        OutOfStock: If no batch can take the line.
    """
    line = model.OrderLine(orderid, sku, qty)
    with uow:
        product = uow.products.get(sku=line.sku)
        if product is None:
            raise InvalidSku(f"Invalid sku {line.sku}")
        batchref = product.allocate(line)
        uow.commit()
    return batchref
```

Update the fake repository and fake UoW in the service tests:

**📄 `tests/unit/test_services.py`**

```python
from datetime import date

import pytest

from allocation.adapters import repository
from allocation.service_layer import services, unit_of_work


class FakeRepository(repository.AbstractRepository):
    def __init__(self, products):
        self._products = set(products)

    def add(self, product):
        self._products.add(product)

    def get(self, sku):
        return next((p for p in self._products if p.sku == sku), None)


class FakeUnitOfWork(unit_of_work.AbstractUnitOfWork):
    def __init__(self):
        self.products = FakeRepository([])
        self.committed = False

    def commit(self):
        self.committed = True

    def rollback(self):
        pass


def test_add_batch_for_new_product():
    uow = FakeUnitOfWork()
    services.add_batch("b1", "CRUNCHY-ARMCHAIR", 100, None, uow)
    assert uow.products.get("CRUNCHY-ARMCHAIR") is not None
    assert uow.committed


def test_add_batch_for_existing_product():
    uow = FakeUnitOfWork()
    services.add_batch("b1", "GARISH-RUG", 100, None, uow)
    services.add_batch("b2", "GARISH-RUG", 99, None, uow)
    assert "b2" in [b.reference for b in uow.products.get("GARISH-RUG").batches]


def test_allocate_returns_allocation():
    uow = FakeUnitOfWork()
    services.add_batch("batch1", "COMPLICATED-LAMP", 100, None, uow)
    result = services.allocate("o1", "COMPLICATED-LAMP", 10, uow)
    assert result == "batch1"


def test_allocate_errors_for_invalid_sku():
    uow = FakeUnitOfWork()
    services.add_batch("b1", "AREALSKU", 100, None, uow)

    with pytest.raises(services.InvalidSku, match="Invalid sku NONEXISTENTSKU"):
        services.allocate("o1", "NONEXISTENTSKU", 10, uow)


def test_allocate_commits():
    uow = FakeUnitOfWork()
    services.add_batch("b1", "OMINOUS-MIRROR", 100, None, uow)
    uow.committed = False

    services.allocate("o1", "OMINOUS-MIRROR", 10, uow)
    assert uow.committed is True


def test_prefers_warehouse_batches_to_shipments():
    uow = FakeUnitOfWork()
    services.add_batch("in-stock-batch", "RETRO-CLOCK", 100, None, uow)
    services.add_batch("shipment-batch", "RETRO-CLOCK", 100, date(2030, 1, 1), uow)

    batchref = services.allocate("oref", "RETRO-CLOCK", 10, uow)

    assert batchref == "in-stock-batch"
```

### Step 6: One helper that builds the engine, then run the suite

Postgres needs `REPEATABLE READ`, but SQLite rejects that value, hence the `if`. Since more than one entrypoint will soon need an engine (ch11 adds a second), put those lines in one function in `orm.py` and call it from the app instead of repeating them:

```python
# orm.py (add `create_engine` and `sessionmaker` to the imports)
def make_session_factory(uri: str) -> sessionmaker:
    """Create the engine, make sure the tables exist, and return a session factory.

    Postgres runs at ``REPEATABLE READ`` so two requests changing the same product conflict
    instead of both succeeding. SQLite does not accept that setting, hence the check.

    Args:
        uri: SQLAlchemy database URL, for example ``sqlite:///allocation.db``.

    Returns:
        A ``sessionmaker`` bound to the new engine.
    """
    kwargs = {"isolation_level": "REPEATABLE READ"} if uri.startswith("postgresql") else {}
    engine = create_engine(uri, **kwargs)
    metadata.create_all(engine)
    return sessionmaker(bind=engine)
```

```python
# in fastapi_app.create_app, replacing the inline engine code:
if session_factory is None:
    orm.start_mappers()
    session_factory = orm.make_session_factory(config.get_settings().db_uri)
```

```bash
uv run pytest -q
# 35 passed, 1 skipped
```

### Step 7: The concurrency test (needs Postgres)

**Why this works:** under `REPEATABLE READ`, if two transactions both read product version 1 and both try to `UPDATE products SET version_number=2`, Postgres lets the first commit and makes the second fail with *"could not serialize access due to concurrent update"*. The loser's whole transaction rolls back, so only one allocation exists. The test starts two threads, holds each transaction open for 200 ms so they overlap, and asserts exactly one winner.

Start a Postgres, add the fixture below to `tests/conftest.py`, and add the test to `tests/integration/test_uow.py`:

```bash
docker run -d --name pg -e POSTGRES_USER=allocation -e POSTGRES_PASSWORD=abc123 \
  -e POSTGRES_DB=allocation -p 5432:5432 postgres:16
uv add --dev "psycopg[binary]"
POSTGRES_URI=postgresql+psycopg://allocation:abc123@localhost:5432/allocation uv run pytest -q
# 36 passed
```

**📄 `tests/conftest.py`**

```python
import os

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
```

**📄 `tests/integration/test_uow.py`**

```python
import threading
import time
import traceback

import pytest
from sqlalchemy import text

from allocation.domain import model
from allocation.service_layer import unit_of_work


def insert_batch(session, ref, sku, qty, eta, product_version=1):
    session.execute(
        text(
            "INSERT INTO products (sku, version_number) VALUES (:sku, :version)"
            " ON CONFLICT (sku) DO NOTHING"
        ),
        dict(sku=sku, version=product_version),
    )
    session.execute(
        text(
            "INSERT INTO batches (reference, sku, _purchased_quantity, eta)"
            " VALUES (:ref, :sku, :qty, :eta)"
        ),
        dict(ref=ref, sku=sku, qty=qty, eta=eta),
    )


def get_allocated_batch_ref(session, orderid, sku):
    [[orderlineid]] = session.execute(
        text("SELECT id FROM order_lines WHERE orderid=:orderid AND sku=:sku"),
        dict(orderid=orderid, sku=sku),
    )
    [[batchref]] = session.execute(
        text(
            "SELECT b.reference FROM allocations JOIN batches AS b ON batch_id = b.id"
            " WHERE orderline_id=:orderlineid"
        ),
        dict(orderlineid=orderlineid),
    )
    return batchref


def test_uow_can_retrieve_a_batch_and_allocate_to_it(session_factory):
    session = session_factory()
    insert_batch(session, "batch1", "HIPSTER-WORKBENCH", 100, None)
    session.commit()

    uow = unit_of_work.SqlAlchemyUnitOfWork(session_factory)
    with uow:
        product = uow.products.get(sku="HIPSTER-WORKBENCH")
        line = model.OrderLine("o1", "HIPSTER-WORKBENCH", 10)
        product.allocate(line)
        uow.commit()

    batchref = get_allocated_batch_ref(session, "o1", "HIPSTER-WORKBENCH")
    assert batchref == "batch1"


def test_rolls_back_uncommitted_work_by_default(session_factory):
    uow = unit_of_work.SqlAlchemyUnitOfWork(session_factory)
    with uow:
        insert_batch(uow.session, "batch1", "MEDIUM-PLINTH", 100, None)

    new_session = session_factory()
    rows = list(new_session.execute(text('SELECT * FROM "batches"')))
    assert rows == []


def test_rolls_back_on_error(session_factory):
    class MyException(Exception):
        pass

    uow = unit_of_work.SqlAlchemyUnitOfWork(session_factory)
    with pytest.raises(MyException), uow:
        insert_batch(uow.session, "batch1", "LARGE-FORK", 100, None)
        raise MyException()

    new_session = session_factory()
    rows = list(new_session.execute(text('SELECT * FROM "batches"')))
    assert rows == []


def try_to_allocate(session_factory, orderid, sku, exceptions):
    line = model.OrderLine(orderid, sku, 10)
    try:
        with unit_of_work.SqlAlchemyUnitOfWork(session_factory) as uow:
            product = uow.products.get(sku=sku)
            product.allocate(line)
            time.sleep(0.2)  # hold the transaction open so the two overlap
            uow.commit()
    except Exception as e:
        print(traceback.format_exc())
        exceptions.append(e)


def test_concurrent_updates_to_version_are_not_allowed(postgres_session_factory):
    sku, batch = "CONCURRENT-SKU", "concurrent-batch"
    session = postgres_session_factory()
    insert_batch(session, batch, sku, 100, eta=None, product_version=1)
    session.commit()

    exceptions = []
    t1 = threading.Thread(
        target=try_to_allocate, args=(postgres_session_factory, "order1", sku, exceptions)
    )
    t2 = threading.Thread(
        target=try_to_allocate, args=(postgres_session_factory, "order2", sku, exceptions)
    )
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    [[version]] = session.execute(
        text("SELECT version_number FROM products WHERE sku=:sku"), dict(sku=sku)
    )
    assert version == 2  # only ONE of the two allocations won
    [exception] = exceptions
    assert "could not serialize access due to concurrent update" in str(exception)

    orders = list(
        session.execute(
            text(
                "SELECT orderid FROM allocations"
                " JOIN batches ON allocations.batch_id = batches.id"
                " JOIN order_lines ON allocations.orderline_id = order_lines.id"
                " WHERE order_lines.sku=:sku"
            ),
            dict(sku=sku),
        )
    )
    assert len(orders) == 1
```

> ⚠️ If this test misbehaves on your machine, compare with the book's `test_uow.py` for this chapter (it uses the same idea). I wrote it carefully but couldn't execute it.

### Stretch

- Replace optimistic locking with a pessimistic lock: in `SqlAlchemyRepository.get`, use `.with_for_update().first()`. The second transaction now *waits* instead of failing. When would you pick each?
- Argue (in two sentences) why `Batch` should *not* get its own repository.

## Check yourself
- What invariant forced us to introduce `Product`?
- Why can't a service load a single `Batch` directly any more?
- Optimistic vs pessimistic locking: when does each win?
- How do you decide aggregate size?
