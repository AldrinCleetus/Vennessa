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
  factor?: number;
  dep?: string;
}

function clone<T>(v: T): T {
  return JSON.parse(JSON.stringify(v));
}

function fmt(n: number): string {
  return `${Math.round(n)}s`;
}

function nodeUtil(id: string, result: SimulationResult): number {
  const n = (result.nodes ?? []).find((x) => x.id === id);
  return n?.peak_util ?? 0;
}

function errCount(id: string, result: SimulationResult): number {
  const n = (result.nodes ?? []).find((x) => x.id === id);
  return n ? Object.values(n.errors ?? {}).reduce((a, b) => a + b, 0) : 0;
}

function scaleFactor(id: string, result: SimulationResult): number {
  const u = nodeUtil(id, result);
  if (u >= 2.0) return 3;
  if (u >= 1.2) return 2;
  return 1.5;
}

/** Rank findings from highest to lowest severity. */
const SEV_ORDER: Record<string, number> = { critical: 0, high: 1, medium: 2, low: 3 };

/**
 * Generate actionable, evidence-based recommendations for an architecture.
 *
 * Combines two sources of truth:
 *  1. Static analysis findings (SPOF, missing retries, boot deadlock risks).
 *  2. Simulation results (actual nodes that saturated, dropped requests, or cascaded).
 */
export function recommendFixes(
  arch: any,
  result?: SimulationResult | null,
  findings: Finding[] = []
): RecommendedFix[] {
  const out: RecommendedFix[] = [];
  const seen = new Set<string>();

  const push = (f: RecommendedFix) => {
    if (seen.has(f.id)) return;
    seen.add(f.id);
    out.push(f);
  };

  // 1. Findings-driven fixes
  for (const f of findings) {
    const target = f.target ?? '';
    const node = target.split('->')[0]?.trim() || target;
    const base = {
      id: `${f.rule}:${target}`,
      why: f.message,
      severity: f.severity,
      target,
    };

    switch (f.rule) {
      case 'spof':
      case 'over_capacity':
      case 'low_headroom':
        push({ ...base, kind: 'replicas', factor: result ? scaleFactor(node, result) : 2, title: `Scale ${node} to absorb the observed load` });
        break;
      case 'cold_cache_stampede':
        push({ ...base, kind: 'read_replicas', title: `Offload cache-miss reads from ${node}` });
        break;
      case 'retry_no_backoff':
      case 'retry_no_jitter':
      case 'retry_amplification':
        push({ ...base, kind: 'retry', title: `Stop retry storms on ${node}` });
        break;
      case 'rate_limit_baseline':
      case 'rate_limit_cold_cache':
        push({ ...base, kind: 'rate_limit', title: `Raise the rate limit on ${node}` });
        break;
      case 'replicas_unused':
        push({ ...base, kind: 'read_replicas', title: `Actually use ${node}'s read replicas` });
        break;
      case 'boot_needs_external': {
        const ext = (arch.nodes ?? []).find((n: any) => n.id === node)?.startup?.requires
          ?.find((r: string) => (arch.nodes ?? []).find((n: any) => n.id === r)?.type === 'external');
        if (ext) push({ ...base, kind: 'startup_relax', dep: ext, title: `Let ${node} boot without ${ext}` });
        break;
      }
      case 'startup_cycle': {
        const ids = target.split('->').map((s) => s.trim());
        if (ids.length > 1) {
          push({ ...base, target: ids[0], kind: 'startup_relax', dep: ids[1], title: `Break the startup cycle at ${ids[0]}` });
        }
        break;
      }
      case 'timeout_caller_shorter':
      case 'timeout_budget_exceeded':
        push({ ...base, kind: 'timeout', title: `Tighten the timeout budget on ${target}` });
        break;
      case 'deep_health_check':
        push({ ...base, kind: 'health_check', title: `Switch ${node} to shallow health checks` });
        break;
    }
  }

  // 2. Simulation evidence-driven fixes (only when a run has actually completed)
  if (result) {
    // Saturated nodes
    for (const n of result.nodes ?? []) {
      const errs = errCount(n.id, result);
      if (n.peak_util >= 1.0 || errs > 0) {
        const archNode = (arch.nodes ?? []).find((x: any) => x.id === n.id);
        const factor = scaleFactor(n.id, result);
        if (archNode?.type === 'datastore') {
          push({
            id: `sim:read_replicas:${n.id}`,
            title: `Add read replicas to ${n.id} to absorb read load`,
            why: `Peak utilization was ${(n.peak_util * 100).toFixed(0)}% with ${errs.toLocaleString()} errors observed during simulation.`,
            severity: n.peak_util >= 1.5 ? 'critical' : 'high',
            target: n.id,
            kind: 'read_replicas',
          });
        } else if (archNode?.type === 'service') {
          push({
            id: `sim:scale:${n.id}`,
            title: `Scale ${n.id} by ${factor}x`,
            why: `Peak utilization was ${(n.peak_util * 100).toFixed(0)}% with ${n.max_queued} queued requests.`,
            severity: n.peak_util >= 1.5 ? 'critical' : 'high',
            target: n.id,
            kind: 'replicas',
            factor,
          });
        }
      }
    }

    // Cascade root causes
    const firstCascade = (result.cascade ?? [])[0];
    if (firstCascade?.node && !seen.has(`cascade:${firstCascade.node}`)) {
      push({
        id: `cascade:${firstCascade.node}`,
        title: `Protect ${firstCascade.node} against cascading overload`,
        why: `First failure in the cascade at ${fmt(firstCascade.t)}: ${firstCascade.what}`,
        severity: 'critical',
        target: firstCascade.node,
        kind: 'replicas',
        factor: 2,
      });
    }

    // Client timeouts
    for (const [key, count] of Object.entries(result.client_errors ?? {})) {
      if (key.includes('timeout') && count > 0) {
        const edgePart = key.split(':')[0] ?? '';
        push({
          id: `sim:timeout:${edgePart}`,
          title: `Increase or protect against timeouts on ${edgePart}`,
          why: `${count.toLocaleString()} client timeouts were recorded during this run.`,
          severity: 'high',
          target: edgePart,
          kind: 'retry',
        });
      }
    }
  }

  // Sort by severity
  return out.sort((a, b) => (SEV_ORDER[a.severity] ?? 3) - (SEV_ORDER[b.severity] ?? 3));
}

/** Apply one recommendation to a copy of the architecture. */
export function applyFix(arch: any, fix: RecommendedFix): any {
  const next = clone(arch);
  const nodes = next.nodes ?? [];
  const edges = next.edges ?? [];
  const node = nodes.find((n: any) => n.id === fix.target);

  switch (fix.kind) {
    case 'replicas':
      if (node) {
        const f = fix.factor ?? 2;
        node.replicas = Math.max((node.replicas ?? 1) + 1, Math.ceil((node.replicas ?? 1) * f));
      }
      break;

    case 'concurrency':
      if (node) {
        node.concurrency_per_replica = Math.ceil((node.concurrency_per_replica ?? 10) * 1.5);
      }
      break;

    case 'retry': {
      const targets = edges.filter((e: any) => e.to === fix.target || e.from === fix.target);
      targets.forEach((e: any) => {
        e.retry = { attempts: 2, backoff: 'exponential', base_ms: 100, max_ms: 1000, jitter: true };
      });
      break;
    }

    case 'timeout': {
      const [from, to] = fix.target.split('->').map((s) => s?.trim());
      const targets = from && to ? edges.filter((e: any) => e.from === from && e.to === to) : edges.filter((e: any) => e.to === fix.target);
      targets.forEach((e: any) => { e.timeout_ms = Math.min(e.timeout_ms ?? 2000, 1200); });
      break;
    }

    case 'read_replicas':
      if (node && node.type === 'datastore') {
        const rep1 = `${node.id}-replica-1`;
        const rep2 = `${node.id}-replica-2`;
        if (!node.read_replicas?.length) {
          node.read_replicas = [rep1, rep2];
        }
        node.read_fallback = true;
        // Ensure the replica nodes exist in next.nodes
        if (!nodes.some((n: any) => n.id === rep1)) {
          nodes.push({
            id: rep1,
            type: 'datastore',
            replicas: 1,
            concurrency_per_replica: node.concurrency_per_replica || 25,
            queue_per_replica: node.queue_per_replica || 100,
            latency_ms: node.latency_ms ? { ...node.latency_ms } : { p50: 15, p99: 60 },
          });
        }
        if (!nodes.some((n: any) => n.id === rep2)) {
          nodes.push({
            id: rep2,
            type: 'datastore',
            replicas: 1,
            concurrency_per_replica: node.concurrency_per_replica || 25,
            queue_per_replica: node.queue_per_replica || 100,
            latency_ms: node.latency_ms ? { ...node.latency_ms } : { p50: 15, p99: 60 },
          });
        }
        edges.filter((e: any) => e.to === fix.target).forEach((e: any) => {
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
