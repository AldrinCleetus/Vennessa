# ArchSim — AI Architecture Designer, Simulator & Breaker

> Working name. Side project. Started 2026-11-17.

## 1. Problem

Architecture changes (like merging the NIC portal with a second portal) are designed on whiteboards and validated in production. Failure modes such as cascading failures, retry storms and restart deadlocks only show up once real traffic hits them.

A whole class of outages looks like this: a core component (for example the load balancer) fails, services try to recover, the recovery overloads their dependencies, and the system can't come back up on its own.

**Goal:** design an architecture with AI, then **simulate** it under load and failure, and have AI **actively try to break it** before it's built or changed.

## 2. Core capabilities

| # | Capability | What it does |
|---|---|---|
| 1 | **Design** | Describe the system in plain English, or import from code/config. The AI produces a structured architecture model. |
| 2 | **Simulate** | Run discrete-event simulations of traffic flowing through the model. |
| 3 | **What-if** | User-defined scenarios: "kill Redis", "3x traffic", "Auth0 at 5s latency". |
| 4 | **Adversary (AI Breaker)** | Given the architecture, the AI **builds scenarios designed to make it fail**, runs them, and keeps the ones that do. |
| 5 | **Report** | SPOFs, cascade paths, bottlenecks, restart deadlocks, plus suggested fixes. |
| 6 | **Compare** | Diff two architectures (e.g. NIC today vs. merged portal) and test both against the same scenarios. |

## 3. High-level architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                              UI                                 │
│   Graph editor  │  Scenario builder  │  Live sim view  │ Reports│
└───────┬─────────────────┬───────────────────┬───────────────────┘
        │                 │                   │
┌───────▼───────┐ ┌───────▼────────┐ ┌────────▼────────┐
│  Designer AI  │ │  Adversary AI  │ │   Analyst AI    │
│ text/code →   │ │ arch → failing │ │ results →       │
│ model         │ │ scenarios      │ │ findings+fixes  │
└───────┬───────┘ └───────┬────────┘ └────────▲────────┘
        │                 │                   │
┌───────▼─────────────────▼───────────────────┴───────────────────┐
│                     Architecture Model (YAML/JSON)              │
│   nodes · edges · capacity · latency · retries · startup deps   │
└───────────────────────────────┬─────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────┐
│                     Simulation Engine (discrete-event)          │
│  traffic gen · queues · timeouts · retries · health checks ·    │
│  failure injection · restart/boot sequencing · autoscaling      │
└───────────────────────────────┬─────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────┐
│               Results Store (time series per node/edge)         │
└─────────────────────────────────────────────────────────────────┘
        ▲
        │ (later) calibration
┌───────┴─────────────────────────────────────────────────────────┐
│  Real metrics: Grafana/Loki logs, Snowflake QUERY_HISTORY, APM  │
└─────────────────────────────────────────────────────────────────┘
```

## 4. The architecture model

This is the single source of truth. Everything reads and writes it.

```yaml
name: nic-portal-current
nodes:
  - id: lb
    type: load_balancer
    replicas: 2
    capacity_rps: 5000
    health_check: { interval_s: 10, unhealthy_after: 3 }

  - id: snowflake-service
    type: service
    replicas: 3
    capacity_rps_per_replica: 200
    latency_ms: { p50: 120, p99: 900 }
    startup:
      time_s: 25
      requires: [redis, auth0]          # can't boot without these
    pools: { db_connections: 20 }

  - id: redis
    type: cache
    capacity_rps: 20000
    cold_start_hit_rate: 0.0
    warm_hit_rate: 0.85

  - id: snowflake-wh
    type: datastore
    concurrency: 8
    queue_max: 100
    latency_ms: { p50: 800, p99: 6000 }

  - id: auth0
    type: external
    latency_ms: { p50: 150, p99: 700 }
    rate_limit_rps: 100

edges:
  - from: lb
    to: snowflake-service
  - from: snowflake-service
    to: redis
    timeout_ms: 200
    on_miss: snowflake-wh
  - from: snowflake-service
    to: snowflake-wh
    timeout_ms: 30000
    retry: { attempts: 3, backoff: none }   # deliberately bad
  - from: snowflake-service
    to: auth0
    timeout_ms: 2000
    retry: { attempts: 2, backoff: exponential, jitter: true }

traffic:
  baseline_rps: 300
  profiles:
    - { name: morning_peak, multiplier: 2.5, ramp_s: 600 }
```

Key point: the model captures **behavior**, not just boxes. Retries, timeouts, startup dependencies, pool sizes and cache warmth are what cause real outages.

## 5. Simulation engine

- **Type:** discrete-event simulation (each request is an event moving through the graph).
- **Models:**
  - queues and capacity per node, with drops when a queue is full
  - timeouts, plus retries with or without backoff and jitter (this is how retry storms emerge)
  - connection pools that run out
  - health checks: the LB removes nodes and sends their traffic to the rest (this is how cascades emerge)
  - **boot sequencing:** a node only becomes healthy once everything it `requires` is healthy (this is how restart deadlocks emerge)
  - cache warmth over time (this is how cold-start stampedes emerge)
  - autoscaling with delay
- **Output:** per-tick metrics per node and edge: RPS, latency, error rate, queue depth, health state.
- **Determinism:** seeded RNG so any failing scenario can be replayed exactly.

## 6. Adversary AI ("make it fail")

This is the differentiator. Instead of waiting for the user to imagine a failure, the AI goes looking for one.

### Loop

```
1. Analyze model  → find suspicious structure
                    (SPOFs, cycles in startup deps, retries without
                     backoff, pools smaller than fan-out, rate-limited
                     externals on the hot path)
2. Hypothesize    → "If Redis restarts during peak, cold cache sends
                     100% of reads to the warehouse (concurrency 8),
                     timeouts trigger 3x retries without backoff →
                     warehouse saturation → service pool exhaustion
                     → LB marks all replicas unhealthy."
3. Generate       → concrete scenario (timeline of injected events + traffic)
4. Run            → simulate
5. Score          → did it fail? how badly? (SLO breach, total outage,
                    failure to recover)
6. Refine         → mutate scenario: smallest trigger, earliest time,
                    lowest load that still breaks it
7. Keep           → save minimal reproducible failing scenarios
```

Step 6 matters. The output isn't just "it breaks at 10x load" but **"it breaks at 1.4x load if Redis restarts — here's the minimal repro."**

### Failure patterns it hunts for

| Pattern | Signal in the model |
|---|---|
| Single point of failure | node with `replicas: 1` on a critical path |
| Retry storm | retries without backoff/jitter, multiplied across layers |
| Cascading overload | LB fails over load onto replicas without headroom |
| Restart deadlock | cycle in `startup.requires`, or a dependency on a down component |
| Thundering herd / cold start | cache dependency with low `cold_start_hit_rate` |
| Pool exhaustion | slow downstream + small pool + long timeout |
| External rate limit | third party (Auth0, Rev.io) with `rate_limit_rps` on the hot path |
| Timeout inversion | caller timeout > callee timeout chain (or the reverse) |
| Metastable failure | system stays broken after the trigger is removed |

### Scenario format (shared by user and AI)

```yaml
scenario: redis-restart-at-peak
generated_by: adversary
hypothesis: cold cache + no-backoff retries saturate warehouse
seed: 4821
traffic: morning_peak
events:
  - at_s: 900
    action: kill
    target: redis
  - at_s: 960
    action: restore
    target: redis
expect:
  slo: { error_rate_max: 0.01, p99_ms_max: 3000 }
  must_recover_within_s: 300
result:            # filled in after the run
  failed: true
  recovered: false        # metastable
  first_breach_s: 942
  cascade: [redis, snowflake-wh, snowflake-service, lb]
```

## 7. Analyst AI (report)

Turns raw results into:
- **Findings:** ranked by severity, each linked to its failing scenario.
- **Cascade timeline:** what failed, in what order, and why.
- **Fixes:** concrete changes (add exponential backoff + jitter, circuit breaker on `snowflake-wh`, warm the cache before rejoining the LB, break the startup cycle, raise the pool size).
- **Verify fix:** apply the change to a copy of the model, re-run the same failing scenario, and confirm it now passes.

## 8. Design AI (input)

Sources it can build the model from:
- plain-English description
- repo scan (service names, env vars pointing at Redis/Snowflake/Auth0, HTTP clients and their retry configs)
- Bruno collections (endpoints → edges)
- docker-compose / k8s manifests
- later: real metrics, to calibrate capacity and latency

The AI proposes the model and the **user confirms**. Unknown numbers are flagged as assumptions, not invented silently.

## 9. First use case: the portal merge

1. Model **NIC portal (today)**: LB, Snowflake Service, User Management, jobber, Redis, Auth0, Snowflake warehouses, Rev.io.
2. Model **Portal B (today)**.
3. Model **merged architecture**: shared auth? shared Redis? shared warehouse?
4. Run the adversary against all three. Things to look for:
   - shared components that become new SPOFs
   - combined traffic exceeding the capacity of a shared dependency (warehouse concurrency, Auth0 rate limits)
   - new startup dependencies between the two portals
   - the migration itself: cutover-day scenarios (dual writes, partial traffic shift)
5. Output: a merge risk report before anything is built.

## 10. Tech stack (proposed)

| Layer | Choice | Why |
|---|---|---|
| Model | YAML + JSON Schema | readable, diffable, AI-friendly |
| Sim engine | Python + SimPy (Rust core later if needed) | fast to iterate; see Performance strategy |
| AI | LLM with tool calls (model read/write, run sim, read results) | the adversary loop needs to call the sim repeatedly |
| API | FastAPI | wraps sim + AI |
| UI | React + React Flow (graph) + charts | graph editing and live sim playback |
| Storage | SQLite → Postgres/Snowflake later | results time series |

### Performance strategy

Speed and accuracy are separate problems:
- **Accuracy** comes from the model (real capacity/latency numbers, correct retry and timeout behavior, realistic traffic), not the language. Calibration (v4) is the main accuracy lever.
- **Speed** matters because of the adversary loop: one search runs hundreds to thousands of sims (generate → run → shrink → rerun, across seeds).

Rough orders of magnitude (estimates, not benchmarks):

| Engine | Events/sec (rough) | 1-hour sim at 1,000 requests/sec, ~5 hops each |
|---|---|---|
| Python + SimPy | ~100k–500k | ~1–3 min per run |
| Rust (custom event loop) | ~10M–50M+ | ~1–2 sec per run |

Plan:
1. **v0–v1 in Python.** Keep the engine behind one clean boundary: `model + scenario → metrics`. No SimPy-specific details outside the engine module.
2. **Measure** adversary-loop wall time once it runs for real.
3. **Cheap wins first:** run independent sims in parallel across CPU cores; offer a coarse mode (request batches per tick instead of individual requests).
4. **If still too slow, rewrite only the engine core in Rust**, exposed to Python via PyO3/maturin. Same interface; the Python engine stays as a reference to cross-check Rust results (same seed → same failures).

Alternative: start with a tiny Rust core (event loop, queues, nodes) from day one and keep everything else in Python. Faster later, slower to iterate early.

## 11. Roadmap

- **v0 — CLI proof of concept**
  - model schema + 4 node types (lb, service, cache, datastore)
  - SimPy engine with queues, timeouts, retries, kill/restore
  - hand-written NIC model
  - reproduce one known bad pattern (retry storm)
- **v1 — Adversary**
  - static analysis of the model for suspicious patterns
  - LLM generates scenarios → runs → refines to a minimal failing scenario
  - markdown report
- **v2 — Design + boot sequencing**
  - startup dependencies + restart deadlock detection
  - AI builds the model from text or a repo scan
  - fix suggestion + verify-fix loop
- **v3 — UI**
  - graph editor, live playback, scenario library
- **v4 — Calibration**
  - pull real latency/capacity from Grafana logs and Snowflake query history

## 12. Open questions

- How accurate does the sim need to be to be useful? (Goal: right *direction* and *order of failure*, not exact numbers.)
- How do we get realistic numbers for Portal B?
- Should the adversary get a "budget" (max events, max load multiplier) so it finds realistic failures, not "10,000x traffic breaks everything"?
- Model the migration process itself as a scenario type?

## 13. Known limitations

- The sim only knows what's in the model. Undocumented dependencies won't be tested; the repo scan and calibration reduce this but won't remove it.
- AI-generated numbers are assumptions until checked against real metrics.
- This doesn't replace chaos testing in real environments. It finds the problems worth chaos-testing.
