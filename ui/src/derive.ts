import type { Plan } from './types.js';

// Shapes the live Plan into the data structure the v2 chart builders + cards expect.
// Real data where we have it; for parts whose backend does not exist yet (30-day
// history, forecast-accuracy, 48h/7d price forecast) it uses the design's own sample
// curves, matching the concept's "vereist backend" notes in the markup.

const Q = 0.25;

interface Kind { kind: 'hp' | 'boiler' | 'ev' | 'other'; label: string; color: string; }
const KIND: Record<string, Kind> = {
  heatpump: { kind: 'hp', label: 'Warmtepomp', color: '#f47a20' },
  boiler_hp: { kind: 'boiler', label: 'Boiler', color: '#f5a623' },
  boiler_electric: { kind: 'boiler', label: 'Boiler', color: '#f5a623' },
  ev: { kind: 'ev', label: 'EV-lader', color: '#1fb89a' },
};
export function kindOf(type: string, id: string): Kind {
  return KIND[type] ?? { kind: 'other', label: id, color: '#3aa0c9' };
}

const sum = (a: number[]) => a.reduce((x, y) => x + y, 0);
const e = (a: number[]) => sum(a) * Q;
const nl = (v: number, dec: number) => v.toFixed(dec).replace('.', ',');

function consumersOf(plan: Plan) {
  return plan.consumers.map((c) => { const k = kindOf(c.type, c.id); return { id: c.id, label: k.label, kind: k.kind, color: k.color, power: c.power }; });
}

export function chartData(plan: Plan): any {
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

  const consE = sum(consumers.map((c) => e(c.power)));
  const consumptionKwh = e(load) + consE;
  const kpis = {
    net_cost: plan.kpis.net_cost_eur,
    import_kwh: plan.kpis.grid_import_kwh,
    export_kwh: plan.kpis.grid_export_kwh,
    solar_kwh: plan.kpis.solar_kwh,
    throughput: plan.kpis.battery_throughput_kwh,
    soc_min: plan.kpis.soc_min * 100,
  };
  const autarky = consumptionKwh > 0 ? Math.max(0, Math.min(100, (consumptionKwh - kpis.import_kwh) / consumptionKwh * 100)) : 0;
  const battToHome = e(discharge);
  const solarToHome = Math.max(0, consumptionKwh - kpis.import_kwh - battToHome);

  // Savings vs "no optimization": buy net load at buy price, no battery arbitrage.
  let baseline = 0;
  for (let i = 0; i < N; i++) {
    const net = load[i] + consumers.reduce((a, c) => a + c.power[i], 0) - solar[i];
    baseline += (net >= 0 ? net * price_buy[i] : net * price_sell[i]) * Q;
  }
  const metCost = kpis.net_cost;
  const save = Math.max(0, baseline - metCost);
  const metW = baseline > 0 ? Math.max(2, Math.min(100, metCost / baseline * 100)) : 100;

  const cijCons = consumers.map((c) => ({ name: c.label, value: +e(c.power).toFixed(1), color: c.color }));
  cijCons.push({ name: 'Huislast', value: +e(load).toFixed(1), color: '#c9c1b2' });

  // Backend-pending sample series (matches the concept; markup keeps the notes).
  const sav30: number[] = [], aut30: number[] = [], cum30: number[] = [], dayLbl: number[] = [];
  let cum = 0;
  for (let k = 0; k < 30; k++) { const sv = Math.max(0.4, 1.7 + 0.7 * Math.sin(k * 0.5) + 0.4 * Math.sin(k * 1.3 + 1) + 0.2 * Math.sin(k * 2.1)); sav30.push(+sv.toFixed(2)); cum += sv; cum30.push(+cum.toFixed(1)); aut30.push(+Math.max(45, Math.min(92, 68 + 13 * Math.sin(k * 0.4) + 6 * Math.sin(k * 1.1 + 2))).toFixed(0)); dayLbl.push(k + 1); }
  const solar30: number[] = [], accu30: number[] = [], net30: number[] = [], acc30: number[] = [];
  for (let k = 0; k < 30; k++) {
    solar30.push(+Math.max(0.6, 9.2 + 4.8 * Math.sin(k * 0.45) + 2.4 * Math.sin(k * 1.7 + 0.6)).toFixed(1));
    accu30.push(+Math.max(0.4, 4.4 + 2.4 * Math.sin(k * 0.62 + 1) + 1.1 * Math.sin(k * 1.9)).toFixed(1));
    net30.push(+Math.max(0.3, 4.8 - 2.3 * Math.sin(k * 0.5) + 1.5 * Math.abs(Math.sin(k * 1.1))).toFixed(1));
    acc30.push(+Math.max(78, Math.min(97, 90 + 5.5 * Math.sin(k * 0.55 + 0.4) + 2.8 * Math.sin(k * 1.6))).toFixed(0));
  }

  const fcSolar: number[] = [], fcPriceA: (number | null)[] = [], fcPriceB: (number | null)[] = [];
  for (let H = 0; H < 48; H++) { const h = H % 24; fcSolar.push(+Math.max(0, 3.6 * Math.exp(-Math.pow(h - 13, 2) / 8) * (H < 24 ? 1 : 0.82)).toFixed(2)); const base = (h < 5 ? 0.21 : h < 9 ? 0.22 + 0.13 * Math.exp(-Math.pow(h - 7.8, 2) / 1.3) : h < 16 ? 0.20 - 0.07 * Math.exp(-Math.pow(h - 13, 2) / 6) : h < 21.5 ? 0.24 + 0.16 * Math.exp(-Math.pow(h - 19, 2) / 2.4) : 0.26); if (H < 24) { fcPriceA.push(+base.toFixed(3)); fcPriceB.push(null); } else { fcPriceA.push(null); fcPriceB.push(+(base * 1.04 + 0.01 * Math.sin(H)).toFixed(3)); } }
  fcPriceB[23] = fcPriceA[23];

  const fc7Solar: number[] = [], fc7PriceA: (number | null)[] = [], fc7PriceB: (number | null)[] = [], fc7Cats: string[] = [];
  for (let H = 0; H < 168; H++) { const h = H % 24, day = Math.floor(H / 24); fc7Cats.push(String(h).padStart(2, '0') + ':00'); fc7Solar.push(+Math.max(0, 3.6 * Math.exp(-Math.pow(h - 13, 2) / 8) * (1 - day * 0.02) * (1 + 0.07 * Math.sin(day * 1.3))).toFixed(2)); const base = (h < 5 ? 0.21 : h < 9 ? 0.22 + 0.13 * Math.exp(-Math.pow(h - 7.8, 2) / 1.3) : h < 16 ? 0.20 - 0.07 * Math.exp(-Math.pow(h - 13, 2) / 6) : h < 21.5 ? 0.24 + 0.16 * Math.exp(-Math.pow(h - 19, 2) / 2.4) : 0.26); const p = +Math.max(0.05, base * (1 + 0.08 * Math.sin(day * 1.1))).toFixed(3); if (H < 48) { fc7PriceA.push(p); fc7PriceB.push(null); } else { fc7PriceA.push(null); fc7PriceB.push(+(p * 1.03 + 0.01 * Math.sin(H)).toFixed(3)); } }
  fc7PriceB[47] = fc7PriceA[47];

  const cij = {
    mix: [{ name: 'Zon', value: +solarToHome.toFixed(1), color: '#f5a623' }, { name: 'Accu', value: +battToHome.toFixed(1), color: '#1fb89a' }, { name: 'Net', value: +kpis.import_kwh.toFixed(1), color: '#e8645a' }],
    cons: cijCons, sav30, aut30, cum30, dayLbl, solar30, accu30, net30, acc30, mape: 8.4,
    comp: { save: nl(save, 2), zonder: nl(baseline, 2), met: nl(metCost, 2), metW: metW.toFixed(0) },
  };

  // expected solar yield per calendar day (kWh) from the real 7-day forecast series (kW per 15-min slot)
  const slotMs = 15 * 60 * 1000;
  const dayBase = Date.now() - (plan.now ?? 0) * slotMs;  // clock time of series slot 0
  const WD = ['zo', 'ma', 'di', 'wo', 'do', 'vr', 'za'];
  const todayKey = new Date(dayBase).toDateString();
  const perDay = new Map<string, { label: string; kwh: number }>();
  (s['solar'] ?? []).forEach((kw: number, i: number) => {
    const dt = new Date(dayBase + i * slotMs);
    const key = dt.toDateString();
    const cur = perDay.get(key) ?? { label: key === todayKey ? 'Vandaag' : WD[dt.getDay()], kwh: 0 };
    cur.kwh += (kw || 0) * 0.25;
    perDay.set(key, cur);
  });
  const solarPerDay = [...perDay.values()].map((v) => ({ label: v.label, kwh: +v.kwh.toFixed(1) }));

  return {
    N, NOW: plan.now, times: plan.times, price_buy, price_sell, solar, load, consumers,
    charge, discharge, soc, socMin: bat?.socMin ?? 0, socMax: bat?.socMax ?? 1, grid_buy, grid_sell,
    kpis, autarky, cij, fcSolar, fcPriceA, fcPriceB, fc7Solar, fc7PriceA, fc7PriceB, fc7Cats, solarPerDay,
  };
}

export interface HeadStats { power: string; powerColor: string; solar: string; save: string; }
export function headStats(d: any, hass?: any, config?: any): HeadStats {
  const i = d.NOW;
  const consNow = d.consumers.reduce((a: number, c: any) => a + c.power[i], 0);
  // Plan fallback (kW); in HA, override with live HA entities (W -> kW).
  let power = d.solar[i] + d.discharge[i] - d.load[i] - consNow - d.charge[i];
  let solar = d.solar[i];
  const stateW = (id: string | undefined): number | null => {
    const s = id ? hass?.states?.[id] : undefined;
    const n = s ? Number(s.state) : NaN;
    return Number.isFinite(n) ? n : null;
  };
  if (hass && config) {
    const slot = config.entities?.p1_total;
    let live: number | null = null;
    if (slot?.net) live = stateW(slot.net);
    else if (slot?.import) { const imp = stateW(slot.import), exp = stateW(slot.export); if (imp != null || exp != null) live = (imp ?? 0) - (exp ?? 0); }
    if (live != null) power = live / 1000;
    // Total solar = sum of the solar groups' distinct power entities (shared inverter counts once).
    const ids = [...new Set((config.solar_panels ?? []).map((g: any) => g.power_entity).filter(Boolean))] as string[];
    if (ids.length) {
      let sum = 0, any = false;
      for (const id of ids) { const v = stateW(id); if (v != null) { sum += v; any = true; } }
      if (any) solar = sum / 1000;
    }
  }
  return {
    power: (power >= 0 ? '+' : '') + nl(power * 1000, 0),
    powerColor: power >= 0 ? '#13967d' : '#e8645a',
    solar: nl(solar * 1000, 0),
    save: d.cij.comp.save,
  };
}

const CONS_ACTIONS: Record<string, string[]> = { hp: ['start', 'pause', 'stop', 'skip'], boiler: ['start', 'stop'], ev: ['start', 'pause', 'skip'], other: ['start', 'stop'] };
const ACT_META: Record<string, { l: string; c: string }> = { start: { l: 'Start', c: '#13967d' }, pause: { l: 'Pauze', c: '#f5a623' }, stop: { l: 'Stop', c: '#e8645a' }, skip: { l: 'Skip', c: '#3aa0c9' } };
const ST_MAP: Record<string, [string, string, string]> = { start: ['Actief', '#13967d', '#1fb89a'], pause: ['Gepauzeerd', '#f5a623', '#f5a623'], stop: ['Gestopt', '#e8645a', '#e8645a'], skip: ['Overgeslagen', '#3aa0c9', '#3aa0c9'] };

// One control card per configured consumer (merging plan runtime where the consumer has a
// power series; inactive otherwise), so every consumer gets a tile even if not scheduled.
export function consumerCards(d: any, consAction: Record<string, string>, consumers?: any[]) {
  const i = d.NOW;
  const byId = new Map<string, any>(d.consumers.map((c: any) => [c.id, c]));
  const list = consumers ?? d.consumers.map((c: any) => ({ id: c.id, type: c.kind }));
  return list.map((cfg: any) => {
    const k = kindOf(cfg.type, cfg.id);
    const planC = byId.get(cfg.id);
    const power: number[] = planC ? planC.power : new Array(d.N).fill(0);
    const pw = power[i]; const onNow = pw > 0.02;
    const eToday = sum(power) * Q;
    let nextIdx = -1; for (let j = i + 1; j < d.N; j++) { if (power[j] > 0.02) { nextIdx = j; break; } }
    const chosen = consAction[cfg.id] || (onNow ? 'start' : '');
    const st: [string, string, string] = (chosen && ST_MAP[chosen]) ? ST_MAP[chosen] : (onNow ? ['Actief', '#13967d', '#1fb89a'] : ['Inactief', 'var(--zm-text-dim)', 'var(--zm-border)']);
    return {
      id: cfg.id, label: k.label, kind: k.kind, color: k.color, type: cfg.type,
      isHp: k.kind === 'hp', isBoiler: k.kind === 'boiler', isEv: k.kind === 'ev',
      iconBg: 'color-mix(in srgb, ' + k.color + ' 15%, transparent)', iconColor: k.color,
      powerW: (pw * 1000).toFixed(0), statusLabel: st[0], statusColor: st[1], statusDot: st[2],
      energyToday: nl(eToday, 1), nextLabel: nextIdx >= 0 ? d.times[nextIdx] : '-',
      actions: (CONS_ACTIONS[k.kind] || ['start', 'stop']).map((a) => { const m = ACT_META[a]; const active = chosen === a; return { id: a, label: m.l, isStart: a === 'start', isPause: a === 'pause', isStop: a === 'stop', isSkip: a === 'skip', fg: active ? '#fff' : m.c, bg: active ? m.c : 'color-mix(in srgb, ' + m.c + ' 12%, transparent)', bd: active ? m.c : 'transparent' }; }),
    };
  });
}

export interface PlanTable { headers: string[]; rows: string[][]; }
export function planTable(d: any): PlanTable {
  const headers = ['tijd', '€ in', '€ uit', 'zon', 'last', ...d.consumers.map((c: any) => c.label), 'net in', 'net uit', 'soc'];
  const rows = d.times.map((t: string, i: number) => [
    t, d.price_buy[i].toFixed(3), d.price_sell[i].toFixed(3), d.solar[i].toFixed(2), d.load[i].toFixed(2),
    ...d.consumers.map((c: any) => c.power[i].toFixed(2)), d.grid_buy[i].toFixed(2), d.grid_sell[i].toFixed(2), (d.soc[i] * 100).toFixed(0),
  ]);
  return { headers, rows };
}

export function popupView(d: any, popup: { kind: string; slot: number; id?: string } | null, consAction: Record<string, string>) {
  if (!popup) return { show: false };
  const slot = popup.slot;
  if (popup.kind === 'bat') {
    const net = d.discharge[slot] - d.charge[slot];
    return { show: true, isCons: false, label: 'Zendure accu', time: d.times[slot], color: net >= 0 ? '#1fb89a' : '#3aa0c9', powerText: Math.abs(net) < 1e-2 ? 'Idle' : (net >= 0 ? 'Ontladen ' : 'Laden ') + nl(Math.abs(net), 2) + ' kW', actions: [] };
  }
  const cc = d.consumers.find((x: any) => x.id === popup.id);
  if (!cc) return { show: false };
  const pw = cc.power[slot]; const chosen = consAction[cc.id] || '';
  return {
    show: true, isCons: true, label: cc.label, time: d.times[slot], color: cc.color,
    powerText: pw > 0.02 ? 'Inzet ' + nl(pw, 2) + ' kW' : 'Niet actief in dit slot',
    actions: (CONS_ACTIONS[cc.kind] || ['start', 'stop']).map((a) => { const m = ACT_META[a]; const active = chosen === a; return { id: a, label: m.l, isStart: a === 'start', isPause: a === 'pause', isStop: a === 'stop', isSkip: a === 'skip', fg: active ? '#fff' : m.c, bg: active ? m.c : 'color-mix(in srgb, ' + m.c + ' 12%, transparent)', bd: active ? m.c : 'transparent' }; }),
  };
}
