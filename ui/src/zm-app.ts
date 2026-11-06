import { LitElement, html, svg, css, nothing, PropertyValues } from 'lit';
import { state, property } from 'lit/decorators.js';
import type { Plan, Schema, TabId, Theme, HistoryDay } from './types.js';
import { tokens } from './tokens.js';
import { api } from './service/api.js';
import { loadTranslations, t } from './service/i18n.js';
import { STATIC_URL } from './service/ha.js';
import { sliceLive, refetchLive } from './service/loader.js';
import { PANEL_COLORS, chartData, headStats, type HeadStats } from './derive.js';
import { byKind } from './service/drivers.js';
import './components/zm-overview.js';
import './components/zm-cijfers.js';
import './components/zm-forecast.js';
import './components/zm-settings.js';

const TAB_LABEL: Record<TabId, string> = { dashboard: 'Planning', devices: 'Apparaten', solar: 'Solar', cijfers: 'History', settings: 'Settings' };

export class ZmApp extends LitElement {
  @property({ type: Boolean }) embedded = false;  // inside the HA panel: hide our own logo (HA titles it)
  @property({ attribute: false }) theme: Theme | null = null;  // driven by HA in the panel; null = standalone toggle
  @property({ attribute: false }) hass: any = null;  // HA object in the panel; lets device telemetry read hass.states
  private _i18n = false;  // the translation dictionary is fetched once
  @state() private _tab: TabId = (localStorage.getItem('zm-tab') as TabId) ?? 'dashboard';
  @state() private _theme: Theme = 'light';
  @state() private _plan: Plan | null = null;
  @state() private _history: HistoryDay[] = [];
  @state() private _config: any = null;
  @state() private _schema: Schema | null = null;
  @state() private _actions: Record<string, any[]> = {};
  @state() private _tiles: Record<string, string[]> = {};
  @state() private _status: 'loading' | 'ok' | 'error' = 'loading';
  @state() private _error = '';
  @state() private _optimizing = true;
  private _d: any = null;
  private _unsubPlan: (() => void) | null = null;

  connectedCallback(): void {
    super.connectedCallback();
    const t = document.documentElement.getAttribute('data-zm-theme');
    this._theme = t === 'dark' ? 'dark' : 'light';
    void this._load();
    void this._subscribe();
    // HA closes the websocket 5 minutes after the tab hides; reload on every reconnect and on tab return
    this.hass?.connection?.addEventListener?.('ready', this._onReconnect);
    document.addEventListener('visibilitychange', this._onVisible);
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    this._unsubPlan?.();
    this._unsubPlan = null;
    this.hass?.connection?.removeEventListener?.('ready', this._onReconnect);
    document.removeEventListener('visibilitychange', this._onVisible);
  }

  private async _loadHistory(): Promise<void> {
    try {
      this._history = (await api.getHistory()).days ?? [];
    } catch { /* history is optional: the trend simply stays empty */ }
  }

  private _onReconnect = () => {
    void this._load();
    if (!this._unsubPlan) void this._subscribe();  // the plan subscription died with the old socket
  };

  private _onVisible = () => {
    if (!document.hidden) void this._load();
  };

  // a dynamic price provider turns the solver on; without one the planning is hidden and the fleet, the solar groups and the settings stay
  private get _solver(): boolean { return (this._config?.price?.provider ?? 'none') !== 'none'; }
  private _tabIds(): TabId[] {
    return this._solver ? ['dashboard', 'devices', 'solar', 'cijfers', 'settings'] : ['devices', 'solar', 'settings'];
  }

  private _window(tab: TabId): [number, number] {
    return tab === 'solar' ? [12, 99999] : [0, 108];   // devices: 3 h of history + 24 h; solar carries the forecast: now -> 7 d
  }

  private _reslice(): void {
    if (!this._config) return;
    const [a, b] = this._window(this._tab);
    this._plan = sliceLive(a, b, this._config);
  }

  private async _subscribe(): Promise<void> {
    try {
      const p = api.subscribePlan(() => { void this._onPush(); });
      if (p) this._unsubPlan = await p;
    } catch { /* no plan events without a solver */ }
  }

  // the schema, the config and the plan; the plan is optional: without a solver the tabs that need it stay empty, the rest renders on the config alone
  private async _fetch(): Promise<void> {
    const bundle = await api.getSchema();
    this._schema = bundle.schema;
    this._config = bundle.config;
    this._actions = bundle.actions ?? {};
    this._tiles = bundle.tiles ?? {};
    try { await refetchLive(); this._reslice(); } catch { /* no plan */ }
  }

  private async _onPush(): Promise<void> {
    // the push also follows a config change made in HA's own dialogs, so the schema and config come along
    try { await this._fetch(); } catch { /* keep the last config and plan */ }
  }

  willUpdate(changed: PropertyValues) {
    if (changed.has('hass') && this.hass && !this._i18n) {  // field names and descriptions come from the integration's translations
      this._i18n = true;
      void loadTranslations(this.hass).then(() => this.requestUpdate());
    }
    if (changed.has('theme') && this.theme) this._theme = this.theme;  // follow HA's dark mode
    if (changed.has('_config') && this._config && !this._tabIds().includes(this._tab)) this._tab = 'devices';  // a hidden tab falls back to the devices
    if (changed.has('_tab')) this._reslice();  // re-slice the cached full plan for this tab's window
    if (changed.has('_plan') || changed.has('_tab') || changed.has('_history')) this._d = this._plan ? chartData(this._plan, this._history) : null;
  }

  private async _load() {
    if (!this._config) this._status = 'loading';  // only the first load blanks the tab; a reload keeps the open popups and drafts
    this._error = '';
    try {
      await this._fetch();
      void this._loadHistory();  // the 30 days change once a day: on load, save, tab return and reconnect, not per tab click or plan push
      this._status = 'ok';
    } catch (e: any) {
      this._error = e?.message ?? String(e);
      this._status = 'error';
    }
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
      header.top { display: flex; align-items: center; gap: 20px; padding: 0 0 14px; flex-wrap: nowrap; }
      .logo { flex: none; position: relative; display: inline-flex; align-items: center; justify-content: center; }
      .logo img { height: 80px; width: auto; display: block; }
      .logo .wordmark { position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%); font-family: var(--zm-font-display); font-weight: 700; font-size: 24px; letter-spacing: 0.01em; text-transform: uppercase; white-space: nowrap; }
      .opt { display: flex; align-items: center; justify-content: center; width: 44px; height: 44px; border-radius: 50%; cursor: pointer; transition: background .3s, box-shadow .3s, color .3s, border-color .3s; }
      .opts { display: flex; align-items: center; gap: 10px; margin-left: 6px; }
      /* the live figures next to the logo, one tile each with its own colour (--c) and tint (--t) */
      /* the tiles keep their own width; only when even that does not fit they wrap, right-aligned */
      .kpis { margin-left: auto; display: flex; align-items: center; justify-content: flex-end; gap: 10px; flex-wrap: wrap; min-width: 0; }
      .kpi { flex: none; position: relative; display: flex; flex-direction: column; justify-content: center; gap: 5px; min-width: 104px; height: 62px; padding: 9px 12px 11px; border-radius: 18px; background: linear-gradient(155deg, var(--zm-card) 45%, var(--t)); border: 1px solid var(--zm-border); box-shadow: var(--zm-shadow-sm); overflow: hidden; }
      .kpi::after { content: ''; position: absolute; left: 14px; right: 14px; bottom: 0; height: 3px; border-radius: 3px 3px 0 0; background: var(--c); opacity: 0.85; }
      .kpi.wide { flex-direction: row; align-items: center; gap: 14px; }
      .kpi .col { display: flex; flex-direction: column; gap: 8px; }
      .kpi .h { display: flex; align-items: center; gap: 7px; font-family: var(--zm-font-body); font-size: var(--zm-fs-s); font-weight: 800; color: var(--zm-text-dim); }
      .kpi .i { flex: none; width: 16px; height: 16px; border-radius: 6px; display: flex; align-items: center; justify-content: center; background: var(--t); color: var(--c); }
      .kpi .s { margin-left: auto; padding-left: 8px; font-size: var(--zm-fs-s); font-weight: 800; text-transform: uppercase; letter-spacing: 0.07em; color: var(--zm-text-faint); }
      .kpi .v { font-family: var(--zm-font-display); font-weight: 600; font-size: var(--zm-fs-l); line-height: 1; letter-spacing: -0.01em; color: var(--c); }
      .kpi .v small { font-size: var(--zm-fs-s); font-weight: 500; color: var(--zm-text-dim); }
      .kpi .ph { display: flex; flex-direction: column; gap: 1px; border-left: 1px solid var(--zm-border); padding-left: 14px; font-family: var(--zm-font-display); font-weight: 600; font-size: var(--zm-fs-s); line-height: 1.1; color: var(--c); }
      .kpi .ph span { display: inline-block; width: 20px; font-family: var(--zm-font-body); font-size: var(--zm-fs-s); font-weight: 800; color: var(--zm-text-dim); }
      .zt { position: relative; display: flex; }
      .zpop { display: none; position: absolute; top: calc(100% + 8px); left: 50%; transform: translateX(-50%); z-index: 20; width: 210px; background: var(--zm-card); border: 1px solid var(--zm-border); border-radius: 14px; box-shadow: var(--zm-shadow); padding: 10px 12px; }
      .zt:hover .zpop, .zt:focus-within .zpop { display: block; }
      .zpop .zh { font-family: var(--zm-font-body); font-size: var(--zm-fs-s); font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: var(--zm-text-dim); margin-bottom: 6px; }
      .zpop .zr { display: flex; align-items: center; gap: 8px; margin-top: 5px; font-family: var(--zm-font-display); font-size: var(--zm-fs-m); color: var(--zm-text); }
      .zpop .zr i { flex: none; width: 4px; height: 16px; border-radius: 2px; }
      .zpop .zr span { flex: 1 1 auto; font-weight: 500; }
      .zpop .zr b { font-weight: 600; }
      .zpop .zr small { font-size: var(--zm-fs-s); color: var(--zm-text-dim); }
      /* the tabstrip is the top of the card the tab's content sits in */
      /* the tabstrip low: little padding, text and icons a size down */
      .tabs { display: flex; gap: 5px; padding: 5px; background: linear-gradient(180deg, var(--zm-card-2), var(--zm-tabbar)); border: 1px solid var(--zm-border); border-radius: 20px 20px 0 0; }
      .tab { flex: 1 1 0; min-width: 0; display: flex; align-items: center; justify-content: center; gap: 8px; padding: 5px 10px; border: none; border-radius: 12px; background: transparent; cursor: pointer; font-family: var(--zm-font-display); font-weight: 500; font-size: var(--zm-fs-m); letter-spacing: 0.01em; color: var(--zm-text-dim); transition: background .15s, color .15s, box-shadow .15s; }
      .tab svg { width: 17px; height: 17px; flex: none; opacity: 0.8; }
      .tab:hover { color: var(--zm-text); background: var(--zm-card); box-shadow: 0 1px 3px rgba(70,52,24,0.08); }
      .tab[aria-selected='true'] { color: #fff; background: linear-gradient(145deg, #2cc7a8, #13967d); box-shadow: 0 4px 14px rgba(31,184,154,0.35), inset 0 1px 0 rgba(255,255,255,0.25); }
      .tab[aria-selected='true'] svg { opacity: 1; }
      .tabbody { background: var(--zm-card); border: 1px solid var(--zm-border); border-top: none; border-radius: 0 0 20px 20px; box-shadow: var(--zm-shadow); padding: 18px; }
      .status { font-family: var(--zm-font-body); font-size: var(--zm-fs-m); color: var(--zm-text-dim); padding: 6px 0; }
      .err { margin: 16px 0; padding: 12px 16px; border-radius: 12px; background: rgba(232,100,90,0.1); border: 1px solid rgba(232,100,90,0.35); color: #e8645a; font-family: var(--zm-font-body); font-size: var(--zm-fs-m); }
      @keyframes zm-optpulse {
        0%,100% { box-shadow: 0 0 0 4px color-mix(in srgb, var(--zm-primary) 18%, transparent), 0 6px 18px color-mix(in srgb, var(--zm-primary) 42%, transparent); }
        50% { box-shadow: 0 0 0 7px color-mix(in srgb, var(--zm-primary) 9%, transparent), 0 6px 22px color-mix(in srgb, var(--zm-primary) 55%, transparent); }
      }
      @media (max-width: 760px) {
        .wrap { padding: 0 14px 90px; }
        header.top { padding: 0 0 2px; gap: 10px; justify-content: space-between; flex-wrap: wrap; }
        .logo img { height: 84px; }
        .tabs { position: fixed; left: 0; right: 0; bottom: 0; z-index: 60; gap: 0; border-radius: 18px 18px 0 0; border-width: 1px 0 0 0; padding: 7px 2px; padding-bottom: calc(7px + env(safe-area-inset-bottom)); box-shadow: 0 -4px 22px rgba(0,0,0,0.1); }
        .tab { flex-direction: column; gap: 3px; padding: 8px 6px; font-size: var(--zm-fs-s); }
        .tab[aria-selected='true'] { background: transparent; color: var(--zm-primary); box-shadow: none; }
        .tab svg { width: 27px; height: 27px; }
        .tabbody { border-top: 1px solid var(--zm-border); border-radius: 20px; padding: 12px; }
        .kpis { margin-left: 0; flex-wrap: wrap; }
      }
    `,
  ];

  private _tabIcon(id: TabId) {
    switch (id) {
      case 'dashboard': return html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 3v16.5a1 1 0 0 0 1 1H21"></path><path d="M7 15l4-5 3 3 5-6"></path></svg>`;
      case 'cijfers': return html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 12a8.5 8.5 0 1 0 2.6-6.1L3.5 8.5"></path><path d="M3.5 3.5v5h5"></path><path d="M12 7.5V12l3 2"></path></svg>`;
      case 'devices': return html`<svg viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="6.5" width="17" height="11" rx="3" fill-opacity="0.18"></rect><path d="M22 10.5v3" fill="none"></path><rect x="5.5" y="9.5" width="7" height="5" rx="1.4" stroke="none"></rect></svg>`;
      case 'solar': return html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="4"></circle><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"></path></svg>`;
      case 'settings': return html`<svg viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path
        d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65
           0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65
           0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65
           1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0
           4h-.09a1.65 1.65 0 0 0-1.51 1z" fill-opacity="0.16"></path><circle cx="12" cy="12" r="3" fill="none"></circle></svg>`;
    }
  }

  private _content() {
    switch (this._tab) {
      case 'dashboard': return html`<zm-overview view="plan" .d=${this._d} .theme=${this._theme} .config=${this._config} .schema=${this._schema} .hass=${this.hass} .actions=${this._actions} .tiles=${this._tiles}></zm-overview>`;
      case 'cijfers': return html`<zm-cijfers .d=${this._d} .theme=${this._theme}></zm-cijfers>`;
      case 'devices': return html`<zm-overview view="tiles" .d=${this._d} .theme=${this._theme} .config=${this._config} .schema=${this._schema} .hass=${this.hass} .actions=${this._actions} .tiles=${this._tiles}></zm-overview>`;
      case 'settings': return html`<zm-settings .schema=${this._schema} .config=${this._config} .hass=${this.hass} ></zm-settings>`;
      case 'solar': return html`<zm-forecast .d=${this._d} .theme=${this._theme} .schema=${this._schema} .config=${this._config} .hass=${this.hass}></zm-forecast>`;
    }
    return nothing;
  }

  // the header tiles: the meter with its phases, the fleet, solar with a hint per group, the saving and the consumers
  private _kpis(s: HeadStats) {
    const icon = (body: unknown) => html`<span class="i"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">${body}</svg></span>`;
    return html`
      <div class="kpi wide" style="--c:${s.p1Color};--t:var(--zm-blue-soft)">
        <div class="col">
          <div class="h">${icon(svg`<rect x="4" y="3" width="16" height="18" rx="3"></rect><path d="M8 8h8M8 12h4"></path>`)}P1<span class="s">${s.p1Dir}</span></div>
          <div class="v">${s.p1}<small> W</small></div>
        </div>
        ${s.phases.length ? html`<div class="ph">${s.phases.map((v, i) => html`<div><span>L${i + 1}</span>${v}</div>`)}</div>` : nothing}
      </div>
      <div class="kpi" style="--c:${s.powerColor};--t:var(--zm-teal-soft)">
        <div class="h">${icon(svg`<path d="M13 2L4.5 13.5h6.5L10 22l9-12h-7z"></path>`)}${t('ui.app.power', 'Vermogen')}<span class="s">${t('ui.app.now', 'nu')}</span></div>
        <div class="v">${s.power}<small> W</small></div>
      </div>
      <div class="zt" tabindex="0">
        <div class="kpi" style="--c:#f5a623;--t:var(--zm-amber-soft)">
          <div class="h">${icon(svg`<circle cx="12" cy="12" r="4"></circle><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.4 1.4M17.6 17.6L19 19M19 5l-1.4 1.4M6.4 17.6L5 19"></path>`)}${t('ui.app.solar', 'Zon')}<span class="s">${t('ui.app.now', 'nu')}</span></div>
          <div class="v">${s.solar}<small> W</small></div>
        </div>
        ${s.panels.length ? html`<div class="zpop"><div class="zh">${t('ui.app.solar_groups', 'Zonnegroepen · nu')}</div>
          ${s.panels.map((p, i) => html`<div class="zr"><i style="background:${PANEL_COLORS[i % PANEL_COLORS.length]}"></i><span>${p.name}</span><b>${p.w}<small> W</small></b></div>`)}</div>` : nothing}
      </div>
      <div class="kpi" style="--c:#1fb89a;--t:var(--zm-teal-soft)">
        <div class="h">${icon(svg`<circle cx="12" cy="12" r="8.5"></circle><path d="M15 9.2a3.6 3.6 0 1 0 0 5.6M7.5 11h5M7.5 13h5"></path>`)}${t('ui.app.savings', 'Besparing')}<span class="s">${t('ui.app.today', 'vandaag')}</span></div>
        <div class="v">€${s.save}</div>
      </div>
      <div class="kpi" style="--c:#f47a20;--t:var(--zm-orange-soft)">
        <div class="h">${icon(svg`<path d="M9 7.5V3M15 7.5V3"></path><path d="M6 7.5h12v3.5a6 6 0 0 1-12 0z"></path><path d="M12 17v4"></path>`)}${t('ui.app.consumers', 'Verbruikers')}<span class="s">${t('ui.app.now', 'nu')}</span></div>
        <div class="v">${s.cons}<small> W</small></div>
      </div>`;
  }

  render() {
    const dark = this._theme === 'dark';
    const logoBase = this.embedded ? STATIC_URL : '/ui';  // assets served from the component in HA, from /ui standalone
    const optId = Object.values(byKind(this._config, 'Distribution'))[0]?.optimize;
    const opt = optId ? this.hass?.states?.[optId]?.state === 'on' : this._optimizing;
    const toggleOpt = () => {
      if (optId && this.hass?.callService) this.hass.callService('switch', 'toggle', { entity_id: optId });
      else this._optimizing = !this._optimizing;
    };
    const stats = this._d ? headStats(this._d, this.hass, this._config) : null;
    const TABIDS = this._tabIds();
    return html`
      <div class="wrap" @zm-saved=${() => this._load()}>
        <header class="top">
          <div class="logo">
            <img src="${logoBase}/nebula-light-color.png" alt="ZenMaster" style="mix-blend-mode:multiply${dark ? ';display:none' : ''}" />
            <img src="${logoBase}/nebula-dark.png" alt="ZenMaster" style="mix-blend-mode:screen${dark ? '' : ';display:none'}" />
            <div class="wordmark" style="text-shadow:${dark ? '0 0 5px var(--zm-bg),0 0 11px var(--zm-bg)' : '0 0 5px rgba(8,12,18,0.82),0 0 11px rgba(8,12,18,0.82),0 0 20px rgba(8,12,18,0.5)'}"><span style="color:#ff8a3d">Zen</span><span style="color:${dark ? '#f2eee7' : '#ffffff'}">Master</span></div>
          </div>

          <div class="kpis">${stats ? this._kpis(stats) : nothing}</div>

          <div class="opts">
            ${this.embedded ? nothing : html`<button class="opt" title=${dark ? t('ui.app.theme_light', 'Licht thema') : t('ui.app.theme_dark', 'Donker thema')} @click=${this._toggleTheme}
              style="background:var(--zm-card);border:1.5px solid var(--zm-border);color:var(--zm-text-dim)">
              ${dark
                ? html`<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 14.5A8 8 0 1 1 9.5 4a6.2 6.2 0 0 0 10.5 10.5z"></path></svg>`
                : html`<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4.5"></circle><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.4 1.4M17.6 17.6l1.4 1.4M19 5l-1.4 1.4M6.4 17.6l-1.4 1.4"></path></svg>`}
            </button>`}
            ${!this._solver ? nothing : html`<button class="opt" title=${opt ? t('ui.app.system_active', 'Systeem actief, klik om te pauzeren') : t('ui.app.system_paused', 'Systeem gepauzeerd, klik om te activeren')} @click=${toggleOpt}
              style="background:${opt ? 'linear-gradient(145deg,#ff9a4d,#f47a20)' : 'var(--zm-card-2)'};border:1.5px solid ${opt ? 'transparent' : 'var(--zm-border)'};color:${opt ? '#fff' : 'var(--zm-text-faint)'};box-shadow:${opt ? '0 0 0 4px color-mix(in srgb, var(--zm-primary) 18%, transparent), 0 6px 18px color-mix(in srgb, var(--zm-primary) 45%, transparent)' : 'none'};animation:${opt ? 'zm-optpulse 2.4s ease-in-out infinite' : 'none'}">
              <svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3.5v8"></path><path d="M6.4 7.2a8 8 0 1 0 11.2 0"></path></svg>
            </button>`}
          </div>
        </header>

        <nav class="tabs" role="tablist">
          ${TABIDS.map((id) => html`<button class="tab" role="tab" aria-selected=${this._tab === id} @click=${() => { this._tab = id; localStorage.setItem('zm-tab', id); }}>${this._tabIcon(id)}<span>${t(`ui.tab.${id}.label`, TAB_LABEL[id])}</span></button>`)}
        </nav>
        <div class="tabbody">
          ${this._status === 'error'
            ? html`<div class="err">${t('ui.app.load_failed', 'Laden mislukt: {error}', { error: this._error })} <button style="margin-left:10px;border:1px solid rgba(232,100,90,0.5);background:transparent;color:#e8645a;border-radius:999px;padding:4px 14px;font-family:var(--zm-font-body);font-size: var(--zm-fs-m);font-weight:700;cursor:pointer" @click=${() => this._load()}>${t('ui.app.retry', 'Opnieuw')}</button></div>`
            : this._status === 'loading'
              ? html`<div class="status">${t('ui.app.loading', 'laden…')}</div>`
              : this._content()}
        </div>
      </div>
    `;
  }
}

if (!customElements.get('zm-app')) customElements.define('zm-app', ZmApp);
