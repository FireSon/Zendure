import { LitElement, html, css, PropertyValues } from 'lit';
import { property } from 'lit/decorators.js';
import { echarts } from '../echarts.js';

// Thin ECharts host: give it a height via inline style and an `option`; it inits,
// re-applies on option change and resizes itself via ResizeObserver.
export class ZmChart extends LitElement {
  @property({ attribute: false }) option: any = null;

  private _chart: any = null;
  private _ro: ResizeObserver | null = null;

  static styles = css`
    :host { display: block; width: 100%; }
    .c { width: 100%; height: 100%; }
  `;

  firstUpdated() {
    const el = this.renderRoot.querySelector('.c') as HTMLElement;
    this._chart = echarts.init(el, undefined, { renderer: 'canvas' });
    this._ro = new ResizeObserver(() => this._fit());
    this._ro.observe(el);
    this._apply();
    this._fit();
  }

  updated(changed: PropertyValues) {
    if (changed.has('option')) this._apply();
  }

  private _apply() {
    if (this._chart && this.option) this._chart.setOption(this.option, { notMerge: true });
  }

  private _fit() {
    const run = () => { try { this._chart?.resize(); } catch { /* noop */ } };
    requestAnimationFrame(run);
    setTimeout(run, 60);
    setTimeout(run, 260);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    this._ro?.disconnect();
    this._ro = null;
    try { this._chart?.dispose(); } catch { /* noop */ }
    this._chart = null;
  }

  render() {
    return html`<div class="c"></div>`;
  }
}

if (!customElements.get('zm-chart')) customElements.define('zm-chart', ZmChart);
