# Chapter 11: Event-Driven Architecture: Using Events to Integrate Microservices

## ✈️ 30,000 ft
Messages don't have to stay inside one process. Publish events to **Redis pub/sub** so other services (a reporting service, a shipping service) can react, and consume external messages as commands. Events are the integration contract between services.

## 🛫 10,000 ft
- Avoid **distributed ball of mud**: services calling each other synchronously via REST ("temporal coupling"). If one is down, all fail.
- Prefer **asynchronous messaging** between services, with each service owning its data and reacting to events from others.
- Terms: **temporal decoupling**, **choreography** over orchestration, **eventual consistency**.
- Redis pub/sub is the book's pragmatic broker, though it's lossy (no persistence); real systems use Kafka, RabbitMQ, SNS/SQS.
- Naming conventions: services publish **events** (facts), receive **commands** (requests) from other systems.
- Trade-off: you gain independence and resilience, but you need monitoring, idempotency and a way to handle lost/duplicate messages.

### 🗺️ Diagram: services talking via Redis

```mermaid
flowchart LR
    subgraph Ext["Other services"]
        WH[Warehouse / shipping service]
        RP[Reporting service]
    end
    subgraph Alloc["Allocation service"]
        Cons[redis_eventconsumer] -->|ChangeBatchQuantity cmd| Bus{{MessageBus}}
        API[FastAPI API] -->|commands| Bus
        Bus --> Pub[redis_eventpublisher handler]
    end
    WH -->|publish change_batch_quantity| Redis[(Redis pub/sub)]
    Redis -->|subscribe| Cons
    Pub -->|publish line_allocated| Redis
    Redis --> RP
```

```mermaid
flowchart LR
    subgraph Bad["Distributed ball of mud: sync REST"]
        A1[Service A] -->|HTTP| B1[Service B] -->|HTTP| C1[Service C]
    end
    subgraph Good["Async events: temporal decoupling"]
        A2[Service A] -->|event| Br[(Broker)]
        Br --> B2[Service B]
        Br --> C2[Service C]
    end
```

## 🛫 5,000 ft: the conventional way vs. the better way

> Evidence tags: 🧪 I ran it · 🧮 arithmetic · 📖 documentation or reasoning (not run).

### Addition 1: telling other services what happened

**The conventional way:** call them over HTTP from the handler.

```python
def allocate(cmd, uow):
    with uow:
        ...
        uow.commit()
    requests.post("http://shipping-service/allocations", json={...}, timeout=2)
    requests.post("http://reporting-service/allocations", json={...}, timeout=2)
```

**Where it breaks**
- Availability multiplies. 🧮 If three services are each up 99.9% of the time and a request needs all three, you get 99.7%, about 26 hours of trouble per year instead of 8.8; with five services, 99.5%.
- When shipping is slow, *your* endpoint is slow (or hangs until the timeout), and retries pile load onto a service that's already struggling.
- Services must be deployed in step with one another because each knows the others' URLs and request shapes.

**The better way** (Steps 2-3): publish an `Allocated` **event** to a broker (here Redis); whoever cares subscribes.

**Why it's right:** allocation no longer depends on who is listening, or whether they are up right now. Adding a fourth consumer changes nothing in this service.
**What it costs:** 📖 Redis pub/sub is fire-and-forget. A subscriber that's offline *misses* the message. Real systems use something durable (Streams, RabbitMQ, Kafka) and need idempotent handlers. You also give up the instant "did it work?" answer, and you need monitoring to notice lost messages.

### Addition 2: sharing a database instead

**The conventional way:** let the other service read your tables directly.

**Where it breaks:** any schema change (rename a column, split a table) now breaks services you may not know exist; there's no owner of the data's meaning; and they bypass every rule your domain model enforces.

**The better way:** each service owns its data and publishes events or answers queries.

**Why it's right:** the event is a deliberate, versioned public contract, while your tables stay private to change.
**What it costs:** data that other services need is *copied* via events, so it can be stale.

### Addition 3: what goes on the wire

**The conventional way:** serialize your internal objects or ORM rows as the message.

**Where it breaks:** rename a field in `Allocated` for internal tidiness and every subscriber silently breaks.

**The better way** (Steps 2, 6): the event dataclass *is* the contract. The outgoing adapter serializes it explicitly; the incoming adapter **translates** the other service's message into our own command at the edge (`ChangeBatchQuantity`), so their format never leaks inside.

**Why it's right:** each side can change internally without touching the other.
**What it costs:** a translation layer to write and test, and you now own backward compatibility for anything you publish.

## 🛬 1,000 ft: design decisions
- `redis_eventconsumer.py`: subscribes to a channel (`change_batch_quantity`), deserialises JSON → `commands.ChangeBatchQuantity` → `messagebus.handle`.
- `redis_eventpublisher.py`: handler `publish_allocated_event` for `Allocated` → `r.publish("line_allocated", json.dumps(asdict(event)))`.
- Handlers are registered in `EVENT_HANDLERS[events.Allocated] = [..., publish_allocated_event]`.
- E2E test with Docker-compose: API + Redis + Postgres + redis consumer container. Test posts to the API, subscribes to Redis, and asserts a message.
- Authors flag the "fake it" idea: test your handlers with a `FakeRedis`/injected publisher (leads to ch13).

## 🛞 Touchdown: code along

> Starts from your finished ch10 project. Goal: let *other services* talk to this one through Redis pub/sub. **Outgoing:** publish a `line_allocated` message whenever stock is allocated. **Incoming:** listen on `change_batch_quantity` and turn each message into a `ChangeBatchQuantity` command. 48 tests pass; 2 are skipped because they need real services (Postgres for ch07's concurrency test, the full stack for the Redis e2e test below). **I could not run that e2e test here (no Docker/Redis in my environment)**, but everything else, including the publisher and consumer logic, is covered by passing unit tests.

```bash
uv add redis
```

### The idea

```
 other service ── change_batch_quantity ──▶ Redis ──▶ redis_eventconsumer ──▶ bus (command)
 other service ◀── line_allocated ───────── Redis ◀── publisher ◀─ Allocated event handler
```

The bus is the single doorway, so Redis is just two more thin adapters around it: the consumer is an *entrypoint* (like FastAPI), the publisher is an *event handler*.

### Step 1: Config

**Do:** add two fields to `Settings`. They are typed, so `REDIS_PORT=banana` now fails at startup with a clear message, not later when something first tries to connect.

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

### Step 2: The outgoing adapter

**Do:** create `adapters/redis_eventpublisher.py`. It serialises an event with `dataclasses.asdict` + JSON and publishes it to a channel.

**Why:** the JSON shape is now a **contract with other services**. `Allocated` has fields `orderid, sku, qty, batchref`; renaming one is a breaking change for subscribers. That is the main new cost of integrating through events.

**📄 `src/allocation/adapters/redis_eventpublisher.py`**

```python
import json
import logging
from dataclasses import asdict

import redis

from allocation import config
from allocation.domain import events

logger = logging.getLogger(__name__)

_settings = config.get_settings()

# Creating the client doesn't connect; it only connects on first use.
r = redis.Redis(host=_settings.redis_host, port=_settings.redis_port)


def publish(channel, event: events.Event):
    logger.info("publishing: channel=%s, event=%s", channel, event)
    r.publish(channel, json.dumps(asdict(event)))
```

> `dataclasses.asdict()` only accepts dataclass instances, so the `Event` base class in `events.py` becomes a `@dataclass` too (it has no fields). The type checker (`ty`, mypy) flags it otherwise.

### Step 3: Subscribe it as an event handler

**Do:** add a handler in `handlers.py` and register it for `Allocated` in `messagebus.py` (this is where the empty list from ch10 gets its first listener).

```python
# handlers.py
from allocation.adapters import email, redis_eventpublisher    # extended import

def publish_allocated_event(event: events.Allocated, uow: unit_of_work.AbstractUnitOfWork):
    redis_eventpublisher.publish("line_allocated", event)
```

```python
# messagebus.py
EVENT_HANDLERS = {
    events.OutOfStock: [handlers.send_out_of_stock_notification],
    events.Allocated: [handlers.publish_allocated_event],
}
```

Because it's an *event* handler (ch10), a Redis outage is logged and swallowed: allocation still succeeds. Is that what you want? That's a business decision; write down your answer.

### Step 4: Keep tests off the network

**Do:** replace the Redis client with a mock for **every** test. Add this autouse fixture to `tests/conftest.py` (needs `import os`, `from unittest import mock` at the top):

```python
@pytest.fixture(autouse=True)
def fake_redis():
    """No test may talk to a real Redis. Replace the module's client with a mock;
    `publish()` still runs for real, so we can assert on what it sends."""
    with mock.patch("allocation.adapters.redis_eventpublisher.r") as client:
        yield client
```

> **Why this matters (I hit it):** without it, every allocation in the e2e tests tried to connect to a Redis that wasn't there, and the suite went from 0.4s to 5s while the errors were swallowed by the event-handler `try/except`. A silent slowdown is a nice demonstration of ch10's "log and continue" trade-off.

### Step 5: Tests for both directions

**Do:** `tests/unit/test_redis.py` checks the JSON that goes out, that an incoming message becomes the right command, and that **malformed messages are dropped** instead of crashing the consumer. Also add one handler test in `test_handlers.py`:

**📄 `tests/unit/test_redis.py`**

```python
import json
from unittest import mock

import pytest

from allocation.adapters import redis_eventpublisher
from allocation.domain import commands, events
from allocation.entrypoints import redis_eventconsumer


def test_publish_serialises_the_event_as_json(fake_redis):
    redis_eventpublisher.publish("line_allocated", events.Allocated("o1", "SOME-SKU", 5, "b1"))

    channel, payload = fake_redis.publish.call_args.args
    assert channel == "line_allocated"
    assert json.loads(payload) == {
        "orderid": "o1",
        "sku": "SOME-SKU",
        "qty": 5,
        "batchref": "b1",
    }


def test_consumer_turns_a_message_into_a_command():
    message = {"data": json.dumps({"batchref": "b1", "qty": 10})}
    uow = object()

    with mock.patch.object(redis_eventconsumer.messagebus, "handle") as handle:
        redis_eventconsumer.handle_change_batch_quantity(message, uow)

    handle.assert_called_once_with(commands.ChangeBatchQuantity(ref="b1", qty=10), uow=uow)


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
    uow = object()

    with mock.patch.object(redis_eventconsumer.messagebus, "handle") as handle:
        redis_eventconsumer.handle_change_batch_quantity(message, uow)

    handle.assert_not_called()
```

```python
# in tests/unit/test_handlers.py (add `import json` at the top)
class TestPublishing:
    def test_allocating_publishes_a_line_allocated_message(self, fake_redis):
        uow = FakeUnitOfWork()
        messagebus.handle(commands.CreateBatch("b1", "LAMP", 100, None), uow)

        messagebus.handle(commands.Allocate("o1", "LAMP", 10), uow)

        channel, payload = fake_redis.publish.call_args.args
        assert channel == "line_allocated"
        assert json.loads(payload)["batchref"] == "b1"
```

### Step 6: The incoming adapter, with pydantic validating the wire format

**Do:** create `entrypoints/redis_eventconsumer.py`.

**Why:**
- The consumer **translates** the other service's message (`{"batchref":..., "qty":...}`) into *our* command (`ChangeBatchQuantity(ref, qty)`). Their wire format stays at the edge; nothing inside knows about it.
- **`ChangeBatchQuantityMessage` is a pydantic model of their format.** Anything arriving from another system is untrusted: it might not be JSON, `qty` might be missing, a string, or negative. `model_validate_json` checks all of that in one call, and a bad message is logged and dropped rather than becoming a half-built command (or a crash that kills the consumer loop). This is the same "validate at the edge" rule as the API's request models.
- `handle_change_batch_quantity` takes the UoW as a parameter so it is testable without a database.

**📄 `src/allocation/entrypoints/redis_eventconsumer.py`**

```python
import logging

import redis
from pydantic import BaseModel, Field, ValidationError

from allocation import config
from allocation.adapters import orm
from allocation.domain import commands
from allocation.service_layer import messagebus, unit_of_work

logger = logging.getLogger(__name__)


class ChangeBatchQuantityMessage(BaseModel):
    """The other service's wire format, validated at the edge before it becomes a command."""

    batchref: str
    qty: int = Field(ge=0)


def main():
    orm.start_mappers()
    settings = config.get_settings()
    session_factory = orm.make_session_factory(settings.db_uri)
    r = redis.Redis(host=settings.redis_host, port=settings.redis_port)
    pubsub = r.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe("change_batch_quantity")

    for m in pubsub.listen():
        handle_change_batch_quantity(m, unit_of_work.SqlAlchemyUnitOfWork(session_factory))


def handle_change_batch_quantity(m, uow):
    logger.info("handling %s", m)
    try:
        msg = ChangeBatchQuantityMessage.model_validate_json(m["data"])
    except ValidationError:
        logger.exception("dropping malformed message %s", m)
        return
    # The wire format belongs to the OTHER service; translate it into our command.
    cmd = commands.ChangeBatchQuantity(ref=msg.batchref, qty=msg.qty)
    messagebus.handle(cmd, uow=uow)


if __name__ == "__main__":
    main()
```

### Step 7: The real thing, end to end (needs Docker)

Start the dependencies (a minimal `docker-compose.yml`):

```yaml
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
uv add "psycopg[binary]"
export DB_URI=postgresql+psycopg://allocation:abc123@localhost:5432/allocation
uv run uvicorn --factory allocation.entrypoints.fastapi_app:create_app --port 5055 &      # terminal 1
uv run python -m allocation.entrypoints.redis_eventconsumer &                            # terminal 2
API_URL=http://localhost:5055 uv run pytest tests/e2e/test_external_events.py -s         # terminal 3
```

The e2e test adds two batches, allocates an order to the earlier one, publishes `change_batch_quantity` shrinking that batch, then listens on `line_allocated` and expects the order to reappear against the *later* batch: bump, reallocation, and outgoing event, all through the bus.

**📄 `tests/e2e/test_external_events.py`**

```python
"""End-to-end test of the Redis integration. Needs the whole stack running:
Postgres + Redis + the FastAPI app + `python -m allocation.entrypoints.redis_eventconsumer`.
Skipped unless API_URL is set, e.g.  API_URL=http://localhost:5000 pytest tests/e2e
"""

import json
import os
import time
import uuid

import httpx2 as httpx
import pytest
import redis

from allocation import config

_s = config.get_settings()

API_URL = os.environ.get("API_URL")
pytestmark = pytest.mark.skipif(not API_URL, reason="set API_URL to run the Redis e2e test")


def random_ref(name=""):
    return f"{name}-{uuid.uuid4().hex[:6]}"


def post_to_add_batch(ref, sku, qty, eta):
    r = httpx.post(f"{API_URL}/add_batch", json={"ref": ref, "sku": sku, "qty": qty, "eta": eta})
    assert r.status_code == 201


def post_to_allocate(orderid, sku, qty):
    return httpx.post(f"{API_URL}/allocate", json={"orderid": orderid, "sku": sku, "qty": qty})


def subscribe_to(channel):
    r = redis.Redis(host=_s.redis_host, port=_s.redis_port)
    pubsub = r.pubsub()
    pubsub.subscribe(channel)
    confirmation = pubsub.get_message(timeout=3)
    assert confirmation["type"] == "subscribe"
    return pubsub


def publish_message(channel, message):
    r = redis.Redis(host=_s.redis_host, port=_s.redis_port)
    r.publish(channel, json.dumps(message))


def test_change_batch_quantity_leading_to_reallocation():
    orderid, sku = random_ref("order"), random_ref("sku")
    earlier_batch, later_batch = random_ref("old"), random_ref("newer")
    post_to_add_batch(earlier_batch, sku, qty=10, eta="2011-01-01")
    post_to_add_batch(later_batch, sku, qty=10, eta="2011-01-02")
    r = post_to_allocate(orderid, sku, 10)
    assert r.json()["batchref"] == earlier_batch

    subscription = subscribe_to("line_allocated")

    # the shipment of the earlier batch is damaged: only 5 units survive
    publish_message("change_batch_quantity", {"batchref": earlier_batch, "qty": 5})

    # the order is bumped and re-allocated to the later batch
    messages = []
    for _attempt in range(5):
        time.sleep(0.5)
        message = subscription.get_message(timeout=1)
        if message:
            messages.append(message)
            print(messages)
            data = json.loads(messages[-1]["data"])
            if data["orderid"] == orderid:
                break
    assert data["orderid"] == orderid
    assert data["batchref"] == later_batch
```

### Stretch

- Pub/sub is fire-and-forget: a subscriber that's offline misses messages. Read about Redis Streams (or RabbitMQ/Kafka) and write what you'd change first, and which of our adapters would be touched.
- Make `change_batch_quantity` idempotent. What happens if the same message arrives twice?

## Check yourself
- Why is request/response between services risky, and what does messaging fix?
- Which side owns the schema of an external event?
- Pub/sub vs queue: what do you lose with Redis pub/sub?
- How would you make a handler idempotent?
