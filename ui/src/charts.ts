import type { Theme } from './types.js';

// ECharts option builders, lifted verbatim from the EnergyMaster v2 design concept.
// Each takes `d` (the chart-data shape produced by derive.chartData) and `C` (theme
// palette). ECharts canvas cannot read CSS vars, so colours are computed here.

export function chartColors(theme: Theme) {
  const dark = theme === 'dark';
  return {
    solar: '#f5a623', consumers: '#f47a20', load: dark ? '#5f594e' : '#d8d0c1',
    import: '#e8645a', export: '#8ad2c0', charge: '#3aa0c9', discharge: '#1fb89a',
    soc: '#1fb89a', priceBuy: '#f47a20', priceSell: '#1fb89a',
    text: dark ? '#f2eee7' : '#34322d', textDim: dark ? '#a79e90' : '#8d877b',
    line: dark ? 'rgba(255,255,255,0.07)' : '#ece6db', grid: dark ? 'rgba(255,255,255,0.05)' : 'rgba(70,52,24,0.05)',
    tipBg: dark ? '#262320' : '#ffffff', tipBorder: dark ? 'rgba(255,255,255,0.1)' : '#ece6db', tipText: dark ? '#f2eee7' : '#34322d',
  };
}
export type ChartColors = ReturnType<typeof chartColors>;

const F = 'Nunito Sans';

function tip(C: ChartColors) {
  return { backgroundColor: C.tipBg, borderColor: C.tipBorder, borderWidth: 1, textStyle: { color: C.tipText, fontFamily: F, fontSize: 12 }, extraCssText: 'border-radius:12px;box-shadow:0 8px 24px rgba(0,0,0,0.12);' };
}
function axis(C: ChartColors) {
  return { axisLine: { lineStyle: { color: C.line } }, axisLabel: { color: C.textDim, fontFamily: F, fontSize: 11 }, splitLine: { lineStyle: { color: C.grid } } };
}
function ival(n: number) { return Math.max(0, Math.round(n / 8) - 1); }
const neg = (a: number[]) => a.map((v) => -v);

export function flowOption(d: any, C: ChartColors) {
  const consSum = new Array(d.N).fill(0);
  for (const cc of d.consumers) cc.power.forEach((v: number, i: number) => (consSum[i] += v));
  const bars = [
    { name: 'Zon', stack: 'pos', color: C.solar, data: d.solar }, { name: 'Teruglevering', stack: 'pos', color: C.export, data: d.grid_sell },
    { name: 'Huislast', stack: 'neg', color: C.load, data: neg(d.load) }, { name: 'Verbruikers', stack: 'neg', color: C.consumers, data: neg(consSum) }, { name: 'Netinkoop', stack: 'neg', color: C.import, data: neg(d.grid_buy) },
  ].map((b: any) => ({ name: b.name, type: 'bar', xAxisIndex: 0, yAxisIndex: 0, stack: b.stack, data: b.data, itemStyle: { color: b.color, borderRadius: 2 }, barCategoryGap: '18%', emphasis: { focus: 'series' } })) as any[];
  bars[0].markLine = { silent: true, symbol: 'none', data: [{ xAxis: d.NOW }], lineStyle: { color: '#f47a20', width: 1.6, opacity: 0.7 }, label: { formatter: 'nu', color: '#f47a20', fontFamily: F, fontWeight: 700, fontSize: 11 } };
  const lines = [{ name: 'Inkoopprijs', color: C.priceBuy, type: 'solid', data: d.price_buy }, { name: 'Verkoopprijs', color: C.priceSell, type: 'dashed', data: d.price_sell }].map((l) => ({ name: l.name, type: 'line', xAxisIndex: 0, yAxisIndex: 1, data: l.data, symbol: 'none', smooth: true, lineStyle: { color: l.color, width: 2, type: l.type }, z: 5 }));
  const soc = { name: 'SoC', type: 'line', xAxisIndex: 0, yAxisIndex: 2, data: d.soc.map((v: number) => v * 100), symbol: 'none', smooth: true, lineStyle: { color: C.soc, width: 2.4, type: 'dotted' }, markLine: { silent: true, symbol: 'none', data: [{ yAxis: d.socMin * 100, lineStyle: { color: C.import, type: 'dashed', width: 1 }, label: { formatter: 'min SoC', color: C.import, fontFamily: F, fontSize: 9, position: 'insideEndTop' } }, { yAxis: d.socMax * 100, lineStyle: { color: C.textDim, type: 'dashed', width: 1 }, label: { formatter: 'max SoC', color: C.textDim, fontFamily: F, fontSize: 9, position: 'insideEndTop' } }] } };
  const lanes: any[] = []; const signed = d.discharge.map((v: number, j: number) => v - d.charge[j]); lanes.push({ label: 'Zendure +/-', kind: 'bat', values: signed, scale: Math.max(0.001, ...signed.map(Math.abs)) });
  for (const cc of d.consumers) lanes.push({ label: cc.label, kind: 'cons', values: cc.power, scale: Math.max(0.001, ...cc.power.map(Math.abs)) });
  const laneLabels = lanes.map((l) => l.label); const ldata: any[] = []; lanes.forEach((lane, row) => lane.values.forEach((v: number, slot: number) => { if (Math.abs(v) < 1e-3) return; ldata.push([slot, row, v, lane.kind === 'bat' ? 1 : 0, lane.scale]); }));
  const swim = { type: 'custom', xAxisIndex: 1, yAxisIndex: 3, renderItem: (_prm: any, api: any) => { const slot = api.value(0), row = api.value(1), val = api.value(2), isBat = api.value(3) === 1, scale = api.value(4) || 1; const start = api.coord([slot, row]), end = api.coord([slot + 1, row]); const rowH = Math.max(2, api.size([0, 1])[1] - 1); const w = Math.max(1, end[0] - start[0]); const bh = Math.max(1.5, rowH * Math.min(1, Math.abs(val) / scale)); const fill = isBat ? (val >= 0 ? C.discharge : C.charge) : C.consumers; const top = start[1] - rowH / 2; return { type: 'group', children: [{ type: 'rect', shape: { x: start[0] + 0.5, y: top, width: Math.max(1, w - 1), height: rowH, r: 2 }, style: { fill: C.grid, opacity: 0.28 } }, { type: 'rect', shape: { x: start[0] + 1.5, y: start[1] + rowH / 2 - bh, width: Math.max(1, w - 3), height: bh, r: 1 }, style: { fill, opacity: 0.96 } }, { type: 'rect', shape: { x: start[0] + 0.5, y: top, width: Math.max(1, w - 1), height: rowH, r: 2 }, style: { fill: 'none', stroke: fill, lineWidth: 1.2, opacity: 0.9 } }] }; }, data: ldata, encode: { x: 0, y: 1 } };
  return {
    backgroundColor: 'transparent', animation: false, textStyle: { fontFamily: F, color: C.textDim },
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross', lineStyle: { color: C.line } }, ...tip(C), formatter: (ps: any) => { if (!ps || !ps.length) return ''; const t = ps[0].axisValue; const rows: string[] = []; ps.forEach((p: any) => { const nm = p.seriesName || ''; if (p.seriesType === 'custom') { const v = p.value; const row = v[1], val = Number(v[2]); const lbl = lanes[row] ? lanes[row].label : ''; const col = row === 0 ? (val >= 0 ? C.discharge : C.charge) : C.consumers; rows.push('<span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:' + col + ';margin-right:6px"></span>' + lbl + ': ' + val.toFixed(2) + ' kW'); } else { let val = Array.isArray(p.value) ? p.value[1] : p.value; if (val == null) return; let txt; if (/rijs/.test(nm)) txt = '€' + Number(val).toFixed(3); else if (nm === 'SoC') txt = Number(val).toFixed(0) + '%'; else txt = Number(val).toFixed(2) + ' kW'; rows.push(p.marker + nm + ': ' + txt); } }); return '<b>' + t + '</b><br/>' + rows.join('<br/>'); } },
    legend: { top: 0, left: 104, icon: 'roundRect', itemWidth: 11, itemHeight: 7, data: ['Zon', 'Teruglevering', 'Huislast', 'Verbruikers', 'Netinkoop', 'Inkoopprijs', 'Verkoopprijs', 'SoC'], textStyle: { color: C.textDim, fontSize: 12, fontFamily: F } },
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    grid: [{ left: 104, right: 54, top: 30, height: 218 }, { left: 104, right: 54, top: 286, bottom: 26 }],
    xAxis: [
      { type: 'category', gridIndex: 0, data: d.times, axisLine: { lineStyle: { color: C.line } }, axisTick: { show: false }, axisLabel: { show: false }, axisPointer: { label: { show: false } } },
      { type: 'category', gridIndex: 1, data: d.times, axisLine: { lineStyle: { color: C.line } }, axisTick: { show: false }, axisLabel: { color: C.textDim, fontFamily: F, fontSize: 11, interval: ival(d.N) }, axisPointer: { label: { show: false } } },
    ],
    yAxis: [
      { type: 'value', gridIndex: 0, name: 'kW', nameTextStyle: { color: C.textDim, fontFamily: F, fontSize: 11 }, axisLine: { lineStyle: { color: C.line } }, axisLabel: { color: C.textDim, fontFamily: F, fontSize: 10 }, splitLine: { lineStyle: { color: C.grid } } },
      { type: 'value', gridIndex: 0, name: '€/kWh', position: 'right', nameTextStyle: { color: C.textDim, fontFamily: F, fontSize: 11 }, axisLine: { lineStyle: { color: C.line } }, axisLabel: { color: C.textDim, fontFamily: F, fontSize: 10, formatter: (v: number) => v.toFixed(2) }, splitLine: { show: false } },
      { type: 'value', gridIndex: 0, name: 'SoC %', position: 'right', offset: 46, min: 0, max: 100, show: false, nameTextStyle: { color: C.soc, fontFamily: F, fontSize: 11 }, axisLine: { show: false }, axisLabel: { show: false }, splitLine: { show: false } },
      { type: 'category', gridIndex: 1, data: laneLabels, inverse: true, axisLine: { lineStyle: { color: C.line } }, axisTick: { show: false }, axisLabel: { color: C.text, fontFamily: F, fontWeight: 700, fontSize: 11 } },
    ],
    series: [...bars, ...lines, soc, swim],
  };
}

export function consSwimOption(d: any, C: ChartColors) {
  const lanes: any[] = []; const signed = d.discharge.map((v: number, j: number) => v - d.charge[j]); lanes.push({ label: 'Zendure +/-', kind: 'bat', values: signed, scale: Math.max(0.001, ...signed.map(Math.abs)) });
  for (const cc of d.consumers) lanes.push({ label: cc.label, kind: 'cons', color: cc.color, values: cc.power, scale: Math.max(0.001, ...cc.power.map(Math.abs)) });
  const labels = lanes.map((l) => l.label); const ldata: any[] = []; lanes.forEach((lane, row) => lane.values.forEach((v: number, slot: number) => { if (Math.abs(v) < 1e-3) return; ldata.push([slot, row, v, lane.kind === 'bat' ? 1 : 0, lane.scale, lane.color || C.consumers]); }));
  return {
    backgroundColor: 'transparent', animation: false,
    tooltip: { trigger: 'axis', axisPointer: { type: 'line', lineStyle: { color: C.priceBuy, width: 1, type: 'dashed' } }, ...tip(C), formatter: (ps: any) => { const s = Math.round(ps[0].axisValue != null ? ps[0].axisValue : ps[0].value[0]); let out = '<b>' + (d.times[s] ?? '') + '</b>'; const act = lanes.filter((l) => Math.abs(l.values[s]) > 1e-2); if (act.length) { act.forEach((l) => { out += '<br/>' + l.label + ': ' + l.values[s].toFixed(2) + ' kW'; }); } else { out += '<br/>geen inzet'; } return out; } },
    grid: { left: 104, right: 24, top: 12, bottom: 34 },
    xAxis: { type: 'value', min: 0, max: d.N, axisPointer: { show: true, label: { show: false } }, axisLine: { lineStyle: { color: C.line } }, axisTick: { show: false }, axisLabel: { color: C.textDim, fontFamily: F, fontSize: 11, formatter: (v: number) => d.times[Math.round(v)] ?? '' }, interval: Math.round(d.N / 8), splitLine: { show: false } },
    yAxis: { type: 'category', data: labels, inverse: true, axisLine: { lineStyle: { color: C.line } }, axisTick: { show: false }, axisLabel: { color: C.text, fontFamily: F, fontWeight: 700, fontSize: 12 } },
    series: [{ type: 'custom', renderItem: (_prm: any, api: any) => { const slot = api.value(0), row = api.value(1), val = api.value(2), isBat = api.value(3) === 1, scale = api.value(4) || 1, col = api.value(5); const start = api.coord([slot, row]), end = api.coord([slot + 1, row]); const rowH = Math.max(2, api.size([0, 1])[1] - 1); const w = Math.max(1, end[0] - start[0]); const bh = Math.max(1.5, rowH * Math.min(1, Math.abs(val) / scale)); const fill = isBat ? (val >= 0 ? C.discharge : C.charge) : col; const top = start[1] - rowH / 2; return { type: 'group', children: [{ type: 'rect', shape: { x: start[0] + 0.5, y: top, width: Math.max(1, w - 1), height: rowH, r: 2 }, style: { fill: C.grid, opacity: 0.28 } }, { type: 'rect', shape: { x: start[0] + 1.5, y: start[1] + rowH / 2 - bh, width: Math.max(1, w - 3), height: bh, r: 1 }, style: { fill, opacity: 0.96 } }, { type: 'rect', shape: { x: start[0] + 0.5, y: top, width: Math.max(1, w - 1), height: rowH, r: 2 }, style: { fill: 'none', stroke: fill, lineWidth: 1.2, opacity: 0.9 } }] }; }, data: ldata, encode: { x: 0, y: 1 } }],
  };
}

export function cijLanesOption(d: any, C: ChartColors) {
  const cj = d.cij;
  const lbl = cj.dayLbl, zon = cj.solar30, accu = cj.accu30, net = cj.net30;
  const tot = lbl.map((_: number, k: number) => Math.max(0.001, zon[k] + accu[k] + net[k]));
  const zonP = zon.map((v: number, k: number) => +(v / tot[k] * 100).toFixed(1));
  const accuP = accu.map((v: number, k: number) => +(v / tot[k] * 100).toFixed(1));
  const netP = net.map((v: number, k: number) => +(v / tot[k] * 100).toFixed(1));
  const cZon = '#f5a623', cAccu = '#3aa0c9', cNet = '#e8645a', cAut = '#13967d', cSav = '#1fb89a', cAcc = '#f47a20';
  const L = 126, R = 24, mixTop = 42, mixH = 128, laneH = 42, gap = 20, laneStart = mixTop + mixH + 46;
  const lanes = [
    { name: 'Zonopbrengst', unit: 'kWh', data: zon, color: cZon },
    { name: 'Accu → huis', unit: 'kWh', data: accu, color: cAccu },
    { name: 'Netinkoop', unit: 'kWh', data: net, color: cNet },
    { name: 'Zelfvoorziening', unit: '%', data: cj.aut30, color: cAut },
    { name: 'Besparing', unit: '€', data: cj.sav30, color: cSav },
    { name: 'Forecast-acc.', unit: '%', data: cj.acc30, color: cAcc },
  ];
  const tcol = C.text, dcol = C.textDim;
  const xLab = { color: dcol, fontFamily: F, fontSize: 10, interval: 4, formatter: (v: any) => 'd' + v };
  const grids: any[] = [{ left: L, right: R, top: mixTop, height: mixH }];
  const xAxes: any[] = [{ type: 'category', gridIndex: 0, data: lbl, axisLine: { lineStyle: { color: C.line } }, axisTick: { show: false }, axisLabel: { show: false } }];
  const yAxes: any[] = [{ type: 'value', gridIndex: 0, min: 0, max: 100, interval: 50, axisLine: { show: false }, axisTick: { show: false }, splitLine: { lineStyle: { color: C.grid } }, axisLabel: { color: dcol, fontFamily: F, fontSize: 10, formatter: (v: number) => v + '%' } }];
  const series: any[] = [
    { name: 'Zon', type: 'bar', stack: 'mix', xAxisIndex: 0, yAxisIndex: 0, data: zonP, barWidth: '62%', itemStyle: { color: cZon } },
    { name: 'Accu', type: 'bar', stack: 'mix', xAxisIndex: 0, yAxisIndex: 0, data: accuP, itemStyle: { color: cAccu } },
    { name: 'Net', type: 'bar', stack: 'mix', xAxisIndex: 0, yAxisIndex: 0, data: netP, itemStyle: { color: cNet } },
  ];
  const graphic: any[] = [
    { type: 'text', left: 12, top: mixTop + mixH / 2 - 15, style: { text: 'Energiemix', fill: tcol, font: '700 12px Nunito Sans' } },
    { type: 'text', left: 12, top: mixTop + mixH / 2 + 2, style: { text: 'aandeel/dag', fill: dcol, font: '11px Nunito Sans' } },
  ];
  lanes.forEach((ln, idx) => {
    const gi = idx + 1, last = idx === lanes.length - 1, top = laneStart + idx * (laneH + gap);
    grids.push({ left: L, right: R, top: top, height: laneH });
    xAxes.push({ type: 'category', gridIndex: gi, data: lbl, axisLine: { lineStyle: { color: C.line } }, axisTick: { show: false }, axisLabel: last ? xLab : { show: false } });
    yAxes.push({ type: 'value', gridIndex: gi, min: 0, scale: true, axisLine: { show: false }, axisTick: { show: false }, splitLine: { show: false }, axisLabel: { show: false } });
    series.push({ name: ln.name, type: 'bar', xAxisIndex: gi, yAxisIndex: gi, data: ln.data, barWidth: '62%', itemStyle: { color: ln.color, borderRadius: [2, 2, 0, 0] } });
    graphic.push({ type: 'text', left: 12, top: top + laneH / 2 - 15, style: { text: ln.name, fill: tcol, font: '700 12px Nunito Sans' } });
    graphic.push({ type: 'text', left: 12, top: top + laneH / 2 + 2, style: { text: ln.unit + '/dag', fill: dcol, font: '11px Nunito Sans' } });
  });
  const savGi = 5;
  yAxes.push({ type: 'value', gridIndex: savGi, min: 0, scale: true, axisLine: { show: false }, axisTick: { show: false }, splitLine: { show: false }, axisLabel: { show: false } });
  series.push({ name: 'Cumulatief', type: 'line', xAxisIndex: savGi, yAxisIndex: yAxes.length - 1, data: cj.cum30, symbol: 'none', smooth: true, lineStyle: { color: cSav, width: 1.5, type: 'dotted' }, z: 6 });
  return {
    backgroundColor: 'transparent', animation: false, textStyle: { fontFamily: F, color: dcol },
    legend: { top: 10, left: L, icon: 'roundRect', itemWidth: 11, itemHeight: 7, data: ['Zon', 'Accu', 'Net'], textStyle: { color: dcol, fontSize: 11, fontFamily: F } },
    axisPointer: { link: [{ xAxisIndex: 'all' }], type: 'line', lineStyle: { color: C.line, width: 1, type: 'dashed' } },
    tooltip: { trigger: 'axis', ...tip(C), formatter: (ps: any) => { if (!ps || !ps.length) return ''; const k = ps[0].dataIndex; return '<b>Dag ' + lbl[k] + '</b>' + '<br/>Mix · zon ' + zonP[k] + '% · accu ' + accuP[k] + '% · net ' + netP[k] + '%' + '<br/>Zon ' + zon[k] + ' · Accu ' + accu[k] + ' · Net ' + net[k] + ' kWh' + '<br/>Zelfvoorziening ' + cj.aut30[k] + '% · Besparing €' + cj.sav30[k].toFixed(2).replace('.', ',') + ' · Forecast ' + cj.acc30[k] + '%'; } },
    grid: grids, xAxis: xAxes, yAxis: yAxes, graphic: graphic, series,
  };
}

export function fc48Option(d: any, C: ChartColors) {
  const A = axis(C); const cats: string[] = []; for (let H = 0; H < 48; H++) cats.push((H % 24).toString().padStart(2, '0') + ':00');
  return { backgroundColor: 'transparent', animation: false, textStyle: { fontFamily: F, color: C.textDim }, tooltip: { trigger: 'axis', axisPointer: { type: 'cross', lineStyle: { color: C.line } }, ...tip(C) }, legend: { bottom: 0, icon: 'roundRect', itemWidth: 11, itemHeight: 7, textStyle: { color: C.textDim, fontSize: 12 } }, grid: { top: 18, right: 52, bottom: 42, left: 44 }, xAxis: { type: 'category', data: cats, ...A, axisLabel: { ...A.axisLabel, interval: 5 }, axisTick: { show: false } }, yAxis: [{ type: 'value', name: 'kW', nameTextStyle: { color: C.textDim, fontFamily: F, fontSize: 11 }, ...A }, { type: 'value', name: '€/kWh', position: 'right', nameTextStyle: { color: C.textDim, fontFamily: F, fontSize: 11 }, ...A, splitLine: { show: false }, axisLabel: { ...A.axisLabel, formatter: (v: number) => v.toFixed(2) } }], series: [{ name: 'Zon-forecast', type: 'bar', data: d.fcSolar, itemStyle: { color: C.solar, borderRadius: 2, opacity: 0.85 } }, { name: 'Prijs (day-ahead)', type: 'line', yAxisIndex: 1, data: d.fcPriceA, symbol: 'none', smooth: true, lineStyle: { color: C.priceBuy, width: 2.4 }, z: 5 }, { name: 'Prijs (extrapolatie)', type: 'line', yAxisIndex: 1, data: d.fcPriceB, symbol: 'none', smooth: true, lineStyle: { color: C.priceBuy, width: 1.8, type: 'dashed', opacity: 0.7 }, z: 5, markLine: { silent: true, symbol: 'none', data: [{ xAxis: 23 }], lineStyle: { color: C.line }, label: { show: false } } }] };
}

export function fc7dOption(d: any, C: ChartColors) {
  const A = axis(C); const days = ['ma', 'di', 'wo', 'do', 'vr', 'za', 'zo'];
  return { backgroundColor: 'transparent', animation: false, textStyle: { fontFamily: F, color: C.textDim }, tooltip: { trigger: 'axis', axisPointer: { type: 'cross', lineStyle: { color: C.line } }, ...tip(C) }, legend: { top: 0, right: 0, icon: 'roundRect', itemWidth: 11, itemHeight: 7, textStyle: { color: C.textDim, fontSize: 12 } }, grid: { top: 30, right: 52, bottom: 30, left: 44 }, xAxis: { type: 'category', data: d.fc7Cats, boundaryGap: true, axisLine: { lineStyle: { color: C.line } }, axisTick: { show: false }, axisLabel: { color: C.textDim, fontFamily: F, fontWeight: 700, fontSize: 11, interval: (idx: number) => idx % 24 === 0, formatter: (_v: string, idx: number) => days[Math.floor(idx / 24)] || '' } }, yAxis: [{ type: 'value', name: 'kW', nameTextStyle: { color: C.textDim, fontFamily: F, fontSize: 11 }, ...A }, { type: 'value', name: '€/kWh', position: 'right', nameTextStyle: { color: C.textDim, fontFamily: F, fontSize: 11 }, ...A, splitLine: { show: false }, axisLabel: { ...A.axisLabel, formatter: (v: number) => v.toFixed(2) } }], series: [{ name: 'Zon-forecast', type: 'bar', data: d.fc7Solar, itemStyle: { color: C.solar, borderRadius: 1, opacity: 0.82 } }, { name: 'Prijs', type: 'line', yAxisIndex: 1, data: d.fc7PriceA, symbol: 'none', smooth: true, lineStyle: { color: C.priceBuy, width: 2.2 }, z: 5, markLine: { silent: true, symbol: 'none', data: [24, 48, 72, 96, 120, 144].map((x) => ({ xAxis: x, lineStyle: { color: x === 48 ? C.textDim : C.line, type: x === 48 ? 'solid' : 'dashed', opacity: x === 48 ? 0.7 : 0.4 }, label: x === 48 ? { formatter: 'day-ahead', color: C.textDim, fontFamily: F, fontSize: 10, position: 'insideEndTop' } : { show: false } })) } }, { name: 'Prijs (extrapolatie)', type: 'line', yAxisIndex: 1, data: d.fc7PriceB, symbol: 'none', smooth: true, lineStyle: { color: C.priceBuy, width: 1.8, type: 'dashed', opacity: 0.65 }, z: 5 }] };
}
