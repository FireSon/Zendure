import { LitElement, html, css } from 'lit';
import { property } from 'lit/decorators.js';
import { tokens } from '../tokens.js';
import type { Theme } from '../types.js';
import { chartColors, cijLanesOption } from '../charts.js';
import { t } from '../service/i18n.js';
import './zm-chart.js';

// "Cijfers": 30-day history from get_history as one tall ECharts (#cijLanes) of 7 vertically
// stacked grids sharing the day axis; the top stacked bar carries the part-of-whole mix.
export class ZmCijfers extends LitElement {
  @property({ attribute: false }) d: any = null;
  @property() theme: Theme = 'light';

  static styles = [
    tokens,
    css`
      :host { display: block; }
      .sechead { display: flex; align-items: center; gap: 10px; margin: 24px 0 14px; }
      .sechead h3 { margin: 0; font-family: var(--zm-font-display); font-weight: 600; font-size: var(--zm-fs-l); color: var(--zm-text); }
      .sechead .hint { font-family: var(--zm-font-body); font-size: var(--zm-fs-m); color: var(--zm-text-dim); margin-left: auto; }
      .card { background: var(--zm-card); border: 1px solid var(--zm-border); border-radius: 20px; box-shadow: var(--zm-shadow); padding: 16px 18px 8px; animation: zm-rise 0.4s ease both; }
      .empty { padding: 48px; text-align: center; color: var(--zm-text-faint); font-family: var(--zm-font-body); }
    `,
  ];

  render() {
    const d = this.d;
    if (!d) return html`<div class="empty">${t('ui.common.no_plan', 'Geen plan geladen.')}</div>`;
    const C = chartColors(this.theme);
    return html`
      <div class="sechead"><h3>${t('ui.trend.title', '30-daagse historie')}</h3><span class="hint">${t('ui.trend.hint', 'elke rij op dezelfde datum-as · hover voor één dag')}</span></div>
      <div class="card"><zm-chart style="height:604px" .option=${cijLanesOption(d, C)}></zm-chart></div>
    `;
  }
}

if (!customElements.get('zm-cijfers')) customElements.define('zm-cijfers', ZmCijfers);
