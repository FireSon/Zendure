import type { Theme } from './types.js';
import { t } from './service/i18n.js';

// ECharts option builders, lifted verbatim from the ZenMaster v2 design concept.
// Each takes `d` (the chart-data shape produced by derive.chartData) and `C` (theme
// palette). ECharts canvas cannot read CSS vars, so colours are computed here.

export function chartColors(theme: Theme) {
  const dark = theme === 'dark';
  return {
    solar: '#f5a623', consumers: '#f47a20', load: dark ? '#5f594e' : '#d8d0c1',
    import: '#e8645a', export: '#8ad2c0', charge: '#3aa0c9', discharge: '#1fb89a',
    soc: '#1fb89a', priceBuy: '#f47a20', priceSell: '#1fb89a',
    temp: dark ? '#8fa3e8' : '#5468c4',
    text: dark ? '#f2eee7' : '#34322d', textDim: dark ? '#a79e90' : '#8d877b',
    line: dark ? 'rgba(255,255,255,0.07)' : '#ece6db', grid: dark ? 'rgba(255,255,255,0.05)' : 'rgba(70,52,24,0.05)',
    tipBg: dark ? '#262320' : '#ffffff', tipBorder: dark ? 'rgba(255,255,255,0.1)' : '#ece6db', tipText: dark ? '#f2eee7' : '#34322d',
  };
}
export type ChartColors = ReturnType<typeof chartColors>;

const F = 'Nunito Sans';

function tip(C: ChartColors) {
  return { backgroundColor: C.tipBg, borderColor: C.tipBorder, borderWidth: 1, textStyle: { color: C.tipText, fontFamily: F, fontSize: 13 }, extraCssText: 'border-radius:12px;box-shadow:0 8px 24px rgba(0,0,0,0.12);' };
}
function axis(C: ChartColors) {
  return { axisLine: { lineStyle: { color: C.line } }, axisLabel: { color: C.textDim, fontFamily: F, fontSize: 10 }, splitLine: { lineStyle: { color: C.grid } } };
}
function ival(n: number) { return Math.max(0, Math.round(n / 8) - 1); }
const neg = (a: number[]) => a.map((v) => -v);

// left: the plot's left edge in px; the caller draws the rows under it on the same time axis
export function flowOption(d: any, C: ChartColors, left = 104) {
  const consSum = new Array(d.N).fill(0);
  for (const cc of d.consumers) cc.power.forEach((v: number, i: number) => (consSum[i] += v));
  // the series names are the legend and the tooltip match, so they are looked up once
  const solar = t('ui.chart.solar', 'Zon'), exp = t('ui.chart.export', 'Teruglevering'), load = t('ui.chart.load', 'Huislast'), cons = t('ui.chart.consumers', 'Verbruikers'), imp = t('ui.chart.import', 'Netinkoop'), buy = t('ui.chart.price_buy', 'Inkoopprijs'), sell = t('ui.chart.price_sell', 'Verkoopprijs'), temp = t('ui.chart.outside_temp', 'Buitentemperatuur');
  const bars = [
    { name: solar, stack: 'pos', color: C.solar, data: d.solar }, { name: exp, stack: 'pos', color: C.export, data: d.grid_sell },
    { name: load, stack: 'neg', color: C.load, data: neg(d.load) }, { name: cons, stack: 'neg', color: C.consumers, data: neg(consSum) }, { name: imp, stack: 'neg', color: C.import, data: neg(d.grid_buy) },
  ].map((b: any) => ({ name: b.name, type: 'bar', xAxisIndex: 0, yAxisIndex: 0, stack: b.stack, data: b.data, itemStyle: { color: b.color, borderRadius: 2 }, barCategoryGap: '18%', emphasis: { focus: 'series' } })) as any[];
  bars[0].markLine = { silent: true, symbol: 'none', data: [{ xAxis: d.NOW }], lineStyle: { color: '#f47a20', width: 1.6, opacity: 0.7 }, label: { formatter: t('ui.app.now', 'nu'), color: '#f47a20', fontFamily: F, fontWeight: 700, fontSize: 10 } };
  const lines = [{ name: buy, color: C.priceBuy, type: 'solid', data: d.price_buy }, { name: sell, color: C.priceSell, type: 'dashed', data: d.price_sell }].map((l) => ({ name: l.name, type: 'line', xAxisIndex: 0, yAxisIndex: 1, data: l.data, symbol: 'none', smooth: true, itemStyle: { color: l.color }, lineStyle: { color: l.color, width: 2, type: l.type }, z: 5 })) as any[];
  // the buy price is the line to read: thicker, lifted off the bars by a halo in the card colour, and its value at now
  lines[0].lineStyle = { color: C.priceBuy, width: 3.5, shadowColor: C.tipBg, shadowBlur: 6 };
  lines[0].markPoint = { symbol: 'roundRect', symbolSize: [50, 20], symbolOffset: [30, -14], itemStyle: { color: C.priceBuy }, label: { color: '#fff', fontFamily: 'Fredoka', fontWeight: 600, fontSize: 13, formatter: (p: any) => '€' + Number(p.value).toFixed(2).replace('.', ',') }, data: [{ coord: [d.NOW, d.price_buy[d.NOW]], value: d.price_buy[d.NOW] }] };
  const soc = { name: 'SoC', type: 'line', xAxisIndex: 0, yAxisIndex: 2, data: d.soc.map((v: number) => v * 100), symbol: 'none', smooth: true, lineStyle: { color: C.soc, width: 2.4, type: 'dotted' }, markLine: { silent: true, symbol: 'none', data: [{ yAxis: 0, lineStyle: { color: C.import, type: 'dashed', width: 1 }, label: { formatter: t('ui.chart.soc_min', 'min SoC'), color: C.import, fontFamily: F, fontSize: 10, position: 'insideEndTop' } }, { yAxis: 100, lineStyle: { color: C.textDim, type: 'dashed', width: 1 }, label: { formatter: t('ui.chart.soc_max', 'max SoC'), color: C.textDim, fontFamily: F, fontSize: 10, position: 'insideEndTop' } }] } };
  // the outside temperature from the forecast: a plain line, more contrast than the bars, less than the buy price
  const outside = { name: temp, type: 'line', xAxisIndex: 0, yAxisIndex: 3, data: d.series.temp ?? [], symbol: 'none', smooth: true, itemStyle: { color: C.temp }, lineStyle: { color: C.temp, width: 2.2 }, z: 4 };
  return {
    backgroundColor: 'transparent', animation: false, textStyle: { fontFamily: F, color: C.textDim },
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross', lineStyle: { color: C.line } }, ...tip(C), formatter: (ps: any) => { if (!ps || !ps.length) return ''; const t = ps[0].axisValue; const rows: string[] = []; ps.forEach((p: any) => { const nm = p.seriesName || ''; let val = Array.isArray(p.value) ? p.value[1] : p.value; if (val == null) return; let txt; if (nm === buy || nm === sell) txt = '€' + Number(val).toFixed(3); else if (nm === 'SoC') txt = Number(val).toFixed(0) + '%'; else if (nm === temp) txt = Number(val).toFixed(1) + ' °C'; else txt = Number(val).toFixed(2) + ' kW'; rows.push(p.marker + nm + ': ' + txt); }); return '<b>' + t + '</b><br/>' + rows.join('<br/>'); } },
    legend: { top: 0, left: left + 28, icon: 'roundRect', itemWidth: 11, itemHeight: 7, data: [solar, exp, load, cons, imp, buy, sell, 'SoC', temp], textStyle: { color: C.textDim, fontSize: 13, fontFamily: F } },  // clear of the kW axis name above the plot's left edge
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    // one grid with the time axis right under it
    grid: [{ left, right: 54, top: 30, bottom: 26 }],
    xAxis: [
      { type: 'category', gridIndex: 0, data: d.times, axisLine: { lineStyle: { color: C.line } }, axisTick: { show: false }, axisLabel: { color: C.textDim, fontFamily: F, fontSize: 10, interval: ival(d.N) }, axisPointer: { label: { show: false } } },
    ],
    yAxis: [
      { type: 'value', gridIndex: 0, name: 'kW', nameTextStyle: { color: C.textDim, fontFamily: F, fontSize: 10 }, axisLine: { lineStyle: { color: C.line } }, axisLabel: { color: C.textDim, fontFamily: F, fontSize: 10 }, splitLine: { lineStyle: { color: C.grid } } },
      { type: 'value', gridIndex: 0, name: '€/kWh', position: 'right', nameTextStyle: { color: C.textDim, fontFamily: F, fontSize: 10 }, axisLine: { lineStyle: { color: C.line } }, axisLabel: { color: C.priceBuy, fontFamily: F, fontWeight: 700, fontSize: 10, formatter: (v: number) => v.toFixed(2) }, axisPointer: { label: { formatter: (p: any) => '€' + Number(p.value).toFixed(2) } }, splitLine: { show: false } },
      // the hidden SoC axis gives the cross pointer no label of its own; the price axis carries it
      { type: 'value', gridIndex: 0, name: 'SoC %', position: 'right', offset: 46, min: 0, max: 100, show: false, nameTextStyle: { color: C.soc, fontFamily: F, fontSize: 10 }, axisLine: { show: false }, axisLabel: { show: false }, axisPointer: { label: { show: false } }, splitLine: { show: false } },
      { type: 'value', gridIndex: 0, scale: true, show: false, axisPointer: { label: { show: false } }, splitLine: { show: false } },
    ],
    series: [...bars, ...lines, soc, outside],
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
  // the mix series names are also the legend entries, so they are looked up once
  const mZon = t('ui.chart.solar', 'Zon'), mAccu = t('ui.chart.battery', 'Accu'), mNet = t('ui.chart.grid', 'Net');
  const lanes = [
    { name: t('ui.trend.solar', 'Zonopbrengst'), unit: 'kWh', data: zon, color: cZon },
    { name: t('ui.trend.battery', 'Accu → huis'), unit: 'kWh', data: accu, color: cAccu },
    { name: t('ui.chart.import', 'Netinkoop'), unit: 'kWh', data: net, color: cNet },
    { name: t('ui.trend.autarky', 'Zelfvoorziening'), unit: '%', data: cj.aut30, color: cAut },
    { name: t('ui.app.savings', 'Besparing'), unit: '€', data: cj.sav30, color: cSav },
    { name: t('ui.trend.accuracy', 'Forecast-acc.'), unit: '%', data: cj.acc30, color: cAcc },
  ];
  const tcol = C.text, dcol = C.textDim;
  const xLab = { color: dcol, fontFamily: F, fontSize: 10, interval: 4, formatter: (v: any) => t('ui.trend.day_short', 'd{n}', { n: v }) };
  const grids: any[] = [{ left: L, right: R, top: mixTop, height: mixH }];
  const xAxes: any[] = [{ type: 'category', gridIndex: 0, data: lbl, axisLine: { lineStyle: { color: C.line } }, axisTick: { show: false }, axisLabel: { show: false } }];
  const yAxes: any[] = [{ type: 'value', gridIndex: 0, min: 0, max: 100, interval: 50, axisLine: { show: false }, axisTick: { show: false }, splitLine: { lineStyle: { color: C.grid } }, axisLabel: { color: dcol, fontFamily: F, fontSize: 10, formatter: (v: number) => v + '%' } }];
  const series: any[] = [
    { name: mZon, type: 'bar', stack: 'mix', xAxisIndex: 0, yAxisIndex: 0, data: zonP, barWidth: '62%', itemStyle: { color: cZon } },
    { name: mAccu, type: 'bar', stack: 'mix', xAxisIndex: 0, yAxisIndex: 0, data: accuP, itemStyle: { color: cAccu } },
    { name: mNet, type: 'bar', stack: 'mix', xAxisIndex: 0, yAxisIndex: 0, data: netP, itemStyle: { color: cNet } },
  ];
  const graphic: any[] = [
    { type: 'text', left: 12, top: mixTop + mixH / 2 - 15, style: { text: t('ui.trend.mix', 'Energiemix'), fill: tcol, font: '700 13px Nunito Sans' } },
    { type: 'text', left: 12, top: mixTop + mixH / 2 + 2, style: { text: t('ui.trend.share', 'aandeel/dag'), fill: dcol, font: '11px Nunito Sans' } },
  ];
  lanes.forEach((ln, idx) => {
    const gi = idx + 1, last = idx === lanes.length - 1, top = laneStart + idx * (laneH + gap);
    grids.push({ left: L, right: R, top: top, height: laneH });
    xAxes.push({ type: 'category', gridIndex: gi, data: lbl, axisLine: { lineStyle: { color: C.line } }, axisTick: { show: false }, axisLabel: last ? xLab : { show: false } });
    yAxes.push({ type: 'value', gridIndex: gi, min: 0, scale: true, axisLine: { show: false }, axisTick: { show: false }, splitLine: { show: false }, axisLabel: { show: false } });
    series.push({ name: ln.name, type: 'bar', xAxisIndex: gi, yAxisIndex: gi, data: ln.data, barWidth: '62%', itemStyle: { color: ln.color, borderRadius: [2, 2, 0, 0] } });
    graphic.push({ type: 'text', left: 12, top: top + laneH / 2 - 15, style: { text: ln.name, fill: tcol, font: '700 13px Nunito Sans' } });
    graphic.push({ type: 'text', left: 12, top: top + laneH / 2 + 2, style: { text: t('ui.trend.per_day', '{unit}/dag', { unit: ln.unit }), fill: dcol, font: '11px Nunito Sans' } });
  });
  const savGi = 5;
  yAxes.push({ type: 'value', gridIndex: savGi, min: 0, scale: true, axisLine: { show: false }, axisTick: { show: false }, splitLine: { show: false }, axisLabel: { show: false } });
  series.push({ name: t('ui.trend.cumulative', 'Cumulatief'), type: 'line', xAxisIndex: savGi, yAxisIndex: yAxes.length - 1, data: cj.cum30, symbol: 'none', smooth: true, lineStyle: { color: cSav, width: 1.5, type: 'dotted' }, z: 6 });
  return {
    backgroundColor: 'transparent', animation: false, textStyle: { fontFamily: F, color: dcol },
    legend: { top: 10, left: L, icon: 'roundRect', itemWidth: 11, itemHeight: 7, data: [mZon, mAccu, mNet], textStyle: { color: dcol, fontSize: 10, fontFamily: F } },
    axisPointer: { link: [{ xAxisIndex: 'all' }], type: 'line', lineStyle: { color: C.line, width: 1, type: 'dashed' } },
    tooltip: { trigger: 'axis', ...tip(C), formatter: (ps: any) => { if (!ps || !ps.length) return ''; const k = ps[0].dataIndex; return '<b>' + t('ui.trend.day', 'Dag {n}', { n: lbl[k] }) + '</b>' + '<br/>' + t('ui.trend.tip_mix', 'Mix · zon {solar}% · accu {battery}% · net {grid}%', { solar: zonP[k], battery: accuP[k], grid: netP[k] }) + '<br/>' + t('ui.trend.tip_kwh', 'Zon {solar} · Accu {battery} · Net {grid} kWh', { solar: zon[k], battery: accu[k], grid: net[k] }) + '<br/>' + t('ui.trend.tip_kpi', 'Zelfvoorziening {autarky}% · Besparing €{savings} · Forecast {accuracy}%', { autarky: cj.aut30[k], savings: cj.sav30[k].toFixed(2).replace('.', ','), accuracy: cj.acc30[k] }); } },
    grid: grids, xAxis: xAxes, yAxis: yAxes, graphic: graphic, series,
  };
}

// Tooltip and kW / €/kWh y-axis pair shared by the two forecast charts
function fcShared(C: ChartColors) {
  const A = axis(C);
  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross', lineStyle: { color: C.line } }, ...tip(C) },
    yAxis: [
      { type: 'value', name: 'kW', nameTextStyle: { color: C.textDim, fontFamily: F, fontSize: 10 }, ...A },
      {
        type: 'value', name: '€/kWh', position: 'right', nameTextStyle: { color: C.textDim, fontFamily: F, fontSize: 10 }, ...A,
        splitLine: { show: false }, axisLabel: { ...A.axisLabel, formatter: (v: number) => v.toFixed(2) },
      },
    ],
  };
}

export function fc48Option(d: any, C: ChartColors) {
  const A = axis(C); const cats: string[] = d.fcCats ?? [];
  return {
    backgroundColor: 'transparent', animation: false, textStyle: { fontFamily: F, color: C.textDim }, ...fcShared(C),
    legend: { bottom: 0, icon: 'roundRect', itemWidth: 11, itemHeight: 7, textStyle: { color: C.textDim, fontSize: 13 } },
    grid: { top: 18, right: 52, bottom: 42, left: 44 },
    xAxis: { type: 'category', data: cats, ...A, axisLabel: { ...A.axisLabel, interval: 5 }, axisTick: { show: false } },
    series: [
      { name: t('ui.chart.solar_forecast', 'Zon-forecast'), type: 'bar', data: d.fcSolar, itemStyle: { color: C.solar, borderRadius: 2, opacity: 0.85 } },
      { name: t('ui.chart.price_dayahead', 'Prijs (day-ahead)'), type: 'line', yAxisIndex: 1, data: d.fcPriceA, symbol: 'none', smooth: true, lineStyle: { color: C.priceBuy, width: 2.4 }, z: 5 },
      {
        name: t('ui.chart.price_extrapolated', 'Prijs (extrapolatie)'), type: 'line', yAxisIndex: 1, data: d.fcPriceB, symbol: 'none', smooth: true,
        lineStyle: { color: C.priceBuy, width: 1.8, type: 'dashed', opacity: 0.7 }, z: 5,
        markLine: { silent: true, symbol: 'none', data: (d.fcDays ?? []).slice(1).map((x: any) => ({ xAxis: x.at })), lineStyle: { color: C.line }, label: { show: false } },
      },
    ],
  };
}

export function fc7dOption(d: any, C: ChartColors) {
  const starts: { at: number; label: string }[] = d.fc7Days ?? [];
  return {
    backgroundColor: 'transparent', animation: false, textStyle: { fontFamily: F, color: C.textDim }, ...fcShared(C),
    legend: { top: 0, right: 0, icon: 'roundRect', itemWidth: 11, itemHeight: 7, textStyle: { color: C.textDim, fontSize: 13 } },
    grid: { top: 30, right: 52, bottom: 30, left: 44 },
    xAxis: {
      type: 'category', data: d.fc7Cats, boundaryGap: true, axisLine: { lineStyle: { color: C.line } }, axisTick: { show: false },
      axisLabel: {
        color: C.textDim, fontFamily: F, fontWeight: 700, fontSize: 10, interval: (idx: number) => starts.some((x) => x.at === idx),
        formatter: (_v: string, idx: number) => (starts.find((x) => x.at === idx) || { label: '' }).label,
      },
    },
    series: [
      { name: t('ui.chart.solar_forecast', 'Zon-forecast'), type: 'bar', data: d.fc7Solar, itemStyle: { color: C.solar, borderRadius: 1, opacity: 0.82 } },
      {
        name: t('ui.chart.price', 'Prijs'), type: 'line', yAxisIndex: 1, data: d.fc7PriceA, symbol: 'none', smooth: true, lineStyle: { color: C.priceBuy, width: 2.2 }, z: 5,
        markLine: { silent: true, symbol: 'none', data: [
          ...starts.slice(1).map((x) => ({ xAxis: x.at, lineStyle: { color: C.line, type: 'dashed', opacity: 0.4 }, label: { show: false } })),
          ...(d.fc7Known ? [{
            xAxis: d.fc7Known, lineStyle: { color: C.textDim, type: 'solid', opacity: 0.7 },
            label: { formatter: t('ui.chart.dayahead', 'day-ahead'), color: C.textDim, fontFamily: F, fontSize: 10, position: 'insideEndTop' },
          }] : []),
        ] },
      },
      {
        name: t('ui.chart.price_extrapolated', 'Prijs (extrapolatie)'), type: 'line', yAxisIndex: 1, data: d.fc7PriceB, symbol: 'none', smooth: true,
        lineStyle: { color: C.priceBuy, width: 1.8, type: 'dashed', opacity: 0.65 }, z: 5,
      },
    ],
  };
}
