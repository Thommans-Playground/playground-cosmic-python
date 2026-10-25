# Chapter 1: Domain Modeling

## ✈️ 30,000 ft
Before touching a database or web framework, model the business problem in plain Python objects that you can test in milliseconds. The domain model is the most valuable, most volatile part of the system, so it must be free of infrastructure.

## 🛫 10,000 ft
Vocabulary (DDD-lite):
- **Value Object**: defined by its data, immutable, equality by value (`OrderLine`).
- **Entity**: has an identity that persists as attributes change; equality by identity (`Batch`, via `reference`).
- **Domain Service**: logic that doesn't naturally belong on one entity (`allocate()`).
- **Ubiquitous language**: class and method names come from the business (batch, allocate, SKU, ETA).

Business rules in this chapter:
- An order line (orderid, sku, qty) is allocated to a **batch** of stock.
- Can't allocate if available quantity < line qty.
- Can't allocate the same line twice (idempotent).
- Warehouse stock (no ETA) is preferred over shipments; earlier ETAs are preferred over later.

### 🗺️ Diagram: the model

```mermaid
classDiagram
    class OrderLine {
        <<value object, frozen>>
        +orderid
        +sku
        +qty
    }
    class Batch {
        <<entity, identity = reference>>
        +reference
        +sku
        +eta
        -purchased_quantity
        -allocations : set~OrderLine~
        +allocate(line)
        +deallocate(line)
        +can_allocate(line) bool
        +available_quantity int
    }
    class allocate {
        <<domain service function>>
        allocate(line, batches) batchref
    }
    Batch "1" o-- "*" OrderLine : allocations
    allocate ..> Batch : sorts and picks
    allocate ..> OrderLine
```

```mermaid
flowchart TD
    A[allocate line, batches] --> B[sort batches: stock first, then earliest ETA]
    B --> C{any batch can_allocate?}
    C -- yes --> D[batch.allocate line, return reference]
    C -- no --> E[raise OutOfStock]
```

## 🛫 5,000 ft: the conventional way vs. the better way

> For every addition in this chapter: what most people write first, where it breaks, what we do instead, and what the better way costs. Evidence tags: 🧪 I ran it · 🧮 arithmetic · 📖 documentation or reasoning (not run).

### Addition 1: the allocation rule ("warehouse stock first, then earliest ETA")

**The conventional way:** let the database pick, inside the web handler.

```python
@app.route("/allocate", methods=["POST"])
def allocate():
    batches = Batch.query.filter_by(sku=sku).order_by(Batch.eta).all()   # the rule lives in this ORDER BY
    for b in batches:
        if b.available >= qty:
            b.available -= qty
            db.session.commit()
            return {"batchref": b.reference}, 201
    return {"message": "out of stock"}, 400
```

**Where it breaks**
- The business rule is now encoded in how a *database* orders `NULL`. Warehouse stock is `eta = NULL`. 🧪 SQLite sorts it first (`['warehouse', 'shipment']`). 📖 PostgreSQL sorts NULLs *last* in ascending order by default, so the same code would prefer shipments over stock in production. A rule that changes meaning with the database is a bug waiting for a migration.
- The rule has no name and no home. The next feature (a CLI, a report, a nightly job) re-implements it slightly differently.
- Testing it requires a database and a web client.

**The better way** (Step 2): `Batch.__gt__` plus a plain `allocate()` function that does `sorted(batches)`.

**Why it's right:** the rule is ordinary Python in one place, with a name, tested in milliseconds, and it behaves identically on every machine.
**What it costs:** sorting happens in memory, so you load all batches for the SKU first. Fine for tens or hundreds; ch07's aggregates deliberately keep that set small.

### Addition 2: equality (what makes two objects "the same")

**The conventional way:** plain dataclasses for everything.

```python
@dataclass
class Batch:
    reference: str
    qty: int
    allocations: list = field(default_factory=list)

@dataclass
class OrderLine:
    orderid: str
    sku: str
    qty: int
```

**Where it breaks**
- 🧪 `Batch("b1", 100) == Batch("b1", 100)` becomes `False` as soon as one of them has an allocation. The *same* batch, observed twice, is "different".
- 🧪 A non-frozen dataclass is unhashable: `{batch}` raises `TypeError: unhashable type`. The same goes for `OrderLine`, so you can't keep allocated lines in a `set`, and "allocating the same line twice does nothing" now needs hand-written list scanning.

**The better way** (Step 2): `OrderLine` is a **value object** (equality by all fields, hashable). `Batch` is an **entity** (equality and hash by `reference` only).

**Why it's right:** equality now matches what the business means: an order line *is* its contents; a batch *is* its reference, whatever its current state.
**What it costs:** entity equality is shallow. `batch_a == batch_b` says nothing about quantities, so tests that care must compare fields explicitly (ch02 does exactly this).

### Addition 3: "how much is left in a batch?"

**The conventional way:** store a counter and decrement it.

```python
class Batch:
    def __init__(self, qty):
        self.available_quantity = qty

    def allocate(self, line):
        self.available_quantity -= line.qty
```

**Where it breaks**
- 🧪 20 in stock, one order line of 10 submitted twice (a double-click, a retried request): the counter reads `0`. The truth is `10`.
- Deallocation has to remember what to add back, and any code path that forgets leaves the number wrong forever. There are now two sources of truth: the counter and the real list of allocations.

**The better way** (Step 2): store only `purchased_quantity` and a **set** of allocated lines; `available_quantity` is a computed property.

**Why it's right:** a derived number can't drift from its inputs, and the set makes the double submit harmless with no extra code.
**What it costs:** the number is recomputed (a sum over allocations) each time it's read. Irrelevant at this size; ch12 moves heavy reads somewhere else anyway.

## 🛬 1,000 ft: design decisions
- A `@dataclass` gives value-object equality for free; `unsafe_hash=True` adds hashability so lines can live in a `set`. (The book uses `frozen=True`; see the version note in the code-along.)
- `Batch` overrides `__eq__` and `__hash__` using `reference` only. Entity semantics.
- Batches keep a `set` of allocated lines, and `available_quantity` is *computed* (purchased − allocated), not stored. Fewer states to keep consistent.
- `__gt__` on `Batch` makes `sorted(batches)` express the business preference rule. Stock (`eta=None`) sorts first.
- `allocate(line, batches)` is a plain function, not a method. The book explains that Python lets you pick the shape that fits the concept.
- `OutOfStock` is a domain exception: the failure is a business concept.

## 🛞 Touchdown: code along

> **How to use this section.** Do each step in order: read the *Do* and *Why*, **type** the code (don't paste it), run the test, and watch it go red then green. All code below was run and passes on Python 3.14.

### Step 0: Project skeleton (uv, pytest, ruff, ty)

This guide uses the current Python toolchain:

| Tool | Replaces | Job |
|---|---|---|
| **uv** | pip, venv, pip-tools, pyenv | creates the project, manages the virtualenv and Python version, locks dependencies |
| **ruff** | flake8, isort, black | linting and formatting, in milliseconds |
| **ty** | mypy / pyright | type checking. It is Astral's checker and still **beta (pre-1.0)**; mypy is a fine substitute |
| **pytest** | | tests |

Install uv once (`brew install uv`, or see <https://docs.astral.sh/uv/>), then:

```bash
uv init --package allocation --python 3.12
cd allocation
uv add --dev uv run pytest ruff ty
mkdir -p src/allocation/domain tests/unit
touch src/allocation/domain/__init__.py
```

`uv init --package` creates a `src/` layout project: a `pyproject.toml`, a `.python-version`, and a stub `main()` in `src/allocation/__init__.py`. We're not building a command-line tool, so **empty that `__init__.py`** and **delete the `[project.scripts]` section** from `pyproject.toml`. `uv add --dev` records dev-only tools under `[dependency-groups]`, creates `.venv`, and installs them. The project itself is installed in editable mode, so `import allocation` just works. (Your version numbers will differ from the ones below; uv writes whatever is current.)

Now add the tool settings at the bottom of `pyproject.toml`. This is the file as it should look at the end of this chapter:

**📄 `pyproject.toml`**

```
[project]
name = "allocation"
version = "0.1.0"
description = "Allocation service from Architecture Patterns with Python"
requires-python = ">=3.12"
dependencies = []

[build-system]
requires = ["uv_build>=0.12.13,<0.13.0"]
build-backend = "uv_build"

[dependency-groups]
dev = [
    "pytest>=8",
    "ruff>=0.8",
    "ty>=0.0.1",
]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py312"
src = ["src", "tests"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM", "RUF"]
```

*Why `src/` layout?* The code in `src/allocation` is the thing you ship. Tests live beside it, not inside it, and import it like any other user would. `uv sync` rebuilds the same environment from `uv.lock` on any machine.

*Why a package called `allocation`?* The name is just the book's domain (allocating orders to stock); call yours anything, but every import in the guide says `allocation`, so keep it while you follow along. The point of `--package` is that your code becomes an **importable package**, installed in editable mode: any file, test or entrypoint can write `from allocation.domain import model` and it resolves the same way everywhere, with no `PYTHONPATH` tricks. It also makes the layers visible in the import lines (`allocation.domain`, `allocation.adapters`, `allocation.service_layer`, `allocation.entrypoints`), which is how you'll spot a dependency pointing the wrong way. A plain `uv init` makes a script-style project (a `main.py`, nothing importable); ch03's exercise deliberately uses one so you can compare. For a tiny script a package is overkill; with a dozen modules importing each other across layers it isn't.

The four commands you'll use all the time:

```bash
uv run pytest        # tests
uv run ruff check    # lint   (add --fix to auto-fix)
uv run ruff format   # format
uv run ty check src  # types
```

### Step 1: Write the first test (red)

**Do:** create `tests/unit/test_batches.py` and start with just this one test and the imports. Run `pytest`. It fails with `ImportError`: that's the correct red.

```python
from datetime import date
from allocation.domain.model import Batch, OrderLine


def test_allocating_to_a_batch_reduces_the_available_quantity():
    batch = Batch("batch-001", "SMALL-TABLE", qty=20, eta=date.today())
    line = OrderLine("order-ref", "SMALL-TABLE", 2)

    batch.allocate(line)

    assert batch.available_quantity == 18
```

**Why:** the test is written in the business's language (batch, order line, allocate). It *is* the first spec of the model.

### Step 2: Make it pass with a minimal model

**Do:** create `src/allocation/domain/model.py` with `OrderLine` and `Batch` (below is the finished file; at this step you only need `OrderLine`, `Batch.__init__`, `allocate`, and `available_quantity`). Add the rest as later tests demand it.

**Why each piece exists:**

| Piece | Reason |
|---|---|
| `@dataclass(unsafe_hash=True) class OrderLine` | **Value object**: two lines with the same orderid/sku/qty *are* the same thing. A dataclass gives you value `__eq__` for free; `unsafe_hash=True` also makes it hashable so it can live in a `set`. |
| `Batch.__eq__` / `__hash__` use only `reference` | **Entity**: a batch stays the same batch even if its quantity changes, so identity is its reference. |
| `_allocations: Set[OrderLine]` | A set makes allocating the same line twice a no-op (idempotence) with no extra code. |
| `available_quantity` is a *property* | Computed = purchased − allocated. There's no second number to keep in sync. |
| `__gt__` on `Batch` | Lets `sorted(batches)` express the business rule: stock first (`eta is None`), then earliest ETA. |
| `allocate()` is a plain function | The rule involves *many* batches, so it doesn't belong to one of them. Python lets a domain service be just a function. |
| `OutOfStock` exception | A business failure with a business name. |

> **⚠️ Version note (this differs from the book).** The book writes `@dataclass(frozen=True)`. With SQLAlchemy 2.x (needed on current Python) that raises `FrozenInstanceError: cannot assign to field '_sa_instance_state'` as soon as you map the class in chapter 2, because SQLAlchemy must attach bookkeeping to the instance. `unsafe_hash=True` keeps value-equality and hashability; just treat the object as immutable by convention.

**📄 `src/allocation/domain/model.py`**

```python
from dataclasses import dataclass
from datetime import date


class OutOfStock(Exception):
    pass


def allocate(line: "OrderLine", batches: list["Batch"]) -> str:
    try:
        batch = next(b for b in sorted(batches) if b.can_allocate(line))
    except StopIteration:
        raise OutOfStock(f"Out of stock for sku {line.sku}") from None
    batch.allocate(line)
    return batch.reference


@dataclass(unsafe_hash=True)
class OrderLine:
    orderid: str
    sku: str
    qty: int


class Batch:
    def __init__(self, ref: str, sku: str, qty: int, eta: date | None):
        self.reference = ref
        self.sku = sku
        self.eta = eta
        self._purchased_quantity = qty
        self._allocations: set[OrderLine] = set()

    def __repr__(self):
        return f"<Batch {self.reference}>"

    def __eq__(self, other):
        if not isinstance(other, Batch):
            return False
        return other.reference == self.reference

    def __hash__(self):
        return hash(self.reference)

    def __gt__(self, other):
        if self.eta is None:
            return False
        if other.eta is None:
            return True
        return self.eta > other.eta

    def allocate(self, line: OrderLine):
        if self.can_allocate(line):
            self._allocations.add(line)

    def deallocate(self, line: OrderLine):
        if line in self._allocations:
            self._allocations.remove(line)

    @property
    def allocated_quantity(self) -> int:
        return sum(line.qty for line in self._allocations)

    @property
    def available_quantity(self) -> int:
        return self._purchased_quantity - self.allocated_quantity

    def can_allocate(self, line: OrderLine) -> bool:
        return self.sku == line.sku and self.available_quantity >= line.qty
```

### Step 3: Add the remaining tests one at a time

Each test below is a rule from the business. Add them **one by one**, each time seeing it fail (or pass) for the reason you expect before moving on. Full test file:

**📄 `tests/unit/test_batches.py`**

```python
from datetime import date, timedelta

import pytest

from allocation.domain.model import Batch, OrderLine, OutOfStock, allocate

today = date.today()
tomorrow = today + timedelta(days=1)
later = tomorrow + timedelta(days=10)


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


def test_prefers_warehouse_batches_to_shipments():
    in_stock_batch = Batch("in-stock-batch", "RETRO-CLOCK", 100, eta=None)
    shipment_batch = Batch("shipment-batch", "RETRO-CLOCK", 100, eta=tomorrow)
    line = OrderLine("oref", "RETRO-CLOCK", 10)

    allocate(line, [in_stock_batch, shipment_batch])

    assert in_stock_batch.available_quantity == 90
    assert shipment_batch.available_quantity == 100


def test_prefers_earlier_batches():
    earliest = Batch("speedy-batch", "MINIMALIST-SPOON", 100, eta=today)
    medium = Batch("normal-batch", "MINIMALIST-SPOON", 100, eta=tomorrow)
    latest = Batch("slow-batch", "MINIMALIST-SPOON", 100, eta=later)
    line = OrderLine("order1", "MINIMALIST-SPOON", 10)

    allocate(line, [medium, earliest, latest])

    assert earliest.available_quantity == 90
    assert medium.available_quantity == 100
    assert latest.available_quantity == 100


def test_returns_allocated_batch_ref():
    in_stock_batch = Batch("in-stock-batch-ref", "HIGHBROW-POSTER", 100, eta=None)
    shipment_batch = Batch("shipment-batch-ref", "HIGHBROW-POSTER", 100, eta=tomorrow)
    line = OrderLine("oref", "HIGHBROW-POSTER", 10)
    allocation = allocate(line, [in_stock_batch, shipment_batch])
    assert allocation == in_stock_batch.reference


def test_raises_out_of_stock_exception_if_cannot_allocate():
    batch = Batch("batch1", "SMALL-FORK", 10, eta=today)
    allocate(OrderLine("order1", "SMALL-FORK", 10), [batch])

    with pytest.raises(OutOfStock, match="SMALL-FORK"):
        allocate(OrderLine("order2", "SMALL-FORK", 1), [batch])
```

Suggested order and what each drives into the model:

1. `can_allocate` tests (greater, smaller, equal) → `can_allocate()` comparing `available_quantity`.
2. SKU mismatch → add `self.sku == line.sku`.
3. Deallocate only allocated lines → `deallocate()` guarded by `if line in self._allocations`.
4. Idempotent allocate → already true because `_allocations` is a set. **Check it passes immediately and ask why.**
5. Prefers warehouse stock / earlier batches → `__gt__` and `allocate()` using `sorted(...)`.
6. Returns the batch reference → `allocate()` returns `batch.reference`.
7. Raises `OutOfStock` → catch `StopIteration` from `next(...)` and raise the domain error.

### Step 4: Run and expect

```bash
uv run pytest -q
# ...........                                                  [100%]
# 11 passed
```

The whole suite runs in a few milliseconds, with no database or web server. That speed is the payoff of a pure model.

### Stretch

- Replace `__gt__` with a `sort_key` function and compare readability.
- Add a rule: a batch with `eta` in the past is treated like warehouse stock. Which one place do you edit?

## Check yourself
- Why is `OrderLine` a value object but `Batch` an entity? What would break if both used the same equality?
- Why is `allocate` not a method on `Batch`?
- Where does the "prefer warehouse stock" rule live, and how would you change it?
- What's the benefit of tests that import only `model`?
