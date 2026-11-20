# ArchSim: How It's Built

A guide to the ArchSim codebase: what each part does, and how one simulated request moves through it.

## 1. The big picture

ArchSim is a Python command-line tool (about 1,900 lines). There's no UI and no AI in it yet. You give it two kinds of YAML file, and it gives you Markdown reports:

```
models/*.yaml      ──┐
(your architecture)  │      ┌──────────────┐      reports/*.md
                     ├────► │   archsim    │ ───► (findings, run results,
scenarios/*.yaml   ──┘      │   (CLI)      │       comparisons)
(what goes wrong)           └──────────────┘
```

- **Model** = *what the system is*: its services, their capacity, and who calls whom.
- **Scenario** = *what happens to it*: the traffic shape over time, plus failures at set times ("kill Redis at 700s").

## 2. The files, and what each one does

| File | Role | In plain words |
|---|---|---|
| `archsim/model.py` | Data definitions | The schema for the architecture YAML (Node, Edge, Retry, HealthCheck, SLO…). Uses Pydantic, so a typo in the YAML fails loudly. It can also build one model from others (see the merged models below). |
| `archsim/scenario.py` | Data definitions | The schema for the scenario YAML: duration, warm-up, traffic ramps and timed events. |
| `archsim/analyze.py` | **Static checks** (no simulation) | Reads the graph and flags problems: single points of failure, startup cycles, retries with no backoff, retries that multiply across layers, and timeouts too long for the caller. |
| `archsim/engine.py` | **The simulator** (the heart) | Runs simulated time and pushes fake requests through the graph. |
| `archsim/metrics.py` | Measurement | Collects per-second success, errors and latency for each node, then summarises them: did it meet the SLO, and how long did recovery take? |
| `archsim/compare.py` | A vs B | Runs the same scenarios against two models and compares the results. |
| `archsim/report.py` | Output | Turns results into Markdown. |
| `archsim/cli.py` | Entry point | The commands you type (Typer). |
| `tests/test_archsim.py` | Tests | 16 tests, all passing. |

## 3. The commands

```bash
archsim validate models/nic_current.yaml          # does the YAML parse?
archsim analyze  models/nic_current.yaml          # static checks only
archsim run      models/nic_current.yaml scenarios/03_redis_restart_at_peak.yaml
archsim suite    models/nic_current.yaml          # every scenario in scenarios/
archsim compare  models/nic_current.yaml models/nic_merged.yaml
```

The four files in `reports/` are outputs from these commands, run on 2026-11-17.

## 4. What a model looks like

From `models/nic_current.yaml`:

```yaml
nodes:
  - id: snowflake-service
    replicas: 3
    concurrency_per_replica: 20   # 20 requests at once per replica
    queue_per_replica: 100        # then up to 100 wait; beyond that, rejected
    latency_ms: {p50: 20, p99: 120}
    startup: {time_s: 25, requires: [redis]}   # can't come up without Redis
    health_check: {interval_s: 10, fail_threshold: 0.5, unhealthy_after: 3}
edges:
  - from: snowflake-service
    to: snowflake-wh
    timeout_ms: ...
    retry: {attempts: 3, backoff: none}
    pool: 40                      # connection pool on this call
```

Note the `# ASSUMPTION` comments throughout the model files: the numbers are placeholders, not measured from production. The simulator is only as accurate as these numbers.

**Models:** `nic_current`, `portal_b`, `nic_merged` (built from the other two) and `nic_plus_reporting`.

**Scenarios:**

1. Baseline
2. 2.5x peak traffic
3. Redis restart at peak
4. Warehouse 5x slower
5. Auth0 outage
6. Losing a load balancer replica
7. Full cold restart

An example scenario (`scenarios/03_redis_restart_at_peak.yaml`):

```yaml
name: redis_restart_at_peak
description: Redis dies for a minute during 2x traffic and comes back empty (cold cache).
duration_s: 1800
warmup_s: 300
traffic:
  ramp:
    - {at_s: 500, multiplier: 2.0, ramp_s: 60}
events:
  - {at_s: 700, action: kill, target: redis}
  - {at_s: 760, action: restore, target: redis}
```

## 5. How the simulator works (`engine.py`)

It uses **SimPy**, a discrete-event simulation library. Nothing runs in real time: the simulation clock jumps from event to event, so 30 simulated minutes take seconds. Every request is a small Python generator that `yield`s whenever it waits, whether that's for processing time, a timeout or a free slot.

### When a run starts (`run()`)

- A traffic generator starts for each traffic source.
- Each scenario event is scheduled.
- A health-check loop starts for each node that has one.
- A ticker takes a snapshot every simulated second.

### One request's journey

1. **`_source`** sends requests at random intervals (Poisson arrivals) at `rps ×` the scenario's current multiplier. 2x traffic just means a higher rate.
2. **`_client`** plays the user. It starts the request and its own timer (e.g. 30s). Whichever finishes first decides between success and a `client_timeout`.
3. **`_handle(node)`** runs at each service:
   - **Node down?** It fails fast, or hangs forever if it was killed in `blackhole` mode.
   - **Rate limited?** It returns a 429 (token bucket, used for external APIs).
   - **Free slot?** Each node has a **`Pool`**: `replicas × concurrency` slots plus a bounded queue. If the queue is full, the request is rejected and the node is logged as "saturated". This is how overload works.
   - **Processing time** is drawn at random from a distribution fitted to your p50 and p99, times the slow factor if a "slow" event is active.
   - Then it **calls each downstream edge**. Optional edges (`required: false`) can fail without failing the request, and `p: 0.2` means only 20% of requests make that call.
4. **`_call(edge)`** is where most of the realism lives:
   - It checks the cache first if the edge goes through one. Hit rates depend on how warm the cache is, so after a restart **the cache is cold**, every request falls through to the database, and you get a stampede.
   - It takes a connection from the edge's pool, which can time out waiting.
   - It calls the downstream node and races it against the timeout.
   - **If the caller times out, the downstream service keeps working anyway.** The connection stays busy until it actually finishes. That's how timeouts plus retries pile up load in real systems.
   - On failure it **retries** with no wait, a fixed wait or exponential backoff (with or without jitter). These retries are what create a retry storm.
5. **Load balancers (`_route`)** only send traffic to backends that have healthy replicas.

### Running in the background

- **`_health_check`**: every interval, if a node's error rate is above the threshold N times in a row, one replica is pulled and restarted. This is how a **cascade** happens: the restart takes away capacity, so the node gets more overloaded, so more replicas get restarted.
- **`_boot`**: a restarting replica waits `startup.time_s`, then **waits until everything in `requires` is up**. If A needs B and B needs A, neither comes back. That's a **restart deadlock**, which shows up in the logs as a `boot_blocked` event.
- **`_event`**: applies the scenario's injected failures: kill, restore, restart, slow and rate-limit.
- **`_ticker`**: records each node's state every simulated second (replicas up, slots in use, queue length).

### Afterwards

**`metrics.summarize`** turns the per-second numbers into answers: did each second meet the SLO (error rate and p99), how long did it take to recover after the last failure, and which nodes caused the errors?

## 6. Concepts worth knowing

- **Seeded randomness** (`random.Random(scenario.seed)`): the same seed gives the same result every time. This is why a demo can be repeated exactly.
- **Each part maps to a real failure pattern:**

  | Code | Real-world failure |
  |---|---|
  | Pool + queue | Overload |
  | Timeout + downstream keeps working | Wasted work |
  | Retry | Retry storm |
  | Cache warm-up | Cold-start stampede |
  | Health check | Cascading restarts |
  | `requires` | Boot deadlock |
- **What it doesn't model:** money, idempotency, duplicate effects, primary failover, replication lag, hot-row lock contention, autoscaling and queue/async patterns such as Pub/Sub. All calls are synchronous. These are the gaps ArchLens would need filled.

## 6a. Databases: shards and read replicas

Added 2026-11-19.

**Shard router.** A node with `type: shard_router`. Each out-edge is a shard, and its `weight` is that shard's share of the key space.

```yaml
nodes:
  - {id: ledger-router, type: shard_router, replicas: 2, concurrency_per_replica: 1000}
  - {id: ledger-0, type: datastore, replicas: 1, concurrency_per_replica: 20}
  - {id: ledger-1, type: datastore, replicas: 1, concurrency_per_replica: 20}
edges:
  - {from: payments, to: ledger-router}
  - {from: ledger-router, to: ledger-0, weight: 3}   # hot shard: 75% of keys
  - {from: ledger-router, to: ledger-1, weight: 1}
```

How it behaves:
- Every request gets a fixed random **key** when it starts (think customer ID). The router sends it to the shard that owns that key.
- **No failover.** A down shard fails exactly its share of requests, and retries go back to the same shard. This is the difference from a `load_balancer`, which skips dead backends.
- The keys come from a separate random stream, so models without shards give exactly the same results as before.

**Read replicas.** List them on the primary, and set `write_ratio` on the calling edge:

```yaml
nodes:
  - {id: db, type: datastore, read_replicas: [db-r1, db-r2], read_fallback: true}
  - {id: db-r1, type: datastore}
  - {id: db-r2, type: datastore}
edges:
  - {from: api, to: db, write_ratio: 0.2}   # 20% writes -> db, 80% reads -> a healthy replica
```

How it behaves:
- Each call is decided once as a read or a write.
- Reads go to a random healthy replica, and each retry can pick a different one.
- If every replica is down, reads go to the primary when `read_fallback: true`. Otherwise they fail with `db:no_read_replica`.

**New analyzer rules:**
- `shard_hotspot`: a shard owns more than 1.5x its fair share of keys.
- `replicas_unused`: the database has replicas, but no caller sets `write_ratio`.

Also, every single-replica shard is now reported as a single point of failure, and the load estimate splits read traffic across the replicas.

**Not done yet:**
- primary failover (a replica taking over)
- replication lag (stale reads)
- lock contention on hot rows

Tests: `tests/test_archsim.py`, section "shards and read replicas" (7 tests).

## 7. Where ArchLens fits on top

```
Gemini text-to-model          ──► writes models/*.yaml
AI Adversary                  ──► writes scenarios/*.yaml
engine.run()                  ──► unchanged, still decides the truth
Gemini narrative/fixes/cost   ──► reads the RunSummary
```

The AI only ever writes YAML and reads results. The simulator in the middle is what makes the output believable.

## 8. Getting familiar with it

A good way to get familiar with it:

1. Run `archsim analyze models/nic_current.yaml`.
2. Then run `archsim run models/nic_current.yaml scenarios/03_redis_restart_at_peak.yaml`.
3. Read the two reports next to the model YAML.
