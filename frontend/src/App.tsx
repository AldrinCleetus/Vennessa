import React, { useState, useEffect } from 'react';
import { 
  ShieldAlert, Play, Cpu, AlertTriangle, CheckCircle2, 
  RefreshCw, Zap, Server, Activity, Layers, ArrowRight, Clock, Flame
} from 'lucide-react';
import { Finding, SimulationResult, ModelSummary, ScenarioSummary } from './types';

export default function App() {
  const [activeTab, setActiveTab] = useState<'simulate' | 'analyzer' | 'graph'>('simulate');
  const [models, setModels] = useState<ModelSummary[]>([]);
  const [scenarios, setScenarios] = useState<ScenarioSummary[]>([]);
  const [selectedModel, setSelectedModel] = useState<string>('');
  const [selectedScenario, setSelectedScenario] = useState<string>('');

  const [modelDetails, setModelDetails] = useState<any>(null);
  const [findings, setFindings] = useState<Finding[]>([]);
  const [analyzing, setAnalyzing] = useState(false);
  const [simulating, setSimulating] = useState(false);
  const [result, setResult] = useState<SimulationResult | null>(null);

  // Fetch models & scenarios on mount
  useEffect(() => {
    fetch('/api/models')
      .then((r) => r.json())
      .then((d) => {
        if (d.models && d.models.length > 0) {
          setModels(d.models);
          setSelectedModel(d.models[0].id);
        }
      })
      .catch(() => {});

    fetch('/api/scenarios')
      .then((r) => r.json())
      .then((d) => {
        if (d.scenarios && d.scenarios.length > 0) {
          setScenarios(d.scenarios);
          setSelectedScenario(d.scenarios[0].id);
        }
      })
      .catch(() => {});
  }, []);

  // Fetch model architecture details whenever selectedModel changes
  useEffect(() => {
    if (!selectedModel) return;
    fetch(`/api/models/${selectedModel}`)
      .then((r) => r.json())
      .then((d) => setModelDetails(d.data))
      .catch(() => {});
  }, [selectedModel]);

  const runAnalysis = async () => {
    setAnalyzing(true);
    try {
      const res = await fetch('/api/analyze', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ architecture: selectedModel }),
      });
      const data = await res.json();
      setFindings(data.findings || []);
      setActiveTab('analyzer');
    } catch (e) {
      console.error(e);
    } finally {
      setAnalyzing(false);
    }
  };

  const runSimulation = async () => {
    setSimulating(true);
    try {
      const res = await fetch('/api/simulate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          architecture: selectedModel,
          scenario: selectedScenario,
        }),
      });
      const data: SimulationResult = await res.json();
      
      // Compute universal blast radius & cascade metrics
      const affected = new Set<string>();
      if (data.cascade) {
        data.cascade.forEach((c) => {
          if (c.node) affected.add(c.node);
        });
      }
      Object.keys(data.node_errors || {}).forEach((node) => affected.add(node));
      
      data.affected_nodes = Array.from(affected);
      data.cascade_depth = data.cascade ? data.cascade.length : 0;
      
      setResult(data);
      setActiveTab('simulate');
    } catch (e) {
      console.error(e);
    } finally {
      setSimulating(false);
    }
  };

  return (
    <div>
      {/* Top Header */}
      <header className="header">
        <div className="brand">
          <ShieldAlert className="w-5 h-5 text-accent" />
          <span>Project Venessa</span>
          <span className="brand-badge">RESILIENCE PRE-MORTEM</span>
        </div>

        <div className="nav-links">
          <button
            className={`nav-btn ${activeTab === 'simulate' ? 'active' : ''}`}
            onClick={() => setActiveTab('simulate')}
          >
            Pre-Mortem Simulation
          </button>
          <button
            className={`nav-btn ${activeTab === 'graph' ? 'active' : ''}`}
            onClick={() => setActiveTab('graph')}
          >
            Architecture Spec
          </button>
          <button
            className={`nav-btn ${activeTab === 'analyzer' ? 'active' : ''}`}
            onClick={() => setActiveTab('analyzer')}
          >
            Static Audit {findings.length > 0 && `(${findings.length})`}
          </button>
        </div>

        <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
          <select
            value={selectedModel}
            onChange={(e) => setSelectedModel(e.target.value)}
            style={{
              background: 'var(--surface)',
              border: '1px solid var(--line-strong)',
              borderRadius: '6px',
              padding: '6px 10px',
              fontSize: '12px',
            }}
          >
            {models.map((m) => (
              <option key={m.id} value={m.id}>
                System: {m.name || m.id}
              </option>
            ))}
          </select>

          <select
            value={selectedScenario}
            onChange={(e) => setSelectedScenario(e.target.value)}
            style={{
              background: 'var(--surface)',
              border: '1px solid var(--line-strong)',
              borderRadius: '6px',
              padding: '6px 10px',
              fontSize: '12px',
            }}
          >
            {scenarios.map((s) => (
              <option key={s.id} value={s.id}>
                Fault Attack: {s.name || s.id}
              </option>
            ))}
          </select>

          <button className="btn btn-primary" onClick={runSimulation} disabled={simulating}>
            <Play className="w-4 h-4" />
            {simulating ? 'Simulating...' : 'Run Simulation'}
          </button>
        </div>
      </header>

      {/* Main Page Content */}
      <main className="container" style={{ paddingBottom: '60px' }}>
        {/* Banner Bar */}
        <div
          style={{
            marginTop: '20px',
            padding: '16px 20px',
            background: 'var(--surface)',
            border: '1px solid var(--line)',
            borderRadius: '12px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
          }}
        >
          <div>
            <h2 style={{ fontSize: '15px', fontWeight: 600, marginBottom: '4px' }}>
              Design-Time Architecture Resilience & Chaos Simulation
            </h2>
            <p style={{ color: 'var(--muted)', fontSize: '13px' }}>
              Simulates distributed queuing, cache cold-starts, connection pools, and cascading thread starvation under adversarial load.
            </p>
          </div>
          <div style={{ display: 'flex', gap: '10px' }}>
            <button className="btn" onClick={runAnalysis} disabled={analyzing}>
              <Cpu className="w-4 h-4" />
              {analyzing ? 'Checking...' : 'Run Static Audit'}
            </button>
          </div>
        </div>

        {/* Tab 1: Simulation Results View */}
        {activeTab === 'simulate' && (
          <div style={{ marginTop: '20px' }}>
            {result ? (
              <div>
                {/* 3 Core Metric Cards */}
                <div className="grid-3" style={{ marginBottom: '20px' }}>
                  <div className="stat-box card">
                    <div className="stat-label">System Resilience State</div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginTop: '6px' }}>
                      <span
                        className={`badge ${
                          result.status === 'ok'
                            ? 'badge-ok'
                            : result.status === 'degraded'
                            ? 'badge-warn'
                            : 'badge-bad'
                        }`}
                      >
                        {result.status.replace('_', ' ')}
                      </span>
                      <span style={{ fontSize: '13px', color: 'var(--muted)' }}>
                        {result.slo_breached ? 'SLO Violated' : 'SLO Compliant'}
                      </span>
                    </div>
                    <div style={{ fontSize: '12px', color: 'var(--muted)', marginTop: '8px' }}>
                      Recovery: {result.recovery_s !== null ? `${result.recovery_s}s` : 'Failed to recover (deadlocked)'}
                    </div>
                  </div>

                  <div className="stat-box card">
                    <div className="stat-label">Availability & Tail Latency</div>
                    <div className="stat-value" style={{ color: result.error_rate > 0 ? 'var(--bad)' : 'var(--ok)' }}>
                      {((1 - result.error_rate) * 100).toFixed(2)}% Available
                    </div>
                    <div style={{ fontSize: '12px', color: 'var(--muted)', marginTop: '4px' }}>
                      p50: {result.p50_ms.toFixed(0)}ms · p99: {result.p99_ms.toFixed(0)}ms
                    </div>
                    <div style={{ fontSize: '11px', color: 'var(--muted)', marginTop: '2px' }}>
                      Total Requests: {result.requests.toLocaleString()} ({Math.round(result.requests * result.error_rate).toLocaleString()} dropped)
                    </div>
                  </div>

                  <div className="stat-box card" style={{ borderColor: result.affected_nodes && result.affected_nodes.length > 0 ? 'rgba(248, 113, 113, 0.4)' : 'var(--line)' }}>
                    <div className="stat-label" style={{ color: result.affected_nodes && result.affected_nodes.length > 0 ? 'var(--bad)' : 'var(--muted)' }}>
                      Cascade Blast Radius
                    </div>
                    <div className="stat-value" style={{ color: result.affected_nodes && result.affected_nodes.length > 0 ? 'var(--bad)' : 'var(--ok)' }}>
                      {result.affected_nodes?.length || 0} Components Failed
                    </div>
                    <div style={{ fontSize: '12px', color: 'var(--muted)', marginTop: '4px' }}>
                      Cascade Sequence Depth: {result.cascade_depth || 0} events
                    </div>
                    <div style={{ fontSize: '11px', color: 'var(--muted)', marginTop: '2px' }}>
                      Simulation compute time: {result.wall_time_s.toFixed(2)}s
                    </div>
                  </div>
                </div>

                {/* Cascade Sequence & Google Cloud Fix */}
                <div className="grid-2">
                  <div className="card">
                    <div className="card-title">
                      <span style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                        <Flame className="w-4 h-4 text-bad" /> Outage Timeline & Cascade Log
                      </span>
                      <span style={{ fontSize: '12px', color: 'var(--muted)' }}>
                        {result.cascade?.length || 0} Events
                      </span>
                    </div>

                    {result.cascade && result.cascade.length > 0 ? (
                      <div style={{ maxHeight: '340px', overflowY: 'auto' }}>
                        {result.cascade.map((c, i) => (
                          <div
                            key={i}
                            style={{
                              display: 'flex',
                              alignItems: 'center',
                              justifyContent: 'space-between',
                              padding: '8px 12px',
                              background: 'var(--bg)',
                              borderRadius: '6px',
                              marginBottom: '6px',
                              fontFamily: 'var(--font-mono)',
                              fontSize: '12px',
                              borderLeft: '3px solid var(--bad)',
                            }}
                          >
                            <span style={{ color: 'var(--muted)', width: '60px' }}>T+{c.t}s</span>
                            <span style={{ fontWeight: 600, color: 'var(--bad)', flex: 1, paddingLeft: '8px' }}>
                              {c.what}
                            </span>
                            <span style={{ color: 'var(--text)', background: 'var(--surface-hover)', padding: '2px 6px', borderRadius: '4px' }}>
                              {c.node || c.edge || '-'}
                            </span>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <div style={{ padding: '30px 0', textAlign: 'center', color: 'var(--muted)' }}>
                        <CheckCircle2 className="w-8 h-8 text-ok" style={{ margin: '0 auto 8px' }} />
                        <p>No cascade failures triggered. The system remained within operational bounds.</p>
                      </div>
                    )}
                  </div>

                  {/* Google Cloud Remediation Recommendations */}
                  <div className="card">
                    <div className="card-title">
                      <span style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                        <Zap className="w-4 h-4 text-accent" /> Recommended Google Cloud Fixes
                      </span>
                    </div>
                    <p style={{ color: 'var(--muted)', fontSize: '13px', marginBottom: '14px' }}>
                      Architecture resilience patterns mapped to Google Cloud native services:
                    </p>

                    <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
                      <div style={{ padding: '12px', background: 'var(--bg)', borderRadius: '8px', border: '1px solid var(--line)' }}>
                        <div style={{ fontWeight: 600, color: 'var(--accent)', fontSize: '13px' }}>
                          1. Google Cloud Memorystore (Redis Cluster)
                        </div>
                        <p style={{ color: 'var(--muted)', fontSize: '12px', marginTop: '4px', lineHeight: 1.5 }}>
                          Deploy Memorystore with request coalescing and pre-warming to shield the underlying datastore from cold-start stampedes.
                        </p>
                      </div>

                      <div style={{ padding: '12px', background: 'var(--bg)', borderRadius: '8px', border: '1px solid var(--line)' }}>
                        <div style={{ fontWeight: 600, color: 'var(--accent)', fontSize: '13px' }}>
                          2. Cloud Pub/Sub Asynchronous Buffer
                        </div>
                        <p style={{ color: 'var(--muted)', fontSize: '12px', marginTop: '4px', lineHeight: 1.5 }}>
                          Decouple high-traffic synchronous call chains into Cloud Pub/Sub topics to absorb traffic spikes without exhausting service connection pools.
                        </p>
                      </div>

                      <div style={{ padding: '12px', background: 'var(--bg)', borderRadius: '8px', border: '1px solid var(--line)' }}>
                        <div style={{ fontWeight: 600, color: 'var(--accent)', fontSize: '13px' }}>
                          3. Cloud SQL Read Replicas & Connection Pooling
                        </div>
                        <p style={{ color: 'var(--muted)', fontSize: '12px', marginTop: '4px', lineHeight: 1.5 }}>
                          Provision read replicas to distribute analytical and read traffic away from primary transaction nodes.
                        </p>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            ) : (
              <div className="card" style={{ textAlign: 'center', padding: '60px 20px', color: 'var(--muted)' }}>
                <Cpu className="w-12 h-12" style={{ margin: '0 auto 16px', opacity: 0.3 }} />
                <h3 style={{ fontSize: '16px', color: 'var(--text)', marginBottom: '8px' }}>
                  No Active Simulation Results
                </h3>
                <p style={{ maxWidth: '440px', margin: '0 auto 20px', fontSize: '13px' }}>
                  Select an architecture and a stress scenario above, then click <strong>"Run Simulation"</strong> to execute the discrete-event test.
                </p>
                <button className="btn btn-primary" onClick={runSimulation} disabled={simulating}>
                  <Play className="w-4 h-4" />
                  {simulating ? 'Simulating...' : 'Run Simulation'}
                </button>
              </div>
            )}
          </div>
        )}

        {/* Tab 2: Architecture Spec View */}
        {activeTab === 'graph' && (
          <div style={{ marginTop: '20px' }}>
            <div className="card">
              <div className="card-title">
                <span>Architecture Graph Components: {selectedModel}</span>
                <span style={{ fontSize: '12px', color: 'var(--muted)' }}>
                  {modelDetails?.nodes?.length || 0} nodes · {modelDetails?.edges?.length || 0} connections
                </span>
              </div>

              {modelDetails ? (
                <div>
                  <h4 style={{ fontSize: '13px', color: 'var(--muted)', marginBottom: '10px', textTransform: 'uppercase' }}>Nodes / Services</h4>
                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))', gap: '12px', marginBottom: '24px' }}>
                    {modelDetails.nodes?.map((n: any) => (
                      <div key={n.id} style={{ padding: '12px', background: 'var(--bg)', borderRadius: '8px', border: '1px solid var(--line)' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '6px' }}>
                          <span style={{ fontWeight: 600, fontFamily: 'var(--font-mono)' }}>{n.id}</span>
                          <span className="badge" style={{ background: 'var(--surface-hover)', color: 'var(--accent)' }}>{n.type || 'service'}</span>
                        </div>
                        <div style={{ fontSize: '12px', color: 'var(--muted)' }}>
                          Replicas: {n.replicas || 1} · Concurrency: {n.concurrency_per_replica || 20}
                        </div>
                        {n.latency_ms && (
                          <div style={{ fontSize: '11px', color: 'var(--muted)', marginTop: '2px' }}>
                            Latency: p50={n.latency_ms.p50}ms, p99={n.latency_ms.p99}ms
                          </div>
                        )}
                      </div>
                    ))}
                  </div>

                  <h4 style={{ fontSize: '13px', color: 'var(--muted)', marginBottom: '10px', textTransform: 'uppercase' }}>Edges / Communication Paths</h4>
                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))', gap: '12px' }}>
                    {modelDetails.edges?.map((e: any, idx: number) => (
                      <div key={idx} style={{ padding: '10px 12px', background: 'var(--bg)', borderRadius: '8px', border: '1px solid var(--line)', fontSize: '12px' }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontWeight: 600, fontFamily: 'var(--font-mono)' }}>
                          <span>{e.from}</span>
                          <ArrowRight className="w-3.5 h-3.5 text-muted" />
                          <span>{e.to}</span>
                        </div>
                        <div style={{ fontSize: '11px', color: 'var(--muted)', marginTop: '4px' }}>
                          Timeout: {e.timeout_ms || 1000}ms · Retries: {e.retry?.attempts || 0} ({e.retry?.backoff || 'none'})
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ) : (
                <p style={{ color: 'var(--muted)', fontSize: '13px' }}>Loading architecture details...</p>
              )}
            </div>
          </div>
        )}

        {/* Tab 3: Static Audit View */}
        {activeTab === 'analyzer' && (
          <div style={{ marginTop: '20px' }}>
            <div className="card">
              <div className="card-title">
                <span>Static Resilience Audit ({findings.length} findings)</span>
                <button className="btn" onClick={runAnalysis} disabled={analyzing} style={{ fontSize: '12px', padding: '4px 10px' }}>
                  <RefreshCw className="w-3.5 h-3.5" /> Re-scan
                </button>
              </div>

              {findings.length > 0 ? (
                <div>
                  {findings.map((f, i) => (
                    <div key={i} className="finding-item">
                      <div className="finding-head">
                        <span className="finding-target">{f.target}</span>
                        <span
                          className={`badge ${
                            f.severity === 'critical'
                              ? 'badge-bad'
                              : f.severity === 'high'
                              ? 'badge-bad'
                              : f.severity === 'medium'
                              ? 'badge-warn'
                              : 'badge-ok'
                          }`}
                        >
                          {f.severity} · {f.rule}
                        </span>
                      </div>
                      <p className="finding-msg">{f.message}</p>
                      <div className="finding-fix">
                        <strong>Recommended Architecture Pattern:</strong> {f.fix}
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div style={{ textAlign: 'center', padding: '40px 0', color: 'var(--muted)' }}>
                  <CheckCircle2 className="w-10 h-10 text-ok" style={{ margin: '0 auto 12px' }} />
                  <p>No static architectural flaws detected for {selectedModel}.</p>
                </div>
              )}
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
