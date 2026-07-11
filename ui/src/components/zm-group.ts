import { LitElement, html, css, nothing } from 'lit';
import { property, state } from 'lit/decorators.js';
import { tokens } from '../tokens.js';
import { api } from '../service/api.js';
import { readValue } from '../fields.js';
import { ensureHaPickers, haPickersReady } from '../service/ha-pickers.js';
import type { Field, SaveResult } from '../types.js';

// Reusable editable schema-group card. Read-only by default; the pencil toggles edit mode
// (live controls + Annuleer/Opslaan). Read-only fields stay read-only in edit mode. Fields
// are grouped under their section_key. Saves its own {entry:{field:value}} patch and
// dispatches zm-saved. (Expert visibility is a global, server-side setting.)
function sectionLabel(key: string): string {
  const last = (key.split('.').pop() ?? key).replace(/_/g, ' ');
  return last ? last.charAt(0).toUpperCase() + last.slice(1) : '';
}

export class ZmGroup extends LitElement {
  @property({ attribute: false }) entry = '';
  @property({ attribute: false }) fields: Field[] = [];
  @property({ attribute: false }) config: any = null;
  @property({ attribute: false }) hass: any = null;  // when present, entity/device fields use HA's ha-selector
  @property() heading = '';
  @property() tag = '';
  @property() iconKey = '';
  // Optional read-only body (e.g. a consumer's control layout). When set, the card shows
  // it instead of the field-display while not editing; the pencil still opens the schema form.
  @property({ attribute: false }) controlView: unknown = null;
  // When set, edit mode shows a delete button that removes this item from the collection (e.g. "consumers", "drivers").
  @property() collection = '';
  // Single-column edit layout (one field per row) instead of the default two columns.
  @property({ type: Boolean }) singleColumn = false;

  @state() private _editing = false;
  @state() private _draft: Record<string, unknown> = {};
  @state() private _saving = false;
  @state() private _result: SaveResult | null = null;

  private _usable(f: Field) { return f.edit_type !== 'list' && f.edit_type !== 'command'; }
  // A field with options is a closed enum (e.g. kind), rendered as a dropdown even when its edit_type is 'string'.
  private _isEnum(f: Field) { return f.edit_type === 'dropdown' || (!!f.options && Object.keys(f.options).length > 0); }

  private _fieldValue(fid: string): unknown {
    return this._editing ? this._draft[fid] : readValue(this.config, this.entry, { id: fid } as Field);
  }
  // visible_when = { other_field: value | [values] }: the field shows only when other_field currently matches.
  private _visibleWhen(f: Field): boolean {
    const cond = f.visible_when as Record<string, unknown> | null;
    if (!cond) return true;
    for (const [k, want] of Object.entries(cond)) {
      const cur = String(this._fieldValue(k) ?? '');
      const arr = (Array.isArray(want) ? want : [want]).map(String);
      if (!arr.includes(cur)) return false;
    }
    return true;
  }
  private get _visible() { return this.fields.filter((f) => this._usable(f) && this._visibleWhen(f)); }
  private _sections(): [string, Field[]][] {
    const map = new Map<string, Field[]>();
    for (const f of this._visible) { const k = f.section_key ?? ''; if (!map.has(k)) map.set(k, []); map.get(k)!.push(f); }
    return [...map.entries()];
  }

  private _value(f: Field): unknown {
    return this._editing ? this._draft[f.id] : readValue(this.config, this.entry, f);
  }

  private _startEdit() {
    const d: Record<string, unknown> = {};
    for (const f of this.fields) if (this._usable(f)) d[f.id] = readValue(this.config, this.entry, f);
    this._draft = d;
    this._result = null;
    this._editing = true;
  }

  private _cancel() {
    this._editing = false;
    this._draft = {};
    this._result = null;
  }

  private _set(fid: string, v: unknown) {
    this._draft = { ...this._draft, [fid]: v };
  }

  private async _delete() {
    if (!this.collection) return;
    if (!confirm(`"${this.heading || this.entry}" verwijderen?`)) return;
    this._saving = true;
    this._result = null;
    try {
      const res = await api.saveSchema('delete', this.collection, { id: this.entry });
      if (res.ok) this.dispatchEvent(new CustomEvent('zm-saved', { bubbles: true, composed: true }));
      else this._result = res;
    } catch (e: any) {
      this._result = { ok: false, errors: [e?.message ?? String(e)] };
    } finally {
      this._saving = false;
    }
  }

  private async _save() {
    const patch: Record<string, unknown> = {};
    for (const f of this.fields) {
      if (!this._usable(f) || f.readonly) continue;
      if (JSON.stringify(this._draft[f.id]) !== JSON.stringify(readValue(this.config, this.entry, f))) patch[f.id] = this._draft[f.id];
    }
    if (!Object.keys(patch).length) { this._editing = false; return; }
    this._saving = true;
    this._result = null;
    try {
      const res = await api.saveSchema('edit', this.entry, patch);
      this._result = res;
      if (res.ok) { this._editing = false; this.dispatchEvent(new CustomEvent('zm-saved', { bubbles: true, composed: true })); }
    } catch (e: any) {
      this._result = { ok: false, errors: [e?.message ?? String(e)] };
    } finally {
      this._saving = false;
    }
  }

  static styles = [
    tokens,
    css`
      :host { display: block; }
      .card { background: var(--zm-card); border: 1px solid var(--zm-border); border-radius: 18px; box-shadow: var(--zm-shadow); padding: 15px 16px; animation: zm-rise 0.4s var(--zm-ease) both; }
      .head { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
      .ic { flex: none; width: 34px; height: 34px; border-radius: 11px; display: flex; align-items: center; justify-content: center; }
      h4 { margin: 0; font-family: var(--zm-font-display); font-weight: 600; font-size: 16px; color: var(--zm-text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .tag { font-family: var(--zm-font-body); font-size: 11.5px; font-weight: 700; color: var(--zm-text-faint); background: var(--zm-card-2); padding: 3px 9px; border-radius: 999px; }
      .sp { margin-left: auto; }
      .cluster { display: flex; align-items: center; gap: 6px; }
      button { font: inherit; cursor: pointer; }
      .iconbtn { width: 32px; height: 32px; border-radius: 9px; border: 1px solid var(--zm-border); background: transparent; color: var(--zm-text-dim); display: inline-flex; align-items: center; justify-content: center; cursor: pointer; }
      .iconbtn:hover { color: var(--zm-text); border-color: var(--zm-text-faint); }
      .iconbtn.primary { border: none; background: var(--zm-primary); color: #fff; box-shadow: 0 2px 8px var(--zm-primary-soft); }
      .iconbtn.danger { color: #e8645a; }
      .iconbtn.danger:hover { border-color: #e8645a; }
      .iconbtn:disabled { opacity: 0.45; cursor: default; }
      .iconbtn svg { width: 17px; height: 17px; }
      .result { font-family: var(--zm-font-body); font-size: 12px; padding: 8px 11px; border-radius: 10px; margin-bottom: 10px; }
      .result.ok { color: #13967d; background: var(--zm-teal-soft); }
      .result.err { color: #e8645a; background: rgba(232,100,90,0.12); }
      .result ul { margin: 3px 0 0; padding-left: 16px; }
      .shead { font-family: var(--zm-font-body); font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.07em; color: var(--zm-text-faint); margin: 12px 0 6px; }
      .shead:first-child { margin-top: 2px; }
      .fgrid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; align-items: start; }
      .fgrid.one { grid-template-columns: minmax(0, 1fr); }
      .field { display: flex; align-items: center; gap: 8px; min-width: 0; background: var(--zm-card-2); border-radius: 9px; padding: 7px 10px; min-height: 38px; box-sizing: border-box; }
      .field.pick { grid-column: 1 / -1; }
      .field .id { flex: 0 0 33%; min-width: 0; font-family: var(--zm-font-body); font-size: 11.5px; font-weight: 700; color: var(--zm-text-dim); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .ctl { flex: 1 1 auto; min-width: 0; display: flex; align-items: center; justify-content: flex-end; }
      input.zin, select.zin, .ro, ha-selector { width: 100%; }
      ha-selector { display: block; }
      input.zin, select.zin { background: var(--zm-card); color: var(--zm-text); border: 1px solid var(--zm-border); border-radius: 7px; font-family: var(--zm-font-body); font-weight: 700; font-size: 12.5px; padding: 4px 9px; text-align: right; box-sizing: border-box; }
      select.zin { text-align: left; }
      input.zin:focus, select.zin:focus { outline: none; border-color: var(--zm-primary); }
      .ro { display: block; background: var(--zm-card); color: var(--zm-text); border: 1px solid var(--zm-border-2); border-radius: 7px; font-family: var(--zm-font-body); font-weight: 700; font-size: 12.5px; padding: 4px 9px; text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; box-sizing: border-box; }
      .rosel { display: flex; align-items: center; gap: 6px; }
      .sw { position: relative; display: inline-block; width: 36px; height: 21px; flex: none; }
      .sw input { position: absolute; inset: 0; opacity: 0; margin: 0; cursor: pointer; }
      .sw .track { position: absolute; inset: 0; border-radius: 999px; background: var(--zm-border); transition: background var(--zm-dur) var(--zm-ease); }
      .sw .knob { position: absolute; top: 3px; left: 3px; width: 15px; height: 15px; border-radius: 50%; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,0.2); transition: transform var(--zm-dur) var(--zm-ease); }
      .sw input:checked + .track { background: var(--zm-primary); }
      .sw input:checked + .track + .knob { transform: translateX(15px); }
      .swro { position: relative; display: inline-block; width: 36px; height: 21px; flex: none; }
    `,
  ];

  private _icon() {
    const map: Record<string, { bg: string; color: string; path: any }> = {
      system: { bg: 'var(--zm-orange-soft)', color: '#f47a20', path: html`<path d="M4 7h9M17 7h3M4 17h3M11 17h9"></path><circle cx="15" cy="7" r="2.3"></circle><circle cx="8" cy="17" r="2.3"></circle>` },
      price: { bg: 'var(--zm-amber-soft)', color: '#f5a623', path: html`<path d="M16.5 8a5 5 0 100 8M4 11h8M4 14h7"></path>` },
      forecast: { bg: 'var(--zm-teal-soft)', color: '#1fb89a', path: html`<circle cx="12" cy="12" r="4.2"></circle><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5 5l1.4 1.4M17.6 17.6l1.4 1.4M19 5l-1.4 1.4M6.4 17.6l-1.4 1.4"></path>` },
      zendure: { bg: 'var(--zm-teal-soft)', color: '#3aa0c9', path: html`<rect x="3" y="7" width="15" height="10" rx="2.5"></rect><path d="M21 10v4"></path>` },
      device: { bg: 'var(--zm-teal-soft)', color: '#1fb89a', path: html`<rect x="3" y="7" width="15" height="10" rx="2.5"></rect><path d="M21 10v4"></path>` },
      hp: { bg: 'var(--zm-orange-soft)', color: '#f47a20', path: html`<path d="M3 8h10a3 3 0 1 0-3-3M3 12h14a3 3 0 1 1-3 3M3 16h8a2.5 2.5 0 1 1-2.5 2.5"></path>` },
      boiler: { bg: 'var(--zm-amber-soft)', color: '#f5a623', path: html`<path d="M12 3s6 6 6 11a6 6 0 0 1-12 0c0-5 6-11 6-11z"></path>` },
      ev: { bg: 'var(--zm-teal-soft)', color: '#1fb89a', path: html`<path d="M13 2L5 13h5l-1 9 9-12h-6z"></path>` },
    };
    const m = map[this.iconKey] ?? { bg: 'var(--zm-orange-soft)', color: '#f47a20', path: html`<path d="M9 2v6M15 2v6M6 8h12v4a6 6 0 01-12 0z"></path><path d="M12 18v4"></path>` };
    return html`<span class="ic" style="background:${m.bg}"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="${m.color}" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round">${m.path}</svg></span>`;
  }

  private _display(f: Field, v: unknown) {
    if (f.edit_type === 'boolean') {
      const on = v === true;
      return html`<span class="swro"><span class="track" style="position:absolute;inset:0;border-radius:999px;background:${on ? 'var(--zm-primary)' : 'var(--zm-border)'};opacity:0.7"></span><span style="position:absolute;top:3px;left:${on ? '18px' : '3px'};width:15px;height:15px;border-radius:50%;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,0.2)"></span></span>`;
    }
    if (f.secret) {
      return html`<span class="ro">${v == null || v === '' ? '-' : '••••••••'}</span>`;
    }
    if (this._isEnum(f)) {
      const raw = (f.options ?? {})[String(v)];
      const label = raw == null ? (v == null ? '-' : String(v)) : (raw.includes('.') ? String(v) : raw);
      return html`<span class="ro" style="text-align:left">${label}</span>`;
    }
    if (f.edit_type === 'device') {
      const id = v == null || v === '' ? '' : String(v);
      const dev = id ? this.hass?.devices?.[id] : null;
      const label = dev ? (dev.name_by_user || dev.name || id) : (id || '-');
      return html`<span class="ro" style="text-align:left" title=${id}>${label}</span>`;
    }
    if (f.edit_type === 'entity') {
      const id = v == null || v === '' ? '' : String(v);
      const label = (id ? this.hass?.states?.[id]?.attributes?.friendly_name : '') || id || '-';
      return html`<span class="ro" style="text-align:left" title=${id}>${label}</span>`;
    }
    return html`<span class="ro">${v == null || v === '' ? '-' : String(v)}</span>`;
  }

  private _control(f: Field) {
    const v = this._draft[f.id];
    if (f.readonly) return this._display(f, v);
    if (f.edit_type === 'boolean') {
      return html`<label class="sw"><input type="checkbox" .checked=${v === true} @change=${(e: Event) => this._set(f.id, (e.target as HTMLInputElement).checked)} /><span class="track"></span><span class="knob"></span></label>`;
    }
    if (this._isEnum(f)) {
      const opts = Object.entries(f.options ?? {});
      return html`<select class="zin" @change=${(e: Event) => this._set(f.id, (e.target as HTMLSelectElement).value)}>${opts.map(([k, l]) => html`<option value=${k} ?selected=${String(v) === k}>${l.includes('.') ? k : l}</option>`)}</select>`;
    }
    if (f.edit_type === 'int' || f.edit_type === 'double') {
      return html`<input class="zin" type="number" step=${f.edit_type === 'int' ? '1' : 'any'} .value=${v == null ? '' : String(v)} min=${f.min_value ?? ''} max=${f.max_value ?? ''} @change=${(e: Event) => { const raw = (e.target as HTMLInputElement).value; this._set(f.id, raw === '' ? null : Number(raw)); }} />`;
    }
    if (this._usesPicker(f)) {
      const selector = f.edit_type === 'device' ? { device: {} } : { entity: {} };
      return html`<ha-selector .hass=${this.hass} .selector=${selector} .value=${v ?? ''} @value-changed=${(e: any) => this._set(f.id, e.detail?.value ?? '')}></ha-selector>`;
    }
    if (f.secret) {
      return html`<input class="zin" type="password" autocomplete="new-password" .value=${v == null ? '' : String(v)} @change=${(e: Event) => this._set(f.id, (e.target as HTMLInputElement).value)} />`;
    }
    return html`<input class="zin" type="text" placeholder=${f.edit_type === 'entity' ? 'sensor.…' : ''} .value=${v == null ? '' : String(v)} @change=${(e: Event) => this._set(f.id, (e.target as HTMLInputElement).value)} />`;
  }

  private _usesPicker(f: Field) { return (f.edit_type === 'entity' || f.edit_type === 'device') && this.hass && haPickersReady(); }

  private _field(f: Field) {
    return html`<div class="field ${this._usesPicker(f) ? 'pick' : ''}"><span class="id" title=${f.id}>${f.id}</span><span class="ctl">${this._editing ? this._control(f) : this._display(f, this._value(f))}</span></div>`;
  }

  protected firstUpdated() { this._loadPickers(); }
  protected updated(ch: Map<string, unknown>) { if (ch.has('hass')) this._loadPickers(); }
  private _loadPickers() { if (this.hass && !haPickersReady()) ensureHaPickers().then(() => this.requestUpdate()); }

  render() {
    if (!this._visible.length && this.controlView == null) return nothing;
    const r = this._result;
    return html`
      <div class="card">
        <div class="head">
          ${this._icon()}
          <h4>${this.heading || this.entry}</h4>
          ${this.tag ? html`<span class="tag">${this.tag}</span>` : nothing}
          <span class="sp"></span>
          ${this._editing
            ? html`<div class="cluster">
                ${this.collection ? html`<button class="iconbtn danger" title="Verwijderen" ?disabled=${this._saving} @click=${this._delete}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13"></path></svg></button>` : nothing}
                <button class="iconbtn" title="Annuleren" ?disabled=${this._saving} @click=${this._cancel}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"></path></svg></button>
                <button class="iconbtn primary" title="Opslaan" ?disabled=${this._saving} @click=${this._save}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M5 13l4 4L19 7"></path></svg></button>
              </div>`
            : html`<button class="iconbtn" title="Bewerken" @click=${this._startEdit}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"></path><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"></path></svg></button>`}
        </div>
        ${r && !r.ok ? html`<div class="result err">niet opgeslagen:<ul>${(r.errors ?? []).map((e) => html`<li>${e}</li>`)}</ul></div>` : nothing}
        ${!this._editing && this.controlView != null
          ? this.controlView
          : this._sections().map(([key, fields]) => html`
              ${key ? html`<div class="shead">${sectionLabel(key)}</div>` : nothing}
              <div class="fgrid ${this.singleColumn ? 'one' : ''}">${fields.map((f) => this._field(f))}</div>
            `)}
      </div>
    `;
  }
}

if (!customElements.get('zm-group')) customElements.define('zm-group', ZmGroup);
