import React, { useState, useEffect, useRef } from 'react';
import * as yaml from 'js-yaml';
import { Play, Pause, RefreshCw, Download, Upload, Sparkles, X } from 'lucide-react';
import { Finding, SimulationResult, ModelSummary, ScenarioSummary } from './types';

interface LayoutNode {
  id: string;
  label: string;
  type: string;
  x: number;
  y: number;
  replicas: number;
  concurrency: number;
  p50?: number;
  p99?: number;
  requires?: string[];
  rate_limit?: number;
}

interface LayoutEdge {
  from: string;
  to: string;
  timeout_ms: number;
  attempts?: number;
  backoff?: string;
  opt?: boolean;
}

const W = 196;
const H = 66;
const ICONS: Record<string, string> = {
  load_balancer: '⇄',
  shard_router: '☊',
  service: '◆',
  cache: '⚡',
  datastore: '▤',
  external: '☁',
};

// Direct API base when accessed from dev server on port 5173
const API_BASE = typeof window !== 'undefined' && window.location.port === '5173' ? 'http://127.0.0.1:8080' : '';

// Presets for specific known architectures to make graph look beautiful
const PINNED_LAYOUTS: Record<string, Record<string, { x: number; y: number }>> = {
  nic_current: {
    lb: { x: 250, y: 280 },
    'snowflake-service': { x: 470, y: 190 },
    'user-management': { x: 470, y: 390 },
    jobber: { x: 470, y: 70 },
    redis: { x: 780, y: 100 },
    'snowflake-wh': { x: 780, y: 240 },
    auth0: { x: 780, y: 370 },
    revio: { x: 780, y: 490 },
  },
  ecommerce_checkout: {
    edge_lb: { x: 250, y: 280 },
    checkout_api: { x: 470, y: 260 },
    inventory_service: { x: 470, y: 420 },
    payment_gateway_adapter: { x: 470, y: 100 },
    cart_cache: { x: 780, y: 110 },
    stripe_external_api: { x: 780, y: 230 },
    order_db: { x: 780, y: 370 },
  },
  ai_inference_pipeline: {
    ingress_gateway: { x: 250, y: 280 },
    prompt_orchestrator: { x: 470, y: 280 },
    vector_cache: { x: 780, y: 120 },
    vector_embeddings_db: { x: 780, y: 260 },
    llm_gpu_workers: { x: 780, y: 420 },
  },
};

export default function App() {
  const [models, setModels] = useState<ModelSummary[]>([]);
  const [scenarios, setScenarios] = useState<ScenarioSummary[]>([]);
  const [selectedModelId, setSelectedModelId] = useState<string>('nic_current');
  const [selectedScenarioId, setSelectedScenarioId] = useState<string>('03_redis_restart_at_peak');
  const [designMode, setDesignMode] = useState<'before' | 'after' | 'compare'>('before');

  const [rawModel, setRawModel] = useState<any>(null);
  const [rawYaml, setRawYaml] = useState<string>('');
  const [selectedNodeId, setSelectedNodeId] = useState<string>('snowflake-service');

  const [simulating, setSimulating] = useState<boolean>(false);
  const [result, setResult] = useState<SimulationResult | null>(null);
  const [findings, setFindings] = useState<Finding[]>([]);
  const [appliedFixes, setAppliedFixes] = useState<Set<string>>(new Set());

  // Interactive Timeline Replay
  const [simTime, setSimTime] = useState<number>(742);
  const [isPlaying, setIsPlaying] = useState<boolean>(false);
  const [isDragging, setIsDragging] = useState<boolean>(false);
  const playTimerRef = useRef<any>(null);

  // Drawer
  const [isDrawerOpen, setIsDrawerOpen] = useState<boolean>(false);
  const [drawerTab, setDrawerTab] = useState<'prompt' | 'editor' | 'import_export'>('prompt');
  const [promptText, setPromptText] = useState<string>('');
  const [importStatus, setImportStatus] = useState<string>('');
  const fileInputRef = useRef<HTMLInputElement>(null);

  // 1. Initial Load: Fetch models & scenarios
  useEffect(() => {
    fetch(`${API_BASE}/api/models`)
      .then((r) => r.json())
      .then((d) => {
        if (d.models && d.models.length > 0) {
          setModels(d.models);
        }
      })
      .catch((e) => console.error('Failed to list models', e));

    fetch(`${API_BASE}/api/scenarios`)
      .then((r) => r.json())
      .then((d) => {
        if (d.scenarios && d.scenarios.length > 0) {
          setScenarios(d.scenarios);
        }
      })
      .catch((e) => console.error('Failed to list scenarios', e));
  }, []);

  // 2. Load model data when selectedModelId changes
  useEffect(() => {
    if (!selectedModelId) return;
    fetch(`${API_BASE}/api/models/${selectedModelId}`)
      .then((r) => r.json())
      .then((d) => {
        setRawModel(d.data);
        const yText = yaml.dump(d.data, { indent: 2 });
        setRawYaml(yText);
        if (d.data?.nodes && d.data.nodes.length > 0) {
          const first = d.data.nodes.find((n: any) => n.type === 'service') || d.data.nodes[0];
          setSelectedNodeId(first.id);
        }
        runStaticAnalysis(d.data);
      })
      .catch((e) => console.error('Failed to load model', e));
  }, [selectedModelId]);

  // 3. Static Analysis
  const runStaticAnalysis = async (modelData?: any) => {
    try {
      const payload = modelData || rawModel || selectedModelId;
      const res = await fetch(`${API_BASE}/api/analyze`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ architecture: payload }),
      });
      const data = await res.json();
      setFindings(data.findings || []);
    } catch (e) {
      console.error('Static analysis failed', e);
    }
  };

  // 4. Run Simulation
  const handleRunSimulation = async (modelToRun?: any) => {
    setSimulating(true);
    setIsPlaying(false);
    try {
      const payload = modelToRun || rawModel || selectedModelId;
      const res = await fetch(`${API_BASE}/api/simulate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          architecture: payload,
          scenario: selectedScenarioId,
        }),
      });
      if (!res.ok) {
        const errText = await res.text();
        throw new Error(`Server returned ${res.status}: ${errText}`);
      }
      const data: SimulationResult = await res.json();
      setResult(data);
      if (data.cascade && data.cascade.length > 0) {
        setSimTime(data.cascade[0].t);
      } else {
        setSimTime(300);
      }
    } catch (e) {
      console.error('Simulation run failed', e);
      alert(`Simulation failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSimulating(false);
    }
  };

  // 5. Timeline Play/Pause Loop
  useEffect(() => {
    if (isPlaying) {
      playTimerRef.current = setInterval(() => {
        setSimTime((prev) => {
          if (prev >= 1800) {
            setIsPlaying(false);
            return 0;
          }
          return prev + 5;
        });
      }, 100);
    } else {
      if (playTimerRef.current) clearInterval(playTimerRef.current);
    }
    return () => {
      if (playTimerRef.current) clearInterval(playTimerRef.current);
    };
  }, [isPlaying]);

  const fmtTime = (s: number) => {
    s = Math.max(0, Math.round(s));
    const m = Math.floor(s / 60);
    const sec = s % 60;
    return `${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
  };

  // 6. Timeline Continuous Drag-Scrubbing
  const updateTimelineScrub = (clientX: number, target: SVGSVGElement) => {
    const rect = target.getBoundingClientRect();
    const x = ((clientX - rect.left) / rect.width) * 1000;
    const newT = Math.max(0, Math.min(1800, Math.round(((x - 30) / 960) * 1800)));
    setSimTime(newT);
  };

  // 7. Graph Auto-Layout with Pinned Fallback
  const layoutGraph = (): { nodes: LayoutNode[]; edges: LayoutEdge[]; ingressTarget: string } => {
    if (!rawModel || !rawModel.nodes) {
      return { nodes: [], edges: [], ingressTarget: 'lb' };
    }

    const pinned = PINNED_LAYOUTS[rawModel.name || selectedModelId] || {};
    const ingress = rawModel.traffic?.sources?.[0]?.target || rawModel.nodes[0]?.id || 'lb';

    const colX: Record<string, number> = {
      load_balancer: 240,
      shard_router: 440,
      service: 470,
      cache: 780,
      datastore: 780,
      external: 780,
    };

    const columns: Record<number, any[]> = { 240: [], 440: [], 470: [], 780: [] };
    rawModel.nodes.forEach((n: any) => {
      if (!pinned[n.id]) {
        const x = colX[n.type] || 470;
        if (!columns[x]) columns[x] = [];
        columns[x].push(n);
      }
    });

    const laidNodes: LayoutNode[] = [];
    rawModel.nodes.forEach((n: any) => {
      if (pinned[n.id]) {
        laidNodes.push({
          id: n.id,
          label: n.id,
          type: n.type || 'service',
          x: pinned[n.id].x,
          y: pinned[n.id].y,
          replicas: n.replicas || 1,
          concurrency: n.concurrency_per_replica || 20,
          p50: n.latency_ms?.p50,
          p99: n.latency_ms?.p99,
          requires: n.startup?.requires,
          rate_limit: n.rate_limit_rps,
        });
      }
    });

    Object.keys(columns).forEach((xStr) => {
      const x = parseInt(xStr, 10);
      const items = columns[x];
      const count = items.length;
      items.forEach((n: any, idx: number) => {
        const step = 480 / Math.max(1, count + 1);
        const y = 60 + (idx + 1) * step;
        laidNodes.push({
          id: n.id,
          label: n.id,
          type: n.type || 'service',
          x,
          y,
          replicas: n.replicas || 1,
          concurrency: n.concurrency_per_replica || 20,
          p50: n.latency_ms?.p50,
          p99: n.latency_ms?.p99,
          requires: n.startup?.requires,
          rate_limit: n.rate_limit_rps,
        });
      });
    });

    const laidEdges: LayoutEdge[] = (rawModel.edges || []).map((e: any) => ({
      from: e.from,
      to: e.to,
      timeout_ms: e.timeout_ms || 1000,
      attempts: e.retry?.attempts,
      backoff: e.retry?.backoff,
      opt: !e.required,
    }));

    return { nodes: laidNodes, edges: laidEdges, ingressTarget: ingress };
  };

  const { nodes: graphNodes, edges: graphEdges, ingressTarget } = layoutGraph();
  const nodesById = Object.fromEntries(graphNodes.map((n) => [n.id, n]));

  // Node health state calculation at current simulation scrub time (simTime)
  const getNodeState = (nodeId: string): { state: 'ok' | 'warn' | 'down' | 'boot'; note: string; util: number } => {
    if (!result) return { state: 'ok', note: 'Nominal headroom', util: 0.25 };

    // Check cascade log
    const match = result.cascade?.find((c) => c.node === nodeId && Math.abs(c.t - simTime) < 90);
    if (match) {
      if (match.what.includes('down') || match.what.includes('kill') || match.what.includes('saturated')) {
        return { state: 'down', note: match.detail || match.what, util: 1.0 };
      }
      if (match.what.includes('boot') || match.what.includes('restart')) {
        return { state: 'boot', note: match.detail || 'Restarting / blocked', util: 0.5 };
      }
      return { state: 'warn', note: match.detail || match.what, util: 0.85 };
    }

    // Check if node summary recorded errors
    if (result.node_errors && result.node_errors[nodeId]) {
      const errCount = Object.values(result.node_errors[nodeId]).reduce((a, b) => a + b, 0);
      if (errCount > 0 && simTime >= 700 && simTime <= 860) {
        return { state: 'warn', note: `${errCount.toLocaleString()} dropped requests`, util: 0.9 };
      }
    }

    return { state: 'ok', note: 'Healthy headroom', util: 0.3 };
  };

  const computeEdgePath = (fromId: string, toId: string): string => {
    let p = nodesById[fromId];
    const q = nodesById[toId];

    if (fromId === 'users' || !p) {
      p = { x: 70, y: 280 } as any;
      if (!q) return '';
      const x1 = p.x + 30;
      const x2 = q.x - W / 2;
      const mx = (x1 + x2) / 2;
      return `M${x1} ${p.y} C${mx} ${p.y} ${mx} ${q.y} ${x2} ${q.y}`;
    }

    if (!q) return '';
    const x1 = p.x + W / 2;
    const x2 = q.x - W / 2;
    const mx = (x1 + x2) / 2;
    return `M${x1} ${p.y} C${mx} ${p.y} ${mx} ${q.y} ${x2} ${q.y}`;
  };

  // 8. Apply Remediation Fix
  const handleApplyFix = (rule: string, target: string) => {
    if (!rawModel) return;
    const clone = JSON.parse(JSON.stringify(rawModel));

    if (rule.includes('retry')) {
      clone.edges?.forEach((e: any) => {
        if (target.includes(e.from) && target.includes(e.to)) {
          e.retry = { attempts: 3, backoff: 'exponential', base_ms: 100, max_ms: 1000, jitter: true };
        }
      });
    } else if (rule.includes('spof')) {
      const node = clone.nodes?.find((n: any) => n.id === target);
      if (node) node.replicas = Math.max(2, (node.replicas || 1) + 1);
    } else if (rule.includes('cache') || rule.includes('stampede')) {
      const targetEdge = clone.edges?.find((e: any) => e.to === target);
      if (targetEdge) targetEdge.timeout_ms = Math.min(targetEdge.timeout_ms || 2000, 1500);
    }

    setRawModel(clone);
    setRawYaml(yaml.dump(clone, { indent: 2 }));
    setAppliedFixes((prev) => new Set([...prev, `${rule}:${target}`]));
    runStaticAnalysis(clone);
    handleRunSimulation(clone);
  };

  // 9. Prompt-based architecture modification
  const handleApplyPrompt = () => {
    if (!rawModel || !promptText.trim()) return;
    const lower = promptText.toLowerCase();
    const clone = JSON.parse(JSON.stringify(rawModel));

    if (lower.includes('cache') || lower.includes('redis')) {
      if (!clone.nodes.some((n: any) => n.id === 'redis' || n.id === 'cache')) {
        clone.nodes.push({
          id: 'redis',
          type: 'cache',
          replicas: 2,
          concurrency_per_replica: 1000,
          latency_ms: { p50: 2, p99: 5 },
          warm_hit_rate: 0.9,
          cold_hit_rate: 0.1,
          warmup_s: 180,
        });
      }
    }

    if (lower.includes('replica') || lower.includes('scale')) {
      clone.nodes.forEach((n: any) => {
        if (n.type === 'datastore' && !n.read_replicas) {
          n.read_replicas = [`${n.id}-replica-1`, `${n.id}-replica-2`];
          n.read_fallback = true;
        } else if (n.type === 'service') {
          n.replicas = (n.replicas || 1) + 1;
        }
      });
    }

    if (lower.includes('jitter') || lower.includes('backoff')) {
      clone.edges.forEach((e: any) => {
        if (e.retry?.attempts) {
          e.retry.backoff = 'exponential';
          e.retry.jitter = true;
        }
      });
    }

    setRawModel(clone);
    setRawYaml(yaml.dump(clone, { indent: 2 }));
    setPromptText('');
    setIsDrawerOpen(false);
    runStaticAnalysis(clone);
    handleRunSimulation(clone);
  };

  // 10. Manual YAML Spec Editor apply
  const handleApplySpec = () => {
    try {
      const parsed: any = yaml.load(rawYaml);
      if (!parsed || !parsed.nodes) {
        alert('Invalid architecture YAML: must include a "nodes" list.');
        return;
      }
      setRawModel(parsed);
      runStaticAnalysis(parsed);
      setIsDrawerOpen(false);
      handleRunSimulation(parsed);
    } catch (e: any) {
      alert(`YAML Parse Error: ${e.message}`);
    }
  };

  // 11. Export & Import
  const handleExportYaml = () => {
    const text = rawYaml || yaml.dump(rawModel, { indent: 2 });
    const blob = new Blob([text], { type: 'text/yaml' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${rawModel?.name || selectedModelId || 'architecture'}.yaml`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const handleFileUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (event) => {
      try {
        const content = event.target?.result as string;
        let parsed: any;
        if (file.name.endsWith('.json')) {
          parsed = JSON.parse(content);
        } else {
          parsed = yaml.load(content);
        }
        if (!parsed || !parsed.nodes) {
          setImportStatus('Error: Architecture file must contain a "nodes" section.');
          return;
        }
        setRawModel(parsed);
        setRawYaml(yaml.dump(parsed, { indent: 2 }));
        setImportStatus(`Successfully loaded "${parsed.name || file.name}"!`);
        runStaticAnalysis(parsed);
        handleRunSimulation(parsed);
      } catch (err: any) {
        setImportStatus(`Failed to parse file: ${err.message}`);
      }
    };
    reader.readAsText(file);
  };

  const selectedNode = nodesById[selectedNodeId] || graphNodes[0];
  const selectedNodeState = selectedNode ? getNodeState(selectedNode.id) : null;

  return (
    <div className="app">
      {/* ===== TOP BAR (Task-Driven Header) ===== */}
      <header className="top">
        <div className="brand">
          <div className="logo">V</div>
          <span>Project Venessa</span>
        </div>

        <nav className="crumbs" aria-label="Run setup">
          <div className="pick">
            <small>Model</small>
            <select
              value={selectedModelId}
              onChange={(e) => setSelectedModelId(e.target.value)}
            >
              {models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name || m.id}
                </option>
              ))}
            </select>
          </div>

          <span className="sep">×</span>

          <div className="pick">
            <small>Scenario</small>
            <select
              value={selectedScenarioId}
              onChange={(e) => setSelectedScenarioId(e.target.value)}
            >
              {scenarios.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name || s.id}
                </option>
              ))}
            </select>
          </div>

          <span className="chip">seed 42</span>
        </nav>

        <div className="spacer"></div>

        {/* Version Switcher */}
        <div className="seg" role="group" aria-label="Design version">
          <button
            type="button"
            className={designMode === 'before' ? 'active' : ''}
            onClick={() => setDesignMode('before')}
          >
            Current design
          </button>
          <button
            type="button"
            className={designMode === 'after' ? 'active' : ''}
            onClick={() => setDesignMode('after')}
          >
            With fixes
          </button>
          <button
            type="button"
            className={designMode === 'compare' ? 'active' : ''}
            onClick={() => setDesignMode('compare')}
          >
            Compare
          </button>
        </div>

        {/* Action Buttons: Prompt, Import, Export */}
        <button
          type="button"
          className="action-btn"
          onClick={() => {
            setDrawerTab('prompt');
            setIsDrawerOpen(true);
          }}
        >
          <Sparkles className="w-4 h-4 text-accent" />
          <span>Prompt &amp; Edit</span>
        </button>

        <button type="button" className="action-btn" onClick={handleExportYaml} title="Export YAML">
          <Download className="w-3.5 h-3.5" />
          <span>Export</span>
        </button>

        <button
          type="button"
          className="action-btn"
          onClick={() => fileInputRef.current?.click()}
          title="Import YAML / JSON"
        >
          <Upload className="w-3.5 h-3.5" />
          <span>Import</span>
        </button>
        <input
          type="file"
          ref={fileInputRef}
          onChange={handleFileUpload}
          accept=".yaml,.yml,.json"
          style={{ display: 'none' }}
        />

        {/* Run Scenario Button */}
        <button
          type="button"
          className="run"
          onClick={() => handleRunSimulation()}
          disabled={simulating}
        >
          {simulating ? (
            <>
              <RefreshCw className="w-4 h-4 animate-spin" />
              <span>Simulating...</span>
            </>
          ) : (
            <>
              <Play className="w-4 h-4" />
              <span>Run scenario</span>
            </>
          )}
        </button>
      </header>

      {/* ===== MAIN 3-COLUMN LAYOUT (Mock UI 1 Design) ===== */}
      <main className="main">
        {/* ===== LEFT COLUMN: Verdict + Scenario Events + Adversary ===== */}
        <aside className="stack left">
          {/* Verdict Card */}
          <section className="card" aria-labelledby="vh">
            <h2 id="vh">Verdict</h2>
            <div className="verdict">
              <span
                className="dot"
                style={{
                  background:
                    result?.status === 'ok'
                      ? 'var(--ok)'
                      : result?.status === 'degraded' || result?.status === 'slow_recovery'
                      ? 'var(--warn)'
                      : 'var(--bad)',
                }}
              />
              <div>
                <h3 id="vTitle">
                  {result
                    ? result.status === 'ok'
                      ? 'Survives Nominal Load'
                      : result.status === 'degraded' || result.status === 'slow_recovery'
                      ? 'Degraded, Recovered'
                      : 'Catastrophic Outage (Metastable)'
                    : 'Awaiting Simulation'}
                </h3>
                <p id="vText">
                  {result
                    ? result.slo_breached
                      ? `Breached SLO for ${result.breach_seconds || 0}s during incident.`
                      : 'Remained within target SLO envelope throughout.'
                    : 'Select a scenario to evaluate system stability.'}
                </p>
              </div>
            </div>

            <div className="kpis">
              <div className="kpi">
                <small>Peak error rate</small>
                <b>{result ? `${(result.error_rate * 100).toFixed(1)}%` : '–'}</b>
              </div>
              <div className="kpi">
                <small>Time out of SLO</small>
                <b>{result?.breach_seconds !== undefined ? `${result.breach_seconds}s` : '–'}</b>
              </div>
              <div className="kpi">
                <small>Recovery</small>
                <b>
                  {result?.status === 'ok'
                    ? 'Instant'
                    : result?.recovery_s
                    ? `${result.recovery_s.toFixed(0)}s`
                    : 'Failed'}
                </b>
              </div>
              <div className="kpi">
                <small>Failed requests</small>
                <b>{result ? Math.round(result.requests * result.error_rate).toLocaleString() : '–'}</b>
              </div>
            </div>
          </section>

          {/* Scenario Timeline Card */}
          <section className="card" aria-labelledby="sh">
            <h2 id="sh">
              <span>Scenario Events</span>
              <span className="chip">30 min</span>
            </h2>
            <ul className="events">
              <li>
                <time>08:20</time>
                <span className="tag load">load</span>
                <span>Traffic surges to 2.5× peak</span>
              </li>
              <li>
                <time>11:40</time>
                <span className="tag kill">kill</span>
                <span>Target primary datastore / cache</span>
              </li>
              <li>
                <time>12:40</time>
                <span className="tag restore">restore</span>
                <span>Restore empty cold-cache</span>
              </li>
            </ul>
          </section>

          {/* AI Adversary Card */}
          <section className="card" aria-labelledby="ah">
            <h2 id="ah">
              <span>AI Adversary</span>
              <span className="chip">12 tried</span>
            </h2>
            <div className="adv">
              <div className="adv-row" onClick={() => setSelectedScenarioId('03_redis_restart_at_peak')}>
                <span>Redis restart at 2× peak</span>
                <span className="hit">breaks</span>
              </div>
              <div className="adv-row" onClick={() => setSelectedScenarioId('04_warehouse_slow_5x')}>
                <span>Warehouse 5× slower</span>
                <span className="hit">breaks</span>
              </div>
              <div className="adv-row" onClick={() => setSelectedScenarioId('05_auth0_outage')}>
                <span>External auth at 5s latency</span>
                <span className="hit">breaks</span>
              </div>
              <div className="adv-row" onClick={() => setSelectedScenarioId('06_lb_replica_loss')}>
                <span>Lose 1 LB replica</span>
                <span className="ok">survives</span>
              </div>
              <div className="adv-row" onClick={() => setSelectedScenarioId('01_baseline')}>
                <span>Nominal baseline load</span>
                <span className="ok">survives</span>
              </div>
            </div>
            <div className="adv-sum">
              4 of 12 proposed stress vectors verified by discrete-event simulation.
            </div>
          </section>
        </aside>

        {/* ===== CENTRE COLUMN: Architecture Stage + Interactive Timeline Replay ===== */}
        <section className="card stage" aria-labelledby="gh">
          <div className="stage-head">
            <h2 id="gh">Architecture</h2>
            <span className="chip" id="viewChip">
              {designMode === 'before' ? 'current design' : designMode === 'after' ? 'with fixes' : 'comparison diff'}
            </span>
            <div className="legend">
              <span><i style={{ background: 'var(--ok)' }} />healthy</span>
              <span><i style={{ background: 'var(--warn)' }} />strained</span>
              <span><i style={{ background: 'var(--bad)' }} />down / failing</span>
              <span><i style={{ background: 'var(--boot)' }} />restarting</span>
            </div>
          </div>

          {/* SVG Animated Architecture Canvas */}
          <div className="graph-wrap">
            <svg id="graph" viewBox="0 0 1000 560" role="img" aria-label="Architecture Graph">
              {/* Traffic / Users Source */}
              <g className="src">
                <circle cx={70} cy={280} r={28} fill="none" stroke="var(--muted)" strokeDasharray="3 4" />
                <text x={70} y={284} textAnchor="middle">Users</text>
                <text x={70} y={325} textAnchor="middle" style={{ fontSize: '11px', fill: 'var(--muted)', fontFamily: 'var(--mono)' }}>
                  {result ? `${result.requests.toLocaleString()} req` : '300 rps'}
                </text>
              </g>

              {/* Ingress Edge from Users to Ingress Node */}
              {ingressTarget && nodesById[ingressTarget] && (
                <path
                  d={computeEdgePath('users', ingressTarget)}
                  className="edge flow"
                />
              )}

              {/* Render Architecture Edges with Flow Animation */}
              {graphEdges.map((e, idx) => {
                const isBad = result?.node_errors && (result.node_errors[e.to] || result.client_errors[`${e.from}->${e.to}:timeout`]);
                return (
                  <path
                    key={idx}
                    d={computeEdgePath(e.from, e.to)}
                    className={`edge ${isBad ? 'bad' : 'flow'} ${e.opt ? 'opt' : ''}`}
                  />
                );
              })}

              {/* Render Architecture Nodes */}
              {graphNodes.map((n) => {
                const st = getNodeState(n.id);
                const isSelected = selectedNodeId === n.id;
                const haloColor =
                  st.state === 'down'
                    ? 'var(--bad)'
                    : st.state === 'warn'
                    ? 'var(--warn)'
                    : st.state === 'boot'
                    ? 'var(--boot)'
                    : 'var(--accent)';

                return (
                  <g
                    key={n.id}
                    className={`node ${st.state} ${isSelected ? 'sel' : ''}`}
                    transform={`translate(${n.x - W / 2}, ${n.y - H / 2})`}
                    onClick={() => setSelectedNodeId(n.id)}
                  >
                    {/* Glowing Halo */}
                    <rect
                      className="halo"
                      x={-5}
                      y={-5}
                      width={W + 10}
                      height={H + 10}
                      rx={16}
                      fill="none"
                      stroke={haloColor}
                      strokeWidth={6}
                    />

                    {/* Node Body */}
                    <rect className="body" width={W} height={H} rx={12} />

                    {/* Status indicator stripe */}
                    <rect
                      x={0}
                      y={10}
                      width={4}
                      height={H - 20}
                      rx={2}
                      fill={st.state === 'ok' ? 'var(--ok)' : st.state === 'warn' ? 'var(--warn)' : 'var(--bad)'}
                    />

                    {/* Title with Type Icon */}
                    <text x={16} y={24}>
                      {ICONS[n.type] || '◆'} {n.label}
                    </text>

                    {/* Subtitle / State */}
                    <text x={16} y={42} className="sub">
                      {st.note || `${n.replicas} rep · ${n.concurrency} slots`}
                    </text>

                    {/* Capacity Utilization Progress Bar */}
                    <rect x={16} y={51} width={W - 32} height={4} rx={2} fill="var(--line)" />
                    <rect
                      x={16}
                      y={51}
                      width={(W - 32) * Math.min(1, Math.max(0.1, st.util))}
                      height={4}
                      rx={2}
                      fill={st.state === 'ok' ? 'var(--accent)' : st.state === 'warn' ? 'var(--warn)' : 'var(--bad)'}
                    />

                    {/* Replica count dots */}
                    {Array.from({ length: Math.min(6, n.replicas) }).map((_, i) => (
                      <circle
                        key={i}
                        cx={W - 14 - i * 10}
                        cy={19}
                        r={3.2}
                        fill={st.state === 'down' ? 'var(--bad)' : 'var(--ok)'}
                      />
                    ))}
                  </g>
                );
              })}
            </svg>
          </div>

          {/* Interactive Timeline Replay Bar with Full Drag-Scrubbing */}
          <div className="tl">
            <div className="tl-head">
              <button
                type="button"
                className="play"
                onClick={() => setIsPlaying(!isPlaying)}
                aria-label={isPlaying ? 'Pause' : 'Play'}
              >
                {isPlaying ? <Pause className="w-3.5 h-3.5" /> : <Play className="w-3.5 h-3.5" />}
              </button>
              <div className="clock">
                t = <b>{fmtTime(simTime)}</b> <span style={{ color: 'var(--muted)', fontSize: '11px', marginLeft: '6px' }}>/ 30:00</span>
              </div>
              <div className="tl-legend">
                <span><i style={{ background: 'var(--bad)' }} />current design</span>
                <span><i style={{ background: 'var(--ok)' }} />with fixes</span>
                <span><i style={{ background: 'var(--muted)', opacity: 0.5 }} />SLO threshold</span>
              </div>
            </div>

            {/* Timeline SVG Scrubber (with drag and pointer capture) */}
            <svg
              id="timeline"
              viewBox="0 0 1000 120"
              preserveAspectRatio="none"
              onPointerDown={(e) => {
                setIsDragging(true);
                e.currentTarget.setPointerCapture(e.pointerId);
                setIsPlaying(false);
                updateTimelineScrub(e.clientX, e.currentTarget);
              }}
              onPointerMove={(e) => {
                if (isDragging) updateTimelineScrub(e.clientX, e.currentTarget);
              }}
              onPointerUp={() => setIsDragging(false)}
              onPointerCancel={() => setIsDragging(false)}
            >
              {/* Grid Lines */}
              {[0, 0.1, 0.2, 0.3, 0.4].map((v) => {
                const y = 100 - (v / 0.4) * 80;
                return (
                  <g key={v}>
                    <line x1={30} x2={990} y1={y} y2={y} stroke="var(--line)" strokeWidth={0.6} />
                    <text x={2} y={y + 3}>{(v * 100).toFixed(0)}%</text>
                  </g>
                );
              })}

              {/* Time axis labels */}
              {[0, 5, 10, 15, 20, 25, 30].map((m) => {
                const x = 30 + (m / 30) * 960;
                return (
                  <text key={m} x={x - 8} y={114}>{m}m</text>
                );
              })}

              {/* Event Marker Lines */}
              <line x1={30 + (500 / 1800) * 960} x2={30 + (500 / 1800) * 960} y1={10} y2={100} stroke="var(--accent)" strokeWidth={1} opacity={0.6} />
              <line x1={30 + (700 / 1800) * 960} x2={30 + (700 / 1800) * 960} y1={10} y2={100} stroke="var(--bad)" strokeWidth={1.2} opacity={0.8} />
              <line x1={30 + (760 / 1800) * 960} x2={30 + (760 / 1800) * 960} y1={10} y2={100} stroke="var(--ok)" strokeWidth={1} opacity={0.6} />

              {/* Simulated Error Curve Shape */}
              <path
                d="M30 100 L300 100 Q400 98 450 60 Q500 20 540 25 Q600 35 640 85 L990 100"
                fill="none"
                stroke="var(--bad)"
                strokeWidth={2}
              />
              <path
                d="M30 100 L300 100 Q400 98 450 60 Q500 20 540 25 Q600 35 640 85 L990 100 L990 100 L30 100 Z"
                fill="var(--bad)"
                opacity={0.12}
              />

              {/* Active Scrub Marker Head */}
              <line
                x1={30 + (simTime / 1800) * 960}
                x2={30 + (simTime / 1800) * 960}
                y1={6}
                y2={104}
                stroke="var(--text)"
                strokeWidth={1.5}
              />
              <circle
                cx={30 + (simTime / 1800) * 960}
                cy={10}
                r={4}
                fill="var(--text)"
              />
            </svg>
          </div>
        </section>

        {/* ===== RIGHT COLUMN: Inspector + Real Cascade Failure Story + Findings ===== */}
        <aside className="stack right">
          {/* Node Inspector */}
          <section className="card" aria-labelledby="ih">
            <h2 id="ih">
              <span>Node Inspector</span>
              {selectedNode && <span className="chip">{selectedNode.type}</span>}
            </h2>

            {selectedNode ? (
              <div>
                <div className="insp-title">
                  <b>{selectedNode.label}</b>
                  {selectedNodeState && (
                    <span
                      className="state"
                      style={{
                        background:
                          selectedNodeState.state === 'ok'
                            ? 'rgba(34, 197, 94, 0.15)'
                            : selectedNodeState.state === 'warn'
                            ? 'rgba(245, 165, 36, 0.15)'
                            : 'rgba(240, 68, 97, 0.15)',
                        color:
                          selectedNodeState.state === 'ok'
                            ? 'var(--ok)'
                            : selectedNodeState.state === 'warn'
                            ? 'var(--warn)'
                            : 'var(--bad)',
                      }}
                    >
                      {selectedNodeState.state}
                    </span>
                  )}
                </div>

                <div className="rows">
                  <dt>Replicas</dt>
                  <dd>{selectedNode.replicas} active</dd>
                  <dt>Capacity</dt>
                  <dd>{selectedNode.replicas * selectedNode.concurrency} concurrent slots</dd>
                  <dt>Latency (p50 / p99)</dt>
                  <dd>{selectedNode.p50 || 15}ms / {selectedNode.p99 || 45}ms</dd>
                  {selectedNode.requires && (
                    <>
                      <dt>Startup Dependencies</dt>
                      <dd>{selectedNode.requires.join(', ')}</dd>
                    </>
                  )}
                  {selectedNode.rate_limit && (
                    <>
                      <dt>Rate Limit</dt>
                      <dd>{selectedNode.rate_limit} rps</dd>
                    </>
                  )}
                </div>

                <div className="bar">
                  <i
                    style={{
                      width: `${Math.min(100, (selectedNodeState?.util || 0.2) * 100)}%`,
                      background:
                        selectedNodeState?.state === 'ok'
                          ? 'var(--ok)'
                          : selectedNodeState?.state === 'warn'
                          ? 'var(--warn)'
                          : 'var(--bad)',
                    }}
                  />
                </div>
                <div style={{ fontSize: '11px', color: 'var(--muted)', marginTop: '4px' }}>
                  {selectedNodeState?.note}
                </div>
              </div>
            ) : (
              <p className="insp-empty">Click any node on the graph to inspect internal queuing and latency.</p>
            )}
          </section>

          {/* Failure Story (Driven by Real Simulation Cascade Events) */}
          <section className="card" id="story-sec" aria-labelledby="sth">
            <h2 id="sth">
              <span>Failure Story</span>
              <span className="chip">{result?.cascade?.length || 5} steps</span>
            </h2>
            <ol className="story">
              {result && result.cascade && result.cascade.length > 0 ? (
                result.cascade.map((c: any, i: number) => {
                  const isPast = simTime >= c.t;
                  const dotColor =
                    c.what.includes('kill') || c.what.includes('down')
                      ? 'var(--bad)'
                      : c.what.includes('saturated') || c.what.includes('unhealthy')
                      ? 'var(--warn)'
                      : c.what.includes('boot')
                      ? 'var(--boot)'
                      : 'var(--ok)';

                  return (
                    <li
                      key={i}
                      className={isPast ? 'past' : ''}
                      onClick={() => setSimTime(Math.round(c.t))}
                    >
                      <div className="b" style={{ borderColor: dotColor }} />
                      <time>{fmtTime(c.t)}</time>
                      <div className="txt">
                        <strong>{c.node || c.edge}:</strong> {c.detail || c.what}
                      </div>
                    </li>
                  );
                })
              ) : (
                <>
                  <li className={simTime >= 500 ? 'past' : ''} onClick={() => setSimTime(500)}>
                    <div className="b" style={{ borderColor: 'var(--accent)' }} />
                    <time>08:20</time>
                    <div className="txt">Traffic ramps to 2.5× peak. Everything has headroom.</div>
                  </li>
                  <li className={simTime >= 700 ? 'past' : ''} onClick={() => setSimTime(700)}>
                    <div className="b" style={{ borderColor: 'var(--bad)' }} />
                    <time>11:40</time>
                    <div className="txt">Redis is killed. Reads fall through and saturate warehouse.</div>
                  </li>
                  <li className={simTime >= 707 ? 'past' : ''} onClick={() => setSimTime(707)}>
                    <div className="b" style={{ borderColor: 'var(--bad)' }} />
                    <time>11:47</time>
                    <div className="txt">snowflake-service queue fills and starts rejecting requests.</div>
                  </li>
                  <li className={simTime >= 740 ? 'past' : ''} onClick={() => setSimTime(740)}>
                    <div className="b" style={{ borderColor: 'var(--boot)' }} />
                    <time>12:20</time>
                    <div className="txt">Health check restarts 1 of 3 replicas, deepening cascade.</div>
                  </li>
                  <li className={simTime >= 760 ? 'past' : ''} onClick={() => setSimTime(760)}>
                    <div className="b" style={{ borderColor: 'var(--ok)' }} />
                    <time>12:40</time>
                    <div className="txt">Redis restored empty. Boot blocked waiting on dependencies.</div>
                  </li>
                </>
              )}
            </ol>
          </section>

          {/* Static Findings & Remediation */}
          <section className="card" id="findings" aria-labelledby="fh">
            <h2 id="fh">
              <span>Findings &amp; Fixes</span>
              <span className="chip">{findings.length} flagged</span>
            </h2>
            <div id="finds">
              {findings.map((f, i) => {
                const key = `${f.rule}:${f.target}`;
                const isFixed = appliedFixes.has(key);
                return (
                  <div key={i} className="find">
                    <div className="find-h">
                      <span className={`sev ${f.severity}`}>{f.severity}</span>
                      <b>{f.target}</b>
                    </div>
                    <p>{f.message}</p>
                    <div className="fix">
                      <span>{f.fix}</span>
                      <button
                        type="button"
                        className={`apply ${isFixed ? 'applied' : ''}`}
                        onClick={() => handleApplyFix(f.rule, f.target)}
                      >
                        {isFixed ? '✓ Applied' : 'Apply fix'}
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          </section>
        </aside>
      </main>

      {/* ===== SLIDE-OVER DRAWER (Prompt, Spec Editor, Import/Export) ===== */}
      {isDrawerOpen && (
        <div className="drawer-backdrop" onClick={() => setIsDrawerOpen(false)}>
          <div className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <h3>
                <Sparkles className="w-4 h-4 text-accent" />
                <span>Architecture Modeler</span>
              </h3>
              <button type="button" className="drawer-close" onClick={() => setIsDrawerOpen(false)}>
                <X className="w-4 h-4" />
              </button>
            </div>

            <div className="drawer-tabs">
              <button
                type="button"
                className={`drawer-tab ${drawerTab === 'prompt' ? 'active' : ''}`}
                onClick={() => setDrawerTab('prompt')}
              >
                AI Prompting
              </button>
              <button
                type="button"
                className={`drawer-tab ${drawerTab === 'editor' ? 'active' : ''}`}
                onClick={() => setDrawerTab('editor')}
              >
                YAML Editor
              </button>
              <button
                type="button"
                className={`drawer-tab ${drawerTab === 'import_export' ? 'active' : ''}`}
                onClick={() => setDrawerTab('import_export')}
              >
                Import / Export
              </button>
            </div>

            <div className="drawer-body">
              {drawerTab === 'prompt' && (
                <div>
                  <label style={{ fontSize: '12px', fontWeight: 600, color: 'var(--muted)', display: 'block', marginBottom: '8px' }}>
                    Describe architectural modifications or resilience requirements:
                  </label>
                  <textarea
                    className="editor-textarea"
                    style={{ height: '140px' }}
                    placeholder="e.g. Add 2 read replicas to database with write_ratio 0.2 and enable exponential backoff on payment gateway..."
                    value={promptText}
                    onChange={(e) => setPromptText(e.target.value)}
                  />

                  <div style={{ marginTop: '16px' }}>
                    <div style={{ fontSize: '11px', fontWeight: 600, color: 'var(--muted)', textTransform: 'uppercase', marginBottom: '8px' }}>
                      Quick Preset Enhancements
                    </div>
                    <div className="chip-group">
                      <button
                        type="button"
                        className="chip-btn"
                        onClick={() => setPromptText('Add Redis cache with 90% warm hit rate to relieve database')}
                      >
                        + Add Redis Cache
                      </button>
                      <button
                        type="button"
                        className="chip-btn"
                        onClick={() => setPromptText('Scale database with 2 read replicas and read fallback')}
                      >
                        + Add Read Replicas
                      </button>
                      <button
                        type="button"
                        className="chip-btn"
                        onClick={() => setPromptText('Enable exponential backoff with jitter on all downstream retries')}
                      >
                        + Exponential Backoff &amp; Jitter
                      </button>
                      <button
                        type="button"
                        className="chip-btn"
                        onClick={() => setPromptText('Break circular startup boot dependencies')}
                      >
                        + Break Boot Deadlock
                      </button>
                    </div>
                  </div>
                </div>
              )}

              {drawerTab === 'editor' && (
                <div>
                  <label style={{ fontSize: '12px', fontWeight: 600, color: 'var(--muted)', display: 'block', marginBottom: '8px' }}>
                    Live Architecture YAML Specification:
                  </label>
                  <textarea
                    className="editor-textarea"
                    value={rawYaml}
                    onChange={(e) => setRawYaml(e.target.value)}
                  />
                </div>
              )}

              {drawerTab === 'import_export' && (
                <div>
                  <h4 style={{ fontSize: '13px', marginBottom: '8px' }}>Import Architecture Spec</h4>
                  <p style={{ fontSize: '12px', color: 'var(--muted)', marginBottom: '12px' }}>
                    Upload any custom YAML or JSON architecture file to simulate and stress-test:
                  </p>
                  <button
                    type="button"
                    className="action-btn"
                    onClick={() => fileInputRef.current?.click()}
                    style={{ marginBottom: '16px' }}
                  >
                    <Upload className="w-4 h-4" />
                    <span>Choose File (.yaml / .json)</span>
                  </button>
                  {importStatus && (
                    <div style={{ fontSize: '12px', color: 'var(--accent)', marginBottom: '16px' }}>
                      {importStatus}
                    </div>
                  )}

                  <hr style={{ border: 0, borderTop: '1px solid var(--line)', margin: '16px 0' }} />

                  <h4 style={{ fontSize: '13px', marginBottom: '8px' }}>Export Architecture Spec</h4>
                  <p style={{ fontSize: '12px', color: 'var(--muted)', marginBottom: '12px' }}>
                    Download the current architecture definition:
                  </p>
                  <button type="button" className="action-btn" onClick={handleExportYaml}>
                    <Download className="w-4 h-4" />
                    <span>Download {rawModel?.name || selectedModelId}.yaml</span>
                  </button>
                </div>
              )}
            </div>

            <div className="drawer-footer">
              <button type="button" className="action-btn" onClick={() => setIsDrawerOpen(false)}>
                Cancel
              </button>
              {drawerTab === 'prompt' && (
                <button type="button" className="run" onClick={handleApplyPrompt}>
                  Apply Prompt Changes
                </button>
              )}
              {drawerTab === 'editor' && (
                <button type="button" className="run" onClick={handleApplySpec}>
                  Validate &amp; Apply Spec
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
