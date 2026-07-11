import { LitElement, html, css } from 'lit';
import { property, state } from 'lit/decorators.js';
import { tokens } from '../tokens.js';
import type { Theme, Schema } from '../types.js';
import { chartColors, consSwimOption } from '../charts.js';
import { consumerCards } from '../derive.js';
import { api } from '../service/api.js';
import './zm-chart.js';
import './zm-group.js';

// Concrete add-types: each preset maps to a consumer class plus the derived subtype (kind/source)
// and an id prefix for the generated unique id.
const CONSUMER_TYPES: Record<string, { label: string; type: string; base: string; kind?: string; source?: string }> = {
  ev: { label: 'EV-lader', type: 'ElectricVehicle', base: 'ev' },
  washingmachine: { label: 'Wasmachine', type: 'Appliance', base: 'wasmachine', kind: 'washingmachine' },
  dryer: { label: 'Droger', type: 'Appliance', base: 'droger', kind: 'dryer' },
  dishwasher: { label: 'Vaatwasser', type: 'Appliance', base: 'vaatwasser', kind: 'dishwasher' },
  appliance: { label: 'Apparaat (overig)', type: 'Appliance', base: 'apparaat', kind: 'custom' },
  heatpump: { label: 'Warmtepomp', type: 'Heating', base: 'warmtepomp', kind: 'house', source: 'hp' },
  boiler_hp: { label: 'Boiler (warmtepomp)', type: 'Heating', base: 'boiler', kind: 'boiler', source: 'hp' },
  boiler_electric: { label: 'Boiler (elektrisch)', type: 'Heating', base: 'boiler', kind: 'boiler', source: 'electric' },
  pool: { label: 'Zwembad (warmtepomp)', type: 'Heating', base: 'zwembad', kind: 'pool', source: 'hp' },
};

// "Verbruikers": the per-device swimlane, then one card per configured consumer. Each card
// is a zm-group whose read-only body is the control layout (status + Vermogen/Vandaag/
// Volgende + Start/Pauze/Stop/Skip) and whose edit layout is the consumer's config schema.
// One tile, two layouts. Lifted from the v2 design + the edit-toggle pattern.
export class ZmConsumers extends LitElement {
  @property({ attribute: false }) d: any = null;
  @property() theme: Theme = 'light';
  @property({ attribute: false }) consAction: Record<string, string> = {};
  @property({ attribute: false }) schema: Schema | null = null;
  @property({ attribute: false }) config: any = null;
  @property({ attribute: false }) hass: any = null;

  @state() private _addType = Object.keys(CONSUMER_TYPES)[0];
  @state() private _busy = false;

  static styles = [
    tokens,
    css`
      :host { display: block; }
      .sechead { display: flex; align-items: center; gap: 10px; margin: 6px 0 14px; }
      .sechead.mt { margin: 26px 0 14px; }
      .sechead h3 { margin: 0; font-family: var(--zm-font-display); font-weight: 600; font-size: 17px; color: var(--zm-text); }
      .sechead .hint { font-family: var(--zm-font-body); font-size: 13px; color: var(--zm-text-dim); margin-left: auto; }
      .add { display: flex; align-items: center; gap: 8px; margin-left: auto; }
      .add select { background: var(--zm-card-2); color: var(--zm-text); border: 1px solid var(--zm-border); border-radius: 8px; font-family: var(--zm-font-body); font-weight: 700; font-size: 12px; padding: 6px 9px; }
      .add button { display: inline-flex; align-items: center; gap: 6px; border: none; background: var(--zm-primary); color: #fff; border-radius: 999px; padding: 7px 13px; font-family: var(--zm-font-body); font-size: 12px; font-weight: 700; cursor: pointer; box-shadow: 0 2px 8px var(--zm-primary-soft); }
      .add button:disabled { opacity: 0.5; cursor: default; }
      .add svg { width: 14px; height: 14px; }
      .card { background: var(--zm-card); border: 1px solid var(--zm-border); border-radius: 20px; box-shadow: var(--zm-shadow); padding: 18px; animation: zm-rise 0.4s ease both; }
      .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 14px; }
      .empty { padding: 48px; text-align: center; color: var(--zm-text-faint); font-family: var(--zm-font-body); }
    `,
  ];

  private _actIcon(a: any) {
    if (a.isStart) return html`<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M7 5l12 7-12 7z"></path></svg>`;
    if (a.isPause) return html`<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1"></rect><rect x="14" y="5" width="4" height="14" rx="1"></rect></svg>`;
    if (a.isStop) return html`<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2.5"></rect></svg>`;
    return html`<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M6 5l9 7-9 7z"></path><rect x="16" y="5" width="2.6" height="14" rx="1"></rect></svg>`;
  }

  private _act(id: string, action: string) {
    this.dispatchEvent(new CustomEvent('cons-action', { detail: { id, action }, bubbles: true, composed: true }));
  }

  private _addConsumer = async () => {
    const preset = CONSUMER_TYPES[this._addType];
    if (!preset) return;
    const ids = new Set((this.config?.consumers ?? []).map((c: any) => c.id));
    let id = preset.base, n = 1;
    while (ids.has(id)) id = `${preset.base}${++n}`;
    const payload: Record<string, unknown> = { type: preset.type, id };
    if (preset.kind) payload.kind = preset.kind;
    if (preset.source) payload.source = preset.source;
    this._busy = true;
    try {
      await api.saveSchema('add', 'consumers', payload);
      this.dispatchEvent(new CustomEvent('zm-saved', { bubbles: true, composed: true }));
    } catch { /* ignore */ }
    finally { this._busy = false; }
  };

  // Read-only body for a consumer card (rendered inside zm-group's shadow, so it uses inline
  // styles + inherited CSS vars rather than this component's classes).
  private _controlView(c: any) {
    return html`
      <div style="font-family:var(--zm-font-body);font-size:11.5px;font-weight:700;color:${c.statusColor};margin-bottom:8px">${c.statusLabel}</div>
      <div style="display:flex;gap:8px">
        <div style="flex:1.4">
          <div style="font-family:var(--zm-font-body);font-size:10px;font-weight:700;color:var(--zm-text-faint);text-transform:uppercase;letter-spacing:0.04em">Vermogen nu</div>
          <div style="font-family:var(--zm-font-display);font-weight:600;font-size:27px;line-height:1.05;color:${c.iconColor};margin-top:2px">${c.powerW}<span style="font-size:12px;color:var(--zm-text-dim)">W</span></div>
        </div>
        <div style="flex:1;background:var(--zm-card-2);border-radius:10px;padding:8px 10px"><div style="font-family:var(--zm-font-body);font-size:10px;font-weight:700;color:var(--zm-text-faint)">Vandaag</div><div style="font-family:var(--zm-font-display);font-weight:600;font-size:15px;color:var(--zm-text);margin-top:1px">${c.energyToday}<span style="font-size:9px;color:var(--zm-text-dim)"> kWh</span></div></div>
        <div style="flex:1;background:var(--zm-card-2);border-radius:10px;padding:8px 10px"><div style="font-family:var(--zm-font-body);font-size:10px;font-weight:700;color:var(--zm-text-faint)">Volgende</div><div style="font-family:var(--zm-font-display);font-weight:600;font-size:15px;color:var(--zm-text);margin-top:1px">${c.nextLabel}</div></div>
      </div>
      <div style="margin-top:14px;border-top:1px solid var(--zm-border-2);padding-top:12px">
        <div style="font-family:var(--zm-font-body);font-size:10px;font-weight:700;color:var(--zm-text-faint);text-transform:uppercase;letter-spacing:0.04em">Acties</div>
        <div style="display:flex;gap:6px;margin-top:8px">
          ${c.actions.map((a: any) => html`<button @click=${() => this._act(c.id, a.id)} style="flex:1 1 0;min-width:0;display:flex;align-items:center;justify-content:center;gap:5px;border:1.5px solid ${a.bd};background:${a.bg};color:${a.fg};border-radius:10px;padding:7px 8px;cursor:pointer;font-family:var(--zm-font-display);font-weight:500;font-size:12.5px"><span style="display:flex">${this._actIcon(a)}</span>${a.label}</button>`)}
        </div>
      </div>
    `;
  }

  render() {
    const d = this.d;
    if (!d) return html`<div class="empty">Geen plan geladen.</div>`;
    const cards = consumerCards(d, this.consAction, this.config?.consumers ?? []);
    return html`
      <div class="sechead"><h3>Inzet per verbruiker · vandaag</h3><span class="hint">swimlane per apparaat</span></div>
      <div class="card"><zm-chart style="height:138px" .option=${consSwimOption(d, chartColors(this.theme))}></zm-chart></div>

      <div class="sechead mt"><h3>Verbruikers</h3>
        <div class="add">
          <select @change=${(e: Event) => (this._addType = (e.target as HTMLSelectElement).value)}>
            ${Object.entries(CONSUMER_TYPES).map(([k, v]) => html`<option value=${k} ?selected=${k === this._addType}>${v.label}</option>`)}
          </select>
          <button ?disabled=${this._busy} @click=${this._addConsumer}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M12 5v14M5 12h14"></path></svg>Toevoegen
          </button>
        </div>
      </div>
      <div class="grid">
        ${cards.map((c: any) => html`<zm-group
          .entry=${c.id}
          .fields=${this.schema?.[c.id] ?? []}
          .config=${this.config}
          heading=${c.label}
          tag=${c.type}
          iconKey=${c.kind}
          collection="consumers"
          .singleColumn=${true}
          .hass=${this.hass}
          .controlView=${this._controlView(c)}
        ></zm-group>`)}
      </div>
    `;
  }
}

if (!customElements.get('zm-consumers')) customElements.define('zm-consumers', ZmConsumers);
