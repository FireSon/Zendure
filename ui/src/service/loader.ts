import type { Plan, ConsumerSeries, BatterySeries, Kpis } from '../types.js';
import { api } from './api.js';

// Maps the live get_plan output into a uniform Plan.
// Dashboard/planning render off the resulting series map regardless of source.

const SLOT_MIN = 15;

function computeKpis(series: Record<string, number[]>): Kpis {
  const q = SLOT_MIN / 60;
  const col = (id: string) => series[id] ?? [];
  const buy = col('grid_buy');
  const sell = col('grid_sell');
  const pb = col('price_buy');
  const ps = col('price_sell');
  let cost = 0;
  for (let i = 0; i < buy.length; i++) cost += (buy[i] * (pb[i] ?? 0) - sell[i] * (ps[i] ?? 0)) * q;
  return { net_cost_eur: cost };
}

function buildPlan(series: Record<string, number[]>, times: string[], start: number, now: number, config: any): Plan {
  let battery: BatterySeries | null = null;
  if (series['bat_charge']) {
    battery = {
      id: 'bat',
      charge: series['bat_charge'] ?? [],
      discharge: series['bat_discharge'] ?? [],
      soc: series['bat_soc'] ?? [],
    };
  }
  const consumers: ConsumerSeries[] = (config?.consumers ?? [])
    .filter((c: any) => series[`${c.id}_power`])
    .map((c: any) => ({ id: c.id, name: c.name, type: c.type, kind: c.kind, power: series[`${c.id}_power`] }));
  return { times, start, now, series, kpis: computeKpis(series), consumers, battery };
}

// get_plan sends every SolverData's full buffer; index 12 = the quarter of the last run (3h history before). The full set is
// cached client-side and each view slices its own window out of it (no refetch on tab switch).
const HISTORY = 12;
const SLOT_MS = SLOT_MIN * 60 * 1000;
let _full: Record<string, number[]> = {};
let _known = 0;  // where the real prices end, from the price series
// epoch ms of index 0 of the cached plan, from the quarter the solver ran in
let _slot0 = 0;

function windowTimes(t0: number, count: number): string[] {
  const out: string[] = [];
  for (let i = 0; i < count; i++) {
    const d = new Date(t0 + i * SLOT_MS);
    out.push(`${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`);
  }
  return out;
}

// Re-fetch the full plan into the cache (first load + on each push).
export async function refetchLive(): Promise<void> {
  const plan = await api.getPlan();
  const full: Record<string, number[]> = {};
  for (const item of plan.series) full[item.id] = item.values;
  _full = full;
  _known = plan.series.find((x) => x.id === 'price_buy')?.known ?? 0;
  // the quarter the solver ran in; before the first run there is none yet, then now
  _slot0 = ((plan.slot ?? Math.floor(Date.now() / SLOT_MS)) - HISTORY) * SLOT_MS;
}

// Build a Plan for one window [start, end) by slicing the cached full series (clamped per series).
export function sliceLive(start: number, end: number, config: any): Plan {
  const series: Record<string, number[]> = {};
  let count = 0;
  for (const id in _full) {
    const v = _full[id].slice(start, end);
    series[id] = v;
    if (v.length > count) count = v.length;
  }
  const t0 = _slot0 + start * SLOT_MS;
  return { ...buildPlan(series, windowTimes(t0, count), t0, Math.max(0, HISTORY - start), config), priceKnown: _known };
}

// one past day as it really went (get_day): the quarters of that day from midnight, without a now
export async function loadDay(day: string, config: any): Promise<Plan> {
  const series: Record<string, number[]> = {};
  for (const item of await api.getDay(day)) series[item.id] = item.values;
  const t0 = new Date(`${day}T00:00`).getTime();
  return buildPlan(series, windowTimes(t0, Object.values(series)[0]?.length ?? 96), t0, -1, config);
}
