import { LitElement, html, css } from 'lit';
import { property, state } from 'lit/decorators.js';
import { tokens } from '../tokens.js';
import type { Theme } from '../types.js';
import { chartColors, flowOption } from '../charts.js';
import { planTable } from '../derive.js';
import './zm-chart.js';

// Tabbed card: power-flow grafiek (with the device swimlane) OR the plan-per-kwartier
// table, switched by the tabstrip at the bottom. Lifted from the v2 design.
export class ZmDashboard extends LitElement {
  @property({ attribute: false }) d: any = null;
  @property() theme: Theme = 'light';
  @state() private _card: 'chart' | 'table' = 'chart';

  static styles = [
    tokens,
    css`
      :host { display: block; }
      .card { background: var(--zm-card); border: 1px solid var(--zm-border); border-radius: 20px; box-shadow: var(--zm-shadow); overflow: hidden; animation: zm-rise 0.4s cubic-bezier(0.2,0.7,0.3,1) both; }
      .flow { width: 100%; height: 402px; }
      .tablewrap { max-height: 466px; overflow: auto; }
      table { border-collapse: collapse; width: 100%; font-family: var(--zm-font-body); font-size: 12.5px; }
      thead th { position: sticky; top: 0; background: var(--zm-card-2); color: var(--zm-text-dim); font-weight: 700; padding: 11px 12px; text-align: right; white-space: nowrap; border-bottom: 1px solid var(--zm-border); }
      tbody td { padding: 8px 12px; text-align: right; color: var(--zm-text); white-space: nowrap; border-bottom: 1px solid var(--zm-border-2); font-variant-numeric: tabular-nums; }
      .strip { display: flex; gap: 6px; border-top: 1px solid var(--zm-border); padding: 9px 14px; background: var(--zm-card-2); }
      .strip button { border: none; border-radius: 999px; padding: 7px 16px; cursor: pointer; font-family: var(--zm-font-display); font-weight: 500; font-size: 13px; background: transparent; color: var(--zm-text-dim); }
      .strip button[aria-pressed='true'] { background: var(--zm-primary); color: #fff; box-shadow: 0 2px 8px var(--zm-primary-soft); }
      .empty { padding: 48px; text-align: center; color: var(--zm-text-faint); font-family: var(--zm-font-body); }
    `,
  ];

  render() {
    const d = this.d;
    if (!d) return html`<div class="empty">Geen plan geladen.</div>`;
    const t = planTable(d);
    return html`
      <div class="card">
        ${this._card === 'chart'
          ? html`<div style="padding:18px 18px 4px"><zm-chart class="flow" .option=${flowOption(d, chartColors(this.theme))}></zm-chart></div>`
          : html`<div class="tablewrap"><table><thead><tr>${t.headers.map((h) => html`<th>${h}</th>`)}</tr></thead><tbody>${t.rows.map((r) => html`<tr>${r.map((c) => html`<td>${c}</td>`)}</tr>`)}</tbody></table></div>`}
        <div class="strip">
          <button aria-pressed=${this._card === 'chart'} @click=${() => (this._card = 'chart')}>Grafiek</button>
          <button aria-pressed=${this._card === 'table'} @click=${() => (this._card = 'table')}>Plan per kwartier</button>
        </div>
      </div>
    `;
  }
}

if (!customElements.get('zm-dashboard')) customElements.define('zm-dashboard', ZmDashboard);
