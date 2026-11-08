import { LitElement, html, css } from 'lit';
import { property, state } from 'lit/decorators.js';
import { tokens } from '../tokens.js';
import { t } from '../service/i18n.js';
import { PANEL_COLORS, stateNum } from '../derive.js';
import './zm-manage.js';

// Zonnepanelen as a flat list: one row per group (name + live Actueel from power_entity).
// Adding, editing and removing go through the Toevoegen dialog, opened on its solar tab.


export class ZmSolarTable extends LitElement {
  @property({ attribute: false }) config: any = null;
  @property({ attribute: false }) schema: any = null;
  @property({ attribute: false }) add: any[] = [];  // the tile catalog of the add dialog
  @property({ attribute: false }) hass: any = null;

  @state() private _manage = false;  // the add and edit dialog is open

  static styles = [
    tokens,
    css`
      :host { display: block; }
      .card { background: var(--zm-card); border: 1px solid var(--zm-border); border-radius: 20px; box-shadow: var(--zm-shadow); padding: 6px 10px 12px; animation: zm-rise 0.4s ease both; }

      .head { display: flex; align-items: center; padding: 10px 6px 12px; font-family: var(--zm-font-body); font-size: var(--zm-fs-s); font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: var(--zm-text-faint); }
      .h-accent { width: 13px; flex: none; }
      .h-name { flex: 1 1 auto; min-width: 0; padding: 0 8px; }
      .h-actual { width: 140px; flex: none; text-align: right; padding-right: 14px; }

      .row { display: flex; align-items: center; background: var(--tint); border: 1px solid var(--zm-border); border-radius: 16px; margin: 8px 0; height: 60px; overflow: hidden; }
      .accent { width: 5px; flex: none; align-self: stretch; background: var(--acc); border-radius: 5px; margin: 8px; }
      .name { flex: 1 1 auto; min-width: 0; padding: 0 8px; font-family: var(--zm-font-display); font-weight: 600; font-size: var(--zm-fs-m); color: var(--zm-text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .actual { width: 140px; flex: none; text-align: right; padding-right: 14px; font-family: var(--zm-font-display); font-weight: 600; font-size: var(--zm-fs-l); color: var(--zm-text); }
      .actual small { font-size: var(--zm-fs-s); font-weight: 600; color: var(--zm-text-dim); }

      .empty { padding: 28px; text-align: center; color: var(--zm-text-faint); font-family: var(--zm-font-body); }
      .addrow { display: flex; justify-content: flex-end; padding: 10px 2px 4px; }
      .addbtn { display: inline-flex; align-items: center; gap: 6px; border: none; background: var(--zm-primary); color: #fff; border-radius: 999px; padding: 7px 13px; font-family: var(--zm-font-body); font-size: var(--zm-fs-m); font-weight: 700; cursor: pointer; box-shadow: 0 2px 8px var(--zm-primary-soft); }
      .addbtn svg { width: 14px; height: 14px; }
    `,
  ];

  private _panels(): any[] { return Array.isArray(this.config?.solar_panels) ? this.config.solar_panels : []; }

  private _row(p: any, i: number) {
    const style = `--acc:${PANEL_COLORS[i % PANEL_COLORS.length]};--tint:color-mix(in srgb, ${PANEL_COLORS[i % PANEL_COLORS.length]} 7%, transparent)`;
    const actualW = stateNum(this.hass, p.power_entity);
    return html`<div class="row" style=${style}>
      <div class="accent"></div>
      <div class="name">${p.name ?? p.id}</div>
      <div class="actual">${actualW == null ? '–' : Math.round(actualW)}<small> W</small></div>
    </div>`;
  }

  private _addBtn() {
    return html`<div class="addrow"><button class="addbtn" @click=${() => (this._manage = true)}>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M12 5v14M5 12h14"></path></svg>${t('ui.manage.add', 'Toevoegen')}
    </button></div>`;
  }

  render() {
    const panels = this._panels();
    const manage = this._manage ? html`<zm-manage tab="solar" .config=${this.config} .schema=${this.schema} .add=${this.add} .hass=${this.hass} @close=${() => (this._manage = false)}></zm-manage>` : '';
    if (!panels.length) return html`<div class="card"><div class="empty">${t('ui.solar.empty', 'geen zonnepanelen in de config')}</div>${this._addBtn()}</div>${manage}`;
    return html`<div class="card">
      <div class="head">
        <span class="h-accent"></span><span class="h-name">${t('ui.solar.name', 'Naam')}</span>
        <span class="h-actual">${t('ui.solar.actual', 'Actueel')}</span>
      </div>
      ${panels.map((p, i) => this._row(p, i))}
      ${this._addBtn()}
    </div>${manage}`;
  }
}

if (!customElements.get('zm-solartable')) customElements.define('zm-solartable', ZmSolarTable);
