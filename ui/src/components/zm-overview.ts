import { LitElement, html, svg, css, nothing } from 'lit';
import { property, state } from 'lit/decorators.js';
import { live } from 'lit/directives/live.js';
import { tokens } from '../tokens.js';
import { api } from '../service/api.js';
import type { Schema, Theme } from '../types.js';
import { chartColors, flowOption } from '../charts.js';
import { consumerCards, nl } from '../derive.js';
import { byKind, fleetDevices } from '../service/drivers.js';
import { ZendureBle } from '../service/ble.js';
import { t } from '../service/i18n.js';
import './zm-chart.js';
import './zm-group.js';
import './zm-ble-onboard.js';

// "Grafiek" and "Apparaten": one card with the plan chart, the Controls tile left of it, and under it one row
// per battery and per consumer on the chart's time axis. The plan view fills a row with its planning, the
// tiles view with the device's readings; the name block on the left is the same in both.

const LBL = 290;  // px: the name column, and the chart's left edge, so the rows and the plan share one time axis
const RSP = 54;   // px: the price axis right of the chart, kept free on every row

// ConnectionType from device.py: 0 offline, 1 local broker, 2 cloud, 3 zensdk (http), 4 ble
const CONN_COLOR: Record<number, string> = { 0: '#e8645a', 1: '#4a90d9', 2: '#1fb89a', 3: '#3aa0c9', 4: '#4a90d9' };
const PHASE_ORDER = ['L1', 'L2', 'L3', 'L1-3', ''];  // the device rows by phase; an unknown phase last

// Concrete add-types: each preset maps to a subentry type (the consumer class in the backend) plus the derived
// subtype (kind); the label is also the proposed name of the new consumer.
const CONSUMER_TYPES: Record<string, { label: string; type: string; kind?: string; color: string; icon: string }> = {
  ev: { label: 'EV-lader', type: 'electric_vehicle', color: '#1fb89a', icon: 'ev' },
  washingmachine: { label: 'Wasmachine', type: 'appliance', kind: 'washingmachine', color: '#3aa0c9', icon: 'washer' },
  dryer: { label: 'Droger', type: 'appliance', kind: 'dryer', color: '#8f7ae5', icon: 'dryer' },
  dishwasher: { label: 'Vaatwasser', type: 'appliance', kind: 'dishwasher', color: '#2fbf9f', icon: 'dish' },
  appliance: { label: 'Apparaat', type: 'appliance', kind: 'custom', color: '#3aa0c9', icon: 'plug' },
  heatpump: { label: 'Warmtepomp', type: 'heating_heat_pump', color: '#f47a20', icon: 'hp' },
  boiler_hp: { label: 'Boiler', type: 'boiler_heat_pump', color: '#f5a623', icon: 'boiler' },
  boiler_electric: { label: 'Boiler', type: 'boiler_electric', color: '#f5a623', icon: 'boiler_e' },
  pool: { label: 'Zwembad', type: 'pool_heat_pump', color: '#4aa3df', icon: 'pool' },
};

interface DevRow {
  id: string; name: string; model: string; eid: string; hasPack: boolean;
  net: number; wattColor: string; socPct: number; dim: boolean;
  kwhAvail: number | null; kwhCap: number;
  inAggr: number | null; outAggr: number | null; rte: number | null;
  solar: number | null; temp: number | null; tempColor: string; conn: number; phase: string;
}

export class ZmOverview extends LitElement {
  @property({ attribute: false }) d: any = null;
  @property() theme: Theme = 'light';
  @property() view: 'plan' | 'tiles' = 'plan';
  @property({ attribute: false }) config: any = null;
  @property({ attribute: false }) schema: Schema | null = null;
  @property({ attribute: false }) hass: any = null;
  @property({ attribute: false }) actions: Record<string, any[]> = {};
  @property({ attribute: false }) tiles: Record<string, string[]> = {};

  @state() private _pendingMode: string | null = null;  // the option just picked, until the entity confirms it
  @state() private _edit: { id: string; consumer: boolean } | null = null;  // the row whose popup is open
  @state() private _activeOnly = localStorage.getItem('zm-active-only') === '1';  // the device rows: all, or only the ones that are not offline; kept per browser like the tab
  @state() private _draftName = '';
  @state() private _nameErr: string | null = null;
  @state() private _picker = false;
  @state() private _busy = false;
  @state() private _onboardName: string | null = null;
  private _option: any = null;  // the chart option, rebuilt only when the plan or the theme changes, not on every hass update

  willUpdate(changed: Map<string, unknown>) {
    // a new option object resets the chart and drops its tooltip; hass changes several times a second
    if (changed.has('d') || changed.has('theme')) this._option = this.d ? flowOption(this.d, chartColors(this.theme), LBL) : null;
  }

  static styles = [
    tokens,
    css`
      :host { display: block; }
      .top { position: relative; }
      .flow { display: block; width: 100%; height: 276px; }
      /* the steering of the distribution, in the chart's left margin */
      .ctl { position: absolute; left: 0; top: 0; bottom: 30px; width: ${LBL - 46}px; display: flex; flex-direction: column; gap: 16px; padding: 16px 16px 18px; border-radius: 18px; background: linear-gradient(160deg, var(--zm-ctl-a), var(--zm-ctl-b)); border: 1px solid var(--zm-ctl-border); box-shadow: var(--zm-shadow-sm); }
      .ctl.inline { position: static; width: 100%; max-width: 520px; margin-bottom: 14px; }
      .ctl .h { display: flex; align-items: center; gap: 7px; font-family: var(--zm-font-body); font-size: var(--zm-fs-s); font-weight: 800; color: var(--zm-text-dim); }
      .ctl .i { flex: none; width: 22px; height: 22px; border-radius: 7px; display: flex; align-items: center; justify-content: center; background: var(--zm-card); color: #1fb89a; }
      .ctl .s { margin-left: auto; font-size: var(--zm-fs-s); font-weight: 800; text-transform: uppercase; letter-spacing: 0.07em; color: var(--zm-text-faint); }
      .ctl .grid { flex: 1 1 auto; display: flex; flex-direction: column; justify-content: center; gap: 8px; }
      .ctl .fld { flex-direction: row; align-items: center; gap: 10px; }
      .ctl .fld > span { flex: none; width: 74px; }
      .ctl .box { flex: 1 1 auto; min-width: 0; height: 36px; }
      .fld { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
      .fld > span { font-family: var(--zm-font-body); font-size: var(--zm-fs-s); font-weight: 500; text-transform: uppercase; letter-spacing: 0.06em; color: var(--zm-text-dim); }
      .box { display: flex; align-items: center; height: 44px; padding: 0 10px; background: var(--zm-card); border: 1px solid var(--zm-ctl-border); border-radius: 12px; box-shadow: inset 0 1px 2px rgba(70,52,24,0.06); }
      .box:focus-within { border-color: #1fb89a; box-shadow: 0 0 0 3px rgba(31,184,154,0.18); }
      .box input, .box select { flex: 1 1 auto; min-width: 0; width: 100%; border: none; outline: none; background: transparent; padding: 0; font-family: var(--zm-font-body); font-weight: 800; font-size: var(--zm-fs-m); color: var(--zm-text); -moz-appearance: textfield; }
      .box select { font-size: var(--zm-fs-m); cursor: pointer; }
      .box input::-webkit-inner-spin-button, .box input::-webkit-outer-spin-button { -webkit-appearance: none; margin: 0; }
      .box em { font-style: normal; font-family: var(--zm-font-body); font-size: var(--zm-fs-s); font-weight: 700; color: var(--zm-text-dim); margin-left: 4px; }
      /* the rows, zebra striped, each one name block, one lane on the time axis and the free price column */
      .rowsw { position: relative; margin-top: 8px; }
      .now { position: absolute; top: 0; bottom: 0; border-left: 1.6px solid #f47a20; opacity: 0.7; pointer-events: none; }
      .row { display: flex; align-items: stretch; height: 44px; }
      .row:nth-child(odd) { background: var(--zm-zebra); }
      .lbl { position: relative; flex: none; width: ${LBL}px; display: flex; align-items: center; gap: 8px; padding: 0 12px 0 6px; }
      .lbl:hover { background: var(--zm-border-2); }
      .row.dim .lbl > :not(.dpop), .row.dim .lane { opacity: 0.6; }
      .nm { flex: 1 1 auto; min-width: 0; }
      .nm .n { display: flex; align-items: center; gap: 3px; min-width: 0; font-family: var(--zm-font-body); font-size: var(--zm-fs-m); font-weight: 800; color: var(--zm-text); }
      .nm .n a, .nm .n span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: inherit; text-decoration: none; text-underline-offset: 3px; }
      .nm .n a:hover { text-decoration: underline; color: #13967d; }
      .nm .n .ph { flex: none; margin-left: 4px; font-size: var(--zm-fs-s); font-weight: 700; color: var(--zm-text-dim); }
      .nm .m { display: flex; align-items: center; gap: 5px; font-family: var(--zm-font-body); font-size: var(--zm-fs-s); font-weight: 700; color: var(--zm-text-dim); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .nm .m i { flex: none; width: 7px; height: 7px; border-radius: 50%; }
      .w { flex: none; width: 68px; text-align: right; font-family: var(--zm-font-display); font-weight: 600; font-size: var(--zm-fs-l); line-height: 1; }
      .w .u { font-size: var(--zm-fs-s); color: var(--zm-text-dim); }
      .bat { flex: none; width: 70px; height: 16px; margin-left: 10px; border: 1.5px solid var(--zm-text-faint); border-radius: 4px; padding: 2px; display: flex; }
      .bat i { background: #1fb89a; border-radius: 2px; }
      .st { flex: none; width: 70px; margin-left: 10px; text-align: right; font-family: var(--zm-font-body); font-size: var(--zm-fs-s); font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .dpop { display: none; position: absolute; left: calc(100% + 8px); top: -8px; z-index: 30; gap: 5px; background: var(--zm-card); border: 1px solid var(--zm-border); border-radius: 14px; box-shadow: var(--zm-shadow); padding: 8px; }
      .lbl:hover .dpop { display: flex; }
      .lane { flex: 1 1 auto; min-width: 0; display: flex; align-items: center; gap: 1px; border-left: 1px solid var(--zm-border); }
      .lane > i { flex: 1 1 0; min-width: 0; border-radius: 2px; }
      .lane.tiles { gap: 6px; padding-left: 12px; }
      .rsp { flex: none; width: ${RSP}px; }
      .dtile { flex: none; width: 82px; display: flex; flex-direction: column; align-items: center; text-align: center; gap: 2px; background: var(--zm-zebra); border: 1px solid rgba(70,52,24,0.07); border-radius: 10px; padding: 4px 3px; }
      .row:nth-child(odd) .dtile { background: var(--zm-zebra-tile); }
      .dtile .k { font-family: var(--zm-font-body); font-size: var(--zm-fs-s); font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em; color: var(--zm-text-faint); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100%; }
      .dtile .v { font-family: var(--zm-font-display); font-weight: 600; font-size: var(--zm-fs-m); line-height: 1; }
      .dtile .v .u { font-size: var(--zm-fs-s); color: var(--zm-text-dim); }
      .acts { display: flex; gap: 6px; margin-left: auto; padding-right: 8px; }
      .edit { flex: none; width: 20px; height: 20px; padding: 0; display: inline-flex; align-items: center; justify-content: center; border: none; border-radius: 6px; background: transparent; color: var(--zm-text-faint); cursor: pointer; }
      .lbl:hover .edit, .edit:hover, .edit:focus-visible { color: #13967d; background: var(--zm-teal-soft); }
      .foot { display: flex; gap: 8px; padding: 12px 6px 0; }
      .addbtn { display: inline-flex; align-items: center; gap: 6px; border: 1px solid var(--zm-border); background: var(--zm-card); color: var(--zm-text-dim); border-radius: 999px; padding: 6px 12px; font-family: var(--zm-font-body); font-size: var(--zm-fs-m); font-weight: 700; cursor: pointer; }
      .addbtn:hover { color: var(--zm-text); border-color: var(--zm-text-faint); }
      .addbtn:disabled { opacity: 0.5; cursor: default; }
      /* the popups: the edit of one row, and the type picker for a new consumer */
      .pop-bg { position: fixed; inset: 0; background: rgba(20,18,14,0.38); backdrop-filter: blur(2px); display: flex; align-items: flex-start; justify-content: center; z-index: 200; padding: 120px 20px 20px; overflow: auto; }
      .pop { width: min(580px, 100%); background: var(--zm-card); border: 1px solid var(--zm-border); border-radius: 22px; box-shadow: 0 24px 60px rgba(0,0,0,0.28); overflow: hidden; }
      .pop.wide { width: min(870px, 100%); }  /* the edit popup of a device or consumer: room for two columns of fields */
      .poph { display: flex; align-items: center; gap: 10px; padding: 14px 18px; background: linear-gradient(160deg, var(--zm-ctl-a), var(--zm-ctl-b)); border-bottom: 1px solid var(--zm-ctl-border); }
      .poph h3 { flex: 1 1 auto; min-width: 0; margin: 0; font-family: var(--zm-font-display); font-weight: 600; font-size: var(--zm-fs-l); color: var(--zm-text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .pill { display: inline-flex; align-items: center; gap: 6px; padding: 7px 12px; border-radius: 999px; background: var(--zm-card); border: 1px solid var(--zm-ctl-border); color: #13967d; font-family: var(--zm-font-body); font-size: var(--zm-fs-m); font-weight: 800; text-decoration: none; cursor: pointer; }
      .x { flex: none; width: 34px; height: 34px; border-radius: 50%; border: 1px solid var(--zm-ctl-border); background: var(--zm-card); color: var(--zm-text-dim); display: flex; align-items: center; justify-content: center; cursor: pointer; }
      .popb { padding: 16px 18px 18px; display: flex; flex-direction: column; gap: 12px; }
      .erow { display: flex; align-items: center; gap: 8px; }
      .erow .box { flex: 1 1 auto; }
      .err { font-family: var(--zm-font-body); font-size: var(--zm-fs-s); color: #e8645a; }
      .types { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
      .type { display: flex; flex-direction: column; align-items: center; gap: 7px; padding: 12px 6px; border: 1px solid var(--zm-border); background: var(--zm-card-2); border-radius: 14px; cursor: pointer; }
      .type:hover { border-color: var(--zm-text-faint); }
      .type span { font-family: var(--zm-font-body); font-size: var(--zm-fs-s); font-weight: 700; color: var(--zm-text-dim); text-align: center; line-height: 1.25; }
      .type .ic { width: 42px; height: 42px; border-radius: 999px; display: flex; align-items: center; justify-content: center; }
      .empty { padding: 48px; text-align: center; color: var(--zm-text-faint); font-family: var(--zm-font-body); }
    `,
  ];

  private _ent(id?: string): any { return id ? this.hass?.states?.[id] : undefined; }

  // the phase behind a name: L1 to L3, or L1-3 for a device on all phases; nothing while it is not known
  private _phase(eid: string | undefined): string {
    const s = String(this._ent(eid)?.state ?? '');
    return /^l[123]$/i.test(s) ? s.toUpperCase() : s === 'all' || s === 'Alle fasen' ? 'L1-3' : '';
  }

  // a consumer's phase from its config, for the sort and behind its name
  private _consPhase(id: string): string { return this._phase((this.config?.consumers ?? []).find((x: any) => x.id === id)?.phase); }

  private _optNum(id?: string): number | null {
    const s = this._ent(id);
    if (!s || s.state == null || s.state === '') return null;
    const n = Number(s.state);
    return Number.isFinite(n) ? n : null;
  }

  private _fmt(v: number | null, digits = 1): string | null { return v == null ? null : v.toFixed(digits); }

  // the battery fleet as rows, by phase and within a phase in the order of the config: a fixed order keeps the planning calm
  private _devices(): DevRow[] {
    const devices = fleetDevices(this.config);
    return Object.keys(devices).map((id) => {
      const e = devices[id];
      const net = Math.round((this._optNum(e.home_output) ?? 0) - (this._optNum(e.home_input) ?? 0));
      const kwhCap = Number(e.kwh) || 0;
      const socPct = Math.max(0, Math.min(100, this._optNum(e.soc) ?? 0));
      const rated = net < 0 ? Math.abs(Number(e.power_charge) || 0) : Number(e.power_discharge) || 0;
      const load = net !== 0 && rated > 0 ? Math.abs(net) / rated : 0;
      const temp = this._optNum(e.temp);
      const bypass = e.bypass ? this._ent(e.bypass)?.state === 'on' : false;
      return {
        id, name: e.name || id, model: e.model ?? '', eid: e.home_output ?? '', hasPack: kwhCap > 0,
        net, wattColor: net === 0 ? 'var(--zm-text-dim)' : load >= 0.97 ? '#e8645a' : load >= 0.85 ? '#f5a623' : '#13967d',
        socPct, dim: net === 0 && !bypass,
        kwhAvail: kwhCap > 0 ? kwhCap * socPct / 100 : null, kwhCap,
        inAggr: this._optNum(e.home_input_aggr), outAggr: this._optNum(e.home_output_aggr), rte: this._optNum(e.rte),
        solar: this._optNum(e.solar), temp, tempColor: temp != null && temp >= 58 ? '#e8645a' : temp != null && temp >= 50 ? '#f5a623' : 'var(--zm-text)',
        conn: Number(e.connection ?? 0), phase: this._phase(e.power_phase),
      };
    }).sort((a, b) => PHASE_ORDER.indexOf(a.phase) - PHASE_ORDER.indexOf(b.phase));
  }

  private _dtile(k: string, v: unknown, u = '', color = 'var(--zm-text)') {
    const none = v == null || v === '';
    return html`<div class="dtile"><div class="k">${k}</div><div class="v" style="color:${none ? 'var(--zm-text-faint)' : color}">${none ? '-' : v}${!none && u ? html`<span class="u">${u}</span>` : nothing}</div></div>`;
  }

  private _devTiles(d: DevRow) {
    return html`
      ${this._dtile('kWh', d.hasPack && d.kwhAvail != null ? html`${d.kwhAvail.toFixed(1)}<span class="u">/${d.kwhCap.toFixed(1)}</span>` : null)}
      ${this._dtile(t('ui.devices.tile_in', 'In'), this._fmt(d.inAggr), 'kWh')}
      ${this._dtile(t('ui.devices.tile_out', 'Uit'), this._fmt(d.outAggr), 'kWh')}
      ${this._dtile('RTE', this._fmt(d.rte, 0), '%')}
      ${this._dtile(t('ui.devices.tile_solar', 'Zon'), d.solar == null ? null : String(Math.round(d.solar)), 'W', d.solar ? '#f5a623' : 'var(--zm-text-dim)')}
      ${this._dtile(t('ui.devices.tile_temp', 'Temp'), this._fmt(d.temp), '°C', d.tempColor)}`;
  }

  // the HA device page of the entity, opened inside HA's own router; standalone there is no HA to go to
  private _haHref(eid: string): string | null {
    const dev = eid ? this.hass?.entities?.[eid]?.device_id : undefined;
    return dev ? `/config/devices/device/${dev}` : null;
  }

  private _go = (e: Event) => {
    e.preventDefault();
    this._edit = null;
    history.pushState(null, '', (e.currentTarget as HTMLAnchorElement).getAttribute('href') ?? '');
    window.dispatchEvent(new CustomEvent('location-changed'));
  };

  // the name links to the HA device, the pencil next to it opens the row's edit popup
  private _haName(name: string, eid: string, id: string, consumer: boolean, phase = '') {
    const href = this._haHref(eid);
    const open = () => { this._edit = { id, consumer }; this._draftName = name; this._nameErr = null; };
    return html`
      ${href ? html`<a href=${href} title=${t('ui.devices.open_ha', 'Open in Home Assistant')} @click=${this._go}>${name}</a>` : html`<span>${name}</span>`}${phase ? html`<span class="ph">${phase}</span>` : nothing}
      <button class="edit" title=${t('ui.common.edit', 'Bewerken')} @click=${open}><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20h4L19 9a2.8 2.8 0 0 0-4-4L4 16z"></path><path d="M13.5 6.5l4 4"></path></svg></button>`;
  }

  private _devRow(d: DevRow) {
    return html`
      <div class="row ${d.dim ? 'dim' : ''}">
        <div class="lbl">
          <div class="nm"><div class="n">${this._haName(d.name, d.eid, d.id, false, d.phase)}</div><div class="m"><i style="background:${CONN_COLOR[d.conn] ?? 'var(--zm-text-faint)'}"></i>${d.model}</div></div>
          <div class="w" style="color:${d.wattColor}">${d.net > 0 ? '+' : ''}${d.net}<span class="u"> W</span></div>
          <div class="bat">${d.hasPack ? html`<i style="width:${d.socPct}%"></i>` : nothing}</div>
          ${this.view === 'plan' ? html`<div class="dpop">${this._devTiles(d)}</div>` : nothing}
        </div>
        <div class="lane ${this.view === 'tiles' ? 'tiles' : ''}">${this.view === 'tiles' ? this._devTiles(d) : this._devLane(d.id)}</div>
        <div class="rsp"></div>
      </div>`;
  }

  // icons by the name the backend sends; an unknown name still gets a button, with the label as its tooltip
  private _actIcon(a: any) {
    switch (a.icon) {
      case 'play': return html`<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M7 5l12 7-12 7z"></path></svg>`;
      case 'pause': return html`<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1"></rect><rect x="14" y="5" width="4" height="14" rx="1"></rect></svg>`;
      case 'stop': return html`<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2.5"></rect></svg>`;
      case 'skip': return html`<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M6 5l9 7-9 7z"></path><rect x="16" y="5" width="2.6" height="14" rx="1"></rect></svg>`;
      case 'ban': return html`<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="12" cy="12" r="8"></circle><path d="M6.4 6.4l11.2 11.2"></path></svg>`;
      case 'check': return html`<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M5 13l4 4L19 7"></path></svg>`;
      default: return html`<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="4"></circle></svg>`;
    }
  }

  private async _act(id: string, action: string) {
    await api.consumerAction(id, action);
    this.dispatchEvent(new CustomEvent('zm-saved', { bubbles: true, composed: true }));  // arm_off changes the config the row reads
  }

  // a battery's plan in this lane: discharge up in teal, charging (over AC and from its own panels) in blue, scaled to its own peak; each bar shows its quarter and power on hover
  private _devLane(id: string) {
    const s = this.d?.series ?? {};
    const out: number[] = s[`${id}_discharge`] ?? [], ac: number[] = s[`${id}_charge`] ?? [], dc: number[] = s[`${id}_solar_in`] ?? [];
    return this._lane(out.map((v, i) => v - (ac[i] ?? 0) - (dc[i] ?? 0)), (v) => (v > 0 ? '#1fb89a' : '#3aa0c9'));
  }

  // one bar per quarter scaled to the lane's peak, with the power under the mouse as its tooltip
  private _lane(values: number[], color: (v: number) => string) {
    const times: string[] = this.d?.times ?? [], peak = Math.max(0.001, ...values.map(Math.abs));
    return values.map((v, i) => html`<i title=${Math.abs(v) < 0.02 ? nothing : `${times[i]} · ${nl(v, 2)} kW`} style="height:${Math.abs(v) < 0.02 ? 0 : Math.max(3, Math.round(Math.abs(v) / peak * 30))}px;background:${color(v)}"></i>`);
  }

  // a consumer's plan in this lane: its power per quarter, scaled to its own peak like a battery's lane; each bar shows its quarter and power on hover
  private _consLane(c: any) {
    return this._lane(this.d?.consumers?.find((x: any) => x.id === c.id)?.power ?? [], () => c.color);
  }

  private _consRow(c: any) {
    const tiles = [{ k: t('ui.consumers.today', 'Vandaag'), v: c.energyToday, u: ' kWh' }, ...(c.hasNext ? [{ k: t('ui.consumers.next', 'Volgende'), v: c.nextLabel, u: '' }] : []), ...c.entityTiles];
    const cfg = (this.config?.consumers ?? []).find((x: any) => x.id === c.id);
    const eid = cfg?.state ?? '';
    return html`
      <div class="row">
        <div class="lbl">
          <div class="nm"><div class="n">${this._haName(c.label, eid, c.id, true, this._consPhase(c.id))}</div><div class="m">${c.sub}</div></div>
          <div class="w" style="color:${c.iconColor}">${c.powerW}<span class="u"> W</span></div>
          <div class="st" style="color:${c.statusColor}">${c.statusLabel}</div>
          ${this.view === 'plan' ? html`<div class="dpop">${tiles.map((x: any) => this._dtile(x.k, x.v, x.u))}</div>` : nothing}
        </div>
        ${this.view === 'tiles'
          ? html`<div class="lane tiles">${tiles.map((x: any) => this._dtile(x.k, x.v, x.u))}<div class="acts">${c.actions.map((a: any) => html`<button title=${t(`action.${a.id}`, a.label)} ?disabled=${!a.enabled} @click=${() => this._act(c.id, a.id)} style="width:70px;height:28px;padding:0;display:flex;align-items:center;justify-content:center;border:1.5px solid ${a.bd};background:${a.bg};color:${a.fg};border-radius:8px;cursor:${a.enabled ? 'pointer' : 'default'};opacity:${a.enabled ? 1 : 0.32}">${this._actIcon(a)}</button>`)}</div></div>`
          : html`<div class="lane">${this._consLane(c)}</div>`}
        <div class="rsp"></div>
      </div>`;
  }

  private async _saveMaxKw(v: number) {
    try {
      await api.saveSchema('edit', 'distribution', { id: 'distribution', max_kw: v });
      this.dispatchEvent(new CustomEvent('zm-saved', { bubbles: true, composed: true }));
    } catch { /* ignore */ }
  }

  // Modus, Handmatig and Correctie are the distribution's entities, Max net its max_kw setting
  private _controls(inline: boolean) {
    const e: any = Object.values(byKind(this.config, 'Distribution'))[0] ?? {};
    const mode = this._ent(e.mode), mp = this._ent(e.manual_power), sc = this._ent(e.setpoint_correction);
    const setNum = (eid: string) => (ev: Event) => this.hass?.callService?.('number', 'set_value', { entity_id: eid, value: Number((ev.target as HTMLInputElement).value) });
    const pick = (ev: Event) => {
      // show the chosen option right away, otherwise every render pulls it back to the old state
      const option = (ev.target as HTMLSelectElement).value;
      this._pendingMode = option;
      window.setTimeout(() => { this._pendingMode = null; }, 2000);
      this.hass?.callService?.('select', 'select_option', { entity_id: e.mode, option });
    };
    return html`
      <div class="ctl ${inline ? 'inline' : ''}">
        <div class="h"><span class="i"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"></path></svg></span>${t('ui.devices.controls', 'Controls')}<span class="s">${t('ui.devices.steering', 'sturing')}</span></div>
        <div class="grid">
          <label class="fld"><span>${t('ui.devices.mode', 'Modus')}</span><div class="box"><select .value=${live(this._pendingMode ?? mode?.state ?? '')} ?disabled=${!mode} @change=${pick}>${(mode?.attributes?.options ?? []).map((o: string) => html`<option value=${o} ?selected=${o === mode.state}>${this.hass?.formatEntityState?.(mode, o) ?? o}</option>`)}</select></div></label>
          <label class="fld"><span>${t('ui.devices.manual', 'Handmatig')}</span><div class="box"><input type="number" .value=${String(mp?.state ?? '')} ?disabled=${!mp} @change=${setNum(e.manual_power)} /><em>W</em></div></label>
          <label class="fld"><span>${t('ui.devices.max_grid', 'Max net')}</span><div class="box"><input type="number" .value=${String(e.max_kw ?? '')} @change=${(ev: Event) => this._saveMaxKw(Number((ev.target as HTMLInputElement).value))} /><em>kW</em></div></label>
          <label class="fld"><span>${t('ui.devices.correction', 'Correctie')}</span><div class="box"><input type="number" .value=${String(sc?.state ?? '')} ?disabled=${!sc} @change=${setNum(e.setpoint_correction)} /><em>W</em></div></label>
        </div>
      </div>`;
  }

  private async _saveName(id: string) {
    this._nameErr = null;
    try {
      const res = await api.saveSchema('edit', id, { device_id: id, name: this._draftName });
      if (res.ok) this.dispatchEvent(new CustomEvent('zm-saved', { bubbles: true, composed: true }));
      else this._nameErr = (res.errors ?? [t('ui.common.unknown_error', 'onbekende fout')]).join('; ');
    } catch (e: any) {
      this._nameErr = e?.message ?? String(e);
    }
  }

  // the edit popup of one row: its schema group in edit mode, for a battery also its name and the BLE pairing
  private _editPopup() {
    const { id, consumer } = this._edit!;
    const dev = consumer ? null : fleetDevices(this.config)[id];
    const cfg = consumer ? (this.config?.consumers ?? []).find((x: any) => x.id === id) : null;
    const name = dev?.name || cfg?.name || id;
    const href = this._haHref(dev?.home_output ?? cfg?.state ?? '');
    const fields = this.schema?.[id];
    const close = () => (this._edit = null);
    return html`
      <div class="pop-bg" @click=${close}>
        <div class="pop wide" role="dialog" @click=${(e: Event) => e.stopPropagation()}>
          <div class="poph">
            <h3>${name}</h3>
            ${href ? html`<a class="pill" href=${href} @click=${this._go}><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"></path></svg>Home Assistant</a>` : nothing}
            ${dev && ZendureBle.available() ? html`<button class="pill" @click=${() => { this._onboardName = dev.ble_name ?? ''; close(); }}>${t('ui.devices.ble_pair', 'BLE koppelen')}</button>` : nothing}
            <button class="x" title=${t('ui.common.close', 'Sluiten')} @click=${close}><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"></path></svg></button>
          </div>
          <div class="popb">
            ${dev ? html`<label class="fld"><span>${t('ui.devices.name', 'Naam')}</span><div class="erow"><div class="box"><input .value=${this._draftName} @input=${(e: Event) => (this._draftName = (e.target as HTMLInputElement).value)} @keydown=${(e: KeyboardEvent) => e.key === 'Enter' && this._saveName(id)} /></div><button class="addbtn" @click=${() => this._saveName(id)}>${t('ui.common.save', 'Opslaan')}</button></div>${this._nameErr ? html`<span class="err">${this._nameErr}</span>` : nothing}</label>` : nothing}
            ${fields
              ? html`<zm-group .entry=${id} .fields=${fields} .config=${this.config} .hass=${this.hass} heading=${name} iconKey=${consumer ? cfg?.kind ?? '' : 'battery'} collection=${consumer ? 'consumers' : ''} ?singleColumn=${consumer} autoEdit @zm-saved=${close}></zm-group>`
              : html`<div class="empty">${t('ui.app.loading', 'laden…')}</div>`}
          </div>
        </div>
      </div>`;
  }

  // one line-icon per type, same stroke and viewBox as the icons the cards already use
  private _typeIcon(name: string) {
    const p: Record<string, any> = {
      ev: svg`<path d="M13 2L5 13h5l-1 9 9-12h-6z"></path>`,
      washer: svg`<rect x="4" y="3" width="16" height="18" rx="2.5"></rect><circle cx="12" cy="13.5" r="4"></circle><path d="M7.5 6.5h1.5"></path>`,
      dryer: svg`<rect x="4" y="3" width="16" height="18" rx="2.5"></rect><path d="M9 11a3 3 0 0 1 5 1.5M15 16a3 3 0 0 1-5-1.5"></path><path d="M7.5 6.5h1.5"></path>`,
      dish: svg`<rect x="4" y="3" width="16" height="18" rx="2.5"></rect><path d="M4 9h16M8.5 13h7M8.5 17h4.5"></path>`,
      plug: svg`<path d="M9 2v6M15 2v6M6 8h12v4a6 6 0 01-12 0z"></path><path d="M12 18v4"></path>`,
      hp: svg`<path d="M3 8h10a3 3 0 1 0-3-3M3 12h14a3 3 0 1 1-3 3M3 16h8a2.5 2.5 0 1 1-2.5 2.5"></path>`,
      boiler: svg`<path d="M12 3s6 6 6 11a6 6 0 0 1-12 0c0-5 6-11 6-11z"></path>`,
      boiler_e: svg`<path d="M12 3s6 6 6 11a6 6 0 0 1-12 0c0-5 6-11 6-11z"></path><path d="M12.8 9.5L10 14h3l-.8 4 3.2-5h-3z"></path>`,
      pool: svg`<path d="M3 15c2 0 2-1.6 4-1.6s2 1.6 4 1.6 2-1.6 4-1.6 2 1.6 4 1.6M3 19c2 0 2-1.6 4-1.6s2 1.6 4 1.6 2-1.6 4-1.6 2 1.6 4 1.6"></path><path d="M8.5 12V6a2 2 0 1 1 4 0"></path>`,
    };
    return p[name] ?? p.plug;
  }

  private _addConsumer = async (key: string) => {
    this._picker = false;
    const preset = CONSUMER_TYPES[key];
    if (!preset) return;
    // the name proposed is the preset label, numbered when a consumer of that name already exists; the backend refuses duplicates
    const names = new Set((this.config?.consumers ?? []).map((c: any) => String(c.name ?? '').toLowerCase()));
    const label = t(`ui.consumer_type.${key}`, preset.label);
    let title = label, n = 1;
    while (names.has(title.toLowerCase())) title = `${label} ${++n}`;
    const data: Record<string, unknown> = {};
    if (preset.kind) data.kind = preset.kind;
    this._busy = true;
    try {
      const res = await api.addSubentry(preset.type, title, data);
      if (res.subentry_id) this._edit = { id: res.subentry_id, consumer: true };  // its popup opens, so the entities can be picked right away
      this.dispatchEvent(new CustomEvent('zm-saved', { bubbles: true, composed: true }));
    } catch { /* ignore */ }
    finally { this._busy = false; }
  };

  private _pickerPopup() {
    return html`
      <div class="pop-bg" @click=${() => (this._picker = false)}>
        <div class="pop" @click=${(e: Event) => e.stopPropagation()}>
          <div class="poph"><h3>${t('ui.consumers.add', 'Verbruiker toevoegen')}</h3><button class="x" @click=${() => (this._picker = false)}><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"></path></svg></button></div>
          <div class="popb"><div class="types">
            ${Object.entries(CONSUMER_TYPES).map(([k, v]) => html`<button class="type" @click=${() => this._addConsumer(k)}>
              <span class="ic" style="background:color-mix(in srgb, ${v.color} 15%, transparent)"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke=${v.color} stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round">${this._typeIcon(v.icon)}</svg></span>
              <span>${t(`ui.consumer_type.${k}`, v.label)}</span>
            </button>`)}
          </div></div>
        </div>
      </div>`;
  }

  render() {
    const d = this.d;
    const devs = this._devices();
    const cards = d ? consumerCards(d, this.config?.consumers ?? [], this.hass, this.actions, this.tiles).sort((a: any, b: any) => PHASE_ORDER.indexOf(this._consPhase(a.id)) - PHASE_ORDER.indexOf(this._consPhase(b.id))) : [];
    // the now line runs from the chart through the rows, in the middle of the current quarter
    const frac = d ? (d.NOW + 0.5) / d.N : 0;
    return html`
      ${d
        ? html`<div class="top"><zm-chart class="flow" .option=${this._option}></zm-chart>${this._controls(false)}</div>`
        : this._controls(true)}
      ${!devs.length && !cards.length
        ? html`<div class="empty">${t('ui.devices.empty', 'Geen Zendure-devices in de config.')}</div>`
        : html`<div class="rowsw">
            <div>${devs.filter((x) => !this._activeOnly || x.conn !== 0).map((x) => this._devRow(x))}${cards.map((c: any) => this._consRow(c))}</div>
            ${d && this.view === 'plan' ? html`<div class="now" style="left:calc(${LBL}px + (100% - ${LBL + RSP}px) * ${frac})"></div>` : nothing}
          </div>`}
      <div class="foot">
        ${d ? html`<button class="addbtn" ?disabled=${this._busy} @click=${() => (this._picker = true)}>+ ${t('ui.consumers.add', 'Verbruiker toevoegen')}</button>` : nothing}
        ${devs.length ? html`<button class="addbtn" @click=${() => { this._activeOnly = !this._activeOnly; localStorage.setItem('zm-active-only', this._activeOnly ? '1' : '0'); }}>${this._activeOnly ? t('ui.devices.show_all', 'Alle apparaten') : t('ui.devices.show_active', 'Actieve apparaten')}</button>` : nothing}
        ${ZendureBle.available() ? html`<button class="addbtn" title=${t('ui.ble.title', 'Apparaat koppelen via Bluetooth')} @click=${() => (this._onboardName = '')}>${t('ui.devices.pair', 'Koppelen')}</button>` : nothing}
      </div>
      ${this._edit ? this._editPopup() : nothing}
      ${this._picker ? this._pickerPopup() : nothing}
      ${this._onboardName !== null
        ? html`<zm-ble-onboard .config=${this.config} .driverId=${Object.keys(byKind(this.config, 'ZendureDriver'))[0] ?? ''} .bleName=${this._onboardName} @close=${() => (this._onboardName = null)}></zm-ble-onboard>`
        : nothing}
    `;
  }
}

if (!customElements.get('zm-overview')) customElements.define('zm-overview', ZmOverview);
