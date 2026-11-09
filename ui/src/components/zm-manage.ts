import { LitElement, html, svg, css, nothing } from 'lit';
import { property, state } from 'lit/decorators.js';
import { tokens } from '../tokens.js';
import { api } from '../service/api.js';
import { byKind } from '../service/drivers.js';
import { ZendureBle } from '../service/ble.js';
import { t } from '../service/i18n.js';
import './zm-group.js';
import './zm-ble-onboard.js';

// "Toevoegen": left the consumers, devices and solar groups per tab, right the tiles of that tab, the form of a new one,
// or the editor of the picked one. On the phone one side at a time, with a way back.

type Kind = 'consumer' | 'device' | 'solar';
const TABS: [Kind, string][] = [['consumer', 'Verbruikers'], ['device', 'Apparaten'], ['solar', 'Zonnepanelen']];
const NARROW = window.matchMedia('(max-width: 760px)');

// one line icon per tile, the same stroke and viewBox as the rest of the panel; a device tile is a battery
const ICONS: Record<string, any> = {
  ev: svg`<path d="M13 2L5 13h5l-1 9 9-12h-6z"></path>`,
  washer: svg`<rect x="4" y="3" width="16" height="18" rx="2.5"></rect><circle cx="12" cy="13.5" r="4"></circle><path d="M7.5 6.5h1.5"></path>`,
  dryer: svg`<rect x="4" y="3" width="16" height="18" rx="2.5"></rect><path d="M9 11a3 3 0 0 1 5 1.5M15 16a3 3 0 0 1-5-1.5"></path><path d="M7.5 6.5h1.5"></path>`,
  dish: svg`<rect x="4" y="3" width="16" height="18" rx="2.5"></rect><path d="M4 9h16M8.5 13h7M8.5 17h4.5"></path>`,
  plug: svg`<path d="M9 2v6M15 2v6M6 8h12v4a6 6 0 01-12 0z"></path><path d="M12 18v4"></path>`,
  hp: svg`<path d="M3 8h10a3 3 0 1 0-3-3M3 12h14a3 3 0 1 1-3 3M3 16h8a2.5 2.5 0 1 1-2.5 2.5"></path>`,
  boiler: svg`<path d="M12 3s6 6 6 11a6 6 0 0 1-12 0c0-5 6-11 6-11z"></path>`,
  boiler_e: svg`<path d="M12 3s6 6 6 11a6 6 0 0 1-12 0c0-5 6-11 6-11z"></path><path d="M12.8 9.5L10 14h3l-.8 4 3.2-5h-3z"></path>`,
  pool: svg`<path d="M3 15c2 0 2-1.6 4-1.6s2 1.6 4 1.6 2-1.6 4-1.6 2 1.6 4 1.6M3 19c2 0 2-1.6 4-1.6s2 1.6 4 1.6 2-1.6 4-1.6 2 1.6 4 1.6"></path><path d="M8.5 12V6a2 2 0 1 1 4 0"></path>`,
  battery: svg`<rect x="3" y="7" width="15" height="10" rx="2.5"></rect><path d="M21 10v4"></path>`,
};

export class ZmManage extends LitElement {
  @property({ attribute: false }) config: any = null;
  @property({ attribute: false }) schema: any = null;
  @property({ attribute: false }) add: any[] = [];  // the tile catalog from get_schema
  @property({ attribute: false }) hass: any = null;
  @property() tab: Kind = 'consumer';
  @state() private _sel: string | null = null;  // the entry being edited (a device by its key or, just added, by its subentry)
  @state() private _tile: any = null;  // the tile being added
  @state() private _right = false;  // the phone shows the right side
  @state() private _name = '';  // the name of a new consumer or solar group, or the new name of the picked device
  @state() private _nameErr: string | null = null;
  @state() private _pair: string | null = null;  // the BLE name being paired
  @state() private _narrow = NARROW.matches;
  private _onNarrow = () => { this._narrow = NARROW.matches; };

  connectedCallback() { super.connectedCallback(); NARROW.addEventListener('change', this._onNarrow); }
  disconnectedCallback() { super.disconnectedCallback(); NARROW.removeEventListener('change', this._onNarrow); }

  static styles = [
    tokens,
    css`
      .pop-bg { position: fixed; inset: 0; background: rgba(20,18,14,0.38); backdrop-filter: blur(2px); display: flex; align-items: flex-start; justify-content: center; z-index: 200; padding: 80px 20px 20px; overflow: auto; }
      .pop { width: min(1000px, 100%); background: var(--zm-card); border: 1px solid var(--zm-border); border-radius: 22px; box-shadow: 0 24px 60px rgba(0,0,0,0.28); overflow: hidden; }
      .poph { display: flex; align-items: center; gap: 10px; padding: 14px 18px; background: linear-gradient(160deg, var(--zm-ctl-a), var(--zm-ctl-b)); border-bottom: 1px solid var(--zm-ctl-border); }
      .poph h3 { flex: 1 1 auto; min-width: 0; margin: 0; font-family: var(--zm-font-display); font-weight: 600; font-size: var(--zm-fs-l); color: var(--zm-text); }
      .x, .back { flex: none; width: 34px; height: 34px; border-radius: 50%; border: 1px solid var(--zm-ctl-border); background: var(--zm-card); color: var(--zm-text-dim); display: flex; align-items: center; justify-content: center; cursor: pointer; }
      .body { display: grid; grid-template-columns: 260px minmax(0, 1fr); min-height: 420px; }
      .narrow .body { grid-template-columns: minmax(0, 1fr); }
      .left { border-right: 1px solid var(--zm-border); padding: 12px; display: flex; flex-direction: column; gap: 8px; }
      .right { padding: 16px 18px 18px; display: flex; flex-direction: column; gap: 12px; min-width: 0; }
      .tabs { display: flex; gap: 4px; background: var(--zm-card-2); border-radius: 12px; padding: 3px; }
      .tabs button { flex: 1 1 0; border: none; border-radius: 9px; padding: 6px 4px; background: transparent; color: var(--zm-text-dim); font-family: var(--zm-font-body); font-size: var(--zm-fs-s); font-weight: 800; cursor: pointer; }
      .tabs button[aria-selected='true'] { background: var(--zm-primary); color: #fff; }
      .item { display: flex; align-items: center; gap: 8px; border: 1px solid transparent; border-radius: 10px; padding: 8px 10px; background: transparent; color: var(--zm-text); font-family: var(--zm-font-body); font-size: var(--zm-fs-m); font-weight: 700; text-align: left; cursor: pointer; }
      .item:hover { background: var(--zm-card-2); }
      .item[aria-selected='true'] { border-color: var(--zm-primary); background: var(--zm-teal-soft); }
      .plus { border: 1px dashed var(--zm-border); color: var(--zm-text-dim); }
      .types { display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 10px; }
      .type { display: flex; flex-direction: column; align-items: center; gap: 7px; padding: 12px 6px; border: 1px solid var(--zm-border); background: var(--zm-card-2); border-radius: 14px; cursor: pointer; }
      .type:hover { border-color: var(--zm-text-faint); }
      .type span { font-family: var(--zm-font-body); font-size: var(--zm-fs-s); font-weight: 700; color: var(--zm-text-dim); text-align: center; line-height: 1.25; }
      .type .ic { width: 42px; height: 42px; border-radius: 999px; display: flex; align-items: center; justify-content: center; }
      .type .ic img { width: 28px; height: 28px; object-fit: contain; }
      .pills { display: flex; gap: 8px; flex-wrap: wrap; }
      .pill { display: inline-flex; align-items: center; gap: 6px; padding: 7px 12px; border-radius: 999px; background: var(--zm-card); border: 1px solid var(--zm-ctl-border); color: #13967d; font-family: var(--zm-font-body); font-size: var(--zm-fs-m); font-weight: 800; text-decoration: none; cursor: pointer; }
      .fld { display: flex; flex-direction: column; gap: 4px; font-family: var(--zm-font-body); font-size: var(--zm-fs-s); font-weight: 700; color: var(--zm-text-faint); }
      .erow { display: flex; gap: 8px; }
      .erow input { flex: 1 1 auto; background: var(--zm-card-2); color: var(--zm-text); border: 1px solid var(--zm-border); border-radius: 8px; font-family: var(--zm-font-body); font-weight: 600; font-size: var(--zm-fs-m); padding: 7px 10px; }
      .save { border: none; border-radius: 999px; background: var(--zm-primary); color: #fff; padding: 6px 14px; font-family: var(--zm-font-body); font-weight: 800; cursor: pointer; }
      .err { font-family: var(--zm-font-body); font-size: var(--zm-fs-s); color: #e8645a; }
    `,
  ];

  // the entries of a tab as [key, name, subentry]: a consumer and a solar group are their subentry, a device has its own key
  private _list(kind: Kind): [string, string, string][] {
    if (kind === 'consumer') return (this.config?.consumers ?? []).map((c: any) => [c.id, c.name ?? c.id, c.id]);
    if (kind === 'solar') return (this.config?.solar_panels ?? []).map((p: any) => [p.id, p.name ?? p.id, p.id]);
    return Object.entries(this.config ?? {}).filter(([, e]: [string, any]) => e?.subentry_id && e.kind !== 'Distribution').map(([k, e]: [string, any]) => [k, e.name || k, e.subentry_id]);
  }

  private _open(tab: Kind, sel: string | null, tile: any = null) {
    this.tab = tab;
    this._sel = sel;
    this._tile = tile;
    this._nameErr = null;
    this._name = sel && tab === 'device' ? this.config?.[sel]?.name ?? '' : '';
    this._right = !!(sel || tile);
  }

  // a tile opens its form; a consumer gets its label as the proposed name, numbered when that name is taken
  private _start(tile: any) {
    this._open(this.tab, null, tile);
    const names = new Set(this._list(this.tab).map(([, n]) => n.toLowerCase()));
    const label = this._label(tile);
    let name = label, n = 1;
    while (names.has(name.toLowerCase())) name = `${label} ${++n}`;
    this._name = tile.group === 'device' ? '' : name;
  }

  private _label(tile: any): string {
    return tile.group === 'device' ? tile.label : t(`ui.consumer_type.${tile.id}`, tile.id);
  }

  private async _saveName(key: string) {
    this._nameErr = null;
    try {
      const res = await api.saveSchema('edit', key, { name: this._name });
      if (res.ok) this.dispatchEvent(new CustomEvent('zm-saved', { bubbles: true, composed: true }));
      else this._nameErr = (res.errors ?? [t('ui.common.unknown_error', 'onbekende fout')]).join('; ');
    } catch (e: any) {
      this._nameErr = e?.message ?? String(e);
    }
  }

  // the HA device page of the entity, opened inside HA's own router
  private _go = (e: Event) => {
    e.preventDefault();
    history.pushState(null, '', (e.currentTarget as HTMLAnchorElement).getAttribute('href') ?? '');
    window.dispatchEvent(new CustomEvent('location-changed'));
  };

  private _tiles() {
    // solar groups have one kind only: the plus opens their form straight away
    const tiles = this.add.filter((a) => a.group === this.tab);
    return html`<div class="types">${tiles.map((a) => html`<button class="type" @click=${() => this._start(a)}>
      <span class="ic" style="background:color-mix(in srgb, ${a.color ?? '#1fb89a'} 15%, transparent)">${a.logo ? html`<img src="https://brands.home-assistant.io/${a.logo}/icon.png" alt="" />` : html`<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke=${a.color ?? '#1fb89a'} stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round">${ICONS[a.icon] ?? ICONS.battery}</svg>`}</span>
      <span>${this._label(a)}</span>
    </button>`)}</div>`;
  }

  private _form(tile: any) {
    // a device gets the name it keeps from its family and serial; a consumer and a solar group are named here
    return html`
      ${tile.group === 'device' ? nothing : html`<label class="fld">${t('ui.devices.name', 'Naam')}<div class="erow"><input .value=${this._name} @input=${(e: Event) => (this._name = (e.target as HTMLInputElement).value)} /></div></label>`}
      <zm-group .fields=${tile.fields} .config=${this.config} .hass=${this.hass} heading=${this._label(tile)} iconKey=${tile.icon ?? 'device'}
        newType=${tile.type} .preset=${tile.data ?? {}} newTitle=${this._name} ?singleColumn=${tile.group !== 'device'} autoEdit
        @zm-added=${(e: CustomEvent) => { this._open(this.tab, e.detail); this.dispatchEvent(new CustomEvent('zm-saved', { bubbles: true, composed: true })); }}
        @zm-cancel=${() => this._open(this.tab, null)}></zm-group>`;
  }

  private _editor(key: string, sub: string) {
    const kind = this.tab;
    const dev = kind === 'device' ? this.config?.[key] : null;
    const cfg = kind === 'consumer' ? (this.config?.consumers ?? []).find((c: any) => c.id === key) : null;
    const entry = kind === 'solar' ? `solar_panel:${key}` : key;
    const name = this._list(kind).find(([k]) => k === key)?.[1] ?? key;
    const eid = dev?.home_output ?? cfg?.state ?? '';
    const ha = eid ? this.hass?.entities?.[eid]?.device_id : undefined;
    return html`
      <div class="pills">
        ${ha ? html`<a class="pill" href=${`/config/devices/device/${ha}`} @click=${this._go}>Home Assistant</a>` : nothing}
        ${dev?.ble_name !== undefined && ZendureBle.available() ? html`<button class="pill" @click=${() => (this._pair = dev.ble_name ?? '')}>${t('ui.devices.ble_pair', 'BLE koppelen')}</button>` : nothing}
      </div>
      ${dev ? html`<label class="fld">${t('ui.devices.name', 'Naam')}<div class="erow"><input .value=${this._name} @input=${(e: Event) => (this._name = (e.target as HTMLInputElement).value)} @keydown=${(e: KeyboardEvent) => e.key === 'Enter' && this._saveName(key)} /><button class="save" @click=${() => this._saveName(key)}>${t('ui.common.save', 'Opslaan')}</button></div>${this._nameErr ? html`<span class="err">${this._nameErr}</span>` : nothing}</label>` : nothing}
      <zm-group .entry=${entry} .fields=${this.schema?.[entry] ?? []} .config=${this.config} .hass=${this.hass} heading=${name} iconKey=${dev ? 'device' : cfg?.kind ?? 'forecast'}
        collection=${dev ? '' : 'subentries'} subentry=${kind === 'consumer' ? '' : sub} ?singleColumn=${!dev} autoEdit></zm-group>`;
  }

  private _rightSide() {
    if (this._tile) return this._form(this._tile);
    // the picked entry, found by its key or, right after it was added, by its subentry; gone after a delete
    const found = this._list(this.tab).find(([k, , s]) => k === this._sel || s === this._sel);
    if (found) return this._editor(found[0], found[2]);
    if (this.tab === 'solar') return this._form(this.add.find((a) => a.group === 'solar'));
    return this._tiles();
  }

  render() {
    const close = () => this.dispatchEvent(new CustomEvent('close'));
    const left = html`<div class="left">
      <div class="tabs">${TABS.map(([k, label]) => html`<button aria-selected=${this.tab === k} @click=${() => this._open(k, null)}>${t(`ui.manage.${k}`, label)}</button>`)}</div>
      ${this._list(this.tab).map(([k, n, s]) => html`<button class="item" aria-selected=${this._sel === k || this._sel === s} @click=${() => this._open(this.tab, k)}>${n}</button>`)}
      <button class="item plus" @click=${() => { this._open(this.tab, null); this._right = true; }}>+ ${t('ui.manage.add', 'Toevoegen')}</button>
    </div>`;
    const showRight = !this._narrow || this._right;
    return html`
      <div class="pop-bg" @click=${close}>
        <div class="pop ${this._narrow ? 'narrow' : ''}" role="dialog" @click=${(e: Event) => e.stopPropagation()}>
          <div class="poph">
            ${this._narrow && this._right ? html`<button class="back" title=${t('ui.common.back', 'Terug')} @click=${() => (this._right = false)}>‹</button>` : nothing}
            <h3>${t('ui.manage.title', 'Toevoegen')}</h3>
            <button class="x" title=${t('ui.common.close', 'Sluiten')} @click=${close}><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"></path></svg></button>
          </div>
          <div class="body">
            ${this._narrow && this._right ? nothing : left}
            ${showRight ? html`<div class="right">${this._rightSide()}</div>` : nothing}
          </div>
        </div>
      </div>
      ${this._pair !== null
        ? html`<zm-ble-onboard .config=${this.config} .driverId=${Object.keys(byKind(this.config, 'ZendureDriver'))[0] ?? ''} .bleName=${this._pair} @close=${() => (this._pair = null)}></zm-ble-onboard>`
        : nothing}
    `;
  }
}

if (!customElements.get('zm-manage')) customElements.define('zm-manage', ZmManage);
