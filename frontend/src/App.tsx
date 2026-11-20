import React, { useState, useEffect } from 'react';
import { ShieldAlert, Play, Cpu, AlertTriangle, CheckCircle2, TrendingDown, RefreshCw, Zap, Server, Database } from 'lucide-react';
import { Finding, SimulationResult, ModelSummary, ScenarioSummary } from './types';

export default function App() {
  const [activeTab, setActiveTab] = useState<'architecture' | 'analyzer' | 'simulate'>('simulate');
  const [models, setModels] = useState<ModelSummary[]>([]);
  const [scenarios, setScenarios] = useState<ScenarioSummary[]>([]);
  const [selectedModel, setSelectedModel] = useState<string>('nic_current');
  const [selectedScenario, setSelectedScenario] = useState<string>('03_redis_restart_at_peak');

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
      
      // Compute estimated Rupee loss if errors occurred
      const failed = Math.round(data.requests * data.error_rate);
      const duplicateDebits = data.status === 'no_recovery' ? Math.round(failed * 0.04) : 0;
      const ticketSize = 850; // ₹850 avg ticket
      const dropoffRate = 0.70;
      const disputeCost = 450; // ₹450 per duplicate dispute
      
      data.duplicate_debits = duplicateDebits;
      data.rupee_loss = Math.round((failed * ticketSize * dropoffRate) + (duplicateDebits * disputeCost));
      
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
      {/* Top Navigation */}
      <header className="header">
        <div className="brand">
          <ShieldAlert className="w-5 h-5 text-accent" />
          <span>Project Venessa</span>
          <span className="brand-badge">Buildathon 2026</span>
        </div>

        <div className="nav-links">
          <button
            className={`nav-btn ${activeTab === 'simulate' ? 'active' : ''}`}
            onClick={() => setActiveTab('simulate')}
          >
            Pre-Mortem Simulation
          </button>
          <button
            className={`nav-btn ${activeTab === 'analyzer' ? 'active' : ''}`}
            onClick={() => setActiveTab('analyzer')}
          >
            Static Analyzer ({findings.length})
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
                Model: {m.name || m.id}
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
                Attack: {s.name || s.id}
              </option>
            ))}
          </select>

          <button className="btn btn-primary" onClick={runSimulation} disabled={simulating}>
            <Play className="w-4 h-4" />
            {simulating ? 'Simulating...' : 'Run Simulation'}
          </button>
        </div>
      </header>

      {/* Main Container */}
      <main className="container" style={{ paddingBottom: '60px' }}>
        {/* Banner */}
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
            <h2 style={{ fontSize: '16px', fontWeight: 600, marginBottom: '4px' }}>
              Deterministic Pre-Mortem for Payment Systems
            </h2>
            <p style={{ color: 'var(--muted)', fontSize: '13px' }}>
              Gemini Adversary proposes architectural stress scenarios; SimPy discrete-event engine proves the failure modes before launch.
            </p>
          </div>
          <div style={{ display: 'flex', gap: '10px' }}>
            <button className="btn" onClick={runAnalysis} disabled={analyzing}>
              <Cpu className="w-4 h-4" />
              {analyzing ? 'Checking...' : 'Run Static Audit'}
            </button>
          </div>
        </div>

        {/* Tab 1: Simulation View */}
        {activeTab === 'simulate' && (
          <div style={{ marginTop: '20px' }}>
            {result ? (
              <div>
                {/* Result KPI Bar */}
                <div className="grid-3" style={{ marginBottom: '20px' }}>
                  <div className="stat-box card">
                    <div className="stat-label">System Health Status</div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginTop: '4px' }}>
                      <span
                        className={`badge ${
                          result.status === 'ok'
                            ? 'badge-ok'
                            : result.status === 'degraded'
                            ? 'badge-warn'
                            : 'badge-bad'
                        }`}
                      >
                        {result.status}
                      </span>
                      <span style={{ fontSize: '13px', color: 'var(--muted)' }}>
                        {result.slo_breached ? 'SLO Breached' : 'Within SLO'}
                      </span>
                    </div>
                  </div>

                  <div className="stat-box card">
                    <div className="stat-label">Failure Rate & Latency</div>
                    <div className="stat-value" style={{ color: result.error_rate > 0 ? 'var(--bad)' : 'var(--ok)' }}>
                      {(result.error_rate * 100).toFixed(1)}% errors
                    </div>
                    <div style={{ fontSize: '12px', color: 'var(--muted)', marginTop: '2px' }}>
                      p50: {result.p50_ms.toFixed(0)}ms · p99: {result.p99_ms.toFixed(0)}ms
                    </div>
                  </div>

                  <div className="stat-box card" style={{ borderColor: result.rupee_loss ? 'rgba(248, 113, 113, 0.4)' : 'var(--line)' }}>
                    <div className="stat-label" style={{ color: 'var(--bad)' }}>Estimated Outage Rupee Loss</div>
                    <div className="stat-value" style={{ color: 'var(--bad)' }}>
                      ₹{result.rupee_loss?.toLocaleString('en-IN') || '0'}
                    </div>
                    <div style={{ fontSize: '12px', color: 'var(--muted)', marginTop: '2px' }}>
                      {result.duplicate_debits || 0} duplicate debits · {Math.round(result.requests * result.error_rate)} dropped txns
                    </div>
                  </div>
                </div>

                {/* Cascade & Root Cause Analysis */}
                <div className="grid-2">
                  <div className="card">
                    <div className="card-title">
                      <span>Failure Cascade Order</span>
                      <span style={{ fontSize: '12px', color: 'var(--muted)' }}>Simulated Wall Time: {result.wall_time_s.toFixed(2)}s</span>
                    </div>
                    {result.cascade && result.cascade.length > 0 ? (
                      <div style={{ maxHeight: '320px', overflowY: 'auto' }}>
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
                            }}
                          >
                            <span style={{ color: 'var(--muted)' }}>T+{c.t}s</span>
                            <span style={{ fontWeight: 600, color: 'var(--bad)' }}>{c.what}</span>
                            <span style={{ color: 'var(--text)' }}>{c.node || c.edge || '-'}</span>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <p style={{ color: 'var(--muted)', fontSize: '13px' }}>No cascade events recorded. System survived the scenario.</p>
                    )}
                  </div>

                  {/* Google Cloud Remediation */}
                  <div className="card">
                    <div className="card-title">
                      <span>Google Cloud Architecture Fix</span>
                      <Zap className="w-4 h-4 text-accent" />
                    </div>
                    <div style={{ fontSize: '13px', lineHeight: 1.6 }}>
                      <p style={{ marginBottom: '12px', color: 'var(--muted)' }}>
                        Recommended architecture changes based on discrete-event bottleneck analysis:
                      </p>

                      <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
                        <div style={{ padding: '10px', background: 'var(--bg)', borderRadius: '8px', border: '1px solid var(--line)' }}>
                          <strong style={{ color: 'var(--accent)' }}>1. Google Cloud Memorystore (Redis):</strong>
                          <p style={{ color: 'var(--muted)', fontSize: '12px', marginTop: '2px' }}>
                            Add Redis caching on reading edges with request coalescing to prevent cold-cache warehouse stampedes.
                          </p>
                        </div>

                        <div style={{ padding: '10px', background: 'var(--bg)', borderRadius: '8px', border: '1px solid var(--line)' }}>
                          <strong style={{ color: 'var(--accent)' }}>2. Idempotency Key & Cloud Pub/Sub:</strong>
                          <p style={{ color: 'var(--muted)', fontSize: '12px', marginTop: '2px' }}>
                            Store unique client idempotency keys in Memorystore with 60s TTL to eliminate duplicate debits on client retries.
                          </p>
                        </div>

                        <div style={{ padding: '10px', background: 'var(--bg)', borderRadius: '8px', border: '1px solid var(--line)' }}>
                          <strong style={{ color: 'var(--accent)' }}>3. Cloud SQL Read Replicas:</strong>
                          <p style={{ color: 'var(--muted)', fontSize: '12px', marginTop: '2px' }}>
                            Provision 2 read replicas and split traffic (`write_ratio: 0.2`) to relieve primary database saturation.
                          </p>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            ) : (
              <div
                className="card"
                style={{
                  textAlign: 'center',
                  padding: '60px 20px',
                  color: 'var(--muted)',
                }}
              >
                <Cpu className="w-12 h-12" style={{ margin: '0 auto 16px', opacity: 0.3 }} />
                <h3 style={{ fontSize: '16px', color: 'var(--text)', marginBottom: '8px' }}>
                  No Simulation Run Yet
                </h3>
                <p style={{ maxWidth: '440px', margin: '0 auto 20px', fontSize: '13px' }}>
                  Select an architecture model and an attack scenario above, then click <strong>"Run Simulation"</strong> to execute a SimPy discrete-event test.
                </p>
                <button className="btn btn-primary" onClick={runSimulation} disabled={simulating}>
                  <Play className="w-4 h-4" />
                  {simulating ? 'Simulating...' : 'Run Simulation'}
                </button>
              </div>
            )}
          </div>
        )}

        {/* Tab 2: Static Analyzer View */}
        {activeTab === 'analyzer' && (
          <div style={{ marginTop: '20px' }}>
            <div className="card">
              <div className="card-title">
                <span>Static Vulnerability Audit ({findings.length} findings)</span>
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
                        <strong>Suggested Remediation:</strong> {f.fix}
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
