import { Finding, SimulationResult } from '../types';

export type FixKind =
  | 'replicas'
  | 'concurrency'
  | 'retry'
  | 'timeout'
  | 'read_replicas'
  | 'rate_limit'
  | 'startup_relax'
  | 'health_check';

export interface RecommendedFix {
  id: string;
  title: string;
  why: string;
  severity: 'critical' | 'high' | 'medium' | 'low';
  target: string;
  kind: FixKind;
  dep?: string;
  /** Scale factor computed from the observed saturation, used by replicas/concurrency fixes. */
  factor?: number;
}

const fmt = (s: number) => {
  const m = Math.floor(s / 60);
  return `${String(m).padStart(2, '0')}:${String(Math.round(s % 60)).padStart(2, '0')}`;
};

const clone = (arch: any) => JSON.parse(JSON.stringify(arch));

const errCount = (node: string, result: SimulationResult) =>
  Object.values(result.node_errors?.[node] ?? {}).reduce((a, b) => a + b, 0);

const nodeOf = (target: string) => {
  const parts = target.split('->');
  return (target.includes('->') ? parts[parts.length - 1] : target).trim();
};

function evidence(target: string, result: SimulationResult): string {
  const node = nodeOf(target);
  const bits: string[] = [];
  const errors = errCount(node, result);
  if (errors) bits.push(`${errors.toLocaleString()} errors at ${node}`);
  const peak = result.nodes?.find((n: any) => n.id === node)?.peak_util ?? 0;
  if (peak) bits.push(`peak utilization ${Math.round(peak * 100)}%`);
  const events = (result.cascade ?? [])
    .filter((c) => c.node === node)
    .slice(0, 2)
    .map((c) => `${fmt(c.t)} ${c.detail || c.what}`);
  bits.push(...events);
  return bits.join(' · ') || `Implicated in the ${result.status} run`;
}

function implicated(node: string, result: SimulationResult): boolean {
  if (errCount(node, result) > 0) return true;
  if ((result.cascade ?? []).some((c) => c.node === node)) return true;
  if ((result.nodes ?? []).some((n: any) => n.id === node && n.peak_util >= 0.9)) return true;
  return Object.keys(result.client_errors ?? {}).some((r) => r.includes(node));
}

/** How much capacity to add so the node lands near ~75% utilization after the fix. */
const scaleFactor = (node: string, result: SimulationResult) => {
  const peak = result.nodes?.find((n: any) => n.id === node)?.peak_util ?? 1;
  return Math.max(1.5, Math.min(4, Math.ceil((peak / 0.75) * 2) / 2));
};

function fromRule(f: Finding, arch: any, result: SimulationResult): Omit<RecommendedFix, 'id' | 'why'> | null {
  const target = f.target;
  const node = nodeOf(target);
  const base = { target, severity: f.severity };

  if (target.includes('->')) {
    if (/retry/.test(f.rule)) {
      return { ...base, kind: 'retry', title: `Add exponential backoff with jitter on ${target}` };
    }
    if (/timeout|pool/.test(f.rule)) {
      return { ...base, kind: 'timeout', title: `Tighten the deadline on ${target}` };
    }
  }

  switch (f.rule) {
    case 'spof':
    case 'over_capacity':
    case 'low_headroom':
      return { ...base, kind: 'replicas', factor: scaleFactor(node, result), title: `Scale ${node} to absorb the observed load` };
    case 'cold_cache_stampede':
      return { ...base, kind: 'read_replicas', title: `Offload cache-miss reads from ${node}` };
    case 'retry_no_backoff':
    case 'retry_no_jitter':
    case 'retry_amplification':
      return { ...base, kind: 'retry', title: `Stop retry storms on ${node}` };
    case 'rate_limit_baseline':
    case 'rate_limit_cold_cache':
      return { ...base, kind: 'rate_limit', title: `Raise the rate limit on ${node}` };
    case 'replicas_unused':
      return { ...base, kind: 'read_replicas', title: `Actually use ${node}'s read replicas` };
    case 'boot_needs_external': {
      const ext = (arch.nodes ?? []).find((n: any) => n.id === node)?.startup?.requires
        ?.find((r: string) => (arch.nodes ?? []).find((n: any) => n.id === r)?.type === 'external');
      return ext ? { ...base, kind: 'startup_relax', dep: ext, title: `Let ${node} boot without ${ext}` } : null;
    }
    case 'startup_cycle': {
      const ids = target.split('->').map((s) => s.trim());
      return ids.length > 1
        ? { ...base, target: ids[0], kind: 'startup_relax', dep: ids[1], title: `Break the startup cycle at ${ids[0]}` }
        : null;
    }
    case 'no_health_check':
    case 'deep_health_check':
      return { ...base, kind: 'health_check', title: `Use a shallow health check on ${node}` };
    case 'shard_hotspot':
      return { ...base, kind: 'replicas', factor: scaleFactor(node, result), title: `Add capacity to hot shard ${node}` };
    case 'timeout_inversion':
    case 'pool_exceeds_capacity':
      return { ...base, kind: 'timeout', title: `Propagate deadlines through ${node}` };
    default:
      return null;
  }
}

/** Build a short, evidence-driven fix list for the run that just finished. */
export function recommendFixes(arch: any, result: SimulationResult, findings: Finding[]): RecommendedFix[] {
  const out: RecommendedFix[] = [];
  const seen = new Set<string>();
  const byTargetAndKind = new Set<string>();

  const add = (fix: RecommendedFix) => {
    const shape = `${fix.kind}:${fix.target}`;
    if (seen.has(fix.id) || byTargetAndKind.has(shape)) return;
    seen.add(fix.id);
    byTargetAndKind.add(shape);
    out.push(fix);
  };

  // Only surface static weaknesses that this run actually exercised.
  for (const f of findings) {
    const node = nodeOf(f.target);
    if (!implicated(node, result)) continue;
    const mapped = fromRule(f, arch, result);
    if (!mapped) continue;
    add({ ...mapped, id: `${f.rule}:${f.target}`, why: evidence(f.target, result) });
  }

  // Saturation observed in the run, even when no static rule fired.
  for (const n of result.nodes ?? []) {
    if (n.peak_util < 0.95 || seen.has(`util:${n.id}`)) continue;
    const isService = (arch.nodes ?? []).find((a: any) => a.id === n.id)?.type === 'service';
    add({
      id: `util:${n.id}`,
      title: isService ? `Add a replica of ${n.id}` : `Give ${n.id} more concurrency`,
      why: `Peak utilization hit ${Math.round(n.peak_util * 100)}% with ${n.max_queued} requests queued.`,
      severity: n.peak_util >= 1 ? 'critical' : 'high',
      target: n.id,
      kind: isService ? 'replicas' : 'concurrency',
      factor: scaleFactor(n.id, result),
    });
  }

  // Failures that left requests hanging or rejected at the edge.
  for (const [reason, count] of Object.entries(result.client_errors ?? {})) {
    if (!count) continue;
    if (reason.endsWith(':queue_full') || reason.endsWith(':dropped')) {
      const node = reason.split(':')[0];
      add({
        id: `saturation:${node}`,
        title: `Add headroom to ${node}`,
        why: `${count.toLocaleString()} requests were rejected with ${reason}.`,
        severity: 'critical',
        target: node,
        kind: 'replicas',
        factor: scaleFactor(node, result),
      });
    }
    if (reason === 'client_timeout') {
      const slow = Object.entries(result.node_errors ?? {})
        .map(([node, errs]) => ({ node, count: Object.entries(errs ?? {}).filter(([r]) => r.includes('timeout')).reduce((a, [, c]) => a + c, 0) }))
        .sort((a, b) => b.count - a.count)[0];
      if (slow?.count) {
        add({
          id: `timeouts:${slow.node}`,
          title: `Cut retries into ${slow.node}`,
          why: `${count.toLocaleString()} client timeouts; ${slow.count.toLocaleString()} downstream timeout errors at ${slow.node}.`,
          severity: 'high',
          target: slow.node,
          kind: 'retry',
        });
      }
    }
  }

  // A run that never recovered points at startup/liveness, not raw capacity.
  if (result.status === 'no_recovery' || result.status === 'slow_recovery') {
    const blocked = (result.cascade ?? []).find((c) => c.what === 'boot_blocked');
    if (blocked?.node) {
      add({
        id: `boot:${blocked.node}`,
        title: `Remove ${blocked.node}'s boot blocker`,
        why: `${fmt(blocked.t)} ${blocked.detail || 'restart blocked by a dependency'}; the run ended as ${result.status}.`,
        severity: 'high',
        target: blocked.node,
        kind: 'startup_relax',
        dep: (blocked.detail || '').replace(/^waiting on\s*/, '').split(',')[0].trim() || undefined,
      });
    }
  }

  // A killed cache is a common trigger for the downstream overload we just saw.
  for (const c of result.cascade ?? []) {
    if (!(c.detail ?? '').toLowerCase().includes('kill')) continue;
    const killed = (arch.nodes ?? []).find((n: any) => n.id === c.node);
    if (killed?.type !== 'cache') continue;
    const protectedNodes = (arch.edges ?? [])
      .filter((e: any) => e.via_cache === c.node)
      .map((e: any) => e.to);
    const victim = protectedNodes.find((id: string) => errCount(id, result) > 0) ?? protectedNodes[0];
    if (!victim) continue;
    add({
      id: `cache-restart:${victim}`,
      title: `Absorb the ${c.node} restart with read replicas on ${victim}`,
      why: `${fmt(c.t)} ${c.node} was killed; ${victim} then recorded ${errCount(victim, result).toLocaleString()} errors.`,
      severity: 'high',
      target: victim,
      kind: 'read_replicas',
    });
  }

  return out.slice(0, 8);
}

/** Apply one recommendation to a copy of the architecture. */
export function applyFix(arch: any, fix: RecommendedFix): any {
  const next = clone(arch);
  const node = (next.nodes ?? []).find((n: any) => n.id === fix.target);
  const edges: any[] = next.edges ?? [];

  switch (fix.kind) {
    case 'replicas':
      if (node) node.replicas = Math.max(2, Math.ceil((node.replicas ?? 1) * (fix.factor ?? 1.5)));
      break;
    case 'concurrency':
      if (node) node.concurrency_per_replica = Math.round((node.concurrency_per_replica ?? 20) * (fix.factor ?? 1.5));
      break;
    case 'retry': {
      const [from, to] = fix.target.split('->').map((s) => s?.trim());
      const targets = from && to
        ? edges.filter((e) => e.from === from && e.to === to)
        : edges.filter((e) => e.to === fix.target);
      targets.forEach((e) => {
        e.retry = { attempts: 2, backoff: 'exponential', base_ms: 100, max_ms: 1000, jitter: true };
      });
      break;
    }
    case 'timeout': {
      const [from, to] = fix.target.split('->').map((s) => s?.trim());
      const targets = from && to ? edges.filter((e) => e.from === from && e.to === to) : edges.filter((e) => e.to === fix.target);
      targets.forEach((e) => { e.timeout_ms = Math.min(e.timeout_ms ?? 2000, 1200); });
      break;
    }
    case 'read_replicas':
      if (node && node.type === 'datastore') {
        if (!node.read_replicas?.length) {
          node.read_replicas = [`${node.id}-replica-1`, `${node.id}-replica-2`];
        }
        edges.filter((e) => e.to === fix.target).forEach((e) => {
          if (e.write_ratio === undefined) e.write_ratio = 0.2;
        });
      }
      break;
    case 'rate_limit':
      if (node?.rate_limit_rps) node.rate_limit_rps = Math.ceil(node.rate_limit_rps * 1.5);
      break;
    case 'startup_relax':
      if (node?.startup?.requires && fix.dep) {
        node.startup.requires = node.startup.requires.filter((r: string) => r !== fix.dep);
      }
      break;
    case 'health_check':
      if (node) node.health_check = { interval_s: 5, fail_threshold: 0.5, unhealthy_after: 3, deep: false };
      break;
  }

  return next;
}
