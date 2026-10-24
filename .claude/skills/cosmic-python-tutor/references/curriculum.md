# Curriculum map

Base URL: `https://www.cosmicpython.com/book/<slug>.html` (the site's own links use `.html`).
Running example: allocating customer order lines to warehouse batches for a furniture retailer.
Companion code (one branch per chapter, handy for diffing when stuck): https://github.com/cosmicpython/code

| Ch | Slug | Pain it answers → pattern | Deliverable by the end |
|---|---|---|---|
| Preface/Intro | `preface`, `introduction` | Big ball of mud → layering, dependency inversion | A hand-drawn dependency diagram of a codebase they know |
| 1 | `chapter_01_domain_model` | Logic buried in infrastructure → pure domain model | `OrderLine` (value object), `Batch` (entity), `allocate()`, `OutOfStock`; 8 passing unit tests |
| 2 | `chapter_02_repository` | Model coupled to ORM → repository + inverted ORM | `orm.py` mapping, `AbstractRepository`, SQLAlchemy repo, `FakeRepository` |
| 3 | `chapter_03_abstractions` | Hard-to-test I/O → functional core / imperative shell, fakes over mocks | The directory-sync exercise, tested edge to edge with a fake |
| 4 | `chapter_04_service_layer` | Orchestration in Flask → service layer | `services.allocate`, Flask endpoint, e2e test |
| 5 | `chapter_05_high_gear_low_gear` | Tests coupled to the model → service-layer tests in domain language | Service tests with no `model.` imports; `add_batch`, `deallocate` services |
| 6 | `chapter_06_uow` | Services juggle session and repo → Unit of Work | `AbstractUnitOfWork`, SQLAlchemy + fake UoW, rollback tests |
| 7 | `chapter_07_aggregate` | No home for the cross-batch invariant, concurrency → `Product` aggregate, version number | `Product.allocate`, repo `get(sku)`, threaded concurrency test (Postgres) |
| 8 | `chapter_08_events_and_message_bus` | Side-effects tangled in allocation → domain events + bus | `events.py`, `messagebus.py`, UoW collecting events, email handler test |
| 9 | `chapter_09_all_messagebus` | Two entrypoints (service, bus) → bus as the only entrypoint, internal queue | Handlers `(event, uow)`, `change_batch_quantity` and reallocation flow |
| 10 | `chapter_10_commands` | Events and requests blurred → commands vs events, error semantics | `commands.py`, split registries, log-and-continue for events |
| 11 | `chapter_11_external_events` | Synchronous service coupling → Redis pub/sub integration | Redis consumer and publisher, docker-compose e2e test |
| 12 | `chapter_12_cqrs` | Domain model used for reads → CQRS read model | `views.py`, `GET /allocations/<orderid>`, `allocations_view` table |
| 13 | `chapter_13_dependency_injection` | `mock.patch` and import-time wiring → bootstrap + DI | `bootstrap.py`, class-based `MessageBus`, fake adapters, zero `mock.patch` |
| Epilogue | `epilogue_1_how_to_get_there_from_here` | Applying it to legacy code → strangler approach, incremental adoption | A one-page adoption plan for their own project |
| App A | `appendix_ds1_table` | Summary of every pattern with pros/cons | Redraw from memory |
| App B | `appendix_project_structure` | Project template (Docker, Makefile, layout) | Use for next project |
| App C | `appendix_csvs` | Proof the infra is swappable | CSV repo + UoW passing the existing tests |
| App D | `appendix_django` | Repo/UoW over a framework ORM | Optional, if they use Django |
| App E | `appendix_validation` | Where syntax, semantics and pragmatics checks live | Optional |

Part pages: `part1` (domain modeling and architecture, ch1-7), `part2` (event-driven architecture, ch8-13).

## The cumulative smell chain (say it aloud when crossing chapters)

ch1 model is pure → ch2 storage hidden behind a repo → ch4 services orchestrate but know about sessions → ch6 UoW hides the session → ch7 aggregate defines the consistency boundary → ch8 events carry side-effects out of the model → ch9 the bus replaces the service layer → ch10 commands and events are distinguished → ch11 events leave the process → ch12 reads leave the domain model → ch13 wiring is centralised so tests stop patching.
