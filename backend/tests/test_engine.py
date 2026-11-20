from pathlib import Path

import pytest
import yaml

from app.core.analyze import analyze, estimate_load
from app.core.engine import Simulation, run
from app.core.model import Architecture, HealthCheck, load_architecture
from app.core.scenario import Scenario

ROOT = Path(__file__).resolve().parents[1]


def arch(**overrides) -> Architecture:
    """Small LB -> api -> (cache) -> db architecture."""
    base = {
        "name": "t",
        "slo": {"error_rate_max": 0.01, "p99_ms_max": 2000, "window_s": 30},
        "traffic": {"sources": [{"name": "users", "target": "lb", "rps": 50}]},
        "nodes": [
            {"id": "lb", "type": "load_balancer", "replicas": 2, "concurrency_per_replica": 1000,
             "latency_ms": {"p50": 1, "p99": 2}},
            {"id": "api", "replicas": 2, "concurrency_per_replica": 20, "queue_per_replica": 50,
             "latency_ms": {"p50": 10, "p99": 30}},
            {"id": "db", "type": "datastore", "replicas": 1, "concurrency_per_replica": 20,
             "queue_per_replica": 100, "latency_ms": {"p50": 20, "p99": 60}},
        ],
        "edges": [
            {"from": "lb", "to": "api", "timeout_ms": 10000},
            {"from": "api", "to": "db", "timeout_ms": 2000},
        ],
    }
    for k, v in overrides.items():
        base[k] = v
    return Architecture.model_validate(base)


def scenario(**kw) -> Scenario:
    return Scenario.model_validate({"name": "s", "duration_s": 300, "warmup_s": 30, **kw})


def node_requests(sim: Simulation, node_id: str, t_from: int = 0) -> int:
    return sum(s.rps for s in sim.col.node_series[node_id] if s.t >= t_from)


# --------------------------------------------------------------------------- engine


def test_low_load_is_healthy():
    s = run(arch(), scenario())
    assert s.status == "ok"
    assert s.error_rate == 0
    # lb 1 + api 10 + db 20 ms medians -> end to end p50 around 30-40 ms
    assert 20 < s.p50_ms < 60


def test_over_capacity_rejects():
    a = arch()
    a.traffic.sources[0].rps = 3000  # api has 40 slots x ~30ms = ~1300 rps max
    s = run(a, scenario())
    assert s.error_rate > 0.2
    assert any("queue_full" in r for r in s.client_errors)
    assert s.status == "no_recovery"


def test_same_seed_is_deterministic():
    a, sc = arch(), scenario(seed=7)
    x, y = run(a, sc), run(a, sc)
    assert (x.requests, x.error_rate, x.p99_ms) == (y.requests, y.error_rate, y.p99_ms)


def test_killing_spof_fails_requests_then_recovers():
    sc = scenario(duration_s=400, events=[
        {"at_s": 100, "action": "kill", "target": "db"},
        {"at_s": 130, "action": "restore", "target": "db"},
    ])
    s = run(arch(), sc)
    assert any("db:down" in r for r in s.client_errors)
    assert s.status == "degraded"
    assert s.recovery_s is not None


def test_retries_multiply_load_on_failing_dependency():
    sc = scenario(events=[{"at_s": 100, "action": "kill", "target": "db"}])

    def db_hits(attempts: int) -> int:
        a = arch()
        a.edges[1].retry.attempts = attempts
        sim = Simulation(a, sc)
        sim.run()
        return node_requests(sim, "db", 110)

    none, three = db_hits(0), db_hits(3)
    assert three > 3.5 * none


def test_blackhole_hangs_until_timeout():
    sc = scenario(events=[{"at_s": 100, "action": "kill", "target": "db", "mode": "blackhole"}])
    s = run(arch(), sc)
    assert any("api->db:timeout" in r for r in s.client_errors)


def test_startup_cycle_never_recovers_and_breaking_it_does():
    def build(cycle: bool) -> Architecture:
        a = arch()
        a.node("api").startup.requires = ["db"]
        a.node("db").startup.requires = ["api"] if cycle else []
        return a

    sc = scenario(duration_s=400, events=[
        {"at_s": 100, "action": "kill", "target": "api"},
        {"at_s": 100, "action": "kill", "target": "db"},
        {"at_s": 110, "action": "restore", "target": "api"},
        {"at_s": 110, "action": "restore", "target": "db"},
    ])
    stuck = run(build(cycle=True), sc)
    assert stuck.status == "no_recovery"
    assert any(c["what"] == "boot_blocked" for c in stuck.cascade)
    assert run(build(cycle=False), sc).status == "degraded"


def test_cache_restart_sends_misses_to_db():
    a = arch(
        nodes=[*arch().model_dump(by_alias=True)["nodes"],
               {"id": "cache", "type": "cache", "replicas": 1, "concurrency_per_replica": 1000,
                "latency_ms": {"p50": 1, "p99": 2}, "warm_hit_rate": 0.9, "warmup_s": 600}],
        edges=[{"from": "lb", "to": "api"}, {"from": "api", "to": "db", "via_cache": "cache"}],
    )
    sim = Simulation(a, scenario(events=[{"at_s": 150, "action": "restart", "target": "cache"}]))
    sim.run()
    before = sum(s.rps for s in sim.col.node_series["db"] if 100 <= s.t < 150)
    after = sum(s.rps for s in sim.col.node_series["db"] if 160 <= s.t < 210)
    assert after > 4 * before


def test_health_check_restarts_overloaded_replicas():
    a = arch()
    a.node("api").health_check = HealthCheck(interval_s=5, fail_threshold=0.3, unhealthy_after=2)
    sc = scenario(traffic={"ramp": [{"at_s": 100, "multiplier": 60}]})
    s = run(a, sc)
    assert any(c["what"] == "unhealthy" and c["node"] == "api" for c in s.cascade)


def test_source_routing_on_shared_lb():
    a = arch()
    data = a.model_dump(by_alias=True)
    data["traffic"]["sources"].append({"name": "other", "target": "lb", "rps": 20})
    data["nodes"].append({"id": "other-api", "replicas": 1, "concurrency_per_replica": 20})
    data["edges"][0]["sources"] = ["users"]
    data["edges"].append({"from": "lb", "to": "other-api", "sources": ["other"]})
    a = Architecture.model_validate(data)
    sim = Simulation(a, scenario())
    sim.run()
    duration = 300
    assert node_requests(sim, "other-api") == pytest.approx(20 * duration, rel=0.1)
    assert node_requests(sim, "api") == pytest.approx(50 * duration, rel=0.1)


# --------------------------------------------------------------------------- model / composition


def write(tmp_path: Path, name: str, data: dict) -> Path:
    p = tmp_path / name
    p.write_text(yaml.safe_dump(data))
    return p


def test_composition_prefix_unify_remove_override(tmp_path):
    base = arch().model_dump(by_alias=True)
    write(tmp_path, "a.yaml", base)
    write(tmp_path, "b.yaml", base | {"name": "b"})
    merged = write(tmp_path, "m.yaml", {
        "name": "m",
        "include": [{"file": "a.yaml"}, {"file": "b.yaml", "prefix": "b_"}],
        "unify": {"b_lb": "lb", "b_db": "db"},
        "override": {"nodes": {"db": {"replicas": 3}},
                     "edges": [{"from": "lb", "to": "b_api", "set": {"weight": 2}}]},
        "remove": {"sources": ["b_users"]},
    })
    m = load_architecture(merged)
    ids = {n.id for n in m.nodes}
    assert ids == {"lb", "api", "db", "b_api"}
    assert m.node("db").replicas == 3
    assert {e.key for e in m.edges} == {("lb", "api"), ("api", "db"), ("lb", "b_api"), ("b_api", "db")}
    assert [e.weight for e in m.edges if e.key == ("lb", "b_api")] == [2]
    assert [s.name for s in m.traffic.sources] == ["users"]


def test_unknown_references_are_rejected():
    with pytest.raises(ValueError, match="unknown node 'nope'"):
        arch(edges=[{"from": "lb", "to": "nope"}])


def test_json_files_load(tmp_path):
    import json
    p = tmp_path / "a.json"
    p.write_text(json.dumps(arch().model_dump(by_alias=True)))
    assert load_architecture(p).name == "t"


def test_shipped_models_validate():
    for p in (ROOT / "models").glob("*.yaml"):
        load_architecture(p)


# --------------------------------------------------------------------------- analysis


def test_analyzer_flags_known_problems():
    a = arch()
    a.edges[1].retry.attempts = 3
    a.node("api").startup.requires = ["db"]
    a.node("db").startup.requires = ["api"]
    rules = {f.rule for f in analyze(a)}
    assert {"retry_no_backoff", "startup_cycle", "spof"} <= rules


def test_load_estimate_matches_simulation():
    a = arch()
    est = estimate_load(a)
    sim = Simulation(a, scenario())
    sim.run()
    measured = node_requests(sim, "db", 30) / (300 - 30 - 2)
    assert measured == pytest.approx(est.rps["db"], rel=0.1)


# --------------------------------------------------------------------------- shards and read replicas


def sharded(weights=(1, 1, 1, 1)) -> Architecture:
    """api -> shard router -> N single-replica shards."""
    data = arch().model_dump(by_alias=True)
    data["nodes"] = [n for n in data["nodes"] if n["id"] != "db"]
    data["nodes"].append({"id": "router", "type": "shard_router", "replicas": 2,
                          "concurrency_per_replica": 1000, "latency_ms": {"p50": 1, "p99": 2}})
    data["edges"] = [e for e in data["edges"] if e["to"] != "db"]
    data["edges"].append({"from": "api", "to": "router", "timeout_ms": 2000})
    for i, w in enumerate(weights):
        data["nodes"].append({"id": f"shard-{i}", "type": "datastore", "replicas": 1,
                              "concurrency_per_replica": 20, "latency_ms": {"p50": 20, "p99": 60}})
        data["edges"].append({"from": "router", "to": f"shard-{i}", "weight": w, "timeout_ms": 1000})
    return Architecture.model_validate(data)


def test_down_shard_fails_only_its_keys():
    sc = scenario(duration_s=300, events=[{"at_s": 100, "action": "kill", "target": "shard-0"}])
    sim = Simulation(sharded(), sc)
    sim.run()
    during = [s for s in sim.col.client_series if 120 <= s.t < 300]
    err = sum(s.rps * s.err_rate for s in during) / sum(s.rps for s in during)
    assert 0.18 < err < 0.32  # ~25%: one of four shards, no failover to the others
    assert sim.col.node_errors["shard-0"]
    assert all(not sim.col.node_errors[f"shard-{i}"] for i in (1, 2, 3))


def test_shard_retries_stay_on_the_same_shard():
    a = sharded()
    a.edges[1].retry.attempts = 3  # api -> router
    sc = scenario(duration_s=300, events=[{"at_s": 100, "action": "kill", "target": "shard-0"}])
    sim = Simulation(a, sc)
    sim.run()
    during = [x for x in sim.col.client_series if 120 <= x.t < 300]
    err = sum(x.rps * x.err_rate for x in during) / sum(x.rps for x in during)
    assert 0.18 < err < 0.32  # retrying doesn't hop to a healthy shard


def test_hot_shard_saturates_first_and_is_flagged():
    a = sharded(weights=(7, 1, 1, 1))  # shard-0 owns 70% of keys
    a.traffic.sources[0].rps = 1000   # 700 rps on a shard with 20 slots x ~22ms ~= 900 rps max
    findings = analyze(a)
    assert any(f.rule == "shard_hotspot" and f.target == "shard-0" for f in findings)
    assert any(f.rule == "spof" and f.target == "shard-1" for f in findings)  # every shard is critical
    load = estimate_load(a)
    assert load.util["shard-0"] > 5 * load.util["shard-1"]


def with_replicas(write_ratio=0.2, fallback=True) -> Architecture:
    data = arch().model_dump(by_alias=True)
    db = next(n for n in data["nodes"] if n["id"] == "db")
    db["read_replicas"] = ["db-r1", "db-r2"]
    db["read_fallback"] = fallback
    for r in ("db-r1", "db-r2"):
        data["nodes"].append({**db, "id": r, "read_replicas": [], "read_fallback": True})
    next(e for e in data["edges"] if e["to"] == "db")["write_ratio"] = write_ratio
    return Architecture.model_validate(data)


def test_reads_go_to_replicas_writes_to_primary():
    sim = Simulation(with_replicas(write_ratio=0.2), scenario())
    sim.run()
    primary, r1, r2 = (node_requests(sim, n, 30) for n in ("db", "db-r1", "db-r2"))
    total = primary + r1 + r2
    assert 0.15 < primary / total < 0.25
    assert abs(r1 - r2) / total < 0.1


def test_replica_loss_falls_back_to_primary_or_fails_reads():
    sc = scenario(events=[{"at_s": 100, "action": "kill", "target": "db-r1"},
                          {"at_s": 100, "action": "kill", "target": "db-r2"}])
    fallback = Simulation(with_replicas(fallback=True), sc)
    fallback.run()
    assert fallback.col.client_errors.total() == 0
    assert node_requests(fallback, "db", 110) > 0.9 * sum(x.rps for x in fallback.col.client_series if x.t >= 110)

    strict = Simulation(with_replicas(fallback=False), sc)
    strict.run()
    assert strict.col.client_errors["db:no_read_replica"] > 0
    during = [x for x in strict.col.client_series if 110 <= x.t < 300]
    err = sum(x.rps * x.err_rate for x in during) / sum(x.rps for x in during)
    assert 0.7 < err < 0.9  # ~80% of calls are reads, all of them fail


def test_replica_refs_are_validated():
    data = with_replicas().model_dump(by_alias=True)
    data["nodes"][2]["read_replicas"] = ["nope"]
    with pytest.raises(ValueError, match="read_replicas references unknown node 'nope'"):
        Architecture.model_validate(data)


def test_unused_replicas_are_flagged():
    a = with_replicas()
    a.edges[1].write_ratio = None
    assert any(f.rule == "replicas_unused" and f.target == "db" for f in analyze(a))
