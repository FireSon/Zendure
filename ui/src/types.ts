export type TabId = 'dashboard' | 'cijfers' | 'consumers' | 'forecast' | 'devices' | 'settings';
export type ScenarioId = 'live' | 'day' | 'day_no_ev' | 'winter' | 'summer_negative';
export type Theme = 'light' | 'dark';
export type Lead = 'balanced' | 'orange' | 'teal';

export interface SolveTable {
  columns: string[];
  rows: Array<Record<string, number | string>>;
}

// One SolverData series from /api/get_plan.
export interface PlanSeries {
  id: string;
  uom: string;
  phase: string | null;
  source: string;
  values: number[];
}

export type EditType = 'int' | 'double' | 'string' | 'dropdown' | 'boolean' | 'entity' | 'device' | 'command' | 'list';

// One OptimizerField descriptor from /api/get_schema (dataclasses.asdict of OptimizerField).
export interface Field {
  id: string;
  title_key: string;
  edit_type: EditType;
  required: boolean;
  default: unknown;
  order: number;
  readonly: boolean;
  section_key: string | null;
  description_key: string | null;
  options: Record<string, string> | null;
  min_value: number | null;
  max_value: number | null;
  visible_when: Record<string, unknown> | null;
  secret?: boolean;
  destructive: boolean;
  expert: boolean;
  list_item_schema: Record<string, Field> | null;
}

export type Schema = Record<string, Field[]>;

export interface SaveResult {
  ok: boolean;
  errors?: string[];
}

export interface Kpis {
  net_cost_eur: number;
  grid_import_kwh: number;
  grid_export_kwh: number;
  solar_kwh: number;
  battery_throughput_kwh: number;
  soc_min: number;
}

export interface ConsumerSeries {
  id: string;
  type: string;
  power: number[];
}

export interface BatterySeries {
  id: string;
  charge: number[];
  discharge: number[];
  soc: number[];
  socMin: number | null;
  socMax: number | null;
  capacityKwh: number | null;
}

export interface Plan {
  scenario: ScenarioId;
  times: string[];
  slotMinutes: number;
  now: number;
  series: Record<string, number[]>;
  kpis: Kpis;
  consumers: ConsumerSeries[];
  battery: BatterySeries | null;
  config: any;
}
