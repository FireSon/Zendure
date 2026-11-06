import type { Plan, HistoryDay } from './types.js';
import { t } from './service/i18n.js';

// Shapes the live Plan, and the 30-day history from get_history, into the data structure the chart builders and cards expect.

const Q = 0.25;

interface Kind { kind: 'hp' | 'boiler' | 'ev' | 'other'; label: string; color: string; name: string; }
// Keyed on the consumer's own kind first, then on its subentry type; the label is the configured name.
const KIND: Record<string, { kind: Kind['kind']; color: string; name: string }> = {
  washingmachine: { kind: 'other', color: '#3aa0c9', name: 'Wasmachine' },
  dryer: { kind: 'other', color: '#8f7ae5', name: 'Droger' },
  dishwasher: { kind: 'other', color: '#2fbf9f', name: 'Vaatwasser' },
  house: { kind: 'hp', color: '#f47a20', name: 'Warmtepomp' },
  boiler: { kind: 'boiler', color: '#f5a623', name: 'Boiler' },
  pool: { kind: 'hp', color: '#4aa3df', name: 'Zwembad' },
  electric_vehicle: { kind: 'ev', color: '#1fb89a', name: 'Laadpaal' },
  heating_heat_pump: { kind: 'hp', color: '#f47a20', name: 'Warmtepomp' },
  boiler_heat_pump: { kind: 'boiler', color: '#f5a623', name: 'Boiler' },
  boiler_electric: { kind: 'boiler', color: '#f5a623', name: 'Boiler' },
  pool_heat_pump: { kind: 'hp', color: '#4aa3df', name: 'Zwembad' },
};
export function kindOf(type: string, name: string, kind?: string): Kind {
  const key = KIND[kind ?? ''] ? kind ?? '' : type;
  const k = KIND[key] ?? { kind: 'other' as const, color: '#3aa0c9', name: type || 'Verbruiker' };
  return { ...k, name: t(`ui.kind.${key || 'consumer'}`, k.name), label: name };
}

const sum = (a: number[]) => a.reduce((x, y) => x + y, 0);
export const nl = (v: number, dec: number) => v.toFixed(dec).replace('.', ',');
// a hass state as a finite number, null when it is missing or not a number
export const stateNum = (hass: any, id?: string): number | null => { const n = id && hass?.states?.[id] ? Number(hass.states[id].state) : NaN; return Number.isFinite(n) ? n : null; };
// one colour per solar group, in config order: the header hint and the solar table use the same
export const PANEL_COLORS = ['#f47a20', '#1fb89a', '#f5a623', '#3aa0c9'];

function consumersOf(plan: Plan) {
  return plan.consumers.map((c) => { const k = kindOf(c.type, c.name || c.id, c.kind); return { id: c.id, label: k.label, kind: k.kind, color: k.color, power: c.power }; });
}

export function chartData(plan: Plan, history?: HistoryDay[]): any {
  const s = plan.series;
  const N = plan.times.length;
  const bat = plan.battery;
  const charge = bat ? bat.charge : new Array(N).fill(0);
  const discharge = bat ? bat.discharge : new Array(N).fill(0);
  const soc = bat ? bat.soc : new Array(N).fill(0);
  const load = s['load'] ?? new Array(N).fill(0);
  const solar = s['solar'] ?? new Array(N).fill(0);
  const grid_buy = s['grid_buy'] ?? new Array(N).fill(0);
  const grid_sell = s['grid_sell'] ?? new Array(N).fill(0);
  const price_buy = s['price_buy'] ?? new Array(N).fill(0);
  const price_sell = s['price_sell'] ?? new Array(N).fill(0);
  const consumers = consumersOf(plan);

  // Savings vs "no optimization": buy net load at buy price, no battery arbitrage.
  let baseline = 0;
  for (let i = 0; i < N; i++) {
    const net = load[i] + consumers.reduce((a, c) => a + c.power[i], 0) - solar[i];
    baseline += (net >= 0 ? net * price_buy[i] : net * price_sell[i]) * Q;
  }
  const metCost = plan.kpis.net_cost_eur;
  const save = Math.max(0, baseline - metCost);

  // realised history per calendar day, straight from the recorder (get_history)
  const hist = history ?? [];
  const sav30: number[] = [], aut30: number[] = [], cum30: number[] = [], dayLbl: number[] = [];
  const solar30: number[] = [], accu30: number[] = [], net30: number[] = [], acc30: number[] = [];
  let cum = 0;
  hist.forEach((h, k) => {
    sav30.push(h.savings);
    cum += h.savings;
    cum30.push(+cum.toFixed(2));
    aut30.push(h.autarky ?? 0);
    dayLbl.push(k + 1);
    solar30.push(h.solar);
    accu30.push(h.accu);
    net30.push(h.net);
    acc30.push(h.accuracy ?? 0);
  });

  const slotMs = 15 * 60 * 1000;
  const dayBase = Date.now() - (plan.now ?? 0) * slotMs;  // clock time of series slot 0
  const WD = t('ui.weekdays', 'zo,ma,di,wo,do,vr,za').split(',');
  const todayKey = new Date(dayBase).toDateString();

  // the forecast charts: the real 15-min solar series averaged into clock-hour buckets, with the
  // day starts (index + weekday label) so the axis and its dividers follow the real calendar.
  const hourStart = new Date(dayBase); hourStart.setMinutes(0, 0, 0);
  const hourBuckets = (hours: number, id = 'solar', digits = 2) => {
    const total = new Array(hours).fill(0), n = new Array(hours).fill(0);
    (s[id] ?? []).forEach((v: number, i: number) => {
      const b = Math.floor((dayBase + i * slotMs - hourStart.getTime()) / 3600000);
      if (b >= 0 && b < hours) { total[b] += v || 0; n[b] += 1; }
    });
    const cats: string[] = [], days: { at: number; label: string }[] = [];
    for (let b = 0; b < hours; b++) {
      const dt = new Date(hourStart.getTime() + b * 3600000);
      cats.push(String(dt.getHours()).padStart(2, '0') + ':00');
      if (b === 0 || dt.getHours() === 0) days.push({ at: b, label: dt.toDateString() === todayKey ? t('ui.app.today', 'vandaag') : WD[dt.getDay()] });
    }
    return { kw: total.map((t, b) => +(n[b] ? t / n[b] : 0).toFixed(digits)), cats, days };
  };
  // the buy price per clock hour: solid while it is the real price, dashed where the shape model takes over
  const known = (plan.priceKnown ?? 0) * 1000;
  const priceLines = (hours: number) => {
    const a: (number | null)[] = [], b: (number | null)[] = [];
    hourBuckets(hours, 'price_buy', 3).kw.forEach((v, h) => { const real = hourStart.getTime() + (h + 1) * 3600000 <= known; a.push(real ? v : null); b.push(real ? null : v); });
    const n = a.filter((v) => v !== null).length;
    if (n) b[n - 1] = a[n - 1];  // the dashed line starts where the solid one ends
    return { a, b, n };
  };
  const fc48 = hourBuckets(48);
  const fcSolar = fc48.kw, fcCats = fc48.cats, fcDays = fc48.days;
  const { a: fcPriceA, b: fcPriceB } = priceLines(48);

  const fc7 = hourBuckets(168);
  const fc7Solar = fc7.kw, fc7Cats = fc7.cats, fc7Days = fc7.days;
  const { a: fc7PriceA, b: fc7PriceB, n: fc7Known } = priceLines(168);

  const cij = {
    sav30, aut30, cum30, dayLbl, solar30, accu30, net30, acc30,
    comp: { save: nl(save, 2) },
  };

  // expected solar yield per calendar day (kWh) from the real 7-day forecast series (kW per 15-min slot)
  const perDay = new Map<string, { label: string; kwh: number }>();
  (s['solar'] ?? []).forEach((kw: number, i: number) => {
    const dt = new Date(dayBase + i * slotMs);
    const key = dt.toDateString();
    const cur = perDay.get(key) ?? { label: key === todayKey ? t('ui.consumers.today', 'Vandaag') : WD[dt.getDay()], kwh: 0 };
    cur.kwh += (kw || 0) * 0.25;
    perDay.set(key, cur);
  });
  const solarPerDay = [...perDay.values()].map((v) => ({ label: v.label, kwh: +v.kwh.toFixed(1) }));

  return {
    series: s,  // every plan series by id, for the rows that read their own (a battery's charge and discharge)
    N, NOW: plan.now, times: plan.times, price_buy, price_sell, solar, load, consumers,
    charge, discharge, soc, grid_buy, grid_sell,
    cij, fcSolar, fcCats, fcDays, fcPriceA, fcPriceB, fc7Solar, fc7PriceA, fc7PriceB, fc7Known, fc7Cats, fc7Days, solarPerDay,
  };
}

export interface HeadStats { p1: string; p1Color: string; p1Dir: string; phases: string[]; power: string; powerColor: string; solar: string; panels: { name: string; w: string }[]; save: string; cons: string; }
export function headStats(d: any, hass?: any, config?: any): HeadStats {
  const i = d.NOW;
  const consNow = d.consumers.reduce((a: number, c: any) => a + c.power[i], 0);
  // Plan fallback (kW); in HA, override with live HA entities (W -> kW).
  let p1 = d.grid_buy[i] - d.grid_sell[i];
  let power = d.discharge[i] - d.charge[i];
  let cons = consNow;
  let phases: string[] = [];
  let panels: { name: string; w: string }[] = [];
  let solar = d.solar[i];
  let save = d.cij.comp.save;
  const stateW = (id: string | undefined) => stateNum(hass, id);
  if (hass && config) {
    // the P1 meter, straight from the two sensors the distribution publishes: import minus export
    const dist = config.distribution ?? {};
    const imp = stateW(dist.grid_import), exp = stateW(dist.grid_export);
    if (imp != null || exp != null) p1 = ((imp ?? 0) - (exp ?? 0)) / 1000;
    // the meter per phase as the distribution computes it; shown only when all three are there
    const ph = [stateW(dist.grid_l1), stateW(dist.grid_l2), stateW(dist.grid_l3)];
    if (ph.every((v) => v != null)) phases = ph.map((v) => nl(v as number, 0));
    // the fleet, from the distribution's own power sensors
    const dis = stateW(dist.discharge_power), chg = stateW(dist.charge_power);
    if (dis != null || chg != null) power = ((dis ?? 0) - (chg ?? 0)) / 1000;
    // the consumers together, each from its own measured power entity
    const watts = (config.consumers ?? []).map((c: any) => measuredW(hass, c.power_entity)).filter((v: number) => Number.isFinite(v));
    if (watts.length) cons = watts.reduce((a: number, v: number) => a + v, 0) / 1000;
    // Total solar = sum of the solar groups' distinct power entities (shared inverter counts once).
    const ids = [...new Set((config.solar_panels ?? []).map((g: any) => g.power_entity).filter(Boolean))] as string[];
    if (ids.length) {
      let sum = 0, any = false;
      for (const id of ids) { const v = stateW(id); if (v != null) { sum += v; any = true; } }
      if (any) solar = sum / 1000;
    }
    // every solar group with its own reading, for the hint on the solar tile
    panels = (config.solar_panels ?? []).map((g: any) => ({ name: g.name || g.id, w: nl(stateW(g.power_entity) ?? 0, 0) }));
    // the saving comes from the backend: realised since midnight plus the plan's margin over the rest of today
    const booked = stateW(config.distribution?.savings_today);
    if (booked != null) save = nl(booked, 2);
  }
  return {
    p1: nl(p1 * 1000, 0),
    p1Color: p1 > 0 ? '#e8645a' : '#13967d',  // buying from the grid is red, exporting is green
    p1Dir: p1 > 0 ? t('ui.app.import', 'import') : t('ui.app.export', 'export'),
    phases,
    power: (power > 0 ? '+' : '') + nl(power * 1000, 0),
    powerColor: '#13967d',
    solar: nl(solar * 1000, 0),
    panels,
    save,
    cons: nl(cons * 1000, 0),
  };
}

const ACT_META: Record<string, { l: string; c: string }> = { start: { l: 'Start', c: '#13967d' }, pause: { l: 'Pauze', c: '#f5a623' }, stop: { l: 'Stop', c: '#e8645a' }, skip: { l: 'Skip', c: '#3aa0c9' }, arm_off: { l: 'Niet armen', c: '#f5a623' }, arm_on: { l: 'Weer beheren', c: '#f5a623' } };

// One control card per configured consumer (merging plan runtime where the consumer has a
// power series; inactive otherwise), so every consumer gets a tile even if not scheduled.
const STATE_NL: Record<string, [string, string]> = {
  running: ['Draait', '#13967d'], armed: ['Wacht', '#f5a623'], planned: ['Gepland', '#3aa0c9'],
  ready: ['Klaar', '#13967d'], aborted: ['Gestopt', '#e8645a'], none: ['Uit', 'var(--zm-text-dim)'],
  unmanaged: ['Niet beheerd', '#f5a623'],
};

// Reads the measured power straight from the consumer's own HA entity; kW sensors are scaled to W.
function measuredW(hass: any, eid?: string): number {
  const st = eid ? hass?.states?.[eid] : null;
  const v = st ? Number(st.state) : NaN;
  if (!Number.isFinite(v)) return NaN;
  return String(st.attributes?.unit_of_measurement ?? '').toLowerCase() === 'kw' ? v * 1000 : v;
}

export function consumerCards(d: any, consumers?: any[], hass?: any, actions?: Record<string, any[]>, tiles?: Record<string, string[]>) {
  const i = d.NOW;
  const byId = new Map<string, any>(d.consumers.map((c: any) => [c.id, c]));
  const list = consumers ?? d.consumers.map((c: any) => ({ id: c.id, type: c.kind }));
  return list.map((cfg: any) => {
    const k = kindOf(cfg.type, cfg.name || cfg.id, cfg.kind);
    const planC = byId.get(cfg.id);
    const power: number[] = planC ? planC.power : new Array(d.N).fill(0);
    const eToday = sum(power) * Q;
    let nextIdx = -1; for (let j = i + 1; j < d.N; j++) { if (power[j] > 0.02) { nextIdx = j; break; } }
    const state = String(hass?.states?.[cfg.state]?.state ?? 'none');
    const [fallback, color] = STATE_NL[state] ?? STATE_NL.none;
    const label = t(`ui.state.${STATE_NL[state] ? state : 'none'}`, fallback);
    const next = nextIdx >= 0 ? d.times[nextIdx] : '';
    const watts = measuredW(hass, cfg.power_entity);
    return {
      id: cfg.id, label: k.label, kind: k.kind, color: k.color, type: cfg.type,
      isHp: k.kind === 'hp', isBoiler: k.kind === 'boiler', isEv: k.kind === 'ev',
      iconBg: 'color-mix(in srgb, ' + k.color + ' 15%, transparent)', iconColor: k.color,
      sub: k.name,
      powerW: Number.isFinite(watts) ? watts.toFixed(0) : '-',
      statusLabel: (state === 'armed' || state === 'planned') && next ? t('ui.consumers.until', '{state} tot {time}', { state: label, time: next }) : label,
      statusColor: color, statusDot: color,
      energyToday: nl(eToday, 1), nextLabel: next || '-', hasNext: !!next,
      // the entity fields its type shows on the row, read live from HA; an unlinked field shows '-'
      entityTiles: (tiles?.[cfg.id] ?? []).map((fid) => {
        const st = cfg[fid] ? hass?.states?.[cfg[fid]] : null;
        const v = st ? Number(st.state) : NaN;
        return { k: t(`field.${fid}.name`, fid), v: Number.isFinite(v) ? nl(v, 1) : (st?.state ?? '-'), u: st?.attributes?.unit_of_measurement ? ' ' + st.attributes.unit_of_measurement : '' };
      }),
      actions: (actions?.[cfg.id] ?? []).map((a: any) => {
        const on = !a.when?.length || a.when.includes(state);   // dimmed, not hidden: every row keeps the same strip
        const c = ACT_META[a.id]?.c ?? '#3aa0c9';
        return { id: a.id, label: a.label, icon: a.icon, enabled: on, fg: c, bg: 'color-mix(in srgb, ' + c + ' 12%, transparent)', bd: 'transparent' };
      }),
    };
  });
}
