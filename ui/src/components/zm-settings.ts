import { LitElement, html, css, nothing } from 'lit';
import { property, state } from 'lit/decorators.js';
import { tokens } from '../tokens.js';
import type { Schema } from '../types.js';
import { api } from '../service/api.js';
import { driverList, DRIVER_TYPES } from '../service/drivers.js';
import './zm-group.js';

// Per-group metadata for the headline groups. Each card is an independently editable
// zm-group (readonly default + pencil -> edit). Used by the Settings tab and reused for
// the forecast group on its own tab.
export const GROUP_META: Record<string, { title: string; tag: string; icon: string }> = {
  system: { title: 'Distributie', tag: 'sturing', icon: 'system' },
  price: { title: 'Prijs', tag: 'dynamisch', icon: 'price' },
  forecast: { title: 'Forecast & zon', tag: 'pv', icon: 'forecast' },
  zendure: { title: 'Zendure fleet', tag: 'fleet', icon: 'zendure' },
};

export class ZmSettings extends LitElement {
  @property({ attribute: false }) schema: Schema | null = null;
  @property({ attribute: false }) config: any = null;
  @property({ attribute: false }) hass: any = null;
  @property({ attribute: false }) only: string[] = ['system', 'price'];

  @state() private _addType = Object.keys(DRIVER_TYPES)[0] ?? '';
  @state() private _busy = false;

  static styles = [
    tokens,
    css`
      :host { display: block; }
      .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(450px, 1fr)); gap: 14px; }
      .empty { padding: 48px; text-align: center; color: var(--zm-text-faint); font-family: var(--zm-font-body); }
      .add { display: flex; align-items: center; gap: 10px; margin: 18px 2px 0; }
      .add label { font-family: var(--zm-font-body); font-size: 12px; font-weight: 700; color: var(--zm-text-dim); }
      .add select { background: var(--zm-card-2); color: var(--zm-text); border: 1px solid var(--zm-border); border-radius: 8px; font-family: var(--zm-font-body); font-weight: 700; font-size: 13px; padding: 7px 10px; }
      .add button { display: inline-flex; align-items: center; gap: 6px; border: none; background: var(--zm-primary); color: #fff; border-radius: 999px; padding: 8px 14px; font-family: var(--zm-font-body); font-size: 12px; font-weight: 700; cursor: pointer; box-shadow: 0 2px 8px var(--zm-primary-soft); }
      .add button:disabled { opacity: 0.5; cursor: default; }
      .add svg { width: 14px; height: 14px; }
    `,
  ];

  private _addDriver = async () => {
    if (!this._addType) return;
    const ids = new Set(driverList(this.config).map((d: any) => d.id));
    const base = this._addType === 'ZendureDriver' ? 'zendure' : this._addType.toLowerCase();
    let id = base, n = 1;
    while (ids.has(id)) id = `${base}${++n}`;
    this._busy = true;
    try {
      await api.saveSchema('add', 'drivers', { type: this._addType, id });
      this.dispatchEvent(new CustomEvent('zm-saved', { bubbles: true, composed: true }));
    } catch { /* ignore */ }
    finally { this._busy = false; }
  };

  render() {
    if (!this.schema) return html`<div class="empty">Schema laden…</div>`;
    const singletons = this.only.filter((k) => (this.schema![k]?.length ?? 0) > 0);
    const drivers = driverList(this.config);
    const types = Object.entries(DRIVER_TYPES);
    return html`
      <div class="grid">
        ${singletons.map((k) => {
          const m = GROUP_META[k] ?? { title: k, tag: '', icon: '' };
          return html`<zm-group .entry=${k} .fields=${this.schema![k]} .config=${this.config} .hass=${this.hass} heading=${m.title} tag=${m.tag} iconKey=${m.icon}></zm-group>`;
        })}
        ${drivers.map((d: any) => {
          const m = GROUP_META[d.id] ?? { title: DRIVER_TYPES[d.type] ?? d.id, tag: 'driver', icon: 'zendure' };
          return html`<zm-group .entry=${d.id} .fields=${this.schema![d.id] ?? []} .config=${this.config} .hass=${this.hass} heading=${m.title} tag=${m.tag} iconKey=${m.icon} collection="drivers"></zm-group>`;
        })}
      </div>
      ${types.length
        ? html`<div class="add">
            <label>Driver toevoegen</label>
            ${types.length > 1
              ? html`<select @change=${(e: Event) => (this._addType = (e.target as HTMLSelectElement).value)}>
                  ${types.map(([k, v]) => html`<option value=${k} ?selected=${k === this._addType}>${v}</option>`)}
                </select>`
              : nothing}
            <button ?disabled=${this._busy} @click=${this._addDriver}>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M12 5v14M5 12h14"></path></svg>
              ${types.length > 1 ? 'Toevoegen' : `${types[0][1]} toevoegen`}
            </button>
          </div>`
        : nothing}
    `;
  }
}

if (!customElements.get('zm-settings')) customElements.define('zm-settings', ZmSettings);
