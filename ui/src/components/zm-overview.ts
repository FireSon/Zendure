import { LitElement, html, css, nothing } from 'lit';
import { property, state } from 'lit/decorators.js';
import { live } from 'lit/directives/live.js';
import { tokens } from '../tokens.js';
import { api } from '../service/api.js';
import type { Schema, Theme } from '../types.js';
import { chartColors, flowOption } from '../charts.js';
import { chartData, consumerCards, nl } from '../derive.js';
import { loadDay } from '../service/loader.js';
import { byKind, fleetDevices } from '../service/drivers.js';
import { ZendureBle } from '../service/ble.js';
import { t } from '../service/i18n.js';
import './zm-chart.js';
import './zm-group.js';
import './zm-ble-onboard.js';
import './zm-manage.js';

// "Grafiek" and "Apparaten": one card with the plan chart, the Controls tile left of it, and under it one row
// per battery and per consumer on the chart's time axis. The plan view fills a row with its planning, the
// tiles view with the device's readings; the name block on the left is the same in both.

const LBL = 290;  // px: the name column, and the chart's left edge, so the rows and the plan share one time axis
const RSP = 54;   // px: the price axis right of the chart, kept free on every row
const NARROW = window.matchMedia('(max-width: 760px)');  // the phone: the chart and the rows scroll sideways under a narrow name column that stays put
const LBL_NARROW = 160;  // px: the name column on the phone

// ConnectionType from device.py: 0 offline, 1 local broker, 2 cloud, 3 zensdk (http), 4 ble
const CONN_COLOR: Record<number, string> = { 0: '#e8645a', 1: '#4a90d9', 2: '#1fb89a', 3: '#3aa0c9', 4: '#4a90d9' };
const CONTROLLER_COLOR = '#9b7bd4';  // regulates itself: online, not steered
const PHASE_ORDER = ['L1', 'L2', 'L3', 'L1-3', ''];  // the device rows by phase; an unknown phase last

interface DevRow {
  id: string; name: string; model: string; eid: string; hasPack: boolean;
  net: number; wattColor: string; socPct: number; dim: boolean;
  kwhAvail: number | null; kwhCap: number;
  inAggr: number | null; outAggr: number | null; rte: number | null;
  solar: number | null; temp: number | null; tempColor: string; dot: string; online: boolean; phase: string;
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
  @property({ attribute: false }) add: any[] = [];  // the tile catalog of the add dialog

  @state() private _pendingMode: string | null = null;  // the option just picked, until the entity confirms it
  @state() private _activeOnly = localStorage.getItem('zm-active-only') === '1';  // the device rows: all, or only the ones that are not offline; kept per browser like the tab
  @state() private _manage = false;  // the add and edit dialog is open
  @state() private _onboardName: string | null = null;
  @state() private _narrow = NARROW.matches;
  @state() private _day: string | null = null;  // the past day shown, null for the live plan
  @state() private _dayD: any = null;
  @state() private _err: string | null = null;
  private get _shown() { return this._day ? this._dayD : this.d; }
  private _option: any = null;  // the chart option, rebuilt only when the plan or the theme changes, not on every hass update
  private _onNarrow = () => { this._narrow = NARROW.matches; };
  private get _lbl() { return this._narrow ? LBL_NARROW : LBL; }

  // follow the phone layout when the screen turns or the window is resized past it
  connectedCallback() { super.connectedCallback(); NARROW.addEventListener('change', this._onNarrow); }
  disconnectedCallback() { super.disconnectedCallback(); NARROW.removeEventListener('change', this._onNarrow); }

  willUpdate(changed: Map<string, unknown>) {
    // a new option object resets the chart and drops its tooltip; hass changes several times a second
    if (changed.has('d') || changed.has('_dayD') || changed.has('_day') || changed.has('theme') || changed.has('_narrow')) this._option = this._shown ? flowOption(this._shown, chartColors(this.theme), this._lbl) : null;
  }

  // a step through the days: the live plan for today, the recorder for a day before it
  private async _step(days: number | null) {
    await this._run(async () => {
      const now = new Date(), cur = this._day ? new Date(this._day + 'T12:00') : now;
      const to = days === null ? now : new Date(cur.getFullYear(), cur.getMonth(), cur.getDate() + days, 12);
      if (to.toDateString() === now.toDateString() || to > now) { this._day = null; return; }
      const iso = `${to.getFullYear()}-${String(to.getMonth() + 1).padStart(2, '0')}-${String(to.getDate()).padStart(2, '0')}`;
      this._dayD = chartData(await loadDay(iso, this.config));
      this._day = iso;
    });
  }

  // an action of the overview: a failure shows above it instead of getting lost
  private async _run(job: () => Promise<unknown>) {
    this._err = null;
    try { await job(); } catch (e: any) { this._err = e?.message ?? String(e); }
  }

  // previous, the day shown (a click goes back to today) and next, above the controls
  private _nav(inline: boolean) {
    const label = this._day ? new Date(this._day + 'T12:00').toLocaleDateString(this.hass?.locale?.language, { weekday: 'short', day: 'numeric', month: 'short' }) : t('ui.consumers.today', 'Vandaag');
    return html`<div class="nav ${inline ? 'inline' : ''}"><button @click=${() => this._step(-1)}><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M15 6l-6 6 6 6"></path></svg></button><button class="d" ?disabled=${!this._day} @click=${() => this._step(null)}>${label}</button><button ?disabled=${!this._day} @click=${() => this._step(1)}><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"></path></svg></button></div>`;
  }

  static styles = [
    tokens,
    css`
      :host { display: block; }
      .err { font-family: var(--zm-font-body); font-size: var(--zm-fs-s); color: #e8645a; }
      .top { position: relative; }
      .flow { display: block; width: 100%; height: 276px; }
      /* the steering of the distribution, in the chart's left margin */
      .nav { position: absolute; left: 0; top: 0; width: ${LBL - 46}px; height: 32px; display: flex; gap: 6px; }
      .nav.inline { position: static; width: 100%; max-width: 520px; margin-bottom: 8px; }
      .nav button { height: 34px; min-width: 38px; display: inline-flex; align-items: center; justify-content: center; border: 1px solid var(--zm-ctl-border); border-radius: 10px; background: linear-gradient(160deg, var(--zm-ctl-a), var(--zm-ctl-b)); color: #1fb89a; cursor: pointer; }
      .nav .d { flex: 1 1 auto; color: var(--zm-text); font-family: var(--zm-font-body); font-size: var(--zm-fs-m); font-weight: 800; }
      .nav button:hover:not(:disabled) { border-color: #1fb89a; }
      .nav button:disabled { opacity: 0.35; cursor: default; }
      .ctl { position: absolute; left: 0; top: 40px; bottom: 6px; width: ${LBL - 46}px; display: flex; flex-direction: column; gap: 16px; padding: 16px 16px 18px; border-radius: 18px; background: linear-gradient(160deg, var(--zm-ctl-a), var(--zm-ctl-b)); border: 1px solid var(--zm-ctl-border); box-shadow: var(--zm-shadow-sm); }
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
      /* on the phone the chart and the rows scroll sideways as one; the name column stays, opaque over the lanes, without the battery bar and status */
      .narrow { overflow-x: auto; -webkit-overflow-scrolling: touch; }
      .narrow .wide { width: 1200px; }
      .narrow .lbl { position: sticky; left: 0; z-index: 2; background: var(--zm-card); }
      .narrow .row:nth-child(odd) .lbl { background: linear-gradient(var(--zm-zebra), var(--zm-zebra)), var(--zm-card); }
      .narrow .bat, .narrow .st { display: none; }
      /* the rows, zebra striped, each one name block, one lane on the time axis and the free price column */
      .rowsw { position: relative; margin-top: 8px; }
      .now { position: absolute; top: 0; bottom: 0; border-left: 1.6px solid #f47a20; opacity: 0.7; pointer-events: none; }
      .row { display: flex; align-items: stretch; height: 44px; }
      .row:nth-child(odd) { background: var(--zm-zebra); }
      .lbl { position: relative; flex: none; width: var(--lbl); display: flex; align-items: center; gap: 8px; padding: 0 12px 0 6px; }
      .lbl:hover { background: var(--zm-border-2); }
      .row.dim .lbl > :not(.dpop), .row.dim .lane { opacity: 0.6; }
      .nm { flex: 1 1 auto; min-width: 0; }
      .nm .n { display: flex; align-items: center; gap: 3px; min-width: 0; font-family: var(--zm-font-body); font-size: var(--zm-fs-m); font-weight: 800; color: var(--zm-text); }
      .nm .n a, .nm .n span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: inherit; text-decoration: none; text-underline-offset: 3px; }
      .nm .n a:hover { text-decoration: underline; color: #13967d; }
      .nm .m .ph { flex: none; font-weight: 700; color: var(--zm-text-dim); }
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
      .foot { display: flex; gap: 8px; padding: 12px 6px 0; }
      .addbtn { display: inline-flex; align-items: center; gap: 6px; border: 1px solid var(--zm-border); background: var(--zm-card); color: var(--zm-text-dim); border-radius: 999px; padding: 6px 12px; font-family: var(--zm-font-body); font-size: var(--zm-fs-m); font-weight: 700; cursor: pointer; }
      .addbtn:hover { color: var(--zm-text); border-color: var(--zm-text-faint); }
      .addbtn:disabled { opacity: 0.5; cursor: default; }
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
      const gridOff = e.grid_state ? this._ent(e.grid_state)?.state === 'off' : false;  // no grid on the AC side: shown as offline
      const offline = [0, 2].includes(Number(e.state ?? 0)) || gridOff;  // CREATED or OFFLINE: never heard or unreachable
      return {
        id, name: e.name || id, model: e.model ?? '', eid: e.home_output ?? '', hasPack: kwhCap > 0,
        net, wattColor: net === 0 ? 'var(--zm-text-dim)' : load >= 0.97 ? '#e8645a' : load >= 0.85 ? '#f5a623' : '#13967d',
        socPct, dim: offline,
        kwhAvail: kwhCap > 0 ? kwhCap * socPct / 100 : null, kwhCap,
        inAggr: this._optNum(e.home_input_aggr), outAggr: this._optNum(e.home_output_aggr), rte: this._optNum(e.rte),
        solar: this._optNum(e.solar), temp, tempColor: temp != null && temp >= 58 ? '#e8645a' : temp != null && temp >= 50 ? '#f5a623' : 'var(--zm-text)',
        dot: offline ? CONN_COLOR[0] : Number(e.state) === 1 ? CONTROLLER_COLOR : CONN_COLOR[Number(e.connection ?? 1)], online: !offline, phase: this._phase(e.power_phase),
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
    history.pushState(null, '', (e.currentTarget as HTMLAnchorElement).getAttribute('href') ?? '');
    window.dispatchEvent(new CustomEvent('location-changed'));
  };

  // the name links to the HA device; adding and editing go through the Toevoegen dialog
  private _haName(name: string, eid: string) {
    const href = this._haHref(eid);
    return html`
      ${href ? html`<a href=${href} title=${t('ui.devices.open_ha', 'Open in Home Assistant')} @click=${this._go}>${name}</a>` : html`<span>${name}</span>`}`;
  }

  private _devRow(d: DevRow) {
    return html`
      <div class="row ${d.dim ? 'dim' : ''}">
        <div class="lbl">
          <div class="nm"><div class="n">${this._haName(d.name, d.eid)}</div><div class="m"><i style="background:${d.dot}"></i>${d.phase ? html`<span class="ph">${d.phase}</span>` : nothing}${d.model}</div></div>
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
    await this._run(async () => {
      await api.consumerAction(id, action);
      this.dispatchEvent(new CustomEvent('zm-saved', { bubbles: true, composed: true }));  // arm_off changes the config the row reads
    });
  }

  // a battery's plan in this lane: discharge up in teal, charging (over AC and from its own panels) in blue, scaled to its own peak; each bar shows its quarter and power on hover
  private _devLane(id: string) {
    const s = this._shown?.series ?? {};
    const out: number[] = s[`${id}_discharge`] ?? [], ac: number[] = s[`${id}_charge`] ?? [], dc: number[] = s[`${id}_solar_in`] ?? [];
    return this._lane(out.map((v, i) => v - (ac[i] ?? 0) - (dc[i] ?? 0)), (v) => (v > 0 ? '#1fb89a' : '#3aa0c9'));
  }

  // one bar per quarter scaled to the lane's peak, with the power under the mouse as its tooltip
  private _lane(values: number[], color: (v: number) => string) {
    const times: string[] = this._shown?.times ?? [], peak = Math.max(0.001, ...values.map(Math.abs));
    return values.map((v, i) => html`<i title=${Math.abs(v) < 0.02 ? nothing : `${times[i]} · ${nl(v, 2)} kW`} style="height:${Math.abs(v) < 0.02 ? 0 : Math.max(3, Math.round(Math.abs(v) / peak * 30))}px;background:${color(v)}"></i>`);
  }

  // a consumer's plan in this lane: its power per quarter, scaled to its own peak like a battery's lane; each bar shows its quarter and power on hover
  private _consLane(c: any) {
    return this._lane(this._shown?.consumers?.find((x: any) => x.id === c.id)?.power ?? [], () => c.color);
  }

  private _consRow(c: any) {
    const tiles = [{ k: t('ui.consumers.today', 'Vandaag'), v: c.energyToday, u: ' kWh' }, ...(c.hasNext ? [{ k: t('ui.consumers.next', 'Volgende'), v: c.nextLabel, u: '' }] : []), ...c.entityTiles];
    const cfg = (this.config?.consumers ?? []).find((x: any) => x.id === c.id);
    const eid = cfg?.state ?? '';
    return html`
      <div class="row">
        <div class="lbl">
          <div class="nm"><div class="n">${this._haName(c.label, eid)}</div><div class="m">${(ph => ph ? html`<span class="ph">${ph}</span>` : nothing)(this._consPhase(c.id))}${c.sub}</div></div>
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
    await this._run(async () => {
      await api.saveSchema('edit', 'distribution', { max_kw: v });
      this.dispatchEvent(new CustomEvent('zm-saved', { bubbles: true, composed: true }));
    });
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

  render() {
    const d = this._shown;
    const devs = this._devices();
    const cards = d ? consumerCards(d, this.config?.consumers ?? [], this.hass, this.actions, this.tiles).sort((a: any, b: any) => PHASE_ORDER.indexOf(this._consPhase(a.id)) - PHASE_ORDER.indexOf(this._consPhase(b.id))) : [];
    // the now line runs from the chart through the rows, in the middle of the current quarter
    const frac = d ? (d.NOW + 0.5) / d.N : 0;
    // on the phone the controls sit above the chart instead of in its left margin
    return html`
      ${this._err ? html`<div class="err">${this._err}</div>` : nothing}
      ${this._narrow && this.view === 'plan' ? this._nav(true) : nothing}
      ${!d || this._narrow ? this._controls(true) : nothing}
      <div class=${this._narrow ? 'narrow' : ''} style="--lbl:${this._lbl}px"><div class="wide">
      ${d ? html`<div class="top"><zm-chart class="flow" .option=${this._option}></zm-chart>${this._narrow || this.view !== 'plan' ? nothing : this._nav(false)}${this._narrow ? nothing : this._controls(false)}</div>` : nothing}
      ${!devs.length && !cards.length
        ? html`<div class="empty">${t('ui.devices.empty', 'Geen Zendure-devices in de config.')}</div>`
        : html`<div class="rowsw">
            <div>${devs.filter((x) => !this._activeOnly || x.online).map((x) => this._devRow(x))}${cards.map((c: any) => this._consRow(c))}</div>
            ${d && this.view === 'plan' && d.NOW >= 0 ? html`<div class="now" style="left:calc(${this._lbl}px + (100% - ${this._lbl + RSP}px) * ${frac})"></div>` : nothing}
          </div>`}
      </div></div>
      <div class="foot">
        <button class="addbtn" @click=${() => (this._manage = true)}>+ ${t('ui.manage.add', 'Toevoegen')}</button>
        ${devs.length ? html`<button class="addbtn" @click=${() => { this._activeOnly = !this._activeOnly; localStorage.setItem('zm-active-only', this._activeOnly ? '1' : '0'); }}>${this._activeOnly ? t('ui.devices.show_all', 'Alle apparaten') : t('ui.devices.show_active', 'Actieve apparaten')}</button>` : nothing}
        ${ZendureBle.available() ? html`<button class="addbtn" title=${t('ui.ble.title', 'Apparaat koppelen via Bluetooth')} @click=${() => (this._onboardName = '')}>${t('ui.devices.pair', 'Koppelen')}</button>` : nothing}
      </div>
      ${this._manage ? html`<zm-manage .config=${this.config} .schema=${this.schema} .add=${this.add} .hass=${this.hass} @close=${() => (this._manage = false)}></zm-manage>` : nothing}
      ${this._onboardName !== null
        ? html`<zm-ble-onboard .config=${this.config} .driverId=${Object.keys(byKind(this.config, 'ZendureDriver'))[0] ?? ''} .bleName=${this._onboardName} @close=${() => (this._onboardName = null)}></zm-ble-onboard>`
        : nothing}
    `;
  }
}

if (!customElements.get('zm-overview')) customElements.define('zm-overview', ZmOverview);
