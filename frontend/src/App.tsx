import React, { useState, useEffect, useRef } from 'react';
import * as yaml from 'js-yaml';
import {
  Play, Pause, RefreshCw, Download, Upload, Sparkles, X, Plus,
  Zap, GitCompare, ShieldCheck, ShieldAlert, Layers, Wand2,
} from 'lucide-react';
import { Finding, SimulationResult, ModelSummary, ScenarioSummary, TimelinePoint } from './types';
import { applyFix, recommendFixes, RecommendedFix } from './lib/recommendations';
import { apiFetch } from './lib/api';

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

// Presets for specific known architectures to make graph look beautiful
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

const fixIdOf = (f: any): string => f?.fix_id || f?.id || '';
const scenarioLabel = (s: any): string => {
  if (!s) return '';
  if (typeof s === 'string') return s;
  if (s.name) return s.name;
  if (s.scenario_id) return s.scenario_id;
  if (s.description) return String(s.description).slice(0, 80);
  return 'AI scenario';
};
const clone = <T,>(v: T): T => JSON.parse(JSON.stringify(v));
const STATUS_RANK: Record<string, number> = {
  ok: 0,
  degraded: 1,
  slow_recovery: 2,
  no_recovery: 3,
};
const numericDelta = (before: any, after: any): number | null => {
  if (typeof before !== 'number' || typeof after !== 'number') return null;
  return after - before;
};
const cellText = (v: any): string =>
  v === undefined || v === null ? '—' : typeof v === 'object' ? JSON.stringify(v) : String(v);

export default function App() {
  const [models, setModels] = useState<ModelSummary[]>([]);
  const [scenarios, setScenarios] = useState<ScenarioSummary[]>([]);
  const [selectedModelId, setSelectedModelId] = useState<string>('ecommerce_checkout');
  const [selectedScenarioId, setSelectedScenarioId] = useState<string>('03_cache_kill_at_peak');
  const [designMode, setDesignMode] = useState<'before' | 'after' | 'compare'>('before');
  const [activeNav, setActiveNav] = useState<string>('architecture');
  const [showAlphaNotice, setShowAlphaNotice] = useState<boolean>(() => {
    try {
      return localStorage.getItem('vennessa.alpha.notice.v1') !== 'seen';
    } catch {
      return true;
    }
  });

  const [rawModel, setRawModel] = useState<any>(null);
  const [rawYaml, setRawYaml] = useState<string>('');
  const [rawScenario, setRawScenario] = useState<any>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<string>('checkout_api');

  const [simulating, setSimulating] = useState<boolean>(false);
  const [simError, setSimError] = useState<string | null>(null);
  const [result, setResult] = useState<SimulationResult | null>(null);
  const [baselineResult, setBaselineResult] = useState<SimulationResult | null>(null);
  const [recommendations, setRecommendations] = useState<RecommendedFix[]>([]);
  const [findings, setFindings] = useState<Finding[]>([]);
  const [appliedFixes, setAppliedFixes] = useState<Set<string>>(new Set());

  // ---- AI-native workflow state ----
  const [isCreatingNew, setIsCreatingNew] = useState(false);
  const [aiScenarios, setAiScenarios] = useState<any[]>([]);
  const [selectedAiScenario, setSelectedAiScenario] = useState<any | null>(null);
  const [aiFixes, setAiFixes] = useState<any[]>([]);
  const [selectedFixIds, setSelectedFixIds] = useState<Set<string>>(new Set());
  const [baselineModel, setBaselineModel] = useState<any | null>(null);
  const [compareData, setCompareData] = useState<any | null>(null);
  const [currentFingerprint, setCurrentFingerprint] = useState<string | null>(null);
  const [lastDiff, setLastDiff] = useState<any | null>(null);
  const [aiStatus, setAiStatus] = useState('');
  const [aiWarnings, setAiWarnings] = useState<string[]>([]);
  const [generatingScenarios, setGeneratingScenarios] = useState(false);
  const [remediating, setRemediating] = useState(false);
  const [applyingFixes, setApplyingFixes] = useState(false);
  const [thinkingSteps, setThinkingSteps] = useState<string[]>([]);
  const skipScenarioFetchRef = useRef<string | null>(null);

  // Interactive Timeline Replay
  const [simTime, setSimTime] = useState<number>(0);
  const [isPlaying, setIsPlaying] = useState<boolean>(false);
  const [isDragging, setIsDragging] = useState<boolean>(false);
  const playTimerRef = useRef<any>(null);

  // Drawer
  const [isDrawerOpen, setIsDrawerOpen] = useState<boolean>(false);
  const [drawerTab, setDrawerTab] = useState<'prompt' | 'editor' | 'import_export'>('prompt');
  const [promptText, setPromptText] = useState<string>('');
  const [importStatus, setImportStatus] = useState<string>('');
  const [isApplyingPrompt, setIsApplyingPrompt] = useState<boolean>(false);
  const [promptError, setPromptError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

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

  // "Before" shows the pristine run; "With fixes"/"Compare" show the latest run.
  const viewResult = designMode === 'before' && baselineResult ? baselineResult : result;

  // Timeline geometry is driven by the scenario under test, not a fixed 30-minute mock.
  const duration = viewResult?.duration_s ?? rawScenario?.duration_s ?? 1800;
  const timeline: TimelinePoint[] = viewResult?.timeline ?? [];
  const compareTimelines = designMode === 'compare' ? (baselineResult?.timeline ?? []) : [];
  const peakErr = [...timeline, ...compareTimelines].reduce((m, p) => Math.max(m, p.err_rate), 0);
  const errScale = Math.max(0.05, Math.ceil((peakErr * 100) / 5) * 5 / 100);
  const xFor = (t: number) => 30 + (Math.max(0, Math.min(duration, t)) / duration) * 960;
  const yFor = (err: number) => 100 - (Math.min(err, errScale) / errScale) * 80;
  const toPath = (points: TimelinePoint[]) => points.length
    ? `M30 100 ${points.map((p) => `L${xFor(p.t).toFixed(1)} ${yFor(p.err_rate).toFixed(1)}`).join(' ')} L990 100`
    : 'M30 100 L990 100';
  const timelinePath = toPath(timeline);
  const baselinePath = compareTimelines.length ? toPath(compareTimelines) : null;
  const scrubErr = timeline.filter((p) => p.t <= simTime).pop()?.err_rate ?? 0;
  const eventMarkers = [
    ...(rawScenario?.traffic?.ramp ?? []).map((r: any) => ({ t: r.at_s, kind: 'load' })),
    ...(rawScenario?.events ?? []).map((e: any) => ({ t: e.at_s, kind: e.action })),
  ];

  const currentScenarioObject: any = selectedAiScenario?.scenario ?? rawScenario ?? null;
  const currentPassed: boolean | null = result
    ? (typeof (result as any).passed === 'boolean'
        ? (result as any).passed
        : (!result.slo_breached && result.status === 'ok'))
    : null;
  const selectedFixes = aiFixes.filter((f) => selectedFixIds.has(fixIdOf(f)));
  const fpShort = currentFingerprint ? currentFingerprint.slice(0, 8) : '—';

  const focusSection = (id: string, mode?: 'before' | 'after' | 'compare') => {
    if (mode) setDesignMode(mode);
    requestAnimationFrame(() => document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' }));
  };

  // 1. Initial Load: Fetch models & scenarios
  useEffect(() => {
    apiFetch(`/api/models`)
      .then((r) => r.json())
      .then((d) => {
        if (d.models && d.models.length > 0) {
          setModels(d.models);
        }
      })
      .catch((e) => console.error('Failed to list models', e));

    apiFetch(`/api/scenarios`)
      .then((r) => r.json())
      .then((d) => {
        if (d.scenarios && d.scenarios.length > 0) {
          setScenarios(d.scenarios);
        }
      })
      .catch((e) => console.error('Failed to list scenarios', e));
  }, []);

  // 1b. Load scenario detail when selectedScenarioId changes (AI-generated ids are skipped).
  useEffect(() => {
    if (!selectedScenarioId) return;
    if (skipScenarioFetchRef.current === selectedScenarioId) {
      skipScenarioFetchRef.current = null;
      return;
    }
    setRawScenario(null);
    setSelectedAiScenario(null);
    setResult(null);
    setBaselineResult(null);
    setRecommendations([]);
    setAppliedFixes(new Set());
    setAiFixes([]);
    setSelectedFixIds(new Set());
    setCompareData(null);
    setDesignMode('before');
    setSimError(null);
    setIsPlaying(false);
    setSimTime(0);
    apiFetch(`/api/scenarios/${selectedScenarioId}`)
      .then((r) => r.json())
      .then((d) => setRawScenario(d.data || null))
      .catch((e) => console.error('Failed to load scenario', e));
  }, [selectedScenarioId]);

  // 2. Load model data when selectedModelId changes
  useEffect(() => {
    if (!selectedModelId) return;
    apiFetch(`/api/models/${selectedModelId}`)
      .then((r) => r.json())
      .then((d) => {
        setRawModel(d.data);
        setRawYaml(yaml.dump(d.data, { indent: 2 }));
        setBaselineResult(null);
        setResult(null);
        setRecommendations([]);
        setAppliedFixes(new Set());
        setAiFixes([]);
        setSelectedFixIds(new Set());
        setAiScenarios([]);
        setSelectedAiScenario(null);
        setCompareData(null);
        setLastDiff(null);
        setDesignMode('before');
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
      const res = await apiFetch(`/api/analyze`, {
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

  // 4a. Ask Gemini for discrete remediation proposals after any failed run.
  const requestAiFixes = async (
    arch: any,
    sim: SimulationResult,
    analysisFindings: Finding[],
    fingerprint: string | null
  ) => {
    setRemediating(true);
    setAiStatus('Gemini is diagnosing the failure…');
    setAiFixes([]);
    setSelectedFixIds(new Set());
    try {
      const res = await apiFetch('/api/ai/remediate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          architecture: arch,
          simulation_result: sim,
          findings: analysisFindings,
          count: 3,
          architecture_fingerprint: fingerprint,
        }),
      });
      if (!res.ok) {
        let message = `Server error (${res.status})`;
        try {
          const err = await res.json();
          if (err?.detail) message = typeof err.detail === 'string' ? err.detail : JSON.stringify(err.detail);
        } catch { /* keep generic message */ }
        throw new Error(message);
      }
      const data = await res.json();
      const fixes = Array.isArray(data.fixes) ? data.fixes : [];
      setAiFixes(fixes);
      setAiWarnings(Array.isArray(data.warnings) ? data.warnings : []);
      setAiStatus(`${fixes.length} discrete fix${fixes.length === 1 ? '' : 'es'} proposed — review and approve.`);
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      console.error('AI remediation failed', e);
      setAiStatus(`Remediation unavailable: ${message}`);
    } finally {
      setRemediating(false);
    }
  };

  // 4b. Run Simulation (optionally with an explicit architecture + explicit scenario dict).
  const handleRunSimulation = async (
    modelToRun?: any,
    scenarioOverride?: any
  ): Promise<SimulationResult | null> => {
    setSimulating(true);
    setIsPlaying(false);
    setSimError(null);
    setAiWarnings([]);
    try {
      const payload = modelToRun ?? rawModel ?? selectedModelId;
      const arch = typeof payload === 'string'
        ? (await (await apiFetch(`/api/models/${payload}`)).json()).data
        : payload;
      if (!arch) throw new Error('Could not resolve the architecture to simulate.');

      // Analyze the exact model being run so fixes match what this run actually hit.
      const analysisRes = await apiFetch(`/api/analyze`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ architecture: arch }),
      });
      const analysis = analysisRes.ok ? await analysisRes.json() : { findings: [] };
      setFindings(analysis.findings || []);

      const res = await apiFetch(`/api/simulate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          architecture: arch,
          scenario: scenarioOverride ?? rawScenario ?? selectedScenarioId,
          seed: 42,
        }),
      });
      if (!res.ok) {
        let errMessage = `Server error (${res.status})`;
        try {
          const errData = await res.json();
          if (errData && errData.detail) {
            errMessage = typeof errData.detail === 'string' ? errData.detail : JSON.stringify(errData.detail);
          }
        } catch {
          const raw = await res.text();
          if (raw) errMessage = raw;
        }
        throw new Error(errMessage);
      }
      const data: SimulationResult = await res.json();
      if (data.architecture_fingerprint) setCurrentFingerprint(data.architecture_fingerprint);
      setResult(data);
      setBaselineResult((prev) => prev ?? data);
      setRecommendations(recommendFixes(arch, data, analysis.findings || []));
      setSimTime(data.cascade?.[0]?.t ?? data.warmup_s ?? 0);

      const passed = typeof (data as any).passed === 'boolean'
        ? (data as any).passed
        : (!data.slo_breached && data.status === 'ok');
      if (passed) {
        setAiFixes([]);
        setSelectedFixIds(new Set());
        setAiStatus('Survived the scenario — nothing to fix.');
      } else {
        await requestAiFixes(arch, data, analysis.findings || [], data.architecture_fingerprint ?? currentFingerprint);
      }
      return data;
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      console.error('Simulation run failed', e);
      setSimError(message);
      setAiStatus(`Simulation failed: ${message}`);
      return null;
    } finally {
      setSimulating(false);
    }
  };

  // 4c. Generate adversarial scenarios with Gemini, then run one directly.
  const handleGenerateScenarios = async () => {
    if (!rawModel) return;
    setGeneratingScenarios(true);
    setAiStatus('Gemini is drafting adversarial scenarios…');
    setAiWarnings([]);
    try {
      const res = await apiFetch('/api/ai/adversary', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          architecture: rawModel,
          count: 3,
          seed: 42,
          exclude_ids: [],
          include_analysis: true,
        }),
      });
      if (!res.ok) {
        let message = `Server error (${res.status})`;
        try {
          const err = await res.json();
          if (err?.detail) message = typeof err.detail === 'string' ? err.detail : JSON.stringify(err.detail);
        } catch { /* keep generic message */ }
        throw new Error(message);
      }
      const data = await res.json();
      const items = Array.isArray(data.scenarios) ? data.scenarios : [];
      setAiScenarios(items);
      setAiWarnings(Array.isArray(data.warnings) ? data.warnings : []);
      setAiStatus(items.length ? `${items.length} scenarios ready — click one to break the architecture.` : 'No scenarios were generated.');
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      console.error('Scenario generation failed', e);
      setAiStatus(`Scenario generation failed: ${message}`);
      setAiScenarios([]);
    } finally {
      setGeneratingScenarios(false);
    }
  };

  const handleRunAiScenario = (item: any) => {
    if (!rawModel || !item?.scenario) return;
    // AI ids do not exist on disk — skip the scenario-detail effect for these ids.
    skipScenarioFetchRef.current = item.scenario_id;
    setRawScenario(item.scenario);
    setSelectedScenarioId(item.scenario_id);
    setSelectedAiScenario(item);
    setAiFixes([]);
    setSelectedFixIds(new Set());
    setCompareData(null);
    setDesignMode('before');
    handleRunSimulation(rawModel, item.scenario);
  };

  // 5. New architecture / prompt-driven modification via text-to-model.
  const handleNewArchitecture = () => {
    setIsCreatingNew(true);
    setPromptText('');
    setPromptError(null);
    setDrawerTab('prompt');
    setIsDrawerOpen(true);
    setAiStatus('Draft mode: describe a new architecture below.');
    focusSection('arch-stage');
  };

  const handleApplyPrompt = async () => {
    if (!promptText.trim()) return;
    const mode = isCreatingNew ? 'create' : 'modify';
    const scenarioForRun = selectedAiScenario?.scenario ?? rawScenario ?? selectedScenarioId;
    setIsApplyingPrompt(true);
    setPromptError(null);
    setThinkingSteps([mode === 'create' ? 'Reading the brief…' : 'Reading current architecture…']);
    try {
      setThinkingSteps((prev) => [...prev, mode === 'create' ? 'Designing the topology…' : 'Applying the requested change…']);
      await new Promise((resolve) => setTimeout(resolve, 260));

      const res = await apiFetch('/api/ai/text-to-model', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          description: promptText.trim(),
          name: isCreatingNew ? undefined : rawModel?.name,
          mode,
          current_architecture: isCreatingNew ? null : rawModel,
        }),
      });

      setThinkingSteps((prev) => [...prev, 'Validating schema…']);
      if (!res.ok) {
        let message = `Server error (${res.status})`;
        try {
          const err = await res.json();
          if (err?.detail) message = typeof err.detail === 'string' ? err.detail : JSON.stringify(err.detail);
        } catch { /* keep generic message */ }
        throw new Error(message);
      }
      const data = await res.json();
      if (!data?.architecture) throw new Error('The model compiler did not return an architecture.');

      setRawModel(data.architecture);
      setRawYaml(data.yaml || yaml.dump(data.architecture, { indent: 2 }));
      setCurrentFingerprint(data.fingerprint ?? null);
      setLastDiff(data.diff ?? null);
      setAiWarnings(Array.isArray(data.warnings) ? data.warnings : []);
      setSelectedNodeId(data.architecture.nodes?.find((n: any) => n.type === 'service')?.id || data.architecture.nodes?.[0]?.id || '');
      setPromptText('');
      setIsDrawerOpen(false);
      setIsCreatingNew(false);
      setAiScenarios([]);
      setSelectedAiScenario(null);
      setAiFixes([]);
      setSelectedFixIds(new Set());
      setCompareData(null);
      setBaselineModel(null);
      setBaselineResult(null);
      setDesignMode('before');
      setAiStatus(mode === 'create'
        ? `New architecture created · ${data.node_count ?? data.architecture.nodes?.length ?? 0} nodes.`
        : `Change applied · ${data.diff?.changes?.length ?? 0} patch operation(s).`);
      if (mode === 'create') setSelectedModelId('');

      await runStaticAnalysis(data.architecture);
      if (scenarioForRun) await handleRunSimulation(data.architecture, scenarioForRun);
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      console.error('Prompt application failed', e);
      setPromptError(message);
      setAiStatus(`Prompt failed: ${message}`);
    } finally {
      setIsApplyingPrompt(false);
      setThinkingSteps([]);
    }
  };

  // 6. Apply selected AI fixes atomically, re-simulate, and compare.
  const toggleFix = (id: string) => {
    setSelectedFixIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const handleSimulateSelectedFixes = async () => {
    if (!rawModel || !selectedFixes.length) return;
    const scenarioForRun = currentScenarioObject ?? selectedScenarioId;
    const beforeModel = clone(rawModel);
    const beforeResult = result;
    const beforeFingerprint = currentFingerprint;

    setApplyingFixes(true);
    setAiStatus(`Applying ${selectedFixes.length} fix(es) atomically…`);
    setCompareData(null);
    try {
      const patch = {
        base_fingerprint: beforeFingerprint ?? undefined,
        ops: selectedFixes.flatMap((f) => (Array.isArray(f.patch?.ops) ? f.patch.ops : [])),
      };
      const res = await apiFetch('/api/architectures/patch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          architecture: rawModel,
          patch,
          fix_ids: selectedFixes.map(fixIdOf),
        }),
      });
      if (!res.ok) {
        let message = `Server error (${res.status})`;
        try {
          const err = await res.json();
          if (err?.detail) message = typeof err.detail === 'string' ? err.detail : JSON.stringify(err.detail);
        } catch { /* keep generic message */ }
        throw new Error(message);
      }
      const data = await res.json();
      const patched = data.architecture;

      setBaselineModel(beforeModel);
      setBaselineResult(beforeResult);
      setRawModel(patched);
      setRawYaml(data.yaml || yaml.dump(patched, { indent: 2 }));
      setCurrentFingerprint(data.fingerprint ?? null);
      setLastDiff(data.diff ?? null);
      setSelectedFixIds(new Set());
      setDesignMode('after');
      await runStaticAnalysis(patched);

      // Re-run against the exact same scenario the baseline failed on.
      const afterResult = await handleRunSimulation(patched, scenarioForRun);

      // Build the comparison from the two runs we already performed. Re-running
      // /api/compare here would duplicate both simulations (and can take minutes
      // for long scenarios), which made the UI feel stalled.
      const bStatus = String(beforeResult?.status || 'ok');
      const aStatus = String(afterResult?.status || 'ok');
      const rankBefore = STATUS_RANK[bStatus] ?? 1;
      const rankAfter = STATUS_RANK[aStatus] ?? 1;
      const statusChange = rankAfter < rankBefore ? 'better' : rankAfter > rankBefore ? 'worse' : 'same';
      setCompareData({
        scenarios: [{
          scenario: scenarioLabel(scenarioForRun) || selectedScenarioId,
          before_run: beforeResult,
          after_run: afterResult,
          delta: {
            status_change: statusChange,
            error_rate_delta: numericDelta(beforeResult?.error_rate, afterResult?.error_rate),
            p99_delta_ms: numericDelta(beforeResult?.p99_ms, afterResult?.p99_ms),
            recovery_delta_s: numericDelta(beforeResult?.recovery_s, afterResult?.recovery_s),
            breach_seconds_delta: numericDelta(beforeResult?.breach_seconds, afterResult?.breach_seconds),
            first_breach_delta_s: numericDelta(beforeResult?.first_breach_s, afterResult?.first_breach_s),
            last_breach_delta_s: numericDelta(beforeResult?.last_breach_s, afterResult?.last_breach_s),
          },
        }],
        findings: { new: [], resolved: [] },
        summary: {
          regressions: rankAfter > rankBefore ? 1 : 0,
          improvements: rankAfter < rankBefore ? 1 : 0,
          new_findings: 0,
          resolved_findings: 0,
        },
      });
      setDesignMode('compare');
      setActiveNav('compare');
      setAiStatus(`${selectedFixes.length} fix(es) applied — before / after ready.`);
      focusSection('compare-sec');
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      console.error('Applying fixes failed', e);
      setAiStatus(`Could not apply fixes: ${message}`);
    } finally {
      setApplyingFixes(false);
    }
  };

  // 7. Timeline Play/Pause Loop
  useEffect(() => {
    if (isPlaying) {
      playTimerRef.current = setInterval(() => {
        setSimTime((prev) => {
          const step = Math.max(1, Math.round(duration / 360));
          if (prev + step >= duration) {
            setIsPlaying(false);
            return duration;
          }
          return prev + step;
        });
      }, 100);
    } else {
      if (playTimerRef.current) clearInterval(playTimerRef.current);
    }
    return () => {
      if (playTimerRef.current) clearInterval(playTimerRef.current);
    };
  }, [isPlaying, duration]);

  const fmtTime = (s: number) => {
    s = Math.max(0, Math.round(s));
    const m = Math.floor(s / 60);
    const sec = s % 60;
    return `${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
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

  // 8. Timeline Continuous Drag-Scrubbing
  const updateTimelineScrub = (clientX: number, target: SVGSVGElement) => {
    const rect = target.getBoundingClientRect();
    const x = ((clientX - rect.left) / rect.width) * 1000;
    const newT = Math.max(0, Math.min(duration, Math.round(((x - 30) / 960) * duration)));
    setSimTime(newT);
  };

  // 9. Graph Auto-Layout with Pinned Fallback
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
    if (!viewResult) return { state: 'ok', note: 'Nominal headroom', util: 0.25 };

    // Use the most recent cascade event at or before the scrub position.
    const latest = (viewResult.cascade ?? [])
      .filter((c) => c.node === nodeId && c.t <= simTime)
      .sort((a, b) => a.t - b.t)
      .pop();
    if (latest) {
      const what = latest.what.toLowerCase();
      const detail = (latest.detail ?? latest.what).toLowerCase();
      if (what.includes('down') || detail.includes('kill') || what.includes('saturated') || detail.includes('rejecting')) {
        return { state: 'down', note: latest.detail || latest.what, util: 1.0 };
      }
      if (what.includes('boot') || detail.includes('restart') || detail.includes('restore') || detail.includes('boot')) {
        return { state: 'boot', note: latest.detail || 'Restarting / blocked', util: 0.5 };
      }
      if (what.includes('unhealthy') || detail.includes('slow') || detail.includes('rate limit')) {
        return { state: 'warn', note: latest.detail || latest.what, util: 0.85 };
      }
      if (what.includes('up') || detail.includes('start')) {
        return { state: 'ok', note: 'Back online', util: 0.45 };
      }
    }

    // Node may be strained without an injected lifecycle event.
    if (viewResult.node_errors?.[nodeId] && simTime >= (viewResult.warmup_s ?? 0)) {
      const errCount = Object.values(viewResult.node_errors[nodeId]).reduce((a, b) => a + b, 0);
      if (errCount > 0) {
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

  // 10. Legacy static recommendation (kept for manual exploration).
  const handleApplyRecommendedFix = (fix: RecommendedFix) => {
    if (!rawModel) return;
    const changed = applyFix(rawModel, fix);
    setRawModel(changed);
    setRawYaml(yaml.dump(changed, { indent: 2 }));
    setAppliedFixes((prev) => new Set([...prev, fix.id]));
    setDesignMode('after');
    runStaticAnalysis(changed);
    handleRunSimulation(changed, currentScenarioObject ?? selectedScenarioId);
  };

  // 11. Manual YAML Spec Editor apply
  const handleApplySpec = () => {
    try {
      const parsed: any = yaml.load(rawYaml);
      if (!parsed || !parsed.nodes) {
        alert('Invalid architecture YAML: must include a "nodes" list.');
        return;
      }
      setRawModel(parsed);
      setCurrentFingerprint(null);
      setLastDiff(null);
      runStaticAnalysis(parsed);
      setIsDrawerOpen(false);
      handleRunSimulation(parsed, currentScenarioObject ?? selectedScenarioId);
    } catch (e: any) {
      alert(`YAML Parse Error: ${e.message}`);
    }
  };

  // 12. Export & Import
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
        setCurrentFingerprint(null);
        setLastDiff(null);
        setImportStatus(`Successfully loaded "${parsed.name || file.name}"!`);
        runStaticAnalysis(parsed);
        handleRunSimulation(parsed, currentScenarioObject ?? selectedScenarioId);
      } catch (err: any) {
        setImportStatus(`Failed to parse file: ${err.message}`);
      }
    };
    reader.readAsText(file);
  };

  // ---- Compare panel helpers ----
  const metric = (run: any, keys: string[]): number | null => {
    for (const k of keys) {
      const v = run?.[k];
      if (typeof v === 'number') return v;
    }
    return null;
  };
  const fmtPct = (v: number | null) => v === null ? '—' : `${(v * 100).toFixed(1)}%`;
  const fmtMs = (v: number | null) => v === null ? '—' : `${Math.round(v)}ms`;
  const fmtSecs = (v: number | null) => v === null ? '—' : `${Math.round(v)}s`;
  const deltaText = (b: number | null, a: number | null, unit: 'pp' | 'ms' | 's') => {
    if (b === null || a === null) return '—';
    const d = a - b;
    if (Math.abs(d) < 1e-9) return 'no change';
    const magnitude = unit === 'pp' ? `${(Math.abs(d) * 100).toFixed(1)}pp` : `${Math.round(Math.abs(d))}${unit}`;
    return `${d < 0 ? '▼' : '▲'} ${magnitude}`;
  };

  const cmpScenario = compareData?.scenarios?.[0] ?? {};
  const statusDelta = cmpScenario?.delta?.status_change;
  const verdictDelta = statusDelta === 'better' ? 'improved' : statusDelta === 'worse' ? 'regressed' : statusDelta === 'same' ? 'same verdict' : (currentPassed === true ? 'survived' : '—');
  const beforeRun = cmpScenario.before_run ?? compareData?.before ?? baselineResult;
  const afterRun = cmpScenario.after_run ?? compareData?.after ?? result;
  const bErr = metric(beforeRun, ['error_rate', 'peak_error_rate']);
  const aErr = metric(afterRun, ['error_rate', 'peak_error_rate']);
  const bP99 = metric(beforeRun, ['p99_ms']);
  const aP99 = metric(afterRun, ['p99_ms']);
  const bRec = metric(beforeRun, ['recovery_s']);
  const aRec = metric(afterRun, ['recovery_s']);
  const bBreach = metric(beforeRun, ['breach_seconds', 'first_breach_s']);
  const aBreach = metric(afterRun, ['breach_seconds', 'first_breach_s']);

  const compareMetrics = [
    { label: 'Verdict', before: cellText(beforeRun?.status), after: cellText(afterRun?.status), delta: verdictDelta },
    { label: 'Error rate', before: fmtPct(bErr), after: fmtPct(aErr), delta: deltaText(bErr, aErr, 'pp') },
    { label: 'p99 latency', before: fmtMs(bP99), after: fmtMs(aP99), delta: deltaText(bP99, aP99, 'ms') },
    { label: 'Recovery', before: fmtSecs(bRec), after: fmtSecs(aRec), delta: deltaText(bRec, aRec, 's') },
    { label: 'First breach', before: fmtSecs(bBreach), after: fmtSecs(aBreach), delta: deltaText(bBreach, aBreach, 's') },
  ];

  const findingsNew = Array.isArray(compareData?.findings?.new) ? compareData.findings.new : [];
  const findingsResolved = Array.isArray(compareData?.findings?.resolved) ? compareData.findings.resolved : [];
  const diffRows: any[] = findingsNew.length || findingsResolved.length
    ? [
        ...findingsNew.map((x: any) => ({
          kind: 'new',
          path: typeof x === 'string' ? x : (x.target || x.path || x.rule || 'finding'),
          before: '—',
          after: typeof x === 'string' ? 'introduced' : (x.message || x.fix || 'introduced'),
        })),
        ...findingsResolved.map((x: any) => ({
          kind: 'resolved',
          path: typeof x === 'string' ? x : (x.target || x.path || x.rule || 'finding'),
          before: typeof x === 'string' ? 'present' : (x.message || 'present'),
          after: '—',
        })),
      ]
    : (Array.isArray(lastDiff?.changes)
        ? lastDiff.changes.map((c: any) => ({ kind: c.op || 'changed', path: c.path || '—', before: c.before, after: c.after }))
        : []);

  const selectedNode = nodesById[selectedNodeId] || graphNodes[0];
  const selectedNodeState = selectedNode ? getNodeState(selectedNode.id) : null;

  return (
    <div className="app">
      {/* ===== FIXED LEFT SIDEBAR ===== */}
      <aside className="sidebar">
        <div className="brand">
          <div className="logo">V</div>
          <span>VENESSA</span>
        </div>
        <div className="micro sidebar-label">Workbench</div>
        <nav className="side-nav" aria-label="Sections">
          <button type="button" className={`nav-item ${activeNav === 'architecture' ? 'active' : ''}`} onClick={() => { setActiveNav('architecture'); focusSection('arch-stage', 'before'); }}>
            <Layers className="w-3.5 h-3.5" /> Architecture
          </button>
          <button type="button" className={`nav-item ${activeNav === 'scenarios' ? 'active' : ''}`} onClick={() => { setActiveNav('scenarios'); focusSection('scenarios-sec'); }}>
            <Zap className="w-3.5 h-3.5" /> Scenarios
          </button>
          <button type="button" className={`nav-item ${activeNav === 'simulate' ? 'active' : ''}`} onClick={() => { setActiveNav('simulate'); focusSection('verdict-sec'); }}>
            <Play className="w-3.5 h-3.5" /> Simulate
          </button>
          <button type="button" className={`nav-item ${activeNav === 'fixes' ? 'active' : ''}`} onClick={() => { setActiveNav('fixes'); focusSection('fixes-sec'); }}>
            <ShieldAlert className="w-3.5 h-3.5" /> Fixes
            {aiFixes.length > 0 && <span className="nav-count">{aiFixes.length}</span>}
          </button>
          <button type="button" className={`nav-item ${activeNav === 'compare' ? 'active' : ''}`} onClick={() => { setActiveNav('compare'); focusSection('compare-sec', 'compare'); }}>
            <GitCompare className="w-3.5 h-3.5" /> Compare
          </button>
        </nav>
        <div className="sidebar-bottom context-card">
          <div className="micro">Active model</div>
          <b className="model-name">{rawModel?.name || selectedModelId || 'untitled architecture'}</b>
          <div className="sidebar-stats">
            <span>{rawModel?.nodes?.length ?? 0} nodes</span>
            <span>{rawModel?.edges?.length ?? 0} edges</span>
          </div>
          <div className="micro">fp {fpShort}</div>
        </div>
      </aside>

      {/* ===== TOP COMMAND STRIP ===== */}
      <header className="top">
        <div className="command-group">
          <div className="pick">
            <small>Model</small>
            <select value={selectedModelId} onChange={(e) => setSelectedModelId(e.target.value)}>
              {!selectedModelId && <option value="">{rawModel?.name || 'generated architecture'}</option>}
              {models.map((m) => (
                <option key={m.id} value={m.id}>{m.name || m.id}</option>
              ))}
            </select>
          </div>
          <span className="sep">/</span>
          <div className="pick">
            <small>Scenario</small>
            <select value={selectedScenarioId} onChange={(e) => setSelectedScenarioId(e.target.value)}>
              {selectedAiScenario && <option value={selectedScenarioId}>{scenarioLabel(selectedAiScenario.scenario) || selectedScenarioId}</option>}
              {scenarios.map((s) => (
                <option key={s.id} value={s.id}>{s.name || s.id}</option>
              ))}
            </select>
          </div>
          <span className="status-pill">seed 42</span>
        </div>

        <div className="spacer"></div>

        <div className="seg" role="group" aria-label="Design version">
          <button type="button" className={designMode === 'before' ? 'active' : ''} onClick={() => setDesignMode('before')}>Before</button>
          <button type="button" className={designMode === 'after' ? 'active' : ''} onClick={() => setDesignMode('after')}>After</button>
          <button type="button" className={designMode === 'compare' ? 'active' : ''} onClick={() => { setDesignMode('compare'); setActiveNav('compare'); }}>Compare</button>
        </div>

        <button type="button" className="action-btn" onClick={handleNewArchitecture}>
          <Plus className="w-3.5 h-3.5" /><span>New architecture</span>
        </button>
        <button type="button" className="action-btn" onClick={() => { setIsCreatingNew(false); setDrawerTab('prompt'); setIsDrawerOpen(true); }}>
          <Sparkles className="w-3.5 h-3.5 text-accent" /><span>Prompt &amp; Edit</span>
        </button>
        <button type="button" className="action-btn icon-only" onClick={handleExportYaml} title="Export YAML"><Download className="w-3.5 h-3.5" /></button>
        <button type="button" className="action-btn icon-only" onClick={() => fileInputRef.current?.click()} title="Import YAML / JSON"><Upload className="w-3.5 h-3.5" /></button>
        <input type="file" ref={fileInputRef} onChange={handleFileUpload} accept=".yaml,.yml,.json" style={{ display: 'none' }} />

        <button type="button" className="run" onClick={() => handleRunSimulation(rawModel, currentScenarioObject ?? selectedScenarioId)} disabled={simulating}>
          {simulating ? <><RefreshCw className="w-3.5 h-3.5 animate-spin" /><span>Simulating…</span></> : <><Play className="w-3.5 h-3.5" /><span>Run scenario</span></>}
        </button>
      </header>

      {/* ===== WORKBENCH ===== */}
      <main className="main workbench">
        {/* LEFT RAIL */}
        <aside className="stack left">
          <section className="card" id="verdict-sec" aria-labelledby="vh">
            <h2 id="vh"><span>Verdict</span><span className="chip">{designMode}</span></h2>
            <div className="verdict">
              <span className="dot" style={{ background: viewResult?.status === 'ok' ? 'var(--green)' : viewResult?.status === 'degraded' || viewResult?.status === 'slow_recovery' ? 'var(--orange)' : 'var(--red)' }} />
              <div>
                <h3 id="vTitle">
                  {viewResult
                    ? viewResult.status === 'ok'
                      ? 'Survives nominal load'
                      : viewResult.status === 'degraded' || viewResult.status === 'slow_recovery'
                      ? 'Degraded, recovered'
                      : 'Catastrophic outage'
                    : 'Awaiting simulation'}
                </h3>
                <p id="vText">
                  {viewResult
                    ? viewResult.slo_breached
                      ? `Breached SLO for ${viewResult.breach_seconds || 0}s during the incident.`
                      : 'Remained within the target SLO envelope.'
                    : 'Pick a scenario and run it to see the verdict.'}
                </p>
              </div>
            </div>

            {simError && <p className="sim-error" role="alert">Simulation failed: {simError}</p>}
            {aiStatus && <div className={`insight-card ${aiStatus.startsWith('Prompt failed') || aiStatus.startsWith('Could not') || aiStatus.startsWith('Scenario generation failed') ? 'error' : ''}`}>
              <span className="micro"> Gemini trace</span>
              <span>{aiStatus}</span>
            </div>}
            {aiWarnings.length > 0 && (
              <div className="insight-card warn">
                <span className="micro">Warnings</span>
                {aiWarnings.map((w, i) => <span key={i}>{typeof w === 'string' ? w : JSON.stringify(w)}</span>)}
              </div>
            )}

            <div className="kpis">
              <div className="kpi"><small>Peak error rate</small><b>{viewResult ? `${(viewResult.error_rate * 100).toFixed(1)}%` : '–'}</b></div>
              <div className="kpi"><small>Time out of SLO</small><b>{viewResult?.breach_seconds !== undefined ? `${viewResult.breach_seconds}s` : '–'}</b></div>
              <div className="kpi"><small>Recovery</small><b>{viewResult?.status === 'ok' ? 'Instant' : viewResult?.recovery_s ? `${viewResult.recovery_s.toFixed(0)}s` : 'Failed'}</b></div>
              <div className="kpi"><small>Failed requests</small><b>{viewResult ? Math.round(viewResult.requests * viewResult.error_rate).toLocaleString() : '–'}</b></div>
            </div>
          </section>

          <section className="card" id="scenarios-sec" aria-labelledby="sh">
            <h2 id="sh"><span>Scenario events</span><span className="chip">{fmtTime(duration)}</span></h2>
            <ul className="events">
              {(rawScenario?.traffic?.ramp ?? []).map((r: any, i: number) => (
                <li key={`ramp-${i}`}>
                  <time>{fmtTime(r.at_s)}</time>
                  <span className="tag load">load</span>
                  <span>Traffic ramps to {r.multiplier}×{r.ramp_s ? ` over ${r.ramp_s}s` : ''}</span>
                </li>
              ))}
              {(rawScenario?.events ?? []).map((e: any, i: number) => (
                <li key={`event-${i}`}>
                  <time>{fmtTime(e.at_s)}</time>
                  <span className={`tag ${e.action}`}>{e.action}</span>
                  <span>{describeEvent(e)}</span>
                </li>
              ))}
              {!rawScenario && <li><span>Loading scenario…</span></li>}
              {rawScenario && (rawScenario.traffic?.ramp ?? []).length === 0 && (rawScenario.events ?? []).length === 0 && (
                <li><span>No injected failures — nominal steady-state load</span></li>
              )}
            </ul>
          </section>

          <section className="card" id="adversary-sec" aria-labelledby="ah">
            <h2 id="ah">
              <span>AI adversary</span>
              <button type="button" className="tiny-btn" onClick={handleGenerateScenarios} disabled={generatingScenarios || !rawModel}>
                <Zap className="w-3 h-3" />{generatingScenarios ? 'Thinking…' : 'Generate 3'}
              </button>
            </h2>

            {generatingScenarios && (
              <div className="thinking">
                <div className="thinking-step">Reading architecture fingerprint…</div>
                <div className="thinking-step">Searching for metastable failure paths…</div>
                <div className="thinking-step">Validating adversarial scenarios…</div>
              </div>
            )}

            {aiScenarios.length > 0 && (
              <div className="scenario-list">
                {aiScenarios.map((item: any, idx: number) => {
                  const isSelected = selectedAiScenario?.scenario_id === item.scenario_id;
                  const targets = Array.isArray(item.target_nodes) ? item.target_nodes.join(', ') : '';
                  const severity = String(item.severity || item.hypothesis?.severity || (idx === 0 ? 'high' : 'medium')).toLowerCase();
                  return (
                    <button
                      type="button"
                      key={item.scenario_id || idx}
                      className={`scenario-card ${isSelected ? 'sel' : ''}`}
                      onClick={() => handleRunAiScenario(item)}
                    >
                      <div className="scenario-top">
                        <span className={`status-pill ${severity}`}>{severity}</span>
                        <span className="micro">#{String(idx + 1).padStart(2, '0')}</span>
                      </div>
                      <b>{scenarioLabel(item.scenario) || `Scenario ${idx + 1}`}</b>
                      <p>{item.hypothesis || item.rationale || 'Gemini adversarial hypothesis'}</p>
                      {targets && <div className="micro">targets · {targets}</div>}
                      <span className="run-link"><Play className="w-3 h-3" /> Run to verify</span>
                    </button>
                  );
                })}
              </div>
            )}

            <div className="dashed" />
            <div className="micro section-label">Disk scenarios</div>
            <div className="adv">
              {scenarios.map((s) => {
                const tried = result?.scenario === (s.name || s.id);
                const verdict = tried ? (result?.slo_breached ? 'breaks' : 'survives') : 'not run';
                return (
                  <div
                    key={s.id}
                    className={`adv-row ${selectedScenarioId === s.id && !selectedAiScenario ? 'sel' : ''}`}
                    onClick={() => setSelectedScenarioId(s.id)}
                  >
                    <span>{s.name || s.id}</span>
                    <span className={verdict === 'breaks' ? 'hit' : verdict === 'survives' ? 'ok' : ''}>{verdict}</span>
                  </div>
                );
              })}
            </div>
          </section>
        </aside>

        {/* CENTRE STAGE */}
        <section className="card stage" id="arch-stage" aria-labelledby="gh">
          <div className="stage-head">
            <h2 id="gh">Architecture</h2>
            <span className="chip" id="viewChip">{designMode === 'before' ? 'current design' : designMode === 'after' ? 'with fixes' : 'comparison diff'}</span>
            {isCreatingNew && <span className="status-pill accent">draft: new</span>}
            <div className="legend">
              <span><i style={{ background: 'var(--green)' }} />healthy</span>
              <span><i style={{ background: 'var(--orange)' }} />strained</span>
              <span><i style={{ background: 'var(--red)' }} />down</span>
              <span><i style={{ background: 'var(--boot)' }} />restarting</span>
            </div>
          </div>

          <div className="graph-wrap">
            <svg id="graph" viewBox="0 0 1000 560" role="img" aria-label="Architecture Graph">
              <g className="src">
                <circle cx={70} cy={280} r={28} fill="none" stroke="var(--ink-3)" strokeDasharray="3 4" />
                <text x={70} y={284} textAnchor="middle">Users</text>
                <text x={70} y={325} textAnchor="middle" style={{ fontSize: '11px', fill: 'var(--ink-3)', fontFamily: 'var(--mono)' }}>
                  {viewResult ? `${viewResult.requests.toLocaleString()} req` : '300 rps'}
                </text>
              </g>

              {ingressTarget && nodesById[ingressTarget] && (
                <path d={computeEdgePath('users', ingressTarget)} className="edge flow" />
              )}

              {graphEdges.map((e, idx) => {
                const isBad = viewResult?.node_errors && (viewResult.node_errors[e.to] || viewResult.client_errors[`${e.from}->${e.to}:timeout`]);
                return (
                  <path
                    key={idx}
                    d={computeEdgePath(e.from, e.to)}
                    className={`edge ${isBad ? 'bad' : 'flow'} ${e.opt ? 'opt' : ''}`}
                  />
                );
              })}

              {graphNodes.map((n) => {
                const st = getNodeState(n.id);
                const isSelected = selectedNodeId === n.id;
                const haloColor = st.state === 'down' ? 'var(--red)' : st.state === 'warn' ? 'var(--orange)' : st.state === 'boot' ? 'var(--boot)' : 'var(--accent)';
                return (
                  <g
                    key={n.id}
                    className={`node ${st.state} ${isSelected ? 'sel' : ''}`}
                    transform={`translate(${n.x - W / 2}, ${n.y - H / 2})`}
                    onClick={() => setSelectedNodeId(n.id)}
                  >
                    <rect className="halo" x={-5} y={-5} width={W + 10} height={H + 10} rx={16} fill="none" stroke={haloColor} strokeWidth={6} />
                    <rect className="body" width={W} height={H} rx={12} />
                    <rect x={0} y={10} width={4} height={H - 20} rx={2} fill={st.state === 'ok' ? 'var(--green)' : st.state === 'warn' ? 'var(--orange)' : 'var(--red)'} />
                    <text x={16} y={24}>{ICONS[n.type] || '◆'} {n.label}</text>
                    <text x={16} y={42} className="sub">{st.note || `${n.replicas} rep · ${n.concurrency} slots`}</text>
                    <rect x={16} y={51} width={W - 32} height={4} rx={2} fill="var(--line)" />
                    <rect
                      x={16}
                      y={51}
                      width={(W - 32) * Math.min(1, Math.max(0.1, st.util))}
                      height={4}
                      rx={2}
                      fill={st.state === 'ok' ? 'var(--accent)' : st.state === 'warn' ? 'var(--orange)' : 'var(--red)'}
                    />
                    {Array.from({ length: Math.min(6, n.replicas) }).map((_, i) => (
                      <circle key={i} cx={W - 14 - i * 10} cy={19} r={3.2} fill={st.state === 'down' ? 'var(--red)' : 'var(--green)'} />
                    ))}
                  </g>
                );
              })}
            </svg>
          </div>

          {viewResult ? (
            <div className="tl">
              <div className="tl-head">
                <button type="button" className="play" onClick={() => setIsPlaying(!isPlaying)} aria-label={isPlaying ? 'Pause' : 'Play'}>
                  {isPlaying ? <Pause className="w-3.5 h-3.5" /> : <Play className="w-3.5 h-3.5" />}
                </button>
                <div className="clock">t = <b>{fmtTime(simTime)}</b> <span style={{ color: 'var(--ink-3)', fontSize: '11px', marginLeft: '6px' }}>/ {fmtTime(duration)}</span></div>
                <div className="tl-legend">
                  <span><i style={{ background: 'var(--red)' }} />{designMode === 'before' ? 'current design' : 'with fixes'}</span>
                  {baselinePath && <span><i style={{ background: 'var(--ink-3)' }} />baseline</span>}
                  <span><i style={{ background: 'var(--ink-3)', opacity: 0.5 }} />SLO threshold</span>
                </div>
              </div>

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
                onPointerMove={(e) => { if (isDragging) updateTimelineScrub(e.clientX, e.currentTarget); }}
                onPointerUp={() => setIsDragging(false)}
                onPointerCancel={() => setIsDragging(false)}
              >
                {[0, 0.25, 0.5, 0.75, 1].map((v) => {
                  const y = 100 - v * 80;
                  return (
                    <g key={v}>
                      <line x1={30} x2={990} y1={y} y2={y} stroke="var(--line)" strokeWidth={0.6} />
                      <text x={2} y={y + 3}>{(errScale * v * 100).toFixed(0)}%</text>
                    </g>
                  );
                })}

                {[0, 1, 2, 3, 4, 5, 6].map((i) => (
                  <text key={i} x={xFor((i / 6) * duration) - 8} y={114}>{fmtTime((i / 6) * duration)}</text>
                ))}

                {eventMarkers.map((m, i) => {
                  const color = m.kind === 'load' ? 'var(--accent)' : ['kill', 'restart'].includes(m.kind) ? 'var(--red)' : m.kind === 'restore' ? 'var(--green)' : 'var(--orange)';
                  return <line key={i} x1={xFor(m.t)} x2={xFor(m.t)} y1={10} y2={100} stroke={color} strokeWidth={1.1} opacity={0.65} />;
                })}

                {viewResult?.slo_error_rate !== undefined && (
                  <line x1={30} x2={990} y1={yFor(viewResult.slo_error_rate)} y2={yFor(viewResult.slo_error_rate)} stroke="var(--ink-3)" strokeWidth={1} strokeDasharray="4 4" opacity={0.7} />
                )}

                {baselinePath && <path d={baselinePath} fill="none" stroke="var(--ink-3)" strokeWidth={1.5} opacity={0.9} />}
                <path d={timelinePath} fill="none" stroke="var(--red)" strokeWidth={2} />
                <path d={`${timelinePath} L990 100 L30 100 Z`} fill="var(--red)" opacity={0.12} />
                {!timeline.length && (
                  <text x={510} y={55} textAnchor="middle" fill="var(--ink-3)" style={{ fontSize: '12px' }}>Run a scenario to plot the real error-rate curve</text>
                )}

                <line x1={xFor(simTime)} x2={xFor(simTime)} y1={6} y2={104} stroke="var(--ink)" strokeWidth={1.5} />
                <circle cx={xFor(simTime)} cy={yFor(scrubErr)} r={4} fill="var(--ink)" />
              </svg>
            </div>
          ) : (
            <div className="tl tl-empty"><span>Run a scenario to generate the replay timeline.</span></div>
          )}
        </section>

        {/* RIGHT RAIL */}
        <aside className="stack right">
          <section className="card" aria-labelledby="ih">
            <h2 id="ih"><span>Node inspector</span>{selectedNode && <span className="chip">{selectedNode.type}</span>}</h2>
            {selectedNode ? (
              <div>
                <div className="insp-title">
                  <b>{selectedNode.label}</b>
                  {selectedNodeState && (
                    <span className="state" style={{
                      background: selectedNodeState.state === 'ok' ? 'rgba(109,219,160,.15)' : selectedNodeState.state === 'warn' ? 'rgba(242,166,90,.15)' : 'rgba(255,107,125,.15)',
                      color: selectedNodeState.state === 'ok' ? 'var(--green)' : selectedNodeState.state === 'warn' ? 'var(--orange)' : 'var(--red)',
                    }}>{selectedNodeState.state}</span>
                  )}
                </div>
                <div className="rows">
                  <dt>Replicas</dt><dd>{selectedNode.replicas} active</dd>
                  <dt>Capacity</dt><dd>{selectedNode.replicas * selectedNode.concurrency} concurrent slots</dd>
                  <dt>Latency (p50 / p99)</dt><dd>{selectedNode.p50 || 15}ms / {selectedNode.p99 || 45}ms</dd>
                  {selectedNode.requires && <><dt>Startup dependencies</dt><dd>{selectedNode.requires.join(', ')}</dd></>}
                  {selectedNode.rate_limit && <><dt>Rate limit</dt><dd>{selectedNode.rate_limit} rps</dd></>}
                </div>
                <div className="bar">
                  <i style={{
                    width: `${Math.min(100, (selectedNodeState?.util || 0.2) * 100)}%`,
                    background: selectedNodeState?.state === 'ok' ? 'var(--green)' : selectedNodeState?.state === 'warn' ? 'var(--orange)' : 'var(--red)',
                  }} />
                </div>
                <div className="micro" style={{ marginTop: '4px' }}>{selectedNodeState?.note}</div>
              </div>
            ) : (
              <p className="insp-empty">Click any node on the graph to inspect queuing and latency.</p>
            )}
          </section>

          <section className="card" id="story-sec" aria-labelledby="sth">
            <h2 id="sth"><span>Failure story</span><span className="chip">{viewResult?.cascade?.length || 0} steps</span></h2>
            <ol className="story">
              {viewResult && viewResult.cascade && viewResult.cascade.length > 0 ? (
                viewResult.cascade.map((c: any, i: number) => {
                  const isPast = simTime >= c.t;
                  const dotColor = c.what.includes('kill') || c.what.includes('down') ? 'var(--red)' : c.what.includes('saturated') || c.what.includes('unhealthy') ? 'var(--orange)' : c.what.includes('boot') ? 'var(--boot)' : 'var(--green)';
                  return (
                    <li key={i} className={isPast ? 'past' : ''} onClick={() => setSimTime(Math.round(c.t))}>
                      <div className="b" style={{ borderColor: dotColor }} />
                      <time>{fmtTime(c.t)}</time>
                      <div className="txt"><strong>{c.node || c.edge}:</strong> {c.detail || c.what}</div>
                    </li>
                  );
                })
              ) : (
                <>
                  {(rawScenario?.traffic?.ramp ?? []).map((r: any, i: number) => (
                    <li key={`story-ramp-${i}`} className={simTime >= r.at_s ? 'past' : ''} onClick={() => setSimTime(r.at_s)}>
                      <div className="b" style={{ borderColor: 'var(--accent)' }} />
                      <time>{fmtTime(r.at_s)}</time>
                      <div className="txt">Traffic ramps to {r.multiplier}× peak.{r.ramp_s ? ` Ramp lasts ${r.ramp_s}s.` : ''}</div>
                    </li>
                  ))}
                  {(rawScenario?.events ?? []).map((e: any, i: number) => (
                    <li key={`story-event-${i}`} className={simTime >= e.at_s ? 'past' : ''} onClick={() => setSimTime(e.at_s)}>
                      <div className="b" style={{ borderColor: ['kill', 'restart'].includes(e.action) ? 'var(--red)' : e.action === 'restore' ? 'var(--green)' : 'var(--orange)' }} />
                      <time>{fmtTime(e.at_s)}</time>
                      <div className="txt">{describeEvent(e)}</div>
                    </li>
                  ))}
                  {!rawScenario && <li><div className="txt">Loading scenario…</div></li>}
                  {rawScenario && (rawScenario.events ?? []).length === 0 && (rawScenario.traffic?.ramp ?? []).length === 0 && (
                    <li className="past"><div className="txt">Nominal steady-state load — no failures injected.</div></li>
                  )}
                </>
              )}
            </ol>
          </section>

          <section className="card" id="fixes-sec" aria-labelledby="fh">
            <h2 id="fh"><span>AI remediation</span><span className="chip">{aiFixes.length} proposals</span></h2>
            <p className="fix-context">{findings.length} static weak spots analyzed · Discrete, atomic patches proposed from the failed run. Select any and re-simulate together.</p>

            {promptError && (
              <div className="insight-card error" role="alert">
                <span className="micro">Prompt error</span>
                <span>{promptError}</span>
              </div>
            )}

            {currentPassed === true && (
              <div className="insight-card success">
                <ShieldCheck className="w-3.5 h-3.5" />
                <span><b>Survived</b> — nothing to fix for this scenario.</span>
              </div>
            )}

            {remediating && (
              <div className="thinking">
                <div className="thinking-step">Correlating cascade events with static findings…</div>
                <div className="thinking-step">Drafting discrete patch operations…</div>
                <div className="thinking-step">Validating candidate patches…</div>
              </div>
            )}

            {!result && <p className="insp-empty">Run an adversarial scenario to surface fix recommendations.</p>}

            {aiFixes.map((fix, idx) => {
              const id = fixIdOf(fix);
              const valid = fix.valid !== false;
              const severity = String(fix.severity || 'medium').toLowerCase();
              const opCount = Array.isArray(fix.patch?.ops) ? fix.patch.ops.length : 0;
              return (
                <div key={id || idx} className={`fix-card ${selectedFixIds.has(id) ? 'sel' : ''} ${valid ? '' : 'invalid'}`}>
                  <div className="fix-head">
                    <label className="fix-label">
                      <input
                        type="checkbox"
                        className="fix-check"
                        checked={selectedFixIds.has(id)}
                        disabled={!valid || applyingFixes}
                        onChange={() => toggleFix(id)}
                      />
                      <span className={`status-pill ${severity}`}>{severity}</span>
                      <b>{fix.recommended_pattern || fix.diagnosis || `Fix ${idx + 1}`}</b>
                    </label>
                  </div>
                  <p className="fix-why">{fix.rationale || fix.diagnosis || 'Gemini recommendation'}</p>
                  <div className="fix-meta">
                    <span>root · <b>{fix.root_cause_node || '—'}</b></span>
                    <span>impact · {cellText(fix.predicted_impact)}</span>
                    <span>{opCount} patch op{opCount === 1 ? '' : 's'}</span>
                  </div>
                  {!valid && (
                    <div className="micro error-text">
                      invalid: {(fix.validation_errors || []).map((v: any) => cellText(v)).join(' · ') || 'patch validation failed'}
                    </div>
                  )}
                </div>
              );
            })}

            <div className="approval-bar">
              <span className="micro">{selectedFixIds.size} selected</span>
              <div className="approval-actions">
                <button type="button" className="ghost" onClick={() => setSelectedFixIds(new Set(aiFixes.filter((f) => f.valid !== false).map(fixIdOf)))} disabled={!aiFixes.length}>Select all</button>
                <button type="button" className="ghost" onClick={() => setSelectedFixIds(new Set())} disabled={!selectedFixIds.size}>Clear</button>
                <button type="button" className="run small" onClick={handleSimulateSelectedFixes} disabled={!selectedFixes.length || applyingFixes}>
                  {applyingFixes ? <RefreshCw className="w-3 h-3 animate-spin" /> : <Play className="w-3 h-3" />}
                  {applyingFixes ? 'Applying…' : 'Simulate selected fixes'}
                </button>
              </div>
            </div>

            {lastDiff && lastDiff.changes?.length > 0 && (
              <div className="context-card">
                <div className="micro">Last patch · {lastDiff.changes.length} change(s)</div>
                <div className="micro">{lastDiff.fingerprint_before?.slice?.(0, 8) || '—'} → {lastDiff.fingerprint_after?.slice?.(0, 8) || fpShort}</div>
              </div>
            )}

            <div className="dashed" />
            <div className="micro section-label">Static hints (manual)</div>
            <div id="finds">
              {recommendations.length === 0 && <p className="insp-empty">No static hints for this architecture.</p>}
              {recommendations.map((fix) => {
                const isApplied = appliedFixes.has(fix.id);
                return (
                  <div key={fix.id} className="find">
                    <div className="find-h"><span className={`sev ${fix.severity}`}>{fix.severity}</span><b>{fix.target}</b></div>
                    <p className="fix-title">{fix.title}</p>
                    <p className="fix-why">{fix.why}</p>
                    <div className="fix">
                      <button type="button" className={`apply ${isApplied ? 'applied' : ''}`} disabled={isApplied || simulating} onClick={() => handleApplyRecommendedFix(fix)}>
                        {isApplied ? '✓ Applied' : 'Apply & re-simulate'}
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          </section>
        </aside>

        {/* COMPARE PANEL */}
        {designMode === 'compare' && (
          <section className="card compare-panel" id="compare-sec" aria-labelledby="ch">
            <h2 id="ch"><span>Before / after</span><span className="chip">seed 42 · same scenario</span></h2>
            <p className="fix-context">
              {cellText(scenarioLabel(currentScenarioObject) || selectedScenarioId)}
              {' · '}
              {baselineModel?.nodes?.length ?? '—'} → {rawModel?.nodes?.length ?? '—'} nodes · {baselineModel?.edges?.length ?? '—'} → {rawModel?.edges?.length ?? '—'} edges
            </p>
            <div className="compare-grid">
              {compareMetrics.map((m) => (
                <div className="insight-card" key={m.label}>
                  <span className="micro">{m.label}</span>
                  <div className="compare-values"><span>{m.before}</span><b>→</b><strong>{m.after}</strong></div>
                  <span className="delta">{m.delta}</span>
                </div>
              ))}
            </div>

            <div className="micro section-label">Difference report</div>
            {diffRows.length ? (
              <table className="diff-table">
                <thead><tr><th>Change</th><th>Path</th><th>Before</th><th>After</th></tr></thead>
                <tbody>
                  {diffRows.map((row, i) => (
                    <tr key={i}>
                      <td><span className={`status-pill ${row.kind === 'resolved' ? 'low' : row.kind === 'new' ? 'high' : ''}`}>{row.kind}</span></td>
                      <td className="mono-cell">{cellText(row.path)}</td>
                      <td className="mono-cell">{cellText(row.before)}</td>
                      <td className="mono-cell">{cellText(row.after)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="insp-empty">No structural differences reported — behaviour changed through the same topology.</p>
            )}
          </section>
        )}
      </main>

      {/* ===== BOTTOM PROMPT DOCK ===== */}
      <div className="prompt-dock">
        <div className="dock-side">
          <span className="context-chip">@architecture</span>
          <span className="status-pill">{isCreatingNew ? 'draft: new' : (rawModel?.name || 'no model')}</span>
        </div>
        <div className="dock-core">
          {thinkingSteps.length > 0 && (
            <div className="thinking inline">
              {thinkingSteps.map((s, i) => <span className="thinking-step" key={i}>{s}</span>)}
            </div>
          )}
          <textarea
            className="dock-input"
            value={promptText}
            onChange={(e) => setPromptText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                handleApplyPrompt();
              }
            }}
            placeholder={isCreatingNew ? 'Describe a new architecture…' : 'Ask Gemini to change only what you describe…'}
            rows={1}
          />
        </div>
        <button type="button" className="dock-send" onClick={handleApplyPrompt} disabled={isApplyingPrompt || !promptText.trim()}>
          {isApplyingPrompt ? <RefreshCw className="w-3.5 h-3.5 animate-spin" /> : <Wand2 className="w-3.5 h-3.5" />}
          <span>{isApplyingPrompt ? 'Working…' : (isCreatingNew ? 'Generate' : 'Apply')}</span>
        </button>
      </div>

      {/* ===== SLIDE-OVER DRAWER (Prompt, Spec Editor, Import/Export) ===== */}
      {isDrawerOpen && (
        <div className="drawer-backdrop" onClick={() => setIsDrawerOpen(false)}>
          <div className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <h3>
                <Sparkles className="w-4 h-4 text-accent" />
                {isCreatingNew ? 'Describe a new architecture' : 'Architecture modeler'}
              </h3>
              <button type="button" className="drawer-close" onClick={() => setIsDrawerOpen(false)}><X className="w-4 h-4" /></button>
            </div>

            <div className="drawer-tabs">
              <button type="button" className={`drawer-tab ${drawerTab === 'prompt' ? 'active' : ''}`} onClick={() => setDrawerTab('prompt')}>AI prompting</button>
              <button type="button" className={`drawer-tab ${drawerTab === 'editor' ? 'active' : ''}`} onClick={() => setDrawerTab('editor')}>YAML editor</button>
              <button type="button" className={`drawer-tab ${drawerTab === 'import_export' ? 'active' : ''}`} onClick={() => setDrawerTab('import_export')}>Import / export</button>
            </div>

            <div className="drawer-body">
              {drawerTab === 'prompt' && (
                <div>
                  <label className="field-label">
                    {isCreatingNew
                      ? 'Describe a new architecture'
                      : 'Describe architectural modifications or resilience requirements'}
                  </label>
                  <textarea
                    className="editor-textarea"
                    style={{ height: '150px' }}
                    placeholder={isCreatingNew
                      ? 'Describe a new architecture…'
                      : 'e.g. Add 2 read replicas to the database with write_ratio 0.2 and enable exponential backoff on payment gateway…'}
                    value={promptText}
                    onChange={(e) => setPromptText(e.target.value)}
                  />
                  {thinkingSteps.length > 0 && (
                    <div className="thinking">
                      {thinkingSteps.map((s, i) => <div className="thinking-step" key={i}>{s}</div>)}
                    </div>
                  )}
                  <div className="dashed" />
                  <div className="micro section-label">Quick presets</div>
                  <div className="chip-group">
                    <button type="button" className="chip-btn" onClick={() => { setIsCreatingNew(false); setPromptText('Add Redis cache with 90% warm hit rate to relieve database'); }}>+ Redis cache</button>
                    <button type="button" className="chip-btn" onClick={() => { setIsCreatingNew(false); setPromptText('Scale database with 2 read replicas and read fallback'); }}>+ Read replicas</button>
                    <button type="button" className="chip-btn" onClick={() => { setIsCreatingNew(false); setPromptText('Enable exponential backoff with jitter on all downstream retries'); }}>+ Backoff &amp; jitter</button>
                    <button type="button" className="chip-btn" onClick={() => { setIsCreatingNew(false); setPromptText('Break circular startup boot dependencies'); }}>+ Break boot deadlock</button>
                  </div>
                  {promptError && <div className="insight-card error" role="alert"><span className="micro">Error</span><span>{promptError}</span></div>}
                </div>
              )}

              {drawerTab === 'editor' && (
                <div>
                  <label className="field-label">Live architecture YAML specification</label>
                  <textarea className="editor-textarea" value={rawYaml} onChange={(e) => setRawYaml(e.target.value)} />
                </div>
              )}

              {drawerTab === 'import_export' && (
                <div>
                  <h4 className="drawer-subtitle">Import architecture spec</h4>
                  <p className="fix-context">Upload any custom YAML or JSON architecture file to simulate and stress-test.</p>
                  <button type="button" className="action-btn" onClick={() => fileInputRef.current?.click()} style={{ marginBottom: '16px' }}>
                    <Upload className="w-4 h-4" /><span>Choose file (.yaml / .json)</span>
                  </button>
                  {importStatus && <div className="insight-card"><span>{importStatus}</span></div>}
                  <div className="dashed" />
                  <h4 className="drawer-subtitle">Export architecture spec</h4>
                  <p className="fix-context">Download the current architecture definition.</p>
                  <button type="button" className="action-btn" onClick={handleExportYaml}>
                    <Download className="w-4 h-4" /><span>Download {rawModel?.name || selectedModelId || 'architecture'}.yaml</span>
                  </button>
                </div>
              )}
            </div>

            <div className="drawer-footer">
              <button type="button" className="action-btn" onClick={() => setIsDrawerOpen(false)}>Cancel</button>
              {drawerTab === 'prompt' && (
                <button type="button" className="run" onClick={handleApplyPrompt} disabled={isApplyingPrompt || !promptText.trim()}>
                  {isApplyingPrompt ? <><RefreshCw className="w-4 h-4 animate-spin" /><span>Working…</span></> : <span>{isCreatingNew ? 'Generate with Gemini' : 'Apply with Gemini'}</span>}
                </button>
              )}
              {drawerTab === 'editor' && <button type="button" className="run" onClick={handleApplySpec}>Validate &amp; apply spec</button>}
            </div>
          </div>
        </div>
      )}

      {simulating && (
        <div className="sim-overlay" role="status" aria-live="polite">
          <div className="sim-card">
            <div className="sim-spinner" />
            <strong>{remediating ? 'Gemini is proposing fixes…' : 'Simulating failure scenario…'}</strong>
            <span>{scenarioLabel(currentScenarioObject) || selectedScenarioId} · {rawModel?.name || selectedModelId}</span>
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
