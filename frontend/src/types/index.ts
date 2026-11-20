export interface Finding {
  rule: str;
  severity: 'critical' | 'high' | 'medium' | 'low';
  target: string;
  message: string;
  fix: string;
}

export interface SimulationResult {
  architecture: string;
  scenario: string;
  status: 'ok' | 'degraded' | 'slow_recovery' | 'no_recovery';
  requests: number;
  error_rate: number;
  p50_ms: number;
  p99_ms: number;
  recovery_s: number | null;
  wall_time_s: number;
  client_errors: Record<string, number>;
  node_errors: Record<string, Record<string, number>>;
  cascade: Array<{ t: number; what: string; node?: string; edge?: string; why?: string }>;
  slo_breached: boolean;
  duplicate_debits?: number;
  rupee_loss?: number;
}

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
