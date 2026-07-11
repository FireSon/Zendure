import { LitElement, html, css, nothing } from 'lit';
import { property, state } from 'lit/decorators.js';
import { tokens } from '../tokens.js';
import type { Theme, Schema } from '../types.js';
import { chartColors, fc48Option, fc7dOption } from '../charts.js';
import './zm-chart.js';
import './zm-group.js';
import './zm-solartable.js';

// "Vooruitblik": 48u/7d toggle on one #fcChart, then the Zonnegroepen (zm-solartable: per-meter
// clustered table, each dakvlak row independently editable), then the Forecast-parameters group
// (solar_panels lives in the Zonnegroepen, so it is filtered out of this form).

export class ZmForecast extends LitElement {
  @property({ attribute: false }) d: any = null;
  @property() theme: Theme = 'light';
  @property({ attribute: false }) schema: Schema | null = null;
  @property({ attribute: false }) config: any = null;
  @property({ attribute: false }) hass: any = null;
  @state() private _horizon: '48u' | '7d' = '7d';

  static styles = [
    tokens,
    css`
      :host { display: block; }
      .sechead { display: flex; align-items: center; gap: 10px; margin: 24px 0 14px; flex-wrap: wrap; }
      .sechead.mt { margin-top: 26px; }
      .sechead h3 { margin: 0; font-family: var(--zm-font-display); font-weight: 600; font-size: 17px; color: var(--zm-text); }
      .sechead .hint { font-family: var(--zm-font-body); font-size: 13px; color: var(--zm-text-dim); }
      .sechead .hint.r { margin-left: auto; }
      .seg { margin-left: auto; display: inline-flex; background: var(--zm-card-2); border: 1px solid var(--zm-border); border-radius: 999px; padding: 3px; gap: 2px; }
      .seg button { border: none; border-radius: 999px; padding: 6px 15px; font-family: var(--zm-font-display); font-weight: 500; font-size: 12.5px; background: transparent; color: var(--zm-text-dim); cursor: pointer; }
      .seg button[aria-pressed='true'] { background: var(--zm-primary); color: #fff; }
      .card { background: var(--zm-card); border: 1px solid var(--zm-border); border-radius: 20px; box-shadow: var(--zm-shadow); padding: 18px; animation: zm-rise 0.4s ease both; }
      .note { font-family: var(--zm-font-body); font-size: 12px; color: var(--zm-text-faint); padding: 6px 2px 0; }
      .days { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
      .day { flex: 1 1 0; min-width: 72px; background: var(--zm-card); border: 1px solid var(--zm-border); border-radius: 12px; padding: 10px 8px; text-align: center; box-shadow: var(--zm-shadow); }
      .day .dl { font-family: var(--zm-font-body); font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em; color: var(--zm-text-faint); }
      .day .dv { font-family: var(--zm-font-display); font-weight: 600; font-size: 18px; color: #f5a623; margin-top: 3px; }
      .day .dv span { font-size: 10px; color: var(--zm-text-dim); margin-left: 2px; }
      .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 14px; }
      .empty { padding: 48px; text-align: center; color: var(--zm-text-faint); font-family: var(--zm-font-body); }
    `,
  ];

  render() {
    const d = this.d;
    if (!d) return html`<div class="empty">Geen plan geladen.</div>`;
    const C = chartColors(this.theme);
    const is48 = this._horizon === '48u';
    const fcFields = (this.schema?.['forecast'] ?? []).filter((f) => f.id !== 'solar_panels');
    return html`
      <div class="sechead">
        <h3>Zon &amp; prijs · vooruitblik</h3>
        <span class="hint">zon als staaf · prijs als lijn</span>
        <div class="seg">
          <button aria-pressed=${is48} @click=${() => (this._horizon = '48u')}>48 uur</button>
          <button aria-pressed=${!is48} @click=${() => (this._horizon = '7d')}>7 dagen</button>
        </div>
      </div>
      <div class="card">
        <zm-chart style="height:360px" .option=${is48 ? fc48Option(d, C) : fc7dOption(d, C)}></zm-chart>
        <div class="note">Prijs voorbij day-ahead is geëxtrapoleerd, wacht op prijs-forecast backend.</div>
      </div>

      ${d.solarPerDay?.length
        ? html`<div class="sechead mt"><h3>Verwachte zonopbrengst per dag</h3><span class="hint r">kWh · uit de 7-daagse forecast</span></div>
            <div class="days">${d.solarPerDay.map((x: any) => html`<div class="day"><div class="dl">${x.label}</div><div class="dv">${x.kwh}<span>kWh</span></div></div>`)}</div>`
        : nothing}

      <div class="sechead mt"><h3>Zonnegroepen</h3><span class="hint r">per dakvlak · oriëntatie &amp; bijdrage</span></div>
      <zm-solartable .config=${this.config} .schema=${this.schema} .hass=${this.hass}></zm-solartable>

      ${fcFields.length
        ? html`<div class="sechead mt"><h3>Forecast-parameters</h3><span class="hint r">zon-model &amp; omvormer</span></div>
            <zm-group .entry=${'forecast'} .fields=${fcFields} .config=${this.config} heading="Zon-parameters" tag="pv" iconKey="forecast"></zm-group>`
        : nothing}
    `;
  }
}

if (!customElements.get('zm-forecast')) customElements.define('zm-forecast', ZmForecast);
