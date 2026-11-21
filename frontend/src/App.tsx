import React, { useEffect, useRef, useState } from 'react';
import * as yaml from 'js-yaml';
import {
  Activity, ArrowLeft, ArrowRight, Check, ChevronRight, Download, FileText,
  Gauge, Layers, Pause, Play, Plus, RefreshCw, ShieldAlert, Sparkles, Target,
  TrendingUp, Upload, X,
} from 'lucide-react';
import {
  Finding, FixVerification, ModelSummary, SimulationResult, TimelinePoint,
} from './types';
import { apiFetch } from './lib/api';

/* ------------------------------------------------------------------ */
/* Constants & small helpers                                           */
/* ------------------------------------------------------------------ */

type StepKey = 'design' | 'scenarios' | 'run' | 'fixes' | 'report' | 'dashboard';
type StepState = 'active' | 'done' | 'ready' | 'locked';

const STEPS: { key: StepKey; label: string; hint: string }[] = [
  { key: 'design', label: 'Design', hint: 'Use or create' },
  { key: 'scenarios', label: 'Stress tests', hint: 'Pick a failure' },
  { key: 'run', label: 'Run', hint: 'See the verdict' },
  { key: 'fixes', label: 'Fixes', hint: 'Approve changes' },
  { key: 'report', label: 'Report', hint: 'Evidence & diff' },
  { key: 'dashboard', label: 'Dashboard', hint: 'Executive summary' },
];

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

// Only used to lay out the three bundled reference designs. Generated designs
// fall back to a generic column layout.
const PINNED_LAYOUTS: Record<string, Record<string, { x: number; y: number }>> = {
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
  saas_analytics_platform: {
    stream_lb: { x: 250, y: 280 },
    ingest_router: { x: 470, y: 280 },
    stream_worker_shard_0: { x: 730, y: 110 },
    stream_worker_shard_1: { x: 730, y: 250 },
    analytics_primary_db: { x: 770, y: 400 },
    analytics_read_replica_1: { x: 920, y: 340 },
    analytics_read_replica_2: { x: 920, y: 470 },
  },
};

interface LayoutNode {
  id: string; label: string; type: string; x: number; y: number;
  replicas: number; concurrency: number; p50?: number; p99?: number;
  requires?: string[]; rate_limit?: number;
}
interface LayoutEdge {
  from: string; to: string; timeout_ms: number; attempts?: number;
  backoff?: string; opt?: boolean;
}

const fixIdOf = (f: any): string => f?.fix_id || f?.id || '';

// Rank by measured impact (verified first), so the strongest option is never
// buried below cosmetic or neutral proposals.
const rankFixes = (items: any[]): any[] => {
  const score = (f: any): number => {
    const v = f?.verification;
    if (!v) return Number.POSITIVE_INFINITY;
    const d = v.deltas || {};
    return (v.verified && v.classification !== 'worse' ? -1000 : 0)
      + (d.error_rate || 0) * 100
      + (d.seconds_out_of_slo || 0) / 10
      + (d.p99_ms || 0) / 1000;
  };
  return [...items].sort((a, b) => score(a) - score(b));
};

const cellText = (v: any): string =>
  v === undefined || v === null ? '—' : typeof v === 'object' ? JSON.stringify(v) : String(v);

const fmtTime = (s: number) => {
  s = Math.max(0, Math.round(s));
  const m = Math.floor(s / 60);
  return `${String(m).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
};

const passedOf = (r: SimulationResult | null): boolean | null => {
  if (!r) return null;
  // SLO breach is authoritative. Never let a status-based `passed` flag
  // claim survival when the measured SLO was violated.
  if (typeof r.survived === 'boolean') return r.survived;
  if (typeof r.slo_breached === 'boolean') return !r.slo_breached;
  return r.status === 'ok';
};

const statusColor = (status?: string) =>
  status === 'ok' ? 'var(--green)'
    : status === 'degraded' || status === 'slow_recovery' ? 'var(--orange)'
      : status === 'no_recovery' ? 'var(--red)' : 'var(--ink-3)';

const verdictText = (r: SimulationResult | null) => {
  if (!r) return 'Not run yet';
  if (r.survived === true || (!r.slo_breached && r.status === 'ok')) return 'Survived the stress test';
  if (r.recovered === true || r.status === 'degraded' || r.status === 'slow_recovery') {
    return r.status === 'slow_recovery' ? 'Recovered, but too slowly' : 'SLO breached, then recovered';
  }
  return 'SLO breached — no recovery';
};

const describeEvent = (e: any) => {
  const bits = [`${e.action} ${e.target}`];
  if (e.replicas) bits.push(`${e.replicas} replica(s)`);
  if (e.action === 'slow') bits.push(`${e.factor ?? 1}× latency`);
  if (e.action === 'rate_limit') bits.push(`${e.rps} rps`);
  if (e.mode === 'blackhole') bits.push('blackhole');
  if (e.duration_s) bits.push(`for ${e.duration_s}s`);
  return bits.join(' · ');
};

const scenarioName = (s: any): string => {
  if (!s) return 'No scenario selected';
  const d = s.scenario || s;
  return d.name || s.scenario_id || 'Adversarial scenario';
};

const errText = (e: unknown) => {
  if (e instanceof Error) return e.message;
  return String(e);
};

async function readError(res: Response): Promise<string> {
  try {
    const d = await res.json();
    if (d?.detail) return typeof d.detail === 'string' ? d.detail : JSON.stringify(d.detail);
  } catch { /* use generic */ }
  return `Server error (${res.status})`;
}

/* ------------------------------------------------------------------ */
/* Graph layout & node health                                         */
/* ------------------------------------------------------------------ */

function layoutGraph(arch: any): { nodes: LayoutNode[]; edges: LayoutEdge[]; ingressTarget: string } {
  if (!arch?.nodes) return { nodes: [], edges: [], ingressTarget: '' };
  const pinned = PINNED_LAYOUTS[arch.name || ''] || {};
  const ingress = arch.traffic?.sources?.[0]?.target || arch.nodes[0]?.id || '';
  const colX: Record<string, number> = {
    load_balancer: 240, shard_router: 440, service: 470,
    cache: 780, datastore: 780, external: 780,
  };
  const columns: Record<number, any[]> = { 240: [], 440: [], 470: [], 780: [] };
  arch.nodes.forEach((n: any) => {
    if (!pinned[n.id]) {
      const x = colX[n.type || 'service'] || 470;
      (columns[x] ||= []).push(n);
    }
  });
  const toLayout = (n: any, x: number, y: number): LayoutNode => ({
    id: n.id, label: n.id, type: n.type || 'service', x, y,
    replicas: n.replicas || 1, concurrency: n.concurrency_per_replica || 20,
    p50: n.latency_ms?.p50, p99: n.latency_ms?.p99,
    requires: n.startup?.requires, rate_limit: n.rate_limit_rps,
  });
  const nodes: LayoutNode[] = [];
  arch.nodes.forEach((n: any) => {
    if (pinned[n.id]) nodes.push(toLayout(n, pinned[n.id].x, pinned[n.id].y));
  });
  Object.keys(columns).forEach((xStr) => {
    const x = Number(xStr);
    const items = columns[x];
    const step = 480 / Math.max(1, items.length + 1);
    items.forEach((n, i) => nodes.push(toLayout(n, x, 60 + (i + 1) * step)));
  });
  const edges: LayoutEdge[] = (arch.edges || []).map((e: any) => ({
    from: e.from, to: e.to, timeout_ms: e.timeout_ms || 1000,
    attempts: e.retry?.attempts, backoff: e.retry?.backoff, opt: !e.required,
  }));
  return { nodes, edges, ingressTarget: ingress };
}

function nodeHealth(result: SimulationResult | null, nodeId: string, t: number) {
  if (!result) return { state: 'ok' as const, note: 'No simulation yet', util: 0.25 };
  const latest = (result.cascade ?? [])
    .filter((c) => c.node === nodeId && c.t <= t)
    .sort((a, b) => a.t - b.t)
    .pop();
  if (latest) {
    const what = `${latest.what} ${latest.detail ?? ''}`.toLowerCase();
    if (what.includes('down') || what.includes('kill') || what.includes('saturated') || what.includes('reject')) {
      return { state: 'down' as const, note: latest.detail || latest.what, util: 1 };
    }
    if (what.includes('boot') || what.includes('restart') || what.includes('restore')) {
      return { state: 'boot' as const, note: latest.detail || 'Restarting / blocked', util: 0.5 };
    }
    if (what.includes('unhealthy') || what.includes('slow') || what.includes('rate limit')) {
      return { state: 'warn' as const, note: latest.detail || latest.what, util: 0.85 };
    }
  }
  if (result.node_errors?.[nodeId] && t >= (result.warmup_s ?? 0)) {
    const count = Object.values(result.node_errors[nodeId]).reduce((a, b) => a + b, 0);
    if (count > 0) return { state: 'warn' as const, note: `${count.toLocaleString()} dropped requests`, util: 0.9 };
  }
  return { state: 'ok' as const, note: 'Healthy headroom', util: 0.3 };
}

/* ------------------------------------------------------------------ */
/* Reusable visual components                                         */
/* ------------------------------------------------------------------ */

function ArchitectureGraph({
  arch, result, simTime, selectedNodeId, onSelectNode, label,
}: {
  arch: any; result: SimulationResult | null; simTime: number;
  selectedNodeId: string; onSelectNode: (id: string) => void; label: string;
}) {
  const { nodes, edges, ingressTarget } = layoutGraph(arch);
  const byId = Object.fromEntries(nodes.map((n) => [n.id, n]));
  const path = (from: string, to: string) => {
    const a = from === 'users' ? { x: 70, y: 280 } : byId[from];
    const b = byId[to];
    if (!a || !b) return '';
    const x1 = from === 'users' ? a.x + 30 : a.x + W / 2;
    const x2 = b.x - W / 2;
    const mx = (x1 + x2) / 2;
    return `M${x1} ${a.y} C${mx} ${a.y} ${mx} ${b.y} ${x2} ${b.y}`;
  };
  const selected = byId[selectedNodeId] || nodes[0];
  const health = selected ? nodeHealth(result, selected.id, simTime) : null;

  return (
    <div className="stage-grid">
      <div className="card graph-card">
        <div className="stage-head">
          <h2>{label}</h2>
          <div className="legend">
            <span><i style={{ background: 'var(--green)' }} />healthy</span>
            <span><i style={{ background: 'var(--orange)' }} />strained</span>
            <span><i style={{ background: 'var(--red)' }} />down</span>
            <span><i style={{ background: 'var(--boot)' }} />restarting</span>
          </div>
        </div>
        <div className="graph-wrap">
          <svg viewBox="0 0 1000 560" role="img" aria-label="Architecture graph">
            <g className="src">
              <circle cx={70} cy={280} r={28} fill="none" stroke="var(--ink-3)" strokeDasharray="3 4" />
              <text x={70} y={284} textAnchor="middle">Users</text>
              <text x={70} y={325} textAnchor="middle" style={{ fontSize: 11, fill: 'var(--ink-3)', fontFamily: 'var(--mono)' }}>
                {result ? `${result.requests.toLocaleString()} req` : `${arch.traffic?.sources?.[0]?.rps ?? '—'} rps`}
              </text>
            </g>
            {ingressTarget && path('users', ingressTarget) && <path d={path('users', ingressTarget)} className="edge flow" />}
            {edges.map((e, i) => {
              const bad = result?.node_errors?.[e.to] || result?.client_errors?.[`${e.from}->${e.to}:timeout`];
              return <path key={i} d={path(e.from, e.to)} className={`edge ${bad ? 'bad' : 'flow'} ${e.opt ? 'opt' : ''}`} />;
            })}
            {nodes.map((n) => {
              const st = nodeHealth(result, n.id, simTime);
              const isSel = selectedNodeId === n.id;
              const halo = st.state === 'down' ? 'var(--red)' : st.state === 'warn' ? 'var(--orange)' : st.state === 'boot' ? 'var(--boot)' : 'var(--accent)';
              return (
                <g key={n.id} className={`node ${st.state} ${isSel ? 'sel' : ''}`}
                  transform={`translate(${n.x - W / 2}, ${n.y - H / 2})`}
                  onClick={() => onSelectNode(n.id)}>
                  <rect className="halo" x={-5} y={-5} width={W + 10} height={H + 10} rx={16} fill="none" stroke={halo} strokeWidth={6} />
                  <rect className="body" width={W} height={H} rx={12} />
                  <rect x={0} y={10} width={4} height={H - 20} rx={2} fill={st.state === 'ok' ? 'var(--green)' : st.state === 'warn' ? 'var(--orange)' : 'var(--red)'} />
                  <text x={16} y={24}>{ICONS[n.type] || '◆'} {n.label}</text>
                  <text x={16} y={42} className="sub">{st.note || `${n.replicas} rep · ${n.concurrency} slots`}</text>
                  <rect x={16} y={51} width={W - 32} height={4} rx={2} fill="var(--line)" />
                  <rect x={16} y={51} width={(W - 32) * Math.min(1, Math.max(0.1, st.util))} height={4} rx={2}
                    fill={st.state === 'ok' ? 'var(--accent)' : st.state === 'warn' ? 'var(--orange)' : 'var(--red)'} />
                  {Array.from({ length: Math.min(6, n.replicas) }).map((_, i) => (
                    <circle key={i} cx={W - 14 - i * 10} cy={19} r={3.2} fill={st.state === 'down' ? 'var(--red)' : 'var(--green)'} />
                  ))}
                </g>
              );
            })}
          </svg>
        </div>
      </div>

      <div className="card inspector-card">
        <h2>Node inspector</h2>
        {selected ? (
          <>
            <div className="insp-title">
              <b>{selected.label}</b>
              {health && <span className="state" style={{
                background: health.state === 'ok' ? 'var(--green-tint)' : health.state === 'warn' ? 'rgba(242,166,90,.15)' : 'rgba(255,107,125,.15)',
                color: health.state === 'ok' ? 'var(--green)' : health.state === 'warn' ? 'var(--orange)' : 'var(--red)',
              }}>{health.state}</span>}
            </div>
            <div className="rows">
              <dt>Type</dt><dd>{selected.type}</dd>
              <dt>Replicas</dt><dd>{selected.replicas} active</dd>
              <dt>Capacity</dt><dd>{selected.replicas * selected.concurrency} concurrent slots</dd>
              <dt>Latency (p50 / p99)</dt><dd>{selected.p50 ?? '—'}ms / {selected.p99 ?? '—'}ms</dd>
              {selected.requires?.length ? <><dt>Startup deps</dt><dd>{selected.requires.join(', ')}</dd></> : null}
              {selected.rate_limit ? <><dt>Rate limit</dt><dd>{selected.rate_limit} rps</dd></> : null}
            </div>
            <div className="bar"><i style={{
              width: `${Math.min(100, (health?.util || 0.2) * 100)}%`,
              background: health?.state === 'ok' ? 'var(--green)' : health?.state === 'warn' ? 'var(--orange)' : 'var(--red)',
            }} /></div>
            <div className="micro" style={{ marginTop: 5 }}>{health?.note}</div>
          </>
        ) : <p className="insp-empty">Click a node in the graph to inspect it.</p>}
      </div>
    </div>
  );
}

function Timeline({
  result, compareTo, simTime, onScrub, isPlaying, onPlay, scenario, label = 'with fixes',
}: {
  result: SimulationResult; compareTo?: SimulationResult | null; simTime: number;
  onScrub: (t: number) => void; isPlaying: boolean; onPlay: () => void;
  scenario?: any; label?: string;
}) {
  const [dragging, setDragging] = useState(false);
  const duration = result.duration_s || 300;
  const curve = result.timeline ?? [];
  const other = compareTo?.timeline ?? [];
  const peak = [...curve, ...other].reduce((m, p) => Math.max(m, p.err_rate), 0);
  const scale = Math.max(0.05, (Math.ceil((peak * 100) / 5) * 5) / 100);
  const x = (t: number) => 30 + (Math.max(0, Math.min(duration, t)) / duration) * 960;
  const y = (err: number) => 100 - (Math.min(err, scale) / scale) * 80;
  const pathOf = (pts: TimelinePoint[]) => pts.length
    ? `M30 100 ${pts.map((p) => `L${x(p.t).toFixed(1)} ${y(p.err_rate).toFixed(1)}`).join(' ')} L990 100`
    : 'M30 100 L990 100';
  const scrub = curve.filter((p) => p.t <= simTime).pop()?.err_rate ?? 0;
  const markers = [
    ...(scenario?.scenario?.traffic?.ramp ?? []).map((r: any) => ({ t: r.at_s, kind: 'load' })),
    ...(scenario?.scenario?.events ?? []).map((e: any) => ({ t: e.at_s, kind: e.action })),
  ];
  const move = (clientX: number, el: SVGSVGElement) => {
    const rect = el.getBoundingClientRect();
    const px = ((clientX - rect.left) / rect.width) * 1000;
    onScrub(Math.max(0, Math.min(duration, Math.round(((px - 30) / 960) * duration))));
  };
  return (
    <div className="card timeline-card">
      <div className="tl-head">
        <button type="button" className="play" onClick={onPlay} aria-label={isPlaying ? 'Pause' : 'Play'}>
          {isPlaying ? <Pause className="w-3.5 h-3.5" /> : <Play className="w-3.5 h-3.5" />}
        </button>
        <div className="clock">t = <b>{fmtTime(simTime)}</b> <span>/ {fmtTime(duration)}</span></div>
        <div className="tl-legend">
          <span><i style={{ background: 'var(--red)' }} />{label}</span>
          {compareTo && <span><i style={{ background: 'var(--ink-3)' }} />original design</span>}
          <span><i style={{ background: 'var(--ink-3)', opacity: 0.5 }} />SLO</span>
        </div>
      </div>
      <svg id="timeline" viewBox="0 0 1000 120" preserveAspectRatio="none"
        onPointerDown={(e) => { setDragging(true); e.currentTarget.setPointerCapture(e.pointerId); move(e.clientX, e.currentTarget); }}
        onPointerMove={(e) => { if (dragging) move(e.clientX, e.currentTarget); }}
        onPointerUp={() => setDragging(false)} onPointerCancel={() => setDragging(false)}>
        {[0, 0.25, 0.5, 0.75, 1].map((v) => {
          const yy = 100 - v * 80;
          return <g key={v}>
            <line x1={30} x2={990} y1={yy} y2={yy} stroke="var(--line)" strokeWidth={0.6} />
            <text x={2} y={yy + 3}>{(scale * v * 100).toFixed(0)}%</text>
          </g>;
        })}
        {[0, 1, 2, 3, 4, 5, 6].map((i) => (
          <text key={i} x={x((i / 6) * duration) - 8} y={114}>{fmtTime((i / 6) * duration)}</text>
        ))}
        {markers.map((m, i) => {
          const color = m.kind === 'load' ? 'var(--accent)' : ['kill', 'restart'].includes(m.kind) ? 'var(--red)' : m.kind === 'restore' ? 'var(--green)' : 'var(--orange)';
          return <line key={i} x1={x(m.t)} x2={x(m.t)} y1={10} y2={100} stroke={color} strokeWidth={1.1} opacity={0.65} />;
        })}
        {result.slo_error_rate !== undefined && (
          <line x1={30} x2={990} y1={y(result.slo_error_rate)} y2={y(result.slo_error_rate)} stroke="var(--ink-3)" strokeWidth={1} strokeDasharray="4 4" opacity={0.7} />
        )}
        {compareTo && <path d={pathOf(other)} fill="none" stroke="var(--ink-3)" strokeWidth={1.5} opacity={0.9} />}
        <path d={pathOf(curve)} fill="none" stroke="var(--red)" strokeWidth={2} />
        <path d={`${pathOf(curve)} L990 100 L30 100 Z`} fill="var(--red)" opacity={0.12} />
        <line x1={x(simTime)} x2={x(simTime)} y1={6} y2={104} stroke="var(--ink)" strokeWidth={1.5} />
        <circle cx={x(simTime)} cy={y(scrub)} r={4} fill="var(--ink)" />
      </svg>
    </div>
  );
}

function ScoreGauge({ score, label, caption }: { score: number | null; label: string; caption: string }) {
  const r = 46;
  const circumference = 2 * Math.PI * r;
  const value = score === null ? 0 : Math.max(0, Math.min(100, score));
  const tone = score === null ? 'var(--ink-3)' : value >= 80 ? 'var(--green)' : value >= 50 ? 'var(--orange)' : 'var(--red)';
  const offset = circumference * (1 - value / 100);
  return (
    <div className="gauge">
      <svg viewBox="0 0 120 120" aria-label={`${label}: ${score === null ? 'not run' : `${value} out of 100`}`}>
        <circle cx={60} cy={60} r={r} fill="none" stroke="var(--line)" strokeWidth={9} />
        <circle cx={60} cy={60} r={r} fill="none" stroke={tone} strokeWidth={9} strokeLinecap="round"
          strokeDasharray={circumference} strokeDashoffset={offset} transform="rotate(-90 60 60)" />
        <text x={60} y={58} textAnchor="middle" className="gauge-value" fill={tone}>{score === null ? '—' : value}</text>
        <text x={60} y={74} textAnchor="middle" className="gauge-unit">/ 100</text>
      </svg>
      <b>{label}</b>
      <span className="micro">{caption}</span>
    </div>
  );
}

function Verdict({ result, label }: { result: SimulationResult | null; label: string }) {
  const passed = passedOf(result);
  return (
    <div className="card verdict-card">
      <span className="micro">{label}</span>
      <div className="verdict">
        <span className="dot" style={{ background: result ? statusColor(result.status) : 'var(--ink-3)' }} />
        <div>
          <h3>{verdictText(result)}</h3>
          <p>{result
            ? result.slo_breached
              ? `SLO breached for ${result.seconds_out_of_slo ?? result.breach_seconds ?? 0}s. ` +
                (result.post_fault_error_rate !== undefined
                  ? `Post-fault error rate ${(result.post_fault_error_rate * 100).toFixed(2)}% — ` +
                    (result.recovered ? 'recovered after the fault.' : 'still failing after the fault.')
                  : 'Recovery not measured.')
              : 'Stayed inside the target SLO envelope.'
            : 'Run a stress test to get a verdict.'}</p>
        </div>
      </div>
      <div className="kpis">
        <div className="kpi"><small>Peak error rate</small><b>{result ? `${(result.error_rate * 100).toFixed(1)}%` : '—'}</b></div>
        <div className="kpi"><small>Time out of SLO</small><b>{result?.seconds_out_of_slo ?? result?.breach_seconds ?? '—'}{result?.seconds_out_of_slo !== undefined || result?.breach_seconds !== undefined ? 's' : ''}</b></div>
        <div className="kpi"><small>Recovery</small><b>{result?.status === 'ok' ? 'Instant' : result?.recovery_s ? `${result.recovery_s.toFixed(0)}s` : result ? 'Failed' : '—'}</b></div>
        <div className="kpi"><small>Failed requests</small><b>{result ? Math.round(result.requests * result.error_rate).toLocaleString() : '—'}</b></div>
      </div>
      {passed === true && <div className="insight-card success"><ShieldCheckIcon /> This design survived the selected stress test.</div>}
      {passed === false && <div className="insight-card error"><ShieldAlert className="w-3.5 h-3.5" /> This design broke under the selected stress test. Fixes are ready in step 4.</div>}
    </div>
  );
}

function ShieldCheckIcon() {
  return <span style={{ color: 'var(--green)', fontWeight: 700 }}>✓</span>;
}

function FailureStory({ result, scenario, simTime, onJump }: {
  result: SimulationResult | null; scenario: any; simTime: number; onJump: (t: number) => void;
}) {
  const cascade = result?.cascade ?? [];
  const ramps = scenario?.scenario?.traffic?.ramp ?? [];
  const events = scenario?.scenario?.events ?? [];
  return (
    <div className="card story-card">
      <h2>Failure story <span className="chip">{cascade.length} steps</span></h2>
      <ol className="story">
        {cascade.length > 0 ? cascade.map((c, i) => {
          const color = c.what.includes('kill') || c.what.includes('down') ? 'var(--red)'
            : c.what.includes('saturated') || c.what.includes('unhealthy') ? 'var(--orange)'
              : c.what.includes('boot') ? 'var(--boot)' : 'var(--green)';
          return (
            <li key={i} className={simTime >= c.t ? 'past' : ''} onClick={() => onJump(Math.round(c.t))}>
              <div className="b" style={{ borderColor: color }} />
              <time>{fmtTime(c.t)}</time>
              <div className="txt"><strong>{c.node || c.edge}:</strong> {c.detail || c.what}</div>
            </li>
          );
        }) : (
          <>
            {ramps.map((r: any, i: number) => (
              <li key={`r${i}`} onClick={() => onJump(r.at_s)}>
                <div className="b" style={{ borderColor: 'var(--accent)' }} />
                <time>{fmtTime(r.at_s)}</time>
                <div className="txt">Traffic ramps to {r.multiplier}×.{r.ramp_s ? ` Ramp lasts ${r.ramp_s}s.` : ''}</div>
              </li>
            ))}
            {events.map((e: any, i: number) => (
              <li key={`e${i}`} onClick={() => onJump(e.at_s)}>
                <div className="b" style={{ borderColor: ['kill', 'restart'].includes(e.action) ? 'var(--red)' : e.action === 'restore' ? 'var(--green)' : 'var(--orange)' }} />
                <time>{fmtTime(e.at_s)}</time>
                <div className="txt">{describeEvent(e)}</div>
              </li>
            ))}
            {!ramps.length && !events.length && <li className="past"><div className="txt">Nominal load — no failures injected.</div></li>}
          </>
        )}
      </ol>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Main application                                                   */
/* ------------------------------------------------------------------ */

export default function App() {
  /* step navigation */
  const [step, setStep] = useState<StepKey>('design');

  /* step 1 — design */
  const [models, setModels] = useState<ModelSummary[]>([]);
  const [templateId, setTemplateId] = useState('');
  const [design, setDesign] = useState<any>(null);
  const [designYaml, setDesignYaml] = useState('');
  const [designFindings, setDesignFindings] = useState<Finding[]>([]);
  const [designSource, setDesignSource] = useState<'template' | 'generated' | 'import' | null>(null);

  /* step 2 — scenarios */
  const [scenarios, setScenarios] = useState<any[]>([]);
  const [selectedScenario, setSelectedScenario] = useState<any>(null);
  const [generatingScenarios, setGeneratingScenarios] = useState(false);
  const [scenarioError, setScenarioError] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);

  /* step 3 — baseline run */
  const [baselineRun, setBaselineRun] = useState<SimulationResult | null>(null);
  const [running, setRunning] = useState(false);
  const [remediating, setRemediating] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);

  /* step 4 — fixes */
  const [fixes, setFixes] = useState<any[]>([]);
  const [selectedFixIds, setSelectedFixIds] = useState<Set<string>>(new Set());
  const [fixError, setFixError] = useState<string | null>(null);

  /* step 5 — patched design + report */
  const [patchedDesign, setPatchedDesign] = useState<any>(null);
  const [patchedYaml, setPatchedYaml] = useState('');
  const [patchedRun, setPatchedRun] = useState<SimulationResult | null>(null);
  const [applying, setApplying] = useState(false);
  const [appliedFixIds, setAppliedFixIds] = useState<string[]>([]);
  const [lastDiff, setLastDiff] = useState<any>(null);
  const [reportWithoutFixes, setReportWithoutFixes] = useState(false);

  /* shared UI */
  const [showAlphaNotice, setShowAlphaNotice] = useState<boolean>(() => {
    try {
      return localStorage.getItem('vennessa.alpha.notice.v1') !== 'seen';
    } catch {
      return true;
    }
  });
  const [simTime, setSimTime] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [selectedNodeId, setSelectedNodeId] = useState('');
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [promptText, setPromptText] = useState('');
  const [promptError, setPromptError] = useState<string | null>(null);
  const [generatingDesign, setGeneratingDesign] = useState(false);
  const [thinkingSteps, setThinkingSteps] = useState<string[]>([]);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    apiFetch('/api/models')
      .then((r) => r.json())
      .then((d) => setModels(Array.isArray(d.models) ? d.models : []))
      .catch((e) => console.error('Failed to list models', e));
  }, []);

  useEffect(() => {
    if (!isPlaying) return;
    const duration = patchedRun?.duration_s || baselineRun?.duration_s || 300;
    const timer = window.setInterval(() => {
      setSimTime((prev) => {
        const next = prev + Math.max(1, Math.round(duration / 360));
        if (next >= duration) { setIsPlaying(false); return duration; }
        return next;
      });
    }, 100);
    return () => window.clearInterval(timer);
  }, [isPlaying, baselineRun, patchedRun]);

  const dismissAlphaNotice = () => {
    try {
      localStorage.setItem('vennessa.alpha.notice.v1', 'seen');
    } catch {
      // Storage can be blocked in private browsing; still dismiss for this session.
    }
    setShowAlphaNotice(false);
  };

  useEffect(() => {
    if (!showAlphaNotice) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') dismissAlphaNotice();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [showAlphaNotice]);

  /* ---- derived state ---- */
  const baselinePassed = passedOf(baselineRun);
  const patchedPassed = passedOf(patchedRun);
  const selectedFixes = fixes.filter((f) => selectedFixIds.has(fixIdOf(f)) && f.valid !== false);

  const unlocked: Record<StepKey, boolean> = {
    design: true,
    scenarios: !!design,
    run: !!design && !!selectedScenario,
    fixes: !!baselineRun && baselinePassed === false,
    report: !!baselineRun && (baselinePassed === true || !!patchedRun || reportWithoutFixes),
    dashboard: !!baselineRun && (baselinePassed === true || !!patchedRun || reportWithoutFixes),
  };
  const stepIndex = (k: StepKey) => STEPS.findIndex((s) => s.key === k);

  const stepState = (k: StepKey): StepState => {
    if (k === step) return 'active';
    if (!unlocked[k]) return 'locked';
    return stepIndex(k) < stepIndex(step) ? 'done' : 'ready';
  };

  const goToStep = (k: StepKey) => { if (unlocked[k]) { setStep(k); setIsPlaying(false); } };

  /* ---- step 1 actions ---- */

  // One reset path for every way of choosing a design. This is what prevents
  // stale results from one run bleeding into the next.
  const installDesign = async (arch: any, source: 'template' | 'generated' | 'import') => {
    if (!arch?.nodes) throw new Error('Architecture must contain nodes.');
    setPromptError(null);
    setDesign(arch);
    setDesignYaml(yaml.dump(arch, { indent: 2 }));
    setDesignSource(source);
    setScenarios([]);
    setSelectedScenario(null);
    setScenarioError(null);
    setWarnings([]);
    setBaselineRun(null);
    setRunError(null);
    setFixes([]);
    setSelectedFixIds(new Set());
    setFixError(null);
    setPatchedDesign(null);
    setPatchedYaml('');
    setPatchedRun(null);
    setAppliedFixIds([]);
    setLastDiff(null);
    setReportWithoutFixes(false);
    setSimTime(0);
    setIsPlaying(false);
    setSelectedNodeId(arch.nodes.find((n: any) => n.type === 'service')?.id || arch.nodes[0]?.id || '');
    setStep('design');
    try {
      const res = await apiFetch('/api/analyze', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ architecture: arch }),
      });
      const d = res.ok ? await res.json() : { findings: [] };
      setDesignFindings(Array.isArray(d.findings) ? d.findings : []);
    } catch { setDesignFindings([]); }
  };

  const loadTemplate = async (id: string) => {
    setTemplateId(id);
    if (!id) return;
    try {
      const res = await apiFetch(`/api/models/${id}`);
      const d = await res.json();
      await installDesign(d.data, 'template');
    } catch (e) { setPromptError(errText(e)); }
  };

  const createFromPrompt = async () => {
    if (!promptText.trim()) return;
    setGeneratingDesign(true);
    setPromptError(null);
    setThinkingSteps(['Reading the brief…', 'Designing the topology…', 'Validating schema…']);
    try {
      const res = await apiFetch('/api/ai/text-to-model', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ description: promptText.trim(), mode: 'create' }),
      });
      if (!res.ok) throw new Error(await readError(res));
      const d = await res.json();
      if (!d.architecture) throw new Error('No architecture was returned.');
      await installDesign(d.architecture, 'generated');
      setWarnings(Array.isArray(d.warnings) ? d.warnings : []);
      setTemplateId('');
      setPromptText('');
      setDrawerOpen(false);
    } catch (e) {
      setPromptError(errText(e));
    } finally {
      setGeneratingDesign(false);
      setThinkingSteps([]);
    }
  };

  const importFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = async (event) => {
      try {
        const text = String(event.target?.result || '');
        const parsed: any = file.name.endsWith('.json') ? JSON.parse(text) : yaml.load(text);
        await installDesign(parsed, 'import');
        setTemplateId('');
      } catch (err) { setPromptError(`Import failed: ${errText(err)}`); }
    };
    reader.readAsText(file);
    e.target.value = '';
  };

  /* ---- step 2 actions ---- */

  const generateScenarios = async () => {
    if (!design) return;
    setGeneratingScenarios(true);
    setScenarioError(null);
    setWarnings([]);
    setSelectedScenario(null);
    try {
      const res = await apiFetch('/api/ai/adversary', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          architecture: design, count: 3, seed: 42,
          exclude_ids: [], include_analysis: true,
        }),
      });
      if (!res.ok) throw new Error(await readError(res));
      const d = await res.json();
      const items = Array.isArray(d.scenarios) ? d.scenarios : [];
      setScenarios(items);
      setWarnings(Array.isArray(d.warnings) ? d.warnings : []);
      if (!items.length) setScenarioError('No scenarios were generated. Try again.');
    } catch (e) {
      setScenarioError(errText(e));
      setScenarios([]);
    } finally { setGeneratingScenarios(false); }
  };

  const advanceToScenarios = async () => {
    if (!design) return;
    setStep('scenarios');
    if (!scenarios.length && !generatingScenarios) await generateScenarios();
  };

  /* ---- step 3 actions ---- */

  const runBaseline = async () => {
    if (!design || !selectedScenario) return;
    setRunning(true);
    setRunError(null);
    setBaselineRun(null);
    setFixes([]);
    setSelectedFixIds(new Set());
    setPatchedDesign(null);
    setPatchedRun(null);
    setReportWithoutFixes(false);
    try {
      const analysisRes = await apiFetch('/api/analyze', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ architecture: design }),
      });
      const analysis = analysisRes.ok ? await analysisRes.json() : { findings: [] };
      const findings: Finding[] = Array.isArray(analysis.findings) ? analysis.findings : [];
      setDesignFindings(findings);

      const res = await apiFetch('/api/simulate', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ architecture: design, scenario: selectedScenario.scenario, seed: 42 }),
      });
      if (!res.ok) throw new Error(await readError(res));
      const result: SimulationResult = await res.json();
      setBaselineRun(result);
      setSimTime(result.cascade?.[0]?.t ?? result.warmup_s ?? 0);

      if (!passedOf(result)) {
        setRemediating(true);
        const fixRes = await apiFetch('/api/ai/remediate', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            architecture: design, simulation_result: result,
            findings, count: 3,
            architecture_fingerprint: result.architecture_fingerprint,
            scenario: selectedScenario.scenario,
          }),
        });
        if (fixRes.ok) {
          const fd = await fixRes.json();
          setFixes(Array.isArray(fd.fixes) ? fd.fixes : []);
          const nextWarnings: string[] = Array.isArray(fd.warnings) ? fd.warnings : [];
          if (fd.status === 'no_verified_fix') {
            const bp = fd.verification?.best_partial;
            const d = bp?.verification?.deltas || {};
            nextWarnings.push(
              'No verified fix found after up to 3 evidence-driven rounds. Best partial: ' +
              `${bp?.fix_id || 'none'} ` +
              `(error ${((d.error_rate || 0) * 100).toFixed(2)}pp, ` +
              `out-of-SLO ${Math.round(d.seconds_out_of_slo || 0)}s).`
            );
          } else if (fd.status === 'unverified') {
            nextWarnings.push('Fixes could not be verified because no scenario was available for re-simulation.');
          }
          setWarnings(nextWarnings);
        } else {
          setFixError(`Fix suggestions unavailable: ${await readError(fixRes)}`);
        }
        setRemediating(false);
      }
    } catch (e) {
      setRunError(errText(e));
    } finally { setRunning(false); setRemediating(false); }
  };

  /* ---- step 4 actions ---- */

  const toggleFix = (id: string) => {
    setSelectedFixIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const applyFixes = async () => {
    if (!design || !baselineRun || !selectedFixes.length) return;
    setApplying(true);
    setFixError(null);
    try {
      const patchRes = await apiFetch('/api/architectures/patch', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          architecture: design,
          patch: {
            base_fingerprint: baselineRun.architecture_fingerprint,
            ops: selectedFixes.flatMap((f) => (Array.isArray(f.patch?.ops) ? f.patch.ops : [])),
          },
          fix_ids: selectedFixes.map(fixIdOf),
        }),
      });
      if (!patchRes.ok) throw new Error(await readError(patchRes));
      const patched = await patchRes.json();
      const newDesign = patched.architecture;

      const simRes = await apiFetch('/api/simulate', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          architecture: newDesign, scenario: selectedScenario.scenario, seed: 42,
        }),
      });
      if (!simRes.ok) throw new Error(await readError(simRes));
      const after: SimulationResult = await simRes.json();

      setPatchedDesign(newDesign);
      setPatchedYaml(patched.yaml || yaml.dump(newDesign, { indent: 2 }));
      setPatchedRun(after);
      setAppliedFixIds(selectedFixes.map(fixIdOf));
      setLastDiff(patched.diff ?? null);
      setSimTime(after.cascade?.[0]?.t ?? after.warmup_s ?? 0);
      setSelectedFixIds(new Set());
      setStep('report');
    } catch (e) {
      setFixError(errText(e));
    } finally { setApplying(false); }
  };

  /* ---- step 5 actions ---- */

  const exportYaml = () => {
    const text = patchedYaml || designYaml;
    const blob = new Blob([text], { type: 'text/yaml' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${(patchedDesign || design)?.name || 'architecture'}.yaml`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const startOver = () => {
    setStep('design');
    setDesign(null);
    setDesignYaml('');
    setDesignSource(null);
    setTemplateId('');
    setScenarios([]);
    setSelectedScenario(null);
    setBaselineRun(null);
    setFixes([]);
    setSelectedFixIds(new Set());
    setPatchedDesign(null);
    setPatchedRun(null);
    setAppliedFixIds([]);
    setLastDiff(null);
    setReportWithoutFixes(false);
    setDesignFindings([]);
    setWarnings([]);
    setScenarioError(null);
    setRunError(null);
    setFixError(null);
    setPromptError(null);
    setSelectedNodeId('');
    setSimTime(0);
    setIsPlaying(false);
  };

  /* ---------------------------------------------------------------- */
  /* Report comparison metrics                                        */
  /* ---------------------------------------------------------------- */
  const metric = (r: any, keys: string[]): number | null => {
    for (const k of keys) if (typeof r?.[k] === 'number') return r[k];
    return null;
  };
  const fmtPct = (v: number | null) => v === null ? '—' : `${(v * 100).toFixed(1)}%`;
  const fmtMs = (v: number | null) => v === null ? '—' : `${Math.round(v)}ms`;
  const fmtSecs = (v: number | null) => v === null ? '—' : `${Math.round(v)}s`;
  const delta = (b: number | null, a: number | null, unit: 'pp' | 'ms' | 's') => {
    if (b === null || a === null) return '—';
    const d = a - b;
    if (Math.abs(d) < 1e-9) return 'no change';
    const mag = unit === 'pp' ? `${(Math.abs(d) * 100).toFixed(1)}pp` : `${Math.round(Math.abs(d))}${unit}`;
    return `${d < 0 ? '▼' : '▲'} ${mag}`;
  };
  const deltaTone = (b: number | null, a: number | null): 'good' | 'bad' | 'flat' => {
    if (b === null || a === null || Math.abs(a - b) < 1e-9) return 'flat';
    return a < b ? 'good' : 'bad';
  };
  const finalRun = patchedRun ?? baselineRun;
  const compareRows = [
    { label: 'Verdict', before: baselineRun?.status ?? '—', after: finalRun?.status ?? '—', change: patchedRun ? (patchedPassed && baselinePassed === false ? 'improved' : 'unchanged') : '—', tone: patchedPassed && baselinePassed === false ? 'good' as const : 'flat' as const },
    { label: 'Error rate', before: fmtPct(metric(baselineRun, ['error_rate'])), after: fmtPct(metric(finalRun, ['error_rate'])), change: delta(metric(baselineRun, ['error_rate']), metric(finalRun, ['error_rate']), 'pp'), tone: deltaTone(metric(baselineRun, ['error_rate']), metric(finalRun, ['error_rate'])) },
    { label: 'p99 latency', before: fmtMs(metric(baselineRun, ['p99_ms'])), after: fmtMs(metric(finalRun, ['p99_ms'])), change: delta(metric(baselineRun, ['p99_ms']), metric(finalRun, ['p99_ms']), 'ms'), tone: deltaTone(metric(baselineRun, ['p99_ms']), metric(finalRun, ['p99_ms'])) },
    { label: 'Time out of SLO', before: fmtSecs(metric(baselineRun, ['seconds_out_of_slo', 'breach_seconds'])), after: fmtSecs(metric(finalRun, ['seconds_out_of_slo', 'breach_seconds'])), change: delta(metric(baselineRun, ['seconds_out_of_slo', 'breach_seconds']), metric(finalRun, ['seconds_out_of_slo', 'breach_seconds']), 's'), tone: deltaTone(metric(baselineRun, ['seconds_out_of_slo', 'breach_seconds']), metric(finalRun, ['seconds_out_of_slo', 'breach_seconds'])) },
    { label: 'Post-fault error rate', before: fmtPct(metric(baselineRun, ['post_fault_error_rate'])), after: fmtPct(metric(finalRun, ['post_fault_error_rate'])), change: delta(metric(baselineRun, ['post_fault_error_rate']), metric(finalRun, ['post_fault_error_rate']), 'pp'), tone: deltaTone(metric(baselineRun, ['post_fault_error_rate']), metric(finalRun, ['post_fault_error_rate'])) },
    { label: 'Recovery', before: fmtSecs(metric(baselineRun, ['recovery_s'])), after: fmtSecs(metric(finalRun, ['recovery_s'])), change: delta(metric(baselineRun, ['recovery_s']), metric(finalRun, ['recovery_s']), 's'), tone: deltaTone(metric(baselineRun, ['recovery_s']), metric(finalRun, ['recovery_s'])) },
  ];
  const diffChanges: any[] = Array.isArray(lastDiff?.changes) ? lastDiff.changes : [];

  /* ---------------------------------------------------------------- */
  /* Executive dashboard derived values                               */
  /* ---------------------------------------------------------------- */
  const readiness = (r: SimulationResult | null): number | null => {
    if (!r) return null;
    const errPp = (r.error_rate || 0) * 100;
    const breach = r.breach_seconds || 0;
    const unrecovered = r.status === 'ok' || r.status === 'degraded' ? 0 : (r.recovery_s || 120);
    return Math.max(0, Math.min(100, Math.round(100 - errPp * 3 - breach * 0.3 - unrecovered * 0.08)));
  };
  const beforeScore = readiness(baselineRun);
  const afterScore = readiness(finalRun);
  const severityCounts = ['critical', 'high', 'medium', 'low'].map((sev) => ({
    sev,
    count: designFindings.filter((f) => String(f.severity || '').toLowerCase() === sev).length,
  }));
  const totalFindings = severityCounts.reduce((n, s) => n + s.count, 0);
  const scenarioEvents: any[] = selectedScenario?.scenario?.events ?? [];
  const scenarioTargets: string[] = Array.isArray(selectedScenario?.target_nodes) ? selectedScenario.target_nodes : [];

  // Shared report fragments, reused by the Report step and by the printable
  // appendix on the Dashboard screen so one PDF contains the whole story.
  const reportHead = () => (
    <div className="report-head">
      <div className="micro">Pre-mortem report · seed 42</div>
      <h1>{design?.name}</h1>
      <p>{scenarioName(selectedScenario)} · {new Date().toLocaleString()}</p>
    </div>
  );

  const reportDetail = () => (
    <>
      <div className="report-banner">
        <div>
          <span className="micro">Original design</span>
          <h3 style={{ color: statusColor(baselineRun?.status) }}>{verdictText(baselineRun)}</h3>
        </div>
        <ArrowRight className="w-5 h-5" />
        <div>
          <span className="micro">{patchedRun ? 'With fixes applied' : 'Final result'}</span>
          <h3 style={{ color: patchedRun ? statusColor(patchedRun.status) : statusColor(baselineRun?.status) }}>
            {verdictText(patchedRun || baselineRun)}
          </h3>
        </div>
        <span className={`status-pill ${patchedPassed === true ? 'success' : patchedPassed === false ? 'error' : ''}`}>
          {patchedRun ? (patchedPassed && baselinePassed === false ? 'fixed' : patchedPassed ? 'still survives' : 'still failing') : reportWithoutFixes ? 'no fixes applied' : 'no fix needed'}
        </span>
      </div>

      <div className="card">
        <h2>Measured impact <span className="chip">same scenario · same seed</span></h2>
        <div className="report-table">
          <div className="report-row head"><span>Metric</span><span>Original</span><span>{patchedRun ? 'With fixes' : 'Final'}</span><span>Change</span></div>
          {compareRows.map((r, i) => (
            <div className="report-row" key={i}>
              <span>{r.label}</span><span>{cellText(r.before)}</span>
              <span><b>{cellText(r.after)}</b></span><span className={`delta ${r.tone}`}>{r.change}</span>
            </div>
          ))}
        </div>
      </div>

      {patchedRun && (
        <Timeline result={patchedRun} compareTo={baselineRun} simTime={simTime}
          onScrub={(t) => { setIsPlaying(false); setSimTime(t); }} isPlaying={isPlaying}
          onPlay={() => setIsPlaying(!isPlaying)} scenario={selectedScenario} />
      )}

      <div className="report-two-col">
        <div className="card">
          <h2>Changes applied <span className="chip">{appliedFixIds.length} fix{appliedFixIds.length === 1 ? '' : 'es'}</span></h2>
          {appliedFixIds.length ? (
            <ul className="report-list">{appliedFixIds.map((id, i) => <li key={i}>{fixes.find((f) => fixIdOf(f) === id)?.recommended_pattern || id}</li>)}</ul>
          ) : <p className="insp-empty">{reportWithoutFixes ? 'No fixes were applied — this is the original design.' : 'No fixes were needed — the original design survived.'}</p>}
        </div>
        <div className="card">
          <h2>Architecture diff <span className="chip">{diffChanges.length} change{diffChanges.length === 1 ? '' : 's'}</span></h2>
          {diffChanges.length ? (
            <div className="diff-table-wrap"><table className="diff-table">
              <thead><tr><th>Op</th><th>Path</th><th>After</th></tr></thead>
              <tbody>{diffChanges.map((c, i) => (
                <tr key={i}><td>{cellText(c.op)}</td><td className="mono-cell">{cellText(c.path)}</td><td className="mono-cell">{cellText(c.value ?? c.after ?? '')}</td></tr>
              ))}</tbody>
            </table></div>
          ) : <p className="insp-empty">No structural changes — behaviour changed through the same topology.</p>}
        </div>
      </div>
    </>
  );

  /* ---------------------------------------------------------------- */
  /* Render                                                           */
  /* ---------------------------------------------------------------- */
  const activeTitle = STEPS.find((s) => s.key === step)?.label ?? '';

  return (
    <div className="app">
      {/* ---------- Sidebar: one step at a time ---------- */}
      <aside className="sidebar">
        <div className="brand"><div className="logo">V</div><span>VENESSA</span></div>
        <div className="micro sidebar-label">Pre-mortem workflow</div>
        <nav className="side-nav stepper" aria-label="Workflow steps">
          {STEPS.map((s, i) => {
            const state = stepState(s.key);
            return (
              <button key={s.key} type="button"
                className={`nav-item step-item ${state}`}
                disabled={state === 'locked'}
                onClick={() => goToStep(s.key)}>
                <span className="step-marker">
                  {state === 'done' ? <Check className="w-3 h-3" /> : state === 'active' ? <ArrowRight className="w-3 h-3" /> : <span>{i + 1}</span>}
                </span>
                <span className="step-copy"><b>{s.label}</b><small>{s.hint}</small></span>
              </button>
            );
          })}
        </nav>
        <div className="sidebar-bottom context-card">
          <div className="micro">Design in scope</div>
          <b className="model-name">{design?.name || 'No design chosen'}</b>
          <div className="sidebar-stats">
            <span>{design?.nodes?.length ?? 0} nodes</span>
            <span>{design?.edges?.length ?? 0} edges</span>
          </div>
          {patchedDesign && <span className="status-pill success">fixes applied</span>}
          {!patchedDesign && design && <span className="status-pill">original design</span>}
        </div>
      </aside>

      {/* ---------- Top bar: only stage context + primary action ---------- */}
      <header className="top">
        <div className="command-group">
          <span className="stage-index">{String(stepIndex(step) + 1).padStart(2, '0')} / 06</span>
          <div><small className="micro">Stage</small><b className="stage-title">{activeTitle}</b></div>
        </div>
        <div className="spacer" />
        {design && <span className="status-pill">{design.name}</span>}
        {selectedScenario && <span className="status-pill accent">{scenarioName(selectedScenario)}</span>}
        {step === 'run' && !baselineRun && (
          <button type="button" className="run" onClick={runBaseline} disabled={running || !selectedScenario}>
            {running ? <RefreshCw className="w-3.5 h-3.5 animate-spin" /> : <Play className="w-3.5 h-3.5" />}
            {running ? 'Running…' : 'Run stress test'}
          </button>
        )}
        {(step === 'report' || step === 'dashboard') && (
          <>
            <button type="button" className="action-btn" onClick={exportYaml}><Download className="w-3.5 h-3.5" /> Export final YAML</button>
            <button type="button" className="run" onClick={() => window.print()}><FileText className="w-3.5 h-3.5" /> Save as PDF</button>
          </>
        )}
      </header>

      <main className="main stage-main">
        {/* ============ STEP 1 — DESIGN ============ */}
        {step === 'design' && (
          <div className="stage-content">
            <div className="stage-intro">
              <h1>Start with a design</h1>
              <p>Pick a reference architecture, describe a new one, or import a spec. Nothing runs until you say so.</p>
            </div>

            {!design ? (
              <div className="choice-grid">
                <button type="button" className="choice-card" onClick={() => document.getElementById('template-list')?.scrollIntoView({ behavior: 'smooth' })}>
                  <span className="choice-icon"><Layers className="w-5 h-5" /></span>
                  <h3>Use an existing design</h3>
                  <p>Choose one of the bundled reference architectures and stress-test it immediately.</p>
                  <span className="choice-cta">Browse templates <ChevronRight className="w-3.5 h-3.5" /></span>
                </button>
                <button type="button" className="choice-card accent-choice" onClick={() => { setPromptError(null); setDrawerOpen(true); }}>
                  <span className="choice-icon"><Sparkles className="w-5 h-5" /></span>
                  <h3>Create a new design</h3>
                  <p>Describe your system in plain English. Gemini compiles it into a simulation-ready model.</p>
                  <span className="choice-cta">Describe architecture <ChevronRight className="w-3.5 h-3.5" /></span>
                </button>
                <button type="button" className="choice-card" onClick={() => fileRef.current?.click()}>
                  <span className="choice-icon"><Upload className="w-5 h-5" /></span>
                  <h3>Import a spec</h3>
                  <p>Already have a YAML or JSON architecture file? Load it as-is.</p>
                  <span className="choice-cta">Choose file <ChevronRight className="w-3.5 h-3.5" /></span>
                </button>
                <input ref={fileRef} type="file" accept=".yaml,.yml,.json" onChange={importFile} style={{ display: 'none' }} />
              </div>
            ) : (
              <>
                <div className="design-confirm">
                  <div>
                    <span className={`status-pill ${designSource === 'generated' ? 'accent' : ''}`}>
                      {designSource === 'generated' ? 'generated by Gemini' : designSource === 'import' ? 'imported spec' : 'reference template'}
                    </span>
                    <h2>{design.name}</h2>
                    <p>{design.nodes?.length ?? 0} nodes · {design.edges?.length ?? 0} edges · {designFindings.length} static weak spots</p>
                  </div>
                  <button type="button" className="ghost" onClick={startOver}><X className="w-3.5 h-3.5" /> Change design</button>
                </div>
                {warnings.length > 0 && <div className="insight-card warn">{warnings.map((w, i) => <span key={i}>{cellText(w)}</span>)}</div>}
                {designFindings.length > 0 && (
                  <div className="card findings-card">
                    <h2>Static weak spots <span className="chip">{designFindings.length}</span></h2>
                    <div className="finding-grid">
                      {designFindings.slice(0, 6).map((f, i) => (
                        <div key={i} className="finding"><span className={`status-pill ${f.severity}`}>{f.severity}</span><b>{f.target}</b><span>{f.message}</span></div>
                      ))}
                    </div>
                  </div>
                )}
                <ArchitectureGraph arch={design} result={null} simTime={0}
                  selectedNodeId={selectedNodeId} onSelectNode={setSelectedNodeId} label="Design preview" />
                <div className="stage-actions">
                  <span className="micro">Next: Gemini proposes failures specific to this design.</span>
                  <button type="button" className="run" onClick={advanceToScenarios}>
                    Continue to stress tests <ArrowRight className="w-3.5 h-3.5" />
                  </button>
                </div>
              </>
            )}

            {!design && models.length > 0 && (
              <div className="card template-card" id="template-list">
                <h2>Reference architectures <span className="chip">{models.length}</span></h2>
                <div className="template-list">
                  {models.map((m) => (
                    <button key={m.id} type="button"
                      className={`template-row ${templateId === m.id ? 'sel' : ''}`}
                      onClick={() => loadTemplate(m.id)}>
                      <span className="radio">{templateId === m.id && <Check className="w-3 h-3" />}</span>
                      <span><b>{m.name || m.id}</b><small>{m.nodes} nodes · {m.edges} edges</small></span>
                      <ChevronRight className="w-3.5 h-3.5" />
                    </button>
                  ))}
                </div>
              </div>
            )}
            {promptError && <div className="insight-card error" role="alert">{promptError}</div>}
          </div>
        )}

        {/* ============ STEP 2 — STRESS TESTS ============ */}
        {step === 'scenarios' && (
          <div className="stage-content">
            <div className="stage-intro">
              <h1>Choose a stress test</h1>
              <p>These failure scenarios were generated for <b>{design?.name}</b>. Select one to run it.</p>
            </div>

            <div className="card">
              <h2>
                Adversarial scenarios <span className="chip">{scenarios.length} generated</span>
                <button type="button" className="tiny-btn" onClick={generateScenarios} disabled={generatingScenarios}>
                  <RefreshCw className={`w-3 h-3 ${generatingScenarios ? 'animate-spin' : ''}`} />
                  {generatingScenarios ? 'Generating…' : 'Regenerate'}
                </button>
              </h2>

              {generatingScenarios && (
                <div className="thinking">
                  <div className="thinking-step">Reading architecture fingerprint…</div>
                  <div className="thinking-step">Searching for metastable failure paths…</div>
                  <div className="thinking-step">Validating adversarial scenarios…</div>
                </div>
              )}
              {scenarioError && <div className="insight-card error">{scenarioError}</div>}
              {warnings.length > 0 && <div className="insight-card warn">{warnings.map((w, i) => <span key={i}>{cellText(w)}</span>)}</div>}

              <div className="scenario-grid">
                {scenarios.map((item: any, i: number) => {
                  const sc = item.scenario || {};
                  const isSel = selectedScenario?.scenario_id === item.scenario_id;
                  const targets: string[] = Array.isArray(item.target_nodes) ? item.target_nodes : [];
                  const events: any[] = sc.events || [];
                  return (
                    <button key={item.scenario_id || i} type="button"
                      className={`scenario-choice ${isSel ? 'sel' : ''}`}
                      onClick={() => setSelectedScenario(item)}>
                      <div className="scenario-top">
                        <span className="status-pill accent">test {String(i + 1).padStart(2, '0')}</span>
                        {item.source === 'fallback' && <span className="status-pill warn">fallback</span>}
                        {isSel && <span className="status-pill success"><Check className="w-3 h-3" /> selected</span>}
                      </div>
                      <h3>{sc.name || `Scenario ${i + 1}`}</h3>
                      <p>{item.hypothesis || sc.description || item.rationale || 'Adversarial failure hypothesis'}</p>
                      <div className="scenario-meta">
                        <span>{events.length} injected event{events.length === 1 ? '' : 's'}</span>
                        <span>{sc.duration_s ?? '—'}s window</span>
                        {targets.length > 0 && <span>targets · {targets.join(', ')}</span>}
                      </div>
                    </button>
                  );
                })}
              </div>
              {!generatingScenarios && scenarios.length === 0 && (
                <p className="insp-empty">No scenarios available. Use Regenerate to try again.</p>
              )}
            </div>

            {selectedScenario && (
              <div className="card">
                <h2>What this test injects <span className="chip">{scenarioName(selectedScenario)}</span></h2>
                <ul className="events">
                  {(selectedScenario.scenario?.traffic?.ramp ?? []).map((r: any, i: number) => (
                    <li key={`r${i}`}><time>{fmtTime(r.at_s)}</time><span className="tag load">load</span><span>Traffic ramps to {r.multiplier}×{r.ramp_s ? ` over ${r.ramp_s}s` : ''}</span></li>
                  ))}
                  {(selectedScenario.scenario?.events ?? []).map((e: any, i: number) => (
                    <li key={`e${i}`}><time>{fmtTime(e.at_s)}</time><span className={`tag ${e.action}`}>{e.action}</span><span>{describeEvent(e)}</span></li>
                  ))}
                </ul>
              </div>
            )}

            <div className="stage-actions">
              <button type="button" className="ghost" onClick={() => goToStep('design')}><ArrowLeft className="w-3.5 h-3.5" /> Back to design</button>
              <button type="button" className="run" onClick={() => goToStep('run')} disabled={!selectedScenario}>
                Continue to run <ArrowRight className="w-3.5 h-3.5" />
              </button>
            </div>
          </div>
        )}

        {/* ============ STEP 3 — RUN ============ */}
        {step === 'run' && (
          <div className="stage-content">
            <div className="stage-intro">
              <h1>Run the stress test</h1>
              <p>This simulates the <b>original design</b> against <b>{scenarioName(selectedScenario)}</b>. Fixes are not applied yet.</p>
            </div>

            {runError && <div className="insight-card error" role="alert">{runError}</div>}
            {warnings.length > 0 && <div className="insight-card warn">{warnings.map((w, i) => <span key={i}>{cellText(w)}</span>)}</div>}

            {!baselineRun ? (
              <div className="card pre-run-card">
                <div className="pre-run-row"><span>Design</span><b>{design?.name}</b><small>{design?.nodes?.length} nodes · {design?.edges?.length} edges</small></div>
                <div className="pre-run-row"><span>Scenario</span><b>{scenarioName(selectedScenario)}</b><small>{(selectedScenario?.scenario?.events ?? []).length} injected failures</small></div>
                <div className="pre-run-row"><span>Seed</span><b>42</b><small>Deterministic replay</small></div>
                <button type="button" className="run wide" onClick={runBaseline} disabled={running}>
                  {running ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
                  {running ? 'Simulating failure scenario…' : 'Run stress test'}
                </button>
              </div>
            ) : (
              <>
                <div className="version-banner">
                  <span className="status-pill">original design · no fixes</span>
                  <span className="micro">This is your design exactly as provided.</span>
                </div>
                <Verdict result={baselineRun} label="Result · original design" />
                <ArchitectureGraph arch={design} result={baselineRun} simTime={simTime}
                  selectedNodeId={selectedNodeId} onSelectNode={setSelectedNodeId} label="Original design · live failure state" />
                <Timeline result={baselineRun} simTime={simTime} label="original design"
                  onScrub={(t) => { setIsPlaying(false); setSimTime(t); }} isPlaying={isPlaying}
                  onPlay={() => setIsPlaying(!isPlaying)} scenario={selectedScenario} />
                <FailureStory result={baselineRun} scenario={selectedScenario}
                  simTime={simTime} onJump={(t) => { setIsPlaying(false); setSimTime(t); }} />
                <div className="stage-actions">
                  <button type="button" className="ghost" onClick={() => goToStep('scenarios')}><ArrowLeft className="w-3.5 h-3.5" /> Choose a different test</button>
                  {baselinePassed === false
                    ? <button type="button" className="run" onClick={() => goToStep('fixes')}>Review fix proposals <ArrowRight className="w-3.5 h-3.5" /></button>
                    : <button type="button" className="run" onClick={() => goToStep('report')}>View report <ArrowRight className="w-3.5 h-3.5" /></button>}
                </div>
              </>
            )}
          </div>
        )}

        {/* ============ STEP 4 — FIXES ============ */}
        {step === 'fixes' && (
          <div className="stage-content">
            <div className="stage-intro">
              <h1>Approve possible fixes</h1>
              <p>Every option below was applied and re-simulated against the same scenario and seed. Only measured improvements can be selected.</p>
            </div>
            {fixError && <div className="insight-card error" role="alert">{fixError}</div>}
            {warnings.length > 0 && <div className="insight-card warn">{warnings.map((w, i) => <span key={i}>{cellText(w)}</span>)}</div>}

            <div className="card diagnosis-card">
              <h2>Why it failed</h2>
              <p>{baselineRun?.cascade?.[0]?.detail || baselineRun?.cascade?.[0]?.what || 'The selected stress test breached the SLO envelope.'}</p>
              <div className="micro">{designFindings.length} static weak spots · {baselineRun?.cascade?.length ?? 0} cascade steps</div>
            </div>

            {fixes.length === 0 && !fixError && <p className="insp-empty">No fix proposals came back for this run.</p>}
            <div className="fix-grid">
              {rankFixes(fixes).map((fix, i) => {
                const id = fixIdOf(fix);
                const valid = fix.valid !== false;
                const v: FixVerification | undefined = fix.verification;
                const verified = v?.verified === true && v.classification !== 'worse';
                const selectable = valid && verified && !applying;
                const isSel = selectedFixIds.has(id);
                const ops: any[] = Array.isArray(fix.patch?.ops) ? fix.patch.ops : [];
                const d = v?.deltas || {};
                const pct = (n: number) => `${n >= 0 ? '+' : ''}${(n * 100).toFixed(2)}pp`;
                return (
                  <button key={id || i} type="button" disabled={!selectable}
                    className={`fix-choice ${isSel ? 'sel' : ''} ${selectable ? '' : 'invalid'}`}
                    onClick={() => toggleFix(id)}>
                    <div className="fix-choice-top">
                      <span className={`checkbox ${isSel ? 'on' : ''}`}>{isSel && <Check className="w-3 h-3" />}</span>
                      <span className="micro">option {String(i + 1).padStart(2, '0')}</span>
                      {v && <span className={`status-pill ${verified ? 'success' : v.classification === 'worse' ? 'error' : ''}`}>
                        {verified ? 'verified'
                          : v.classification === 'worse' ? 'made worse'
                            : v.classification === 'improved' ? 'improved, but rejected'
                              : 'no improvement'}
                      </span>}
                      {!valid && <span className="status-pill error">invalid</span>}
                    </div>
                    <h3>{fix.recommended_pattern || fix.diagnosis || `Fix ${i + 1}`}</h3>
                    <p>{fix.rationale || fix.diagnosis || 'Gemini recommendation'}</p>
                    <div className="fix-choice-meta">
                      <span>root · <b>{fix.root_cause_node || '—'}</b></span>
                      <span>{ops.length} patch op{ops.length === 1 ? '' : 's'}</span>
                    </div>
                    {v && (
                      <div className="fix-measured">
                        <span><small>Measured error</small><b className={v.better?.error_rate ? 'good' : 'bad'}>{pct(d.error_rate || 0)}</b></span>
                        <span><small>Out of SLO</small><b className={v.better?.seconds_out_of_slo ? 'good' : 'bad'}>{Math.round(d.seconds_out_of_slo || 0)}s</b></span>
                        <span><small>Post-fault</small><b className={v.better?.post_fault_error_rate ? 'good' : 'bad'}>{pct(d.post_fault_error_rate || 0)}</b></span>
                      </div>
                    )}
                    {v?.candidate && (
                      <div className={`micro post-fix-note ${v.candidate.slo_breached ? 'breach' : 'clear'}`}>
                        {v.candidate.slo_breached
                          ? `after fix: still in SLO breach · ${Math.round(v.candidate.seconds_out_of_slo || 0)}s out of SLO`
                          : 'after fix: inside SLO'}
                      </div>
                    )}
                    {fix.predicted_impact && <div className="micro">predicted · {cellText(fix.predicted_impact)}</div>}
                    {fix.rejected_because && <div className="error-text">{cellText(fix.rejected_because)}</div>}
                    {!valid && <div className="error-text">{(fix.validation_errors || []).map(cellText).join(' · ') || 'Patch validation failed'}</div>}
                  </button>
                );
              })}
            </div>

            <div className="stage-actions">
              <button type="button" className="ghost" onClick={() => goToStep('run')}><ArrowLeft className="w-3.5 h-3.5" /> Back to result</button>
              <div className="action-cluster">
                <span className="micro">{selectedFixIds.size} selected</span>
                <button type="button" className="ghost" onClick={() => setSelectedFixIds(new Set(fixes.filter((f) => f.valid !== false).map(fixIdOf)))} disabled={!fixes.length}>Select all</button>
                <button type="button" className="ghost" onClick={() => { setReportWithoutFixes(true); setStep('report'); }}>Report without fixes</button>
                <button type="button" className="run" onClick={applyFixes} disabled={!selectedFixes.length || applying}>
                  {applying ? <RefreshCw className="w-3.5 h-3.5 animate-spin" /> : <Play className="w-3.5 h-3.5" />}
                  {applying ? 'Applying & re-simulating…' : 'Apply selected & compare'}
                </button>
              </div>
            </div>
          </div>
        )}

        {/* ============ STEP 5 — REPORT ============ */}
        {step === 'report' && (
          <div className="stage-content report-content">
            {reportHead()}
            {reportDetail()}
            <div className="stage-actions no-print">
              <button type="button" className="ghost" onClick={() => goToStep(baselinePassed === false ? 'fixes' : 'run')}>
                <ArrowLeft className="w-3.5 h-3.5" /> Back
              </button>
              <button type="button" className="run" onClick={() => goToStep('dashboard')}>
                View executive dashboard <ArrowRight className="w-3.5 h-3.5" />
              </button>
            </div>
          </div>
        )}

        {/* ============ STEP 6 — DASHBOARD ============ */}
        {step === 'dashboard' && (
          <div className="stage-content dashboard-content">
            <div className="stage-intro">
              <h1>Pre-mortem dashboard</h1>
              <p>Everything you need to decide, on one screen. This is the verified outcome for <b>{design?.name}</b>.</p>
            </div>

            {/* headline: readiness + verdict + scenario */}
            <div className="dash-hero">
              <div className="card dash-scores">
                <h2>Readiness score <span className="chip">0 — 100</span></h2>
                <div className="gauge-row">
                  <ScoreGauge score={beforeScore} label="Before fixes" caption="original design" />
                  <ArrowRight className="w-5 h-5 gauge-arrow" />
                  <ScoreGauge score={afterScore} label="After fixes" caption={patchedRun ? 'verified re-run' : 'same design'} />
                </div>
                {beforeScore !== null && afterScore !== null && afterScore !== beforeScore && (
                  <div className={`score-delta ${afterScore > beforeScore ? 'up' : 'down'}`}>
                    <TrendingUp className="w-3.5 h-3.5" />
                    {afterScore > beforeScore ? '+' : ''}{afterScore - beforeScore} points
                    {afterScore > beforeScore ? ' of resilience recovered' : ' net change'}
                  </div>
                )}
              </div>

              <div className="card dash-verdict">
                <h2>Final verdict</h2>
                <div className="dash-verdict-main">
                  <span className="dash-verdict-dot" style={{ background: statusColor(finalRun?.status) }} />
                  <div>
                    <h3>{verdictText(finalRun)}</h3>
                    <p>{finalRun?.slo_breached
                      ? `SLO breached for ${finalRun.breach_seconds ?? 0}s in the final run.`
                      : 'Stayed inside the target SLO envelope.'}</p>
                  </div>
                </div>
                <div className="dash-verdict-track">
                  <span className={`status-pill ${patchedPassed === false ? 'error' : patchedPassed === true ? 'success' : ''}`}>
                    {patchedRun ? (patchedPassed && baselinePassed === false ? 'fixed after re-simulation' : patchedPassed ? 'survived both runs' : 'still failing') : 'no fix needed'}
                  </span>
                  <span className="micro">seed 42 · deterministic</span>
                </div>
              </div>

              <div className="card dash-scenario">
                <h2><Target className="w-3.5 h-3.5" /> Stress test applied</h2>
                <h3>{scenarioName(selectedScenario)}</h3>
                <p>{selectedScenario?.hypothesis || selectedScenario?.scenario?.description || 'Adversarial failure hypothesis'}</p>
                <div className="dash-scenario-stats">
                  <span><b>{scenarioEvents.length}</b> events injected</span>
                  <span><b>{selectedScenario?.scenario?.duration_s ?? '—'}s</b> window</span>
                  <span><b>{scenarioTargets.length}</b> target{scenarioTargets.length === 1 ? '' : 's'}</span>
                </div>
                {scenarioTargets.length > 0 && <div className="micro">targets · {scenarioTargets.join(', ')}</div>}
              </div>
            </div>

            {/* KPI cards */}
            <div className="card">
              <h2><Activity className="w-3.5 h-3.5" /> Key metrics <span className="chip">before → after</span></h2>
              <div className="kpi-grid">
                {[
                  { label: 'Peak error rate', before: fmtPct(metric(baselineRun, ['error_rate'])), after: fmtPct(metric(finalRun, ['error_rate'])), change: delta(metric(baselineRun, ['error_rate']), metric(finalRun, ['error_rate']), 'pp'), tone: deltaTone(metric(baselineRun, ['error_rate']), metric(finalRun, ['error_rate'])) },
                  { label: 'p99 latency', before: fmtMs(metric(baselineRun, ['p99_ms'])), after: fmtMs(metric(finalRun, ['p99_ms'])), change: delta(metric(baselineRun, ['p99_ms']), metric(finalRun, ['p99_ms']), 'ms'), tone: deltaTone(metric(baselineRun, ['p99_ms']), metric(finalRun, ['p99_ms'])) },
                  { label: 'Time out of SLO', before: fmtSecs(metric(baselineRun, ['seconds_out_of_slo', 'breach_seconds'])), after: fmtSecs(metric(finalRun, ['seconds_out_of_slo', 'breach_seconds'])), change: delta(metric(baselineRun, ['seconds_out_of_slo', 'breach_seconds']), metric(finalRun, ['seconds_out_of_slo', 'breach_seconds']), 's'), tone: deltaTone(metric(baselineRun, ['seconds_out_of_slo', 'breach_seconds']), metric(finalRun, ['seconds_out_of_slo', 'breach_seconds'])) },
                  { label: 'Post-fault error rate', before: fmtPct(metric(baselineRun, ['post_fault_error_rate'])), after: fmtPct(metric(finalRun, ['post_fault_error_rate'])), change: delta(metric(baselineRun, ['post_fault_error_rate']), metric(finalRun, ['post_fault_error_rate']), 'pp'), tone: deltaTone(metric(baselineRun, ['post_fault_error_rate']), metric(finalRun, ['post_fault_error_rate'])) },
                  { label: 'Recovery', before: fmtSecs(metric(baselineRun, ['recovery_s'])), after: fmtSecs(metric(finalRun, ['recovery_s'])), change: delta(metric(baselineRun, ['recovery_s']), metric(finalRun, ['recovery_s']), 's'), tone: deltaTone(metric(baselineRun, ['recovery_s']), metric(finalRun, ['recovery_s'])) },
                ].map((k) => (
                  <div className="kpi-card" key={k.label}>
                    <span className="micro">{k.label}</span>
                    <b>{k.after}</b>
                    <div className="kpi-card-foot"><span>was {k.before}</span><span className={`delta ${k.tone}`}>{k.change}</span></div>
                  </div>
                ))}
              </div>
            </div>

            {/* risk posture + shape of the system */}
            <div className="report-two-col">
              <div className="card">
                <h2><Gauge className="w-3.5 h-3.5" /> Risk posture <span className="chip">{totalFindings} static weak spots</span></h2>
                {totalFindings > 0 ? (
                  <div className="risk-bars">
                    {severityCounts.map((s) => (
                      <div className="risk-row" key={s.sev}>
                        <span className={`status-pill ${s.sev}`}>{s.sev}</span>
                        <div className="risk-bar"><i style={{ width: `${(s.count / totalFindings) * 100}%` }} /></div>
                        <b>{s.count}</b>
                      </div>
                    ))}
                  </div>
                ) : <p className="insp-empty">No static weak spots were detected in this design.</p>}
              </div>
              <div className="card">
                <h2><Layers className="w-3.5 h-3.5" /> System at a glance</h2>
                <div className="dash-facts">
                  <div><span>Architecture</span><b>{design?.name || '—'}</b></div>
                  <div><span>Nodes / edges</span><b>{design?.nodes?.length ?? 0} / {design?.edges?.length ?? 0}</b></div>
                  <div><span>Source</span><b>{designSource === 'generated' ? 'Gemini-generated' : designSource === 'import' ? 'Imported spec' : 'Reference template'}</b></div>
                  <div><span>Fixes applied</span><b>{appliedFixIds.length}</b></div>
                  <div><span>Structural changes</span><b>{diffChanges.length}</b></div>
                </div>
              </div>
            </div>

            {/* fixes + mini evidence */}
            <div className="card">
              <h2><Check className="w-3.5 h-3.5" /> Fix summary <span className="chip">{appliedFixIds.length} approved</span></h2>
              {appliedFixIds.length ? (
                <div className="fix-summary-grid">
                  {appliedFixIds.map((id, i) => {
                    const fix = fixes.find((f) => fixIdOf(f) === id);
                    return (
                      <div className="fix-summary" key={i}>
                        <span className="step-marker done"><Check className="w-3 h-3" /></span>
                        <div><b>{fix?.recommended_pattern || id}</b><span className="micro">root cause · {fix?.root_cause_node || '—'}</span></div>
                      </div>
                    );
                  })}
                </div>
              ) : (
                <p className="insp-empty">{reportWithoutFixes ? 'No fixes were applied — this is the original design.' : 'The original design survived, so no fixes were needed.'}</p>
              )}
              {patchedRun && (
                <Timeline result={patchedRun} compareTo={baselineRun} simTime={simTime}
                  onScrub={(t) => { setIsPlaying(false); setSimTime(t); }} isPlaying={isPlaying}
                  onPlay={() => setIsPlaying(!isPlaying)} scenario={selectedScenario} />
              )}
            </div>

            <div className="stage-actions no-print">
              <button type="button" className="ghost" onClick={startOver}><Plus className="w-3.5 h-3.5" /> Start a new pre-mortem</button>
              <div className="action-cluster">
                <button type="button" className="ghost" onClick={() => goToStep('report')}><ArrowLeft className="w-3.5 h-3.5" /> Detailed report</button>
                <button type="button" className="action-btn" onClick={exportYaml}><Download className="w-3.5 h-3.5" /> Export final YAML</button>
                <button type="button" className="run" onClick={() => window.print()}><FileText className="w-3.5 h-3.5" /> Save dashboard + report as PDF</button>
              </div>
            </div>

            {/* Print-only appendix: the detailed report follows the dashboard. */}
            <div className="print-report-appendix report-content">
              {reportHead()}
              {reportDetail()}
            </div>
          </div>
        )}
      </main>

      {/* ---------- Create-by-prompt drawer ---------- */}
      {drawerOpen && (
        <div className="drawer-backdrop" onClick={() => setDrawerOpen(false)}>
          <div className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <h3><Sparkles className="w-4 h-4 text-accent" /> Describe a new architecture</h3>
              <button type="button" className="drawer-close" onClick={() => setDrawerOpen(false)}><X className="w-4 h-4" /></button>
            </div>
            <div className="drawer-body">
              <label className="field-label">Plain-language architecture brief</label>
              <textarea className="editor-textarea" style={{ height: 260 }}
                placeholder={'e.g. A 3-tier web app: ingress load balancer → API service (4 replicas) → Redis cache and Postgres database, with an external email API.'}
                value={promptText} onChange={(e) => setPromptText(e.target.value)} />
              {thinkingSteps.length > 0 && <div className="thinking">{thinkingSteps.map((s, i) => <div className="thinking-step" key={i}>{s}</div>)}</div>}
              {promptError && <div className="insight-card error" role="alert">{promptError}</div>}
              <div className="dashed" />
              <div className="micro section-label">Tips for a good brief</div>
              <ul className="tip-list">
                <li>Name each hop and its direction (gateway → service → database).</li>
                <li>Include replica counts, concurrency, timeouts and retry behaviour.</li>
                <li>Mention caches, external dependencies and startup dependencies.</li>
              </ul>
            </div>
            <div className="drawer-footer">
              <button type="button" className="action-btn" onClick={() => setDrawerOpen(false)}>Cancel</button>
              <button type="button" className="run" onClick={createFromPrompt} disabled={generatingDesign || !promptText.trim()}>
                {generatingDesign ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Sparkles className="w-4 h-4" />}
                {generatingDesign ? 'Generating…' : 'Generate with Gemini'}
              </button>
            </div>
          </div>
        </div>
      )}

      {(running || applying) && (
        <div className="sim-overlay" role="status" aria-live="polite">
          <div className="sim-card">
            <div className="sim-spinner" />
            <strong>{applying ? 'Applying fixes and re-simulating…' : remediating ? 'Gemini is proposing fixes…' : 'Simulating failure scenario…'}</strong>
            <span>{scenarioName(selectedScenario)} · {design?.name}</span>
          </div>
        </div>
      )}

      {showAlphaNotice && (
        <div className="alpha-overlay" role="dialog" aria-modal="true" aria-labelledby="alpha-title">
          <div className="alpha-card">
            <div className="alpha-top">
              <span className="alpha-mark"><Sparkles className="w-4 h-4" /></span>
              <div>
                <div className="micro">Early access · Alpha</div>
                <h2 id="alpha-title">Venessa is very early stage</h2>
              </div>
            </div>
            <p>
              This is an alpha build. The core workflow is in place and working end to end,
              but it is still waiting to be expanded on. Describe an architecture, let Gemini
              attack it, simulate the fallout, and try the proposed fixes.
            </p>
            <ul className="alpha-list">
              <li>Create → simulate → fix → compare works today.</li>
              <li>Expect rough edges, evolving AI output, and a limited set of failure models.</li>
              <li>More depth is coming: richer topologies, saved workspaces, and deeper analysis.</li>
            </ul>
            <div className="alpha-actions">
              <button type="button" className="run" onClick={dismissAlphaNotice}>Let&apos;s explore</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
