# Methodology

These are the hard rules for every change to this repository. They aren't guidelines: each one is checked automatically, and CI fails if a change breaks any of them. A change that can't meet a rule doesn't get merged with an exception. Either the change is reworked, or the rule is changed on purpose (see [Changing a rule](#changing-a-rule)).

| # | Rule | Enforced by |
|---|---|---|
| 1 | Follow the hexagonal architecture | `tests/test_methodology.py` (`HexagonalLayerTests`) |
| 2 | Docstrings on every module, class and function | `tests/test_methodology.py` (`DocstringTests`) and ruff (`D1`) |
| 3 | Pass the lint rules in `pyproject.toml` | ruff, in the CI `lint` job |
| 4 | Tests pass, and new behaviour comes with a test | the CI `test` and `smoke` jobs |

Check everything locally before pushing:

```bash
pip install -r requirements.txt -r requirements-dev.txt
ruff check .
python -m unittest
```

## Rule 1: hexagonal architecture

The service is built as ports and adapters. The business rules sit in the middle and know nothing about HTTP, SQLite, FastAPI or any sportsbook's format. Everything that talks to the outside world is an adapter that plugs into a port.

```
                    ┌───────────────────────────────────────────┐
  Northstar ──▶     │ adapters/providers   ─┐                   │
  Cascade   ──▶     │                       ▼                   │
                    │      ports.py ──▶ application/ ──▶ domain │
  HTTP clients ──▶  │ adapters/api.py ──────┘     ▲             │
                    │ adapters/sqlite_store.py ───┘ (via ports) │
                    └───────────────────────────────────────────┘
```

### The layers

| Layer | Path | What belongs here | May import |
|---|---|---|---|
| Domain | `betsync/domain.py` | The canonical model (bets, legs, events), identity rules, odds conversion, and the conflict rule `decide()` | The standard library only |
| Ports | `betsync/ports.py` | The interfaces the application needs: `Provider`, `SyncStore`, `PageWriter`, `ReadStore`, plus the errors and records they exchange | Domain, standard library |
| Application | `betsync/application/` | The use cases: `sync.py` (fetch, translate, run the conflict rule, store) and `read.py` (canonical state plus freshness) | Domain, ports, standard library |
| Adapters | `betsync/adapters/` | Implementations of the ports: `providers/` (one per sportsbook), `sqlite_store.py`, and `api.py` (the HTTP API) | Domain, ports and third-party packages. `api.py` may also call the application |
| Simulator | `betsync/simulator/` | The simulated sportsbooks. They stand in for the outside world, so they sit outside the hexagon | Domain (reference data such as team names) |
| Composition root | `betsync/__main__.py`, `betsync/config.py` | Choosing the adapters and wiring them to the use cases | Anything |

### The rules

1. **Dependencies point inwards.** Domain imports nothing from the service; ports import only the domain; the application imports only the domain and ports. No inner layer ever imports an adapter.
2. **The inner layers use only the standard library.** No FastAPI, requests, sqlite3-specific types or other third-party code in `domain.py`, `ports.py` or `application/`.
3. **Adapters don't import each other.** `providers/`, `sqlite_store.py` and `api.py` are independent. They meet only through the ports, in the composition root.
4. **Business decisions live in the domain, and only the application runs them.** In particular, only the application calls `decide()`. Storage writes what the use case tells it to; it never decides what wins.
5. **Provider details stop at the adapter.** A provider's field names, status codes, ID formats, odds, money and timestamp formats are translated in its adapter's `to_observation()`. Nothing past that point mentions them, and no use case contains `if provider == ...`.
6. **Wiring happens in one place.** Only `__main__.py` constructs adapters and hands them to use cases. Use cases receive their ports as constructor arguments, so tests can pass fakes.

### Where a change goes

| Change | Where |
|---|---|
| Add a sportsbook | A new module in `adapters/providers/` implementing `Provider`, listed in `providers.build()`. Nothing in `application/` or `domain.py` changes |
| Change storage (e.g. Postgres) | A new adapter implementing `SyncStore`, `PageWriter` and `ReadStore`, chosen in `__main__.py` |
| Add an API endpoint | A route in `adapters/api.py` that calls a method on the read service; the query itself goes in `application/read.py` and, if it needs new data, in `ReadStore` plus the store adapter |
| Change what wins a conflict | `domain.decide()`, with a test in `tests/test_domain.py` |
| Change retries, backoff or scheduling | `application/sync.py` (`SyncPolicy`, `SyncWorker`) |
| A new kind of upstream fault | The simulator (`simulator/faults.py` and the book that has it), then the handling in the adapter or use case |

If a change needs a new capability from the outside world, add it to a port first, then implement it in an adapter. Never reach around the port.

## Rule 2: docstrings on everything

Every module, package, class and function has a docstring: public and private, top-level and nested, in `betsync/` and in `tests/`.

- **Say what it does and anything that isn't obvious from the name:** units, what `None` means, which errors it raises, side effects, or why it exists.
- **Keep it short.** One line is often enough. Don't restate the signature.
- **Test docstrings state the behaviour being proven,** e.g. "An older version arriving after a newer one is stale and not applied."
- **Docstrings change with the code.** A docstring that no longer matches what the function does is a bug.

Ruff's `D1` rules cover public modules, classes and functions. Ruff skips private (`_name`) and nested functions, so `DocstringTests` checks every one of those too.

## Rule 3: lint

`ruff check .` must report nothing. The rules are in `pyproject.toml`, and the ruff version is pinned in `requirements-dev.txt` so CI and your machine agree.

| Ruleset | Catches |
|---|---|
| `E`, `W` | pycodestyle errors and warnings, including lines over 120 characters |
| `F` | unused imports and variables, undefined names |
| `I` | import order |
| `B` | likely bugs (mutable defaults, `zip()` without `strict`, and so on) |
| `UP` | outdated syntax for Python 3.11 |
| `N` | naming conventions |
| `SIM`, `C4` | needlessly complicated code and comprehensions |
| `D1`, `D3`, `D419` | missing, badly quoted or empty docstrings |
| `T20` | `print()` (use `logging`) |

Every ignored rule in `pyproject.toml` carries a comment saying why. Don't add a `# noqa` to get past a rule. If a rule is wrong for this codebase, change it in `pyproject.toml`, with the reason.

## Rule 4: tests

- `python -m unittest` passes, and so do the CI `test` and `smoke` jobs.
- A change in behaviour comes with a test that would fail without it. A bug fix comes with a test that reproduces the bug.
- Tests use the simulator or fakes for the outside world. Nothing reaches the network beyond `127.0.0.1`.

## Definition of done

A change is ready when:

- [ ] it respects the layers (rule 1), and `HexagonalLayerTests` passes
- [ ] every new or changed module, class and function has an accurate docstring (rule 2)
- [ ] `ruff check .` is clean (rule 3)
- [ ] `python -m unittest` passes, with a test for the new behaviour (rule 4)
- [ ] the README still describes the code: run instructions, example calls and file paths

## Changing a rule

The rules can change, but only deliberately: update this file, the check that enforces the rule (`pyproject.toml` or `tests/test_methodology.py`), and any code it affects, all in the same commit, and say why in the commit message.
