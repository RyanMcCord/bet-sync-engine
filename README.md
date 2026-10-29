# Bet sync engine

A bettor has accounts at two sportsbooks. Each book reports their bets in its own format, through its own kind of API, and each misbehaves in its own way. This service keeps one dependable, canonical view of every bet, and is upfront about how fresh that view is.

Both sportsbooks are simulated inside the repo, so there are no credentials, accounts or network services to set up.

## Run it

Python 3.11 or newer.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m betsync --fresh
```

This starts one process with three parts: the two simulated sportsbooks, a sync worker per book, and the API on <http://127.0.0.1:8000>. Interactive API docs are at <http://127.0.0.1:8000/docs>. Simulated time runs 60× faster than real time (`--speed`), so games finish and bets settle within a few minutes. The default fault scenario is `realistic`; use `--scenario calm` for well-behaved books or `--scenario chaos` for heavy weather. `python -m betsync --help` lists every setting.

Tests: `python -m unittest` (about 50 tests, a few seconds).

### Try it

```bash
curl -s localhost:8000/status | python -m json.tool               # is sync healthy, degraded or stale?
curl -s 'localhost:8000/bets?status=won' | python -m json.tool     # canonical bets, with freshness
curl -s 'localhost:8000/bets?sportsbook=cascade&limit=5' | python -m json.tool
curl -s localhost:8000/bets/cascade:77003 | python -m json.tool    # one bet and every observation behind it
curl -s localhost:8000/events | python -m json.tool               # games, with each book's own ID for them

# Take Cascade down and watch its freshness degrade while Northstar carries on:
curl -s -X POST localhost:8000/upstream/cascade/_control -H 'content-type: application/json' -d '{"outage": true}'
sleep 40; curl -s localhost:8000/status | python -m json.tool      # cascade: "stale", overall: "stale"
curl -s 'localhost:8000/bets?sportsbook=cascade&limit=1' | python -m json.tool   # still served, marked stale
curl -s -X POST localhost:8000/upstream/cascade/_control -H 'content-type: application/json' -d '{"outage": false}'
curl -s -X POST 'localhost:8000/sync?provider=cascade'            # don't wait for the backoff
```

The `_control` endpoint accepts any fault setting, for example `{"error_rate": 0.5}`, `{"timeout_rate": 1}` or `{"rate_limit_per_sec": 1}`.

## The two sportsbooks

| | Northstar | Cascade |
|---|---|---|
| API style | current state of each bet, paged by `lastModified` | ordered change feed by sequence number, plus a single-ticket lookup |
| Bet ID | `"NS-10007"` | `77012` |
| Odds | American, as a string: `"+150"` | decimal: `2.5` |
| Money | dollar strings: `"25.00"` | integer cents: `2500` |
| Timestamps | ISO 8601 | epoch seconds |
| Status | `PENDING`, `SETTLED_WIN`, `VOIDED`, ... | `O`, `W`, `L`, `P`, `V`, `C` |
| Teams | codes: `PHX @ LAL` | full names, home/away sides |
| Its trouble | duplicates across pages; a lagging replica that serves older versions (out of order) | at-least-once redelivery; records missing optional fields |
| Both | random 5xx errors, timeouts, extra latency, rate limits (429 with `Retry-After`), outages on demand | |

The simulation is seeded (`--seed`). The same seed gives the same games, bets and failures every time. It lives in `betsync/simulator/`, kept separate from the service's own code.

## How it works

```
 simulated books           adapters                     sync engine                          API
 (betsync/simulator)       (betsync/providers)          (betsync/sync.py, store.py)          (api.py, service.py)

 Northstar ──HTTP──▶ NorthstarProvider ─┐   fetch page → translate → decide → store + cursor   /bets
                                         ├─▶ (one worker per book, own retries and health)  ─▶ /bets/{id}
 Cascade   ──HTTP──▶ CascadeProvider  ──┘                     │                                /events
                                                              ▼                                /status
                                                   SQLite: canonical state + history
```

### Provider adapters (`betsync/providers/`)

An adapter does two things: `fetch(cursor)` gets a page of raw records, and `to_observation(raw)` translates one record into the canonical model. It can optionally do a third, `fetch_record(id)`, which re-fetches one record to fill in missing fields. Retries, deduplication, conflict resolution, storage and health live in the engine, so neither adapter contains logic for the other book, and adding a third book means writing one file. Adapters raise one of four typed errors: `TransientError`, `RateLimited`, `PermanentError`, or `RecordError` for a single bad record.

### Canonical model and data ownership (`betsync/domain.py`)

This is where provider data becomes ours. Past this point, nothing knows a provider's field names, status codes or formats.

- **Bet:** ID `sportsbook:external_id`; status `open` / `won` / `lost` / `push` / `void` / `cashed_out`; stake and payout in integer cents; odds in American format; legs that point at canonical events.
- **Event:** the same real game is described differently by each book, as codes vs full names and with slightly different start times. Both resolve to one ID, like `nba-2026-11-01-phx-at-lal`, built from the league, the game's local date and the canonical team codes. Northstar's own event IDs are kept as aliases. Teams we don't recognise still get a stable ID, flagged `teams_recognised: false`.
- **Ownership:** each sportsbook owns its bets, and is the only source for them. The service owns the canonical identity (bet and event IDs), the merge decisions and freshness. Nothing is ever written back to a book.

### Lifecycle of a record

Following one Cascade ticket from start to finish (the values are an example):

1. The Cascade worker asks the change feed for changes after its saved cursor (`since_seq=212`) and gets ticket `77003` as `{"state": "W", "stake": 2500, "decimal_odds": 2.5, "return": 6250, "updated": 1793671800, ...}`.
2. `CascadeProvider.to_observation` translates it to a canonical `Bet`: `cascade:77003`, `won`, 2500 cents at `+150`, paying 6250, with its leg on `nba-2026-11-01-phx-at-lal`. The provider's timestamp comes along, plus any optional fields that were absent.
3. `decide()` compares it with what's stored. It's newer, so it's an **update**. A redelivered copy would be a **duplicate**, and an older version arriving late would be **stale**.
4. In one SQLite transaction: the bet row is updated (version 2), the observation is appended to its history with that outcome, and the cursor moves to 213. If the process dies first, the page is simply fetched again.
5. `GET /bets/cascade:77003` returns the canonical bet, Cascade's freshness (`ok`, last synced 3s ago), and the history: `inserted` (open), then `updated` (won).

### Storage (`betsync/store.py`)

SQLite in WAL mode. The tables:
- **`bets`:** canonical state.
- **`events` and `event_aliases`:** canonical games, and each book's IDs for them.
- **`observations`:** every record received and what was done with it. This history is what makes a bet's current state explainable.
- **`rejected`:** records that couldn't be translated.
- **`provider_state`:** cursor and health per book.
- **`sync_runs`:** per-cycle counts.

## Failure, duplicates, ordering and freshness

**Retries.** A failed request is tried up to 4 times, with exponential backoff and full jitter between tries (a random wait of up to 0.5s, then 1s, then 2s, never more than 8s). A 429 waits for `Retry-After`, capped at 30s. A `PermanentError` (another 4xx, or a response we can't parse) isn't retried. Requests time out after 3s, so a hung book costs one timeout, not a stuck worker.

**Isolation.** Each book has its own worker thread, cursor and health. One book being slow or down never delays or changes the other.

**Partial success.** Each page is applied and the cursor advanced together, atomically. If page 3 fails, pages 1 and 2 are kept and the next cycle resumes at page 3. A record that can't be translated is set aside in `rejected`, and the rest of the page still syncs.

**Recovery.** After a failed cycle, the worker waits longer each time before the next one: the 5s poll interval doubles per consecutive failure, up to 60s. That stops a down book from being hammered. One success resets it.

**Duplicates.** Writes are idempotent. Content identical to what's stored is a no-op, apart from noting that the book confirmed it again. Duplicates come from three places:
- Cascade's at-least-once redelivery.
- Northstar repeating records across pages.
- Our own overlap re-read. After each Northstar sweep, the next starts two minutes before the newest change seen, because paging a list that changes underneath you can skip records that share a timestamp.

Replaying either feed from the start changes nothing; a test checks this.

**Ordering and conflicts.** The newest provider timestamp wins. An older version that arrives late, like Northstar's lagging replica serving a settled bet as still open, is recorded in the history as `stale` and never applied, so a settled bet can't be reopened. A different record at the *same* timestamp is treated as a correction and applied. Timestamps are only compared within one book, since each bet has exactly one source.

**Missing fields.** If an update leaves out an optional field (odds, payout or settlement time), the value we already hold is kept, and the bet notes which fields were carried over. If we never had the value, such as a ticket first seen already settled but without its payout, the bet is marked with `unknown_fields`. The worker then re-fetches that ticket on later cycles until the book sends the full record. Northstar has no single-record endpoint, so for Northstar the gap would stay visible rather than being guessed at.

**Freshness.** Each book has a status:
- **`ok`:** synced recently.
- **`degraded`:** recent attempts are failing, but our data is still inside the freshness window (30s by default).
- **`stale`:** no successful sync within the window.
- **`never_synced`:** no successful sync yet.

`/status` reports each book's status and the worst of them overall, along with last success, last error, consecutive failures, the next attempt and recent run counts. Every bet carries its own book's freshness, so a client can tell "this bet is open" from "this bet was open the last time we reached that book". Stale data is still served, because the best local answer beats no answer, but it's clearly marked.

Worker logs show one line per cycle, like `cascade: ok pages=1 received=12 inserted=0 updated=3 duplicate=9 stale=0 rejected=0 retries=1 repaired=0`.

## At much higher scale, I'd change first

1. **Work distribution.** Thousands of users with accounts at many books means one worker per (book, account), with a lease so only one process syncs it at a time. A queue or a Postgres `SKIP LOCKED` table would replace the in-process threads, and rate limits would be shared per book across workers.
2. **Storage.** Postgres instead of SQLite, with `(sportsbook, external_id)` as the idempotency key. The observations history would be partitioned by time with a retention window, or moved to cheap storage, since it grows with every poll.
3. **Push over poll.** Use webhooks or streams where books offer them, and keep polling as a reconciliation sweep that catches anything missed.
4. **Event identity.** Matching games by league, date and team names is fine for two books. At scale it needs a maintained mapping service, with manual review for anything unmatched.
5. **Observability.** Prometheus-style metrics and alerts (sync lag, error rate, stale books, rejected records) instead of a status endpoint you have to go and read.

## Layout

```
betsync/
  domain.py          canonical model, team/event identity, the conflict rule
  providers/         the adapter contract, the HTTP client, one adapter per book
  sync.py            per-book workers: retries, backoff, cursors, repair
  store.py           SQLite: canonical state, history, provider state
  service.py         what the API returns, and the freshness rules
  api.py             FastAPI routes
  simulator/         the two simulated books, their seeded world and their faults
tests/               domain, adapters, sync (in-process), real-HTTP end to end, API
```

## AI & Tools

> **TODO (Ryan): write this section yourself.** Pikkit asks which tools you used and for one place where you *materially changed, rejected or reworked* their output. It should be true and in your own words. Delete this note when done.
