import { LitElement, html, css, nothing } from 'lit';
import { property, state } from 'lit/decorators.js';
import { tokens } from '../tokens.js';
import { api } from '../service/api.js';
import { ensureHaPickers, haPickersReady } from '../service/ha-pickers.js';
import type { Field } from '../types.js';

// Zonnegroepen as a flat list: one row per group (name + live Actueel from power_entity).
// The per-row pencil flips that group to an inline editor (schema entry "solar_panel:<i>").

const ACCENTS = ['#1fb89a', '#3aa0c9', '#f5a623', '#f47a20', '#e8645a'];

export class ZmSolarTable extends LitElement {
  @property({ attribute: false }) config: any = null;
  @property({ attribute: false }) schema: any = null;
  @property({ attribute: false }) hass: any = null;

  @state() private _editI: number | null = null;
  @state() private _draft: Record<string, any> = {};
  @state() private _saving = false;
  @state() private _error: string | null = null;

  static styles = [
    tokens,
    css`
      :host { display: block; }
      .card { background: var(--zm-card); border: 1px solid var(--zm-border); border-radius: 20px; box-shadow: var(--zm-shadow); padding: 6px 10px 12px; animation: zm-rise 0.4s ease both; }

      .head { display: flex; align-items: center; padding: 10px 6px 12px; font-family: var(--zm-font-body); font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: var(--zm-text-faint); }
      .h-accent { width: 13px; flex: none; }
      .h-name { flex: 1 1 auto; min-width: 0; padding: 0 8px; }
      .h-actual { width: 140px; flex: none; text-align: right; }
      .h-acts { width: 52px; flex: none; }

      .row { display: flex; align-items: center; background: var(--tint); border: 1px solid var(--zm-border); border-radius: 16px; margin: 8px 0; height: 60px; overflow: hidden; }
      .accent { width: 5px; flex: none; align-self: stretch; background: var(--acc); border-radius: 5px; margin: 8px; }
      .name { flex: 1 1 auto; min-width: 0; padding: 0 8px; font-family: var(--zm-font-display); font-weight: 600; font-size: 15px; color: var(--zm-text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .actual { width: 140px; flex: none; text-align: right; font-family: var(--zm-font-display); font-weight: 600; font-size: 17px; color: var(--zm-text); }
      .actual small { font-size: 11px; font-weight: 600; color: var(--zm-text-dim); }

      .acts { width: 52px; flex: none; display: flex; align-items: center; justify-content: center; }
      .pencil { width: 30px; height: 30px; border-radius: 9px; border: 1px solid var(--zm-border); background: var(--zm-card); color: var(--zm-text-dim); display: flex; align-items: center; justify-content: center; cursor: pointer; }
      .pencil:hover { color: var(--zm-text); border-color: var(--zm-text-faint); }
      .pencil svg { width: 14px; height: 14px; }

      .row.editing { align-items: stretch; height: auto; }
      .editor { flex: 1 1 auto; min-width: 0; padding: 12px 10px; }
      .efields { display: grid; grid-template-columns: 1fr 1fr; gap: 9px 12px; }
      .erow { display: flex; flex-direction: column; gap: 4px; }
      .erow.full { grid-column: 1 / -1; }
      .erow .k { font-family: var(--zm-font-body); font-size: 10px; font-weight: 700; color: var(--zm-text-faint); text-transform: uppercase; letter-spacing: 0.03em; }
      .erow input { background: var(--zm-card-2); color: var(--zm-text); border: 1px solid var(--zm-border); border-radius: 8px; font-family: var(--zm-font-body); font-weight: 600; font-size: 13px; padding: 7px 10px; }
      .erow input:focus { outline: none; border-color: var(--zm-primary); }
      .erow ha-selector { display: block; width: 100%; }
      .err { font-family: var(--zm-font-body); font-size: 11.5px; color: #e8645a; padding: 6px 2px 0; }
      .cl { display: flex; gap: 7px; justify-content: flex-end; padding: 10px 2px 0; }
      .iconbtn { width: 34px; height: 34px; border-radius: 10px; border: 1px solid var(--zm-border); background: var(--zm-card); color: var(--zm-text-dim); display: inline-flex; align-items: center; justify-content: center; cursor: pointer; }
      .iconbtn:hover { color: var(--zm-text); border-color: var(--zm-text-faint); }
      .iconbtn.primary { border: none; background: var(--zm-primary); color: #fff; box-shadow: 0 2px 8px var(--zm-primary-soft); }
      .iconbtn.danger { color: #e8645a; }
      .iconbtn.danger:hover { border-color: #e8645a; }
      .iconbtn:disabled { opacity: 0.45; cursor: default; }
      .iconbtn svg { width: 17px; height: 17px; }

      .empty { padding: 28px; text-align: center; color: var(--zm-text-faint); font-family: var(--zm-font-body); }
      .addrow { display: flex; justify-content: flex-end; padding: 10px 2px 4px; }
      .addbtn { display: inline-flex; align-items: center; gap: 6px; border: none; background: var(--zm-primary); color: #fff; border-radius: 999px; padding: 7px 13px; font-family: var(--zm-font-body); font-size: 12px; font-weight: 700; cursor: pointer; box-shadow: 0 2px 8px var(--zm-primary-soft); }
      .addbtn:disabled { opacity: 0.5; cursor: default; }
      .addbtn svg { width: 14px; height: 14px; }
    `,
  ];

  private _panels(): any[] { return Array.isArray(this.config?.solar_panels) ? this.config.solar_panels : []; }

  private _num(id?: string): number | null {
    const s = id ? this.hass?.states?.[id] : undefined;
    const n = s ? Number(s.state) : NaN;
    return Number.isFinite(n) ? n : null;
  }

  private _fields(i: number): Field[] { return this.schema?.[`solar_panel:${i}`] ?? []; }

  private _startEdit(i: number) {
    const p = this.config?.solar_panels?.[i] ?? {};
    const d: Record<string, any> = {};
    for (const f of this._fields(i)) d[f.id] = p[f.id];
    this._draft = d; this._editI = i; this._error = null;
  }
  private _cancel() { this._editI = null; this._draft = {}; this._error = null; }

  private _addPanel = async () => {
    const panels = this._panels();
    const names = new Set(panels.map((p: any) => p.name));
    let name = 'Nieuwe groep', n = 1;
    while (names.has(name)) name = `Nieuwe groep ${++n}`;
    this._saving = true; this._error = null;
    try {
      await api.saveSchema('add', `solar_panel:${panels.length}`, { name });
      this.dispatchEvent(new CustomEvent('zm-saved', { bubbles: true, composed: true }));
    } catch (e: any) { this._error = e?.message ?? String(e); }
    finally { this._saving = false; }
  };

  private async _save(i: number) {
    const p = this.config?.solar_panels?.[i] ?? {};
    const patch: Record<string, unknown> = {};
    for (const f of this._fields(i)) if (JSON.stringify(this._draft[f.id]) !== JSON.stringify(p[f.id])) patch[f.id] = this._draft[f.id];
    if (!Object.keys(patch).length) { this._cancel(); return; }
    this._saving = true; this._error = null;
    try {
      const res = await api.saveSchema('edit', `solar_panel:${i}`, patch);
      if (res.ok) { this._editI = null; this.dispatchEvent(new CustomEvent('zm-saved', { bubbles: true, composed: true })); }
      else this._error = (res.errors ?? ['onbekende fout']).join('; ');
    } catch (e: any) { this._error = e?.message ?? String(e); }
    finally { this._saving = false; }
  }

  private async _delete(i: number) {
    const p = this.config?.solar_panels?.[i] ?? {};
    if (!confirm(`"${p.name ?? `solar_panel:${i}`}" verwijderen?`)) return;
    this._saving = true; this._error = null;
    try {
      const res = await api.saveSchema('delete', `solar_panel:${i}`, {});
      if (res.ok) { this._editI = null; this.dispatchEvent(new CustomEvent('zm-saved', { bubbles: true, composed: true })); }
      else this._error = (res.errors ?? ['onbekende fout']).join('; ');
    } catch (e: any) { this._error = e?.message ?? String(e); }
    finally { this._saving = false; }
  }

  protected firstUpdated() { this._loadPickers(); }
  protected updated(ch: Map<string, unknown>) { if (ch.has('hass')) this._loadPickers(); }
  private _loadPickers() { if (this.hass && !haPickersReady()) ensureHaPickers().then(() => this.requestUpdate()); }

  private _input(f: Field) {
    const v = this._draft[f.id];
    if (f.edit_type === 'entity' && this.hass && haPickersReady()) {
      return html`<ha-selector .hass=${this.hass} .selector=${{ entity: {} }} .value=${v ?? ''} @value-changed=${(e: any) => { this._draft = { ...this._draft, [f.id]: e.detail?.value ?? '' }; }}></ha-selector>`;
    }
    const num = f.edit_type === 'int' || f.edit_type === 'double';
    return html`<input
      type=${num ? 'number' : 'text'} step=${f.edit_type === 'int' ? '1' : f.edit_type === 'double' ? 'any' : nothing}
      placeholder=${f.edit_type === 'entity' ? 'sensor.…' : ''}
      .value=${v == null ? '' : String(v)}
      @change=${(e: Event) => { const r = (e.target as HTMLInputElement).value; this._draft = { ...this._draft, [f.id]: num ? (r === '' ? null : Number(r)) : r }; }} />`;
  }

  private _editor(i: number) {
    return html`<div class="editor">
      <div class="efields">
        ${this._fields(i).map((f) => html`<div class="erow ${f.edit_type === 'entity' || f.edit_type === 'list' ? 'full' : ''}"><span class="k" title=${f.id}>${f.id}</span>${this._input(f)}</div>`)}
      </div>
      ${this._error ? html`<div class="err">${this._error}</div>` : nothing}
      <div class="cl">
        <button class="iconbtn danger" title="Verwijderen" ?disabled=${this._saving} @click=${() => this._delete(i)}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13"></path></svg></button>
        <button class="iconbtn" title="Annuleren" ?disabled=${this._saving} @click=${this._cancel}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"></path></svg></button>
        <button class="iconbtn primary" title="Opslaan" ?disabled=${this._saving} @click=${() => this._save(i)}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M5 13l4 4L19 7"></path></svg></button>
      </div>
    </div>`;
  }

  private _row(p: any, i: number) {
    const style = `--acc:${ACCENTS[i % ACCENTS.length]};--tint:color-mix(in srgb, ${ACCENTS[i % ACCENTS.length]} 7%, transparent)`;
    if (this._editI === i) {
      return html`<div class="row editing" style=${style}><div class="accent"></div>${this._editor(i)}</div>`;
    }
    const actualW = this._num(p.power_entity);
    return html`<div class="row" style=${style}>
      <div class="accent"></div>
      <div class="name">${p.name ?? `solar_panel:${i}`}</div>
      <div class="actual">${actualW == null ? '–' : Math.round(actualW)}<small> W</small></div>
      <div class="acts"><button class="pencil" title="Bewerken" @click=${() => this._startEdit(i)}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"></path><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"></path></svg></button></div>
    </div>`;
  }

  private _addBtn() {
    return html`<div class="addrow"><button class="addbtn" ?disabled=${this._saving} @click=${this._addPanel}>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M12 5v14M5 12h14"></path></svg>Zonnepaneel toevoegen
    </button></div>`;
  }

  render() {
    const panels = this._panels();
    if (!panels.length) return html`<div class="card"><div class="empty">geen zonnegroepen in de config</div>${this._addBtn()}</div>`;
    return html`<div class="card">
      <div class="head">
        <span class="h-accent"></span><span class="h-name">Naam</span>
        <span class="h-actual">Actueel</span><span class="h-acts"></span>
      </div>
      ${panels.map((p, i) => this._row(p, i))}
      ${this._addBtn()}
    </div>`;
  }
}

if (!customElements.get('zm-solartable')) customElements.define('zm-solartable', ZmSolarTable);
