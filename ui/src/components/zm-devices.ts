import { LitElement, html, svg, css, nothing } from 'lit';
import { property, state } from 'lit/decorators.js';
import { tokens } from '../tokens.js';
import { api } from '../service/api.js';
import type { Plan, Schema, Field } from '../types.js';
import './zm-settings.js';
import './zm-ble-onboard.js';
import { ZendureBle } from '../service/ble.js';
import { allDevices, allSmartmeters, primaryDriver } from '../service/drivers.js';

// "Devices": Zendure fleet cards (faint SoC backfill, battery glyph with SMIL flow when
// not idle, Zon/Temp tiles). Per-unit telemetry is MQTT-fed (HA-only, reads 0 standalone;
// SoC falls back to the fleet aggregate). Each card has an edit toggle: the display name
// is editable, the discovery facts (model/sn/ip/...) are read-only. Plus the Fleet-
// instellingen group. Lifted from the v2 design + edit-toggle pattern.

interface DeviceView {
  id: string; idx: number; name: string; model: string; bleName: string;
  socPct: string; socY: string; socH: string; battY: string; battH: string;
  powerVal: string; powerColor: string;
  solar: number; temp: number | null;
  availKwh: number | null; homeIn: number | null; homeOut: number | null;
  flowOn: boolean; flowTo: string; flowDur: string;
  connState: string; connLabel: string; connColor: string;
}

// DeviceConnection status -> dot colour. The sensor state may arrive as a name ("mqtt") or an int
// ("2"/"2.0"); normConn() folds both to the canonical name that keys the colour + icon.
const CONN_COLOR: Record<string, string> = {
  discovered: 'var(--zm-text-faint)',
  offline: '#e8645a',
  mqtt_cloud: '#1fb89a',
  mqtt_local: '#1fb89a',
  modbus: '#3aa0c9',
  http: '#3aa0c9',
  ble: '#4a90d9',
};
const CONN_NAME: Record<string, string> = { '0': 'discovered', '1': 'offline', '2': 'mqtt_cloud', '3': 'mqtt_local', '4': 'modbus', '5': 'http', '6': 'ble' };
function normConn(s: string): string {
  if (!s) return '';
  const k = s.toLowerCase();
  if (k in CONN_COLOR) return k;
  const n = Number(s);
  return Number.isFinite(n) ? (CONN_NAME[String(Math.trunc(n))] ?? '') : '';
}

export class ZmDevices extends LitElement {
  @property({ attribute: false }) config: any = null;
  @property({ attribute: false }) plan: Plan | null = null;
  @property({ attribute: false }) schema: Schema | null = null;
  @property({ attribute: false }) hass: any = null;  // live telemetry read from hass.states[entity_id]

  @state() private _editId: string | null = null;
  @state() private _draftName = '';
  @state() private _saving = false;
  @state() private _error: string | null = null;
  @state() private _onboardName: string | null = null;

  static styles = [
    tokens,
    css`
      :host { display: block; }
      .sechead { display: flex; align-items: center; gap: 10px; margin: 24px 0 14px; }
      .sechead.mt { margin-top: 26px; }
      .sechead h3 { margin: 0; font-family: var(--zm-font-display); font-weight: 600; font-size: 17px; color: var(--zm-text); }
      .sechead .hint { font-family: var(--zm-font-body); font-size: 13px; color: var(--zm-text-dim); margin-left: auto; }
      /* every card is sized to N tiles of 63px (+6px gaps +30px padding) so the tiles are identical width everywhere */
      .row { display: flex; flex-wrap: wrap; gap: 14px; align-items: flex-start; }
      .grid.fleet { display: grid; grid-template-columns: repeat(auto-fill, 300px); gap: 14px; }
      .dcard { position: relative; overflow: hidden; background: var(--zm-card); border: 1px solid var(--zm-border); border-radius: 18px; box-shadow: var(--zm-shadow); padding: 12px 15px; box-sizing: border-box; animation: zm-rise 0.4s ease both; }
      .dcard.ctrl { width: 438px; max-width: 100%; }
      .dcard.meter { width: 231px; max-width: 100%; }
      .tiles { display: flex; gap: 6px; margin-top: 10px; }
      .tile { flex: 1 1 0; min-width: 0; box-sizing: border-box; display: flex; flex-direction: column; align-items: center; text-align: center; gap: 2px; background: var(--zm-card-2); border-radius: 10px; padding: 5px 3px; }
      .tile .k { font-family: var(--zm-font-body); font-size: 8.5px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em; color: var(--zm-text-faint); }
      .tile .v { font-family: var(--zm-font-display); font-weight: 600; font-size: 14px; line-height: 1; }
      .pencil { flex: none; width: 26px; height: 26px; border-radius: 8px; border: 1px solid var(--zm-border); background: transparent; color: var(--zm-text-dim); display: flex; align-items: center; justify-content: center; cursor: pointer; }
      .pencil:hover { color: var(--zm-text); border-color: var(--zm-text-faint); }
      .pencil svg { width: 14px; height: 14px; }
      .erow { display: flex; align-items: center; gap: 8px; margin-top: 8px; }
      .erow .k { flex: none; width: 58px; font-family: var(--zm-font-body); font-size: 10px; font-weight: 700; color: var(--zm-text-faint); text-transform: uppercase; letter-spacing: 0.03em; }
      .erow input { flex: 1 1 auto; min-width: 0; background: var(--zm-card-2); color: var(--zm-text); border: 1px solid var(--zm-border); border-radius: 7px; font-family: var(--zm-font-body); font-weight: 700; font-size: 12.5px; padding: 5px 9px; box-sizing: border-box; }
      .erow input:focus { outline: none; border-color: var(--zm-primary); }
      .erow .ro { flex: 1 1 auto; min-width: 0; font-family: var(--zm-font-body); font-weight: 700; font-size: 12px; color: var(--zm-text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; text-align: right; }
      .cluster { display: flex; gap: 6px; margin-top: 12px; justify-content: flex-end; }
      .iconbtn { width: 32px; height: 32px; border-radius: 9px; border: 1px solid var(--zm-border); background: transparent; color: var(--zm-text-dim); display: inline-flex; align-items: center; justify-content: center; cursor: pointer; }
      .iconbtn:hover { color: var(--zm-text); border-color: var(--zm-text-faint); }
      .iconbtn.primary { border: none; background: var(--zm-primary); color: #fff; box-shadow: 0 2px 8px var(--zm-primary-soft); }
      .iconbtn:disabled { opacity: 0.45; cursor: default; }
      .iconbtn svg { width: 17px; height: 17px; }
      .err { font-family: var(--zm-font-body); font-size: 11px; color: #e8645a; margin-top: 8px; }
      .empty { padding: 48px; text-align: center; color: var(--zm-text-faint); font-family: var(--zm-font-body); }
      .addbtn { display: inline-flex; align-items: center; gap: 6px; margin-right: 8px; border: 1px solid var(--zm-border); background: var(--zm-card); color: var(--zm-text-dim); border-radius: 999px; padding: 6px 12px; font-family: var(--zm-font-body); font-size: 12px; font-weight: 700; cursor: pointer; }
      .addbtn:hover { color: var(--zm-text); border-color: var(--zm-text-faint); }
      .addbtn svg { width: 14px; height: 14px; }
      .crow { display: flex; align-items: center; gap: 8px; margin-top: 9px; }
      .crow .ck { flex: none; width: 82px; font-family: var(--zm-font-body); font-size: 10px; font-weight: 700; color: var(--zm-text-faint); text-transform: uppercase; letter-spacing: 0.03em; }
      .crow select, .crow input { flex: 1 1 auto; min-width: 0; background: var(--zm-card-2); color: var(--zm-text); border: 1px solid var(--zm-border); border-radius: 7px; font-family: var(--zm-font-body); font-weight: 700; font-size: 12.5px; padding: 5px 9px; box-sizing: border-box; }
      .crow select:focus, .crow input:focus { outline: none; border-color: var(--zm-primary); }
      .crow .cu { flex: none; font-family: var(--zm-font-body); font-size: 10px; font-weight: 700; color: var(--zm-text-faint); }
    `,
  ];

  private _num(entityId: string | undefined): number {
    const s = entityId ? this.hass?.states?.[entityId] : undefined;
    const n = s ? Number(s.state) : NaN;
    return Number.isFinite(n) ? n : 0;
  }

  private _optNum(entityId: string | undefined): number | null {
    const s = entityId ? this.hass?.states?.[entityId] : undefined;
    if (!s || s.state == null || s.state === '') return null;
    const n = Number(s.state);
    return Number.isFinite(n) ? n : null;
  }

  private _devices(): DeviceView[] {
    const devices = allDevices(this.config);
    const fleetSoc = this.plan?.battery ? Math.max(0, Math.min(100, this.plan.battery.soc[0] * 100)) : 0;
    return Object.keys(devices).map((id, idx) => {
      const dd = devices[id];
      const e = dd.entities ?? {};
      const soc = e.soc ? this._num(e.soc) : fleetSoc;
      const output = this._num(e.home_output), input = this._num(e.home_input), solar = this._num(e.solar);
      const ts = e.temp ? this.hass?.states?.[e.temp]?.state : undefined;
      const temp: number | null = ts != null && ts !== '' && Number.isFinite(Number(ts)) ? Number(ts) : null;
      const availKwh = this._optNum(e.available_kwh), homeIn = this._optNum(e.home_input_aggr), homeOut = this._optNum(e.home_output_aggr);
      const net = output - input;
      const fh = soc / 100 * 56;
      const connObj = e.connection ? this.hass?.states?.[e.connection] : undefined;
      const connState = normConn(connObj?.state ?? '');
      const connLabel = connObj ? (this.hass?.formatEntityState?.(connObj) ?? connObj.state) : 'Onbekend';
      const connColor = CONN_COLOR[connState] ?? 'var(--zm-text-faint)';
      return {
        id, idx, name: dd.name ?? id, model: dd.model ?? '', bleName: dd.bleName ?? '',
        socPct: soc.toFixed(0), socY: (100 - soc).toFixed(1), socH: soc.toFixed(1),
        battY: (66 - fh).toFixed(1), battH: fh.toFixed(1),
        powerVal: (net >= 0 ? '+' : '') + net, powerColor: net > 0 ? '#1fb89a' : net < 0 ? '#3aa0c9' : 'var(--zm-text-dim)',
        solar, temp, availKwh, homeIn, homeOut,
        flowOn: net !== 0, flowTo: net < 0 ? '0 -12' : '0 12', flowDur: (1.9 - 1.35 * Math.min(1, Math.abs(net) / 900)).toFixed(2) + 's',
        connState, connLabel, connColor,
      };
    });
  }

  private _ent(id?: string): any { return id ? this.hass?.states?.[id] : undefined; }

  private async _saveMaxKw(v: number) {
    try {
      await api.saveSchema('edit', 'system', { max_kw: v });
      this.dispatchEvent(new CustomEvent('zm-saved', { bubbles: true, composed: true }));
    } catch { /* ignore */ }
  }

  private _controlCard() {
    const e = this.config?.system?.entities ?? {};
    const mode = this._ent(e.mode), mp = this._ent(e.manualpower);
    const numOf = (id?: string) => { const s = this._ent(id); const n = s ? Number(s.state) : NaN; return Number.isFinite(n) ? n : null; };
    const tiles = [
      { k: 'SoC', id: e.soc, u: '%', d: 0, c: '#1fb89a' },
      { k: 'Import', id: e.grid_import, u: 'W', d: 0, c: '#e8645a' },
      { k: 'Export', id: e.grid_export, u: 'W', d: 0, c: '#1fb89a' },
      { k: 'Kosten', id: e.cost_today, u: '€', d: 2, c: 'var(--zm-text)' },
      { k: 'Besparing', id: e.savings_today, u: '€', d: 2, c: '#13967d' },
      { k: 'Autarkie', id: e.autarky, u: '%', d: 0, c: 'var(--zm-text)' },
    ];
    return html`
      <div class="dcard ctrl">
        <div style="display:flex;align-items:center;gap:9px">
          <span style="flex:none;width:32px;height:32px;border-radius:10px;background:var(--zm-primary-soft);display:flex;align-items:center;justify-content:center"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="var(--zm-primary)" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v3M12 18v3M3 12h3M18 12h3"></path><circle cx="12" cy="12" r="4"></circle></svg></span>
          <div style="flex:1 1 auto;min-width:0;font-family:var(--zm-font-display);font-weight:600;font-size:14px;color:var(--zm-text)">Distributie</div>
        </div>
        ${mode ? html`<div class="crow"><span class="ck">Modus</span>
          <select @change=${(ev: Event) => this.hass?.callService?.('select', 'select_option', { entity_id: e.mode, option: (ev.target as HTMLSelectElement).value })}>
            ${(mode.attributes?.options ?? []).map((o: string) => html`<option ?selected=${o === mode.state}>${o}</option>`)}
          </select></div>` : nothing}
        ${mp ? html`<div class="crow"><span class="ck">Handmatig</span>
          <input type="number" .value=${String(mp.state ?? '')} @change=${(ev: Event) => this.hass?.callService?.('number', 'set_value', { entity_id: e.manualpower, value: Number((ev.target as HTMLInputElement).value) })} />
          <span class="cu">W</span></div>` : nothing}
        <div class="crow"><span class="ck">Max net</span>
          <input type="number" .value=${String(this.config?.system?.max_kw ?? '')} @change=${(ev: Event) => this._saveMaxKw(Number((ev.target as HTMLInputElement).value))} />
          <span class="cu">kW</span></div>
        <div class="tiles">
          ${tiles.map((t) => { const v = numOf(t.id); return html`<div class="tile"><div class="k">${t.k}</div><div class="v" style="color:${t.c}">${v == null ? '-' : v.toFixed(t.d)}<span style="font-size:8.5px;color:var(--zm-text-dim)">${t.u}</span></div></div>`; })}
        </div>
      </div>
    `;
  }

  private _smartmeters() {
    const sms = allSmartmeters(this.config);
    return Object.keys(sms).map((id) => {
      const sm = sms[id];
      const e = sm.entities ?? {};
      return { id, name: sm.name ?? id, model: sm.model_name ?? 'SmartMeter', total: this._num(e.totalPower), phases: [this._num(e.phase1_power), this._num(e.phase2_power), this._num(e.phase3_power)] };
    });
  }

  private _flow(d: DeviceView) {
    if (!d.flowOn) return nothing;
    const bars = [-12, 0, 12, 24, 36, 48, 60, 72];
    return svg`
      <clipPath id="zmbc${d.idx}"><rect x="8" y=${d.battY} width="28" height=${d.battH} rx="4"></rect></clipPath>
      <g clip-path="url(#zmbc${d.idx})">
        <g>
          ${bars.map((y) => svg`<rect x="8" y=${y} width="28" height="3.6" fill="#ffffff" opacity="0.55"></rect>`)}
          <animateTransform attributeName="transform" attributeType="XML" type="translate" from="0 0" to=${d.flowTo} dur=${d.flowDur} repeatCount="indefinite"></animateTransform>
        </g>
      </g>
    `;
  }

  private _startEdit(id: string, name: string) { this._editId = id; this._draftName = name; this._error = null; }
  private _cancel() { this._editId = null; this._error = null; }
  private async _save(id: string) {
    this._saving = true;
    this._error = null;
    try {
      const res = await api.saveSchema('edit', 'devices', { id, name: this._draftName });
      if (res.ok) { this._editId = null; this.dispatchEvent(new CustomEvent('zm-saved', { bubbles: true, composed: true })); }
      else this._error = (res.errors ?? ['onbekende fout']).join('; ');
    } catch (e: any) {
      this._error = e?.message ?? String(e);
    } finally {
      this._saving = false;
    }
  }

  private _editCard(d: DeviceView) {
    const facts = (this.schema?.[d.id] ?? []).filter((f: Field) => f.readonly);
    const dd = allDevices(this.config)[d.id] ?? {};
    return html`
      <div class="dcard">
        <div style="display:flex;align-items:center;gap:9px">
          <span style="flex:none;width:32px;height:32px;border-radius:10px;background:var(--zm-teal-soft);display:flex;align-items:center;justify-content:center"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="#1fb89a" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="7" width="15" height="10" rx="2.5"></rect><path d="M21 10v4"></path></svg></span>
          <div style="flex:1 1 auto;min-width:0;font-family:var(--zm-font-display);font-weight:600;font-size:14px;color:var(--zm-text)">Apparaat bewerken</div>
        </div>
        <div class="erow"><span class="k">Naam</span><input .value=${this._draftName} @input=${(e: Event) => (this._draftName = (e.target as HTMLInputElement).value)} /></div>
        ${facts.map((f: Field) => html`<div class="erow"><span class="k">${f.id}</span><span class="ro" title=${String(dd[f.id] ?? '')}>${dd[f.id] == null || dd[f.id] === '' ? '-' : String(dd[f.id])}</span></div>`)}
        ${this._error ? html`<div class="err">${this._error}</div>` : nothing}
        <div class="cluster">
          <button class="iconbtn" title="Annuleren" ?disabled=${this._saving} @click=${this._cancel}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"></path></svg></button>
          <button class="iconbtn primary" title="Opslaan" ?disabled=${this._saving} @click=${() => this._save(d.id)}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M5 13l4 4L19 7"></path></svg></button>
        </div>
      </div>
    `;
  }

  private _connIcon(state: string) {
    switch (state) {
      case 'ble': return svg`<path d="m7 7 10 10-5 5V2l5 5L7 17"></path>`;  // bluetooth
      case 'mqtt_cloud': return svg`<path d="M17.5 19a4.5 4.5 0 0 0 0-9 6 6 0 0 0-11.6 1.5A4 4 0 0 0 6.5 19z"></path>`;  // cloud broker
      case 'mqtt_local': return svg`<path d="M3 10.5 12 3l9 7.5"></path><path d="M5 9.5V20h14V9.5"></path>`;  // local/home broker
      case 'http': return svg`<circle cx="12" cy="12" r="9"></circle><path d="M3 12h18"></path><path d="M12 3a15 15 0 0 1 0 18 15 15 0 0 1 0-18"></path>`;  // globe/web
      case 'modbus': return svg`<rect x="3" y="4" width="18" height="7" rx="2"></rect><rect x="3" y="13" width="18" height="7" rx="2"></rect><path d="M7 7.5h.01"></path><path d="M7 16.5h.01"></path>`;  // server/wired bus
      case 'offline': return svg`<path d="M18.36 6.64A9 9 0 1 1 5.64 6.64"></path><path d="M12 2v10"></path>`;  // power/unavailable
      case 'discovered': return svg`<circle cx="11" cy="11" r="7"></circle><path d="m20 20-3.5-3.5"></path>`;  // search/just found
      default: return svg`<circle cx="12" cy="12" r="8" stroke-dasharray="3 3"></circle>`;  // unknown
    }
  }

  private _telemetryCard(d: DeviceView) {
    return html`
      <div class="dcard reveal">
        <svg viewBox="0 0 100 100" preserveAspectRatio="none" style="position:absolute;inset:0;width:100%;height:100%;pointer-events:none">
          <defs><linearGradient id="zmds${d.idx}" x1="0" y1="1" x2="0" y2="0"><stop offset="0" stop-color="#1fb89a" stop-opacity="0.20"></stop><stop offset="1" stop-color="#1fb89a" stop-opacity="0"></stop></linearGradient></defs>
          <rect x="0" y=${d.socY} width="100" height=${d.socH} fill="url(#zmds${d.idx})"></rect>
        </svg>
        <div style="position:relative">
          <div style="display:flex;align-items:center;gap:9px">
            <span title=${d.connLabel} style="flex:none;width:32px;height:32px;border-radius:10px;background:color-mix(in srgb, ${d.connColor} 16%, transparent);color:${d.connColor};display:flex;align-items:center;justify-content:center"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">${this._connIcon(d.connState)}</svg></span>
            <div style="flex:1 1 auto;min-width:0">
              <div style="font-family:var(--zm-font-display);font-weight:600;font-size:14px;color:var(--zm-text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${d.name}</div>
              <div style="font-family:var(--zm-font-body);font-size:11px;color:var(--zm-text-dim);font-weight:600">${d.model}</div>
            </div>
            <button class="pencil" title="Koppelen via Bluetooth" @click=${() => (this._onboardName = d.bleName)}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m7 7 10 10-5 5V2l5 5L7 17"></path></svg></button>
            <button class="pencil" title="Naam bewerken" @click=${() => this._startEdit(d.id, d.name)}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"></path><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"></path></svg></button>
          </div>
          <div style="display:flex;align-items:center;gap:12px;margin-top:9px">
            <div style="flex:1 1 auto;min-width:0;display:grid;grid-template-columns:auto auto;justify-content:start;column-gap:18px;row-gap:2px">
              <div style="font-family:var(--zm-font-body);font-size:10px;font-weight:700;color:var(--zm-text-faint);text-transform:uppercase;letter-spacing:0.04em;white-space:nowrap">Vermogen nu</div>
              <div style="font-family:var(--zm-font-body);font-size:10px;font-weight:700;color:var(--zm-text-faint);text-transform:uppercase;letter-spacing:0.04em;white-space:nowrap">Beschikbaar</div>
              <div style="font-family:var(--zm-font-display);font-weight:600;font-size:25px;line-height:1.05;color:${d.powerColor}">${d.powerVal}<span style="font-size:12px;color:var(--zm-text-dim)">W</span></div>
              <div style="font-family:var(--zm-font-display);font-weight:600;font-size:25px;line-height:1.05;color:var(--zm-text)">${d.availKwh == null ? '-' : html`${d.availKwh.toFixed(1)}<span style="font-size:12px;color:var(--zm-text-dim)">kWh</span>`}</div>
            </div>
            <div style="flex:none;text-align:center;border-left:1px solid var(--zm-border-2);padding-left:12px">
              <svg width="34" height="56" viewBox="0 0 44 74">
                <defs><linearGradient id="zmbat${d.idx}" x1="0" y1="1" x2="0" y2="0"><stop offset="0" stop-color="#138a74"></stop><stop offset="0.55" stop-color="#1fb89a"></stop><stop offset="1" stop-color="#6fe0c8"></stop></linearGradient></defs>
                <rect x="15" y="1" width="14" height="4" rx="1.5" fill="var(--zm-text-faint)"></rect>
                <rect x="4" y="6" width="36" height="64" rx="8" fill="none" stroke="var(--zm-text-faint)" stroke-width="2.4"></rect>
                <rect x="8" y=${d.battY} width="28" height=${d.battH} rx="4" fill="url(#zmbat${d.idx})"></rect>
                ${this._flow(d)}
              </svg>
              <div style="font-family:var(--zm-font-display);font-weight:600;font-size:16px;line-height:1;color:var(--zm-text);margin-top:4px">${d.socPct}<span style="font-size:10px;color:var(--zm-text-dim)">%</span></div>
            </div>
          </div>
          <div class="tiles">
            <div class="tile"><div class="k">Zon</div><div class="v" style="color:#f5a623">${d.solar}<span style="font-size:8.5px;color:var(--zm-text-dim)">W</span></div></div>
            <div class="tile"><div class="k">Temp</div><div class="v" style="color:var(--zm-text)">${d.temp == null ? '-' : html`${d.temp.toFixed(1)}<span style="font-size:8.5px;color:var(--zm-text-dim)">°C</span>`}</div></div>
            <div class="tile"><div class="k">In</div><div class="v" style="color:var(--zm-text)">${d.homeIn == null ? '-' : html`${d.homeIn.toFixed(1)}<span style="font-size:8.5px;color:var(--zm-text-dim)">kWh</span>`}</div></div>
            <div class="tile"><div class="k">Uit</div><div class="v" style="color:var(--zm-text)">${d.homeOut == null ? '-' : html`${d.homeOut.toFixed(1)}<span style="font-size:8.5px;color:var(--zm-text-dim)">kWh</span>`}</div></div>
          </div>
        </div>
      </div>
    `;
  }

  private _meterCard(m: { id: string; name: string; model: string; total: number; phases: number[] }) {
    const col = (v: number) => (v > 0 ? '#1fb89a' : v < 0 ? '#3aa0c9' : 'var(--zm-text-dim)');
    return html`
      <div class="dcard meter">
        <div style="display:flex;align-items:center;gap:9px">
          <span style="flex:none;width:32px;height:32px;border-radius:10px;background:var(--zm-card-2);display:flex;align-items:center;justify-content:center"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="#f5a623" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a9 9 0 0 0-9 9h4a5 5 0 0 1 10 0h4a9 9 0 0 0-9-9z"></path><path d="M12 12l4-2"></path></svg></span>
          <div style="flex:1 1 auto;min-width:0">
            <div style="font-family:var(--zm-font-display);font-weight:600;font-size:14px;color:var(--zm-text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${m.name}</div>
            <div style="font-family:var(--zm-font-body);font-size:11px;color:var(--zm-text-dim);font-weight:600">${m.model}</div>
          </div>
          <span style="flex:none;width:9px;height:9px;border-radius:50%;background:#1fb89a;box-shadow:0 0 0 3px var(--zm-teal-soft)"></span>
        </div>
        <div style="margin-top:10px;font-family:var(--zm-font-body);font-size:10px;font-weight:700;color:var(--zm-text-faint);text-transform:uppercase;letter-spacing:0.04em">Totaal</div>
        <div style="font-family:var(--zm-font-display);font-weight:600;font-size:28px;line-height:1.05;color:${col(m.total)}">${m.total >= 0 ? '+' : ''}${m.total}<span style="font-size:13px;color:var(--zm-text-dim)">W</span></div>
        <div class="tiles">
          ${m.phases.map((p, i) => html`<div class="tile"><div class="k">L${i + 1}</div><div class="v" style="color:${col(p)}">${p}<span style="font-size:8.5px;color:var(--zm-text-dim)">W</span></div></div>`)}
        </div>
      </div>
    `;
  }

  render() {
    const ids = Object.keys(allDevices(this.config));
    return html`
      <div class="sechead"><h3>Zendure fleet</h3><span class="hint">live telemetrie · HA-only</span>${ZendureBle.available()
        ? html`<button class="addbtn" title="Apparaat koppelen via Bluetooth" @click=${() => (this._onboardName = '')}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m7 7 10 10-5 5V2l5 5L7 17"></path></svg>Koppelen</button>`
        : nothing}</div>
      ${!ids.length
        ? html`<div class="empty">Geen Zendure-devices in de config.</div>`
        : html`<div class="grid fleet">${this._devices().map((d) => (this._editId === d.id ? this._editCard(d) : this._telemetryCard(d)))}</div>`}

      <div class="sechead mt"><h3>Controls</h3><span class="hint">sturing & meting</span></div>
      <div class="row">
        ${this._controlCard()}
        ${this._smartmeters().map((m) => this._meterCard(m))}
      </div>

      ${this._onboardName !== null
        ? html`<zm-ble-onboard .config=${this.config} .driverId=${primaryDriver(this.config)?.id ?? ''} .bleName=${this._onboardName} @close=${() => (this._onboardName = null)}></zm-ble-onboard>`
        : nothing}
    `;
  }
}

if (!customElements.get('zm-devices')) customElements.define('zm-devices', ZmDevices);
