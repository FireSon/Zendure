import { LitElement, html, css, nothing, PropertyValues } from 'lit';
import { state, property } from 'lit/decorators.js';
import type { Plan, Schema, TabId, Theme } from './types.js';
import { tokens } from './tokens.js';
import { api } from './service/api.js';
import { STATIC_URL } from './service/ha.js';
import { sliceLive, refetchLive } from './service/loader.js';
import { chartData, headStats, popupView } from './derive.js';
import './components/zm-dashboard.js';
import './components/zm-cijfers.js';
import './components/zm-consumers.js';
import './components/zm-forecast.js';
import './components/zm-devices.js';
import './components/zm-settings.js';

const HEAD: Record<TabId, { title: string; subtitle: string }> = {
  dashboard: { title: 'Energie-optimalisatie', subtitle: 'Slim plannen rond zon, dynamische prijzen en je Zendure-accu.' },
  cijfers: { title: 'Trend', subtitle: 'Prestaties van vandaag en de afgelopen 30 dagen.' },
  consumers: { title: 'Verbruikers', subtitle: 'Inzet per apparaat en directe bediening.' },
  forecast: { title: 'Vooruitblik', subtitle: 'Zon en prijs voor de komende uren en dagen.' },
  devices: { title: 'Apparaten', subtitle: 'Je Zendure-fleet, live.' },
  settings: { title: 'Instellingen', subtitle: 'Systeem, prijs, forecast en fleet configureren.' },
};

export class ZmApp extends LitElement {
  @property({ type: Boolean }) embedded = false;  // inside the HA panel: hide our own logo (HA titles it)
  @property({ attribute: false }) theme: Theme | null = null;  // driven by HA in the panel; null = standalone toggle
  @property({ attribute: false }) hass: any = null;  // HA object in the panel; lets device telemetry read hass.states
  @state() private _tab: TabId = 'dashboard';
  @state() private _theme: Theme = 'light';
  @state() private _plan: Plan | null = null;
  @state() private _config: any = null;
  @state() private _schema: Schema | null = null;
  @state() private _status: 'loading' | 'ok' | 'error' = 'loading';
  @state() private _error = '';
  @state() private _optimizing = true;
  @state() private _consAction: Record<string, string> = {};
  @state() private _popup: { kind: string; slot: number; id?: string } | null = null;
  private _d: any = null;
  private _unsubPlan: (() => void) | null = null;

  connectedCallback(): void {
    super.connectedCallback();
    const t = document.documentElement.getAttribute('data-zm-theme');
    this._theme = t === 'dark' ? 'dark' : 'light';
    void this._load();
    void this._subscribe();
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    this._unsubPlan?.();
    this._unsubPlan = null;
  }

  private _window(tab: TabId): [number, number] {
    return tab === 'forecast' ? [12, 99999] : [0, 108];   // dashboard/device/...: 3u historie + 24u; forecast: nu -> 7d
  }

  private _reslice(): void {
    if (!this._config) return;
    const [a, b] = this._window(this._tab);
    this._plan = sliceLive(a, b, this._config);
  }

  private async _subscribe(): Promise<void> {
    const p = api.subscribePlan(() => { void this._onPush(); });
    if (p) this._unsubPlan = await p;
  }

  private async _onPush(): Promise<void> {
    try { await refetchLive(); this._reslice(); } catch { /* keep last plan */ }
  }

  willUpdate(changed: PropertyValues) {
    if (changed.has('theme') && this.theme) this._theme = this.theme;  // follow HA's dark mode
    if (changed.has('_tab')) this._reslice();  // re-slice the cached full plan for this tab's window
    if (changed.has('_plan') || changed.has('_tab')) this._d = this._plan ? chartData(this._plan) : null;
  }

  private async _load() {
    this._status = 'loading';
    this._error = '';
    try {
      const bundle = await api.getSchema();
      this._schema = bundle.schema;
      this._config = bundle.config;
      await refetchLive();
      this._reslice();
      this._status = 'ok';
    } catch (e: any) {
      this._error = e?.message ?? String(e);
      this._status = 'error';
    }
  }

  private _onChartClick(e: CustomEvent) {
    const p = e.detail;
    if (p.seriesType !== 'custom' || !this._d) return;
    const slot = p.value[0], row = p.value[1];
    if (row === 0) this._popup = { kind: 'bat', slot };
    else { const cc = this._d.consumers[row - 1]; if (cc) this._popup = { kind: 'cons', id: cc.id, slot }; }
  }

  private _onConsAction(e: CustomEvent) {
    const { id, action } = e.detail;
    this._consAction = { ...this._consAction, [id]: action };
  }

  private _toggleTheme() {
    this._theme = this._theme === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-zm-theme', this._theme);
  }

  static styles = [
    tokens,
    css`
      :host { display: block; min-height: 100vh; }
      .wrap { padding: 0 32px 80px; }
      header.top { display: flex; align-items: center; gap: 20px; padding: 0 0 6px; flex-wrap: wrap; }
      .logo { position: relative; display: inline-flex; align-items: center; justify-content: center; }
      .logo img { height: 128px; width: auto; display: block; }
      .logo .wordmark { position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%); font-family: var(--zm-font-display); font-weight: 700; font-size: 24px; letter-spacing: 0.01em; text-transform: uppercase; white-space: nowrap; }
      .zm-nav { margin: 0 auto; display: flex; align-items: center; gap: 4px; background: var(--zm-card); border: 1px solid var(--zm-border); border-radius: 999px; padding: 5px; box-shadow: var(--zm-shadow-sm); }
      .zm-tab { display: flex; align-items: center; justify-content: center; gap: 7px; border: none; border-radius: 999px; padding: 9px 13px; cursor: pointer; background: transparent; color: var(--zm-text-dim); transition: background .2s, color .2s; }
      .zm-tab[aria-selected='true'] { background: var(--zm-primary); color: #fff; box-shadow: 0 2px 8px var(--zm-primary-soft); }
      .zm-tab svg { width: 23px; height: 23px; flex: none; }
      .opt { display: flex; align-items: center; justify-content: center; width: 44px; height: 44px; border-radius: 50%; cursor: pointer; transition: background .3s, box-shadow .3s, color .3s, border-color .3s; }
      .pagehead { display: flex; align-items: flex-end; justify-content: space-between; gap: 24px; flex-wrap: wrap; margin: 0 0 14px; }
      .pagehead h1 { margin: 0; font-family: var(--zm-font-display); font-weight: 600; font-size: 29px; letter-spacing: -0.015em; color: var(--zm-text); }
      .pagehead p { margin: 5px 0 0; font-family: var(--zm-font-body); font-size: 15px; color: var(--zm-text-dim); }
      .stats { display: flex; align-items: stretch; gap: 10px; padding-bottom: 2px; }
      .statcard { display: flex; flex-direction: column; align-items: center; text-align: center; gap: 3px; background: var(--zm-card); border: 1px solid var(--zm-border); border-radius: 14px; padding: 9px 14px; min-width: 96px; }
      .statcard .k { font-family: var(--zm-font-body); font-size: 11px; font-weight: 700; color: var(--zm-text-dim); }
      .statcard .v { font-family: var(--zm-font-display); font-weight: 600; font-size: 18px; line-height: 1; }
      .statcard .v small { font-size: 10px; color: var(--zm-text-dim); font-weight: 600; }
      .statcard .sub { font-family: var(--zm-font-body); font-size: 9px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: var(--zm-text-faint); }
      .status { font-family: var(--zm-font-body); font-size: 13px; color: var(--zm-text-dim); padding: 6px 0; }
      .err { margin: 16px 0; padding: 12px 16px; border-radius: 12px; background: rgba(232,100,90,0.1); border: 1px solid rgba(232,100,90,0.35); color: #e8645a; font-family: var(--zm-font-body); font-size: 13px; }
      @keyframes zm-optpulse {
        0%,100% { box-shadow: 0 0 0 4px color-mix(in srgb, var(--zm-primary) 18%, transparent), 0 6px 18px color-mix(in srgb, var(--zm-primary) 42%, transparent); }
        50% { box-shadow: 0 0 0 7px color-mix(in srgb, var(--zm-primary) 9%, transparent), 0 6px 22px color-mix(in srgb, var(--zm-primary) 55%, transparent); }
      }
      /* popup */
      .pop-bg { position: fixed; inset: 0; background: rgba(20,18,14,0.34); backdrop-filter: blur(2px); display: flex; align-items: center; justify-content: center; z-index: 200; padding: 20px; }
      .pop { background: var(--zm-card); border: 1px solid var(--zm-border); border-radius: 20px; box-shadow: 0 24px 60px rgba(0,0,0,0.28); padding: 20px 22px; width: 340px; max-width: 100%; animation: zm-rise 0.22s ease both; }
      .pop .x { margin-left: auto; flex: none; border: none; background: var(--zm-card-2); width: 30px; height: 30px; border-radius: 50%; cursor: pointer; color: var(--zm-text-dim); display: flex; align-items: center; justify-content: center; }
      @media (max-width: 760px) {
        .wrap { padding: 0 14px 90px; }
        header.top { padding: 0 0 2px; gap: 10px; justify-content: space-between; }
        .logo img { height: 84px; }
        .zm-nav { position: fixed; left: 0; right: 0; bottom: 0; width: 100%; margin: 0; gap: 0; border-radius: 18px 18px 0 0; border-width: 1px 0 0 0; padding: 7px 2px; padding-bottom: calc(7px + env(safe-area-inset-bottom)); justify-content: space-around; z-index: 60; box-shadow: 0 -4px 22px rgba(0,0,0,0.1); }
        .zm-tab { flex-direction: column; gap: 3px; padding: 8px 6px; flex: 1 1 0; min-width: 0; border-radius: 14px; }
        .zm-tab[aria-selected='true'] { background: transparent; color: var(--zm-primary); box-shadow: none; }
        .zm-tab svg { width: 27px; height: 27px; }
      }
    `,
  ];

  private _tabIcon(id: TabId) {
    switch (id) {
      case 'dashboard': return html`<svg viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7.6" height="8" rx="2.2" fill-opacity="0.18"></rect><rect x="13.4" y="3" width="7.6" height="5" rx="2.2" fill-opacity="0.18"></rect><rect x="13.4" y="12" width="7.6" height="9" rx="2.2" fill-opacity="0.18"></rect><rect x="3" y="15" width="7.6" height="6" rx="2.2" fill-opacity="0.18"></rect></svg>`;
      case 'cijfers': return html`<svg viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 3v16.5a1 1 0 0 0 1 1H21" fill="none"></path><rect x="6" y="11" width="4" height="6" rx="1.4" fill-opacity="0.18"></rect><rect x="11.8" y="6.5" width="4" height="10.5" rx="1.4" fill-opacity="0.18"></rect><rect x="17.6" y="13.5" width="4" height="3.5" rx="1.4" fill-opacity="0.18"></rect></svg>`;
      case 'consumers': return html`<svg viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9 7.5V3M15 7.5V3" fill="none"></path><path d="M6 7.5h12v3.5a6 6 0 0 1-12 0z" fill-opacity="0.18"></path><path d="M12 17v4" fill="none"></path></svg>`;
      case 'forecast': return html`<svg viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="8.5" cy="8" r="3.4" fill-opacity="0.18"></circle><path d="M8.5 1.6v1.7M8.5 12.7v1.7M1.9 8h1.7M13.4 8h1.7M3.8 3.3l1.2 1.2M13.2 3.3l-1.2 1.2" fill="none"></path><path d="M8 20h9.4a3.3 3.3 0 0 0 .2-6.6A4.9 4.9 0 0 0 9 11.8 3.5 3.5 0 0 0 8 20z" fill-opacity="0.18"></path></svg>`;
      case 'devices': return html`<svg viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="6.5" width="17" height="11" rx="3" fill-opacity="0.18"></rect><path d="M22 10.5v3" fill="none"></path><rect x="5.5" y="9.5" width="7" height="5" rx="1.4" stroke="none"></rect></svg>`;
      case 'settings': return html`<svg viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" fill-opacity="0.16"></path><circle cx="12" cy="12" r="3" fill="none"></circle></svg>`;
    }
  }

  private _content() {
    switch (this._tab) {
      case 'dashboard': return html`<zm-dashboard .d=${this._d} .theme=${this._theme}></zm-dashboard>`;
      case 'cijfers': return html`<zm-cijfers .d=${this._d} .theme=${this._theme}></zm-cijfers>`;
      case 'consumers': return html`<zm-consumers .d=${this._d} .theme=${this._theme} .consAction=${this._consAction} .schema=${this._schema} .config=${this._config} .hass=${this.hass}></zm-consumers>`;
      case 'forecast': return html`<zm-forecast .d=${this._d} .theme=${this._theme} .schema=${this._schema} .config=${this._config} .hass=${this.hass}></zm-forecast>`;
      case 'devices': return html`<zm-devices .config=${this._config} .plan=${this._plan} .schema=${this._schema} .hass=${this.hass}></zm-devices>`;
      case 'settings': return html`<zm-settings .schema=${this._schema} .config=${this._config} .hass=${this.hass} .only=${['system', 'price']}></zm-settings>`;
    }
    return nothing;
  }

  private _popupOverlay() {
    const v = popupView(this._d, this._popup, this._consAction);
    if (!v.show) return nothing;
    return html`
      <div class="pop-bg" @click=${() => (this._popup = null)}>
        <div class="pop" @click=${(e: Event) => e.stopPropagation()}>
          <div style="display:flex;align-items:center;gap:10px">
            <span style="flex:none;width:13px;height:13px;border-radius:4px;background:${v.color}"></span>
            <div style="font-family:var(--zm-font-display);font-weight:600;font-size:18px;color:var(--zm-text)">${v.label}</div>
            <button class="x" @click=${() => (this._popup = null)}><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"></path></svg></button>
          </div>
          <div style="font-family:var(--zm-font-body);font-size:13px;font-weight:700;color:var(--zm-text-dim);margin-top:6px">${v.time}</div>
          <div style="font-family:var(--zm-font-display);font-weight:600;font-size:22px;color:${v.color};margin-top:12px">${v.powerText}</div>
          ${v.isCons
            ? html`<div style="font-family:var(--zm-font-body);font-size:10px;font-weight:700;color:var(--zm-text-faint);text-transform:uppercase;letter-spacing:0.04em;margin-top:18px">Acties</div>
                <div style="display:flex;gap:6px;margin-top:8px">
                  ${(v.actions ?? []).map((a: any) => html`<button @click=${() => { this._consAction = { ...this._consAction, [this._popup!.id!]: a.id }; }} style="flex:1 1 0;min-width:0;display:flex;align-items:center;justify-content:center;gap:5px;border:1.5px solid ${a.bd};background:${a.bg};color:${a.fg};border-radius:11px;padding:9px 8px;cursor:pointer;font-family:var(--zm-font-display);font-weight:500;font-size:12.5px">${a.label}</button>`)}
                </div>`
            : nothing}
        </div>
      </div>
    `;
  }

  render() {
    const dark = this._theme === 'dark';
    const logoBase = this.embedded ? STATIC_URL : '/ui';  // assets served from the component in HA, from /ui standalone
    const h = HEAD[this._tab];
    const optId = this._config?.system?.entities?.optimize;
    const opt = optId ? this.hass?.states?.[optId]?.state === 'on' : this._optimizing;
    const toggleOpt = () => {
      if (optId && this.hass?.callService) this.hass.callService('switch', 'toggle', { entity_id: optId });
      else this._optimizing = !this._optimizing;
    };
    const stats = this._d ? headStats(this._d, this.hass, this._config) : null;
    const TABIDS: TabId[] = ['dashboard', 'cijfers', 'consumers', 'forecast', 'devices', 'settings'];
    return html`
      <div class="wrap" @chart-click=${this._onChartClick} @cons-action=${this._onConsAction} @zm-saved=${() => this._load()}>
        <header class="top">
          <div class="logo">
            <img src="${logoBase}/nebula-light-color.png" alt="EnergyMaster" style="mix-blend-mode:multiply${dark ? ';display:none' : ''}" />
            <img src="${logoBase}/nebula-dark.png" alt="EnergyMaster" style="mix-blend-mode:screen${dark ? '' : ';display:none'}" />
            <div class="wordmark" style="text-shadow:${dark ? '0 0 5px var(--zm-bg),0 0 11px var(--zm-bg)' : '0 0 5px rgba(8,12,18,0.82),0 0 11px rgba(8,12,18,0.82),0 0 20px rgba(8,12,18,0.5)'}"><span style="color:#ff8a3d">Zen</span><span style="color:${dark ? '#f2eee7' : '#ffffff'}">Master</span></div>
          </div>

          <nav class="zm-nav" role="tablist">
            ${TABIDS.map((id) => html`<button class="zm-tab" role="tab" aria-selected=${this._tab === id} title=${HEAD[id].title} @click=${() => (this._tab = id)}><span style="display:flex;align-items:center;justify-content:center">${this._tabIcon(id)}</span></button>`)}
          </nav>

          <div style="display:flex;align-items:center;gap:10px">
            ${this.embedded ? nothing : html`<button class="opt" title=${dark ? 'Licht thema' : 'Donker thema'} @click=${this._toggleTheme}
              style="background:var(--zm-card);border:1.5px solid var(--zm-border);color:var(--zm-text-dim)">
              ${dark
                ? html`<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 14.5A8 8 0 1 1 9.5 4a6.2 6.2 0 0 0 10.5 10.5z"></path></svg>`
                : html`<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4.5"></circle><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.4 1.4M17.6 17.6l1.4 1.4M19 5l-1.4 1.4M6.4 17.6l-1.4 1.4"></path></svg>`}
            </button>`}
            <button class="opt" title=${opt ? 'Systeem actief, klik om te pauzeren' : 'Systeem gepauzeerd, klik om te activeren'} @click=${toggleOpt}
              style="background:${opt ? 'linear-gradient(145deg,#ff9a4d,#f47a20)' : 'var(--zm-card-2)'};border:1.5px solid ${opt ? 'transparent' : 'var(--zm-border)'};color:${opt ? '#fff' : 'var(--zm-text-faint)'};box-shadow:${opt ? '0 0 0 4px color-mix(in srgb, var(--zm-primary) 18%, transparent), 0 6px 18px color-mix(in srgb, var(--zm-primary) 45%, transparent)' : 'none'};animation:${opt ? 'zm-optpulse 2.4s ease-in-out infinite' : 'none'}">
              <svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3.5v8"></path><path d="M6.4 7.2a8 8 0 1 0 11.2 0"></path></svg>
            </button>
          </div>
        </header>

        <div class="pagehead">
          <div><h1>${h.title}</h1><p>${h.subtitle}</p></div>
          ${this._tab !== 'settings' && stats
            ? html`<div class="stats">
                <div class="statcard"><div class="k">Vermogen</div><div class="v" style="color:${stats.powerColor}">${stats.power}<small> W</small></div><div class="sub">nu</div></div>
                <div class="statcard"><div class="k">Zon</div><div class="v" style="color:#f5a623">${stats.solar}<small> W</small></div><div class="sub">nu</div></div>
                <div class="statcard"><div class="k">Besparing</div><div class="v" style="color:#13967d">€${stats.save}</div><div class="sub">vandaag</div></div>
              </div>`
            : nothing}
        </div>

        ${this._status === 'error'
          ? html`<div class="err">Laden mislukt: ${this._error}</div>`
          : this._status === 'loading'
            ? html`<div class="status">laden…</div>`
            : this._content()}

        ${this._popupOverlay()}
      </div>
    `;
  }
}

if (!customElements.get('zm-app')) customElements.define('zm-app', ZmApp);
