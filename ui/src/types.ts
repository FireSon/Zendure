export type TabId = 'dashboard' | 'devices' | 'solar' | 'cijfers' | 'settings';
export type Theme = 'light' | 'dark';

// One SolverData series from /api/get_plan.
export interface PlanSeries {
  id: string;
  uom: string;
  phase: string | null;
  source: string;
  known?: number | null;  // epoch seconds where the real values end; later ones are a forecast
  values: number[];
}

// One HA selector spec, e.g. { text: {} }, { number: { min: 0 } }, { select: { options: [...] } }.
export interface Selector {
  text?: { type?: string };
  number?: { min?: number; max?: number; step?: number; mode?: string; unit_of_measurement?: string };
  boolean?: Record<string, never>;
  entity?: Record<string, unknown>;
  device?: Record<string, unknown>;
  select?: { mode?: string; options: Array<{ value: string; label: string }> };
  button?: { label: string; command: string; icon?: string };
}

// One ConfigField descriptor from zenmaster/get_schema (dataclasses.asdict of ConfigField, minus cast).
export interface Field {
  id: string;
  selector: Selector;
  is_key: boolean;
  required: boolean;
  readonly: boolean;
  expert: boolean;
  hidden?: boolean;
  transient?: boolean;
  visible_when: Record<string, unknown> | null;
  section_key?: string | null;
  entry?: string;  // set by the UI when a card carries a field of another entry; it reads and saves there
}

// One entry of a consumer's ACTIONS list: a row button, enabled while the live status is in `when`.
export interface RowAction {
  id: string;
  label: string;
  icon: string;
  when: string[];
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

export interface HistoryDay {
  day: string;
  solar: number;
  accu: number;
  net: number;
  autarky: number | null;
  savings: number;
  accuracy: number | null;
}

export interface ConsumerSeries {
  id: string;  // the subentry id
  name?: string;  // the subentry title, what the user sees
  type: string;
  kind?: string;
  power: number[];
}

export interface BatterySeries {
  id: string;
  charge: number[];
  discharge: number[];
  soc: number[];
}

export interface Plan {
  times: string[];
  now: number;
  series: Record<string, number[]>;
  kpis: Kpis;
  consumers: ConsumerSeries[];
  battery: BatterySeries | null;
  priceKnown?: number;  // epoch seconds where the real prices end
}
