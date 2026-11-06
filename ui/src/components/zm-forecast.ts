import { LitElement, html, css, nothing, PropertyValues } from 'lit';
import { property, state } from 'lit/decorators.js';
import { tokens } from '../tokens.js';
import type { Theme, Schema } from '../types.js';
import { chartColors, fc48Option, fc7dOption } from '../charts.js';
import { t } from '../service/i18n.js';
import './zm-chart.js';
import './zm-solartable.js';

// "Vooruitblik": 48u/7d toggle on one #fcChart, then the Zonnegroepen (zm-solartable: per-meter
// clustered table, each dakvlak row independently editable).

export class ZmForecast extends LitElement {
  @property({ attribute: false }) d: any = null;
  @property() theme: Theme = 'light';
  @property({ attribute: false }) schema: Schema | null = null;
  @property({ attribute: false }) config: any = null;
  @property({ attribute: false }) hass: any = null;
  @state() private _horizon: '48u' | '7d' = '7d';
  private _option: any = null;  // the chart option, rebuilt only when the plan, the theme or the horizon changes, not on every hass update

  willUpdate(changed: PropertyValues) {
    if (changed.has('d') || changed.has('theme') || changed.has('_horizon')) this._option = this.d ? (this._horizon === '48u' ? fc48Option : fc7dOption)(this.d, chartColors(this.theme)) : null;
  }

  static styles = [
    tokens,
    css`
      :host { display: block; }
      .sechead { display: flex; align-items: center; gap: 10px; margin: 24px 0 14px; flex-wrap: wrap; }
      .sechead.mt { margin-top: 26px; }
      .sechead h3 { margin: 0; font-family: var(--zm-font-display); font-weight: 600; font-size: var(--zm-fs-l); color: var(--zm-text); }
      .sechead .hint { font-family: var(--zm-font-body); font-size: var(--zm-fs-m); color: var(--zm-text-dim); }
      .sechead .hint.r { margin-left: auto; }
      .seg { margin-left: auto; display: inline-flex; background: var(--zm-card-2); border: 1px solid var(--zm-border); border-radius: 999px; padding: 3px; gap: 2px; }
      .seg button { border: none; border-radius: 999px; padding: 6px 15px; font-family: var(--zm-font-display); font-weight: 500; font-size: var(--zm-fs-m); background: transparent; color: var(--zm-text-dim); cursor: pointer; }
      .seg button[aria-pressed='true'] { background: var(--zm-primary); color: #fff; }
      .card { background: var(--zm-card); border: 1px solid var(--zm-border); border-radius: 20px; box-shadow: var(--zm-shadow); padding: 18px; animation: zm-rise 0.4s ease both; }
      .note { font-family: var(--zm-font-body); font-size: var(--zm-fs-m); color: var(--zm-text-faint); padding: 6px 2px 0; }
      .days { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
      .day { flex: 1 1 0; min-width: 72px; background: var(--zm-card); border: 1px solid var(--zm-border); border-radius: 12px; padding: 10px 8px; text-align: center; box-shadow: var(--zm-shadow); }
      .day .dl { font-family: var(--zm-font-body); font-size: var(--zm-fs-s); font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em; color: var(--zm-text-faint); }
      .day .dv { font-family: var(--zm-font-display); font-weight: 600; font-size: var(--zm-fs-l); color: #f5a623; margin-top: 3px; }
      .day .dv span { font-size: var(--zm-fs-s); color: var(--zm-text-dim); margin-left: 2px; }
      .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 14px; }
      .empty { padding: 48px; text-align: center; color: var(--zm-text-faint); font-family: var(--zm-font-body); }
    `,
  ];

  render() {
    const d = this.d;
    // the solar groups need no plan, so they also show with the solver off
    const groups = html`<div class="sechead mt"><h3>${t('ui.forecast.groups', 'Zonnegroepen')}</h3><span class="hint r">${t('ui.forecast.groups_hint', 'per dakvlak · oriëntatie & bijdrage')}</span></div>
      <zm-solartable .config=${this.config} .schema=${this.schema} .hass=${this.hass}></zm-solartable>`;
    if (!d) return html`<div class="empty">${t('ui.common.no_plan', 'Geen plan geladen.')}</div>${groups}`;
    const is48 = this._horizon === '48u';
    return html`
      ${d.solarPerDay?.length
        ? html`<div class="sechead"><h3>${t('ui.forecast.per_day', 'Verwachte zonopbrengst per dag')}</h3><span class="hint r">${t('ui.forecast.per_day_hint', 'kWh · uit de 7-daagse forecast')}</span></div>
            <div class="days">${d.solarPerDay.map((x: any) => html`<div class="day"><div class="dl">${x.label}</div><div class="dv">${x.kwh}<span>kWh</span></div></div>`)}</div>`
        : nothing}

      <div class="sechead mt">
        <h3>${t('ui.forecast.title', 'Zon & prijs · vooruitblik')}</h3>
        <span class="hint">${t('ui.forecast.hint', 'zon als staaf · prijs als lijn')}</span>
        <div class="seg">
          <button aria-pressed=${is48} @click=${() => (this._horizon = '48u')}>${t('ui.forecast.h48', '48 uur')}</button>
          <button aria-pressed=${!is48} @click=${() => (this._horizon = '7d')}>${t('ui.forecast.d7', '7 dagen')}</button>
        </div>
      </div>
      <div class="card">
        <zm-chart style="height:360px" .option=${this._option}></zm-chart>
        <div class="note">${t('ui.forecast.note', 'Prijs voorbij day-ahead is een verwachting uit het prijsmodel.')}</div>
      </div>

      ${groups}
    `;
  }
}

if (!customElements.get('zm-forecast')) customElements.define('zm-forecast', ZmForecast);
