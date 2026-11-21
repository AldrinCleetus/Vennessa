export interface Finding {
  rule: string;
  severity: 'critical' | 'high' | 'medium' | 'low';
  target: string;
  message: string;
  fix: string;
}

export interface CascadeEvent {
  t: number;
  what: string;
  node?: string;
  edge?: string;
  why?: string;
  detail?: string;
}

export interface TimelinePoint {
  t: number;
  err_rate: number;
  rps: number;
  p50: number;
  p99: number;
}

export interface NodeSummary {
  id: string;
  peak_util: number;
  mean_util: number;
  max_queued: number;
  min_up: number;
  replicas: number;
  requests: number;
  errors: Record<string, number>;
  restarts: number;
}

export interface SimulationResult {
  architecture: string;
  scenario: string;
  status: 'ok' | 'degraded' | 'slow_recovery' | 'no_recovery';
  requests: number;
  error_rate: number;
  p50_ms: number;
  p99_ms: number;
  breach_seconds?: number;
  recovery_s: number | null;
  wall_time_s: number;
  client_errors: Record<string, number>;
  node_errors: Record<string, Record<string, number>>;
  nodes: NodeSummary[];
  cascade: CascadeEvent[];
  slo_breached: boolean;
  duration_s: number;
  warmup_s?: number;
  slo_error_rate?: number;
  timeline: TimelinePoint[];
  affected_nodes?: string[];
  cascade_depth?: number;
  passed?: boolean;
  first_breach_s?: number | null;
  last_breach_s?: number | null;
  skipped_targets?: string[];
  sim_events?: any[];
  slo_p99_ms?: number;
  slo_recover_within_s?: number;
  slo_window_s?: number;
  architecture_fingerprint?: string;
  survived?: boolean;
  recovered?: boolean;
  fault_error_rate?: number;
  post_fault_error_rate?: number;
  seconds_out_of_slo?: number;
}

export type FixVerification = {
  verified: boolean;
  classification: 'improved' | 'worse' | 'neutral';
  scope?: 'single' | 'combination';
  verdict_improved?: boolean;
  post_fault_regression?: boolean;
  deltas?: Record<string, number>;
  better?: Record<string, boolean>;
  original?: Partial<SimulationResult>;
  candidate?: Partial<SimulationResult>;
};

export interface ModelSummary {
  id: string;
  name: string;
  nodes: number;
  edges: number;
  path: string;
}

export interface ScenarioSummary {
  id: string;
  name: string;
  description: string;
  duration_s: number;
  path: string;
}
