import type { Plan, ConsumerSeries, BatterySeries, Kpis, PlanSeries } from '../types.js';
import { api } from './api.js';

// Maps the live get_plan output into a uniform Plan.
// Dashboard/planning render off the resulting series map regardless of source.

const SLOT_MIN = 15;

function sum(a: number[]): number {
  return a.reduce((s, v) => s + v, 0);
}

function computeKpis(series: Record<string, number[]>, battery: BatterySeries | null): Kpis {
  const q = SLOT_MIN / 60;
  const col = (id: string) => series[id] ?? [];
  const buy = col('grid_buy');
  const sell = col('grid_sell');
  const pb = col('price_buy');
  const ps = col('price_sell');
  const solar = col('solar');
  let cost = 0;
  for (let i = 0; i < buy.length; i++) cost += (buy[i] * (pb[i] ?? 0) - sell[i] * (ps[i] ?? 0)) * q;
  const throughput = battery ? sum(battery.charge.map((v, i) => v + (battery.discharge[i] ?? 0))) * q : 0;
  const socMin = battery && battery.soc.length ? Math.min(...battery.soc) : 0;
  return {
    net_cost_eur: cost,
    grid_import_kwh: sum(buy) * q,
    grid_export_kwh: sum(sell) * q,
    solar_kwh: sum(solar) * q,
    battery_throughput_kwh: throughput,
    soc_min: socMin,
  };
}

function buildPlan(series: Record<string, number[]>, times: string[], now: number, config: any): Plan {
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
  return { times, now, series, kpis: computeKpis(series, battery), consumers, battery };
}

// get_plan sends every SolverData's full buffer; index 12 = now (3h history before). The full set is
// cached client-side and each view slices its own window out of it (no refetch on tab switch).
const HISTORY = 12;
let _full: Record<string, number[]> = {};
let _known = 0;  // where the real prices end, from the price series

function windowTimes(start: number, count: number): string[] {
  const slotMs = SLOT_MIN * 60 * 1000;
  const t0 = Math.floor(Date.now() / slotMs) * slotMs + (start - HISTORY) * slotMs;
  const out: string[] = [];
  for (let i = 0; i < count; i++) {
    const d = new Date(t0 + i * slotMs);
    out.push(`${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`);
  }
  return out;
}

// Re-fetch the full plan into the cache (first load + on each push).
export async function refetchLive(): Promise<void> {
  const arr: PlanSeries[] = await api.getPlan();
  const full: Record<string, number[]> = {};
  for (const item of arr) full[item.id] = item.values;
  _full = full;
  _known = arr.find((x) => x.id === 'price_buy')?.known ?? 0;
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
  return { ...buildPlan(series, windowTimes(start, count), Math.max(0, HISTORY - start), config), priceKnown: _known };
}
