import { LitElement, html, svg, css, nothing } from 'lit';
import { property, state } from 'lit/decorators.js';
import { tokens } from '../tokens.js';
import { api } from '../service/api.js';
import { t } from '../service/i18n.js';
import { readValue } from '../fields.js';
import { ensureHaPickers, haPickersReady } from '../service/ha-pickers.js';
import type { Field, SaveResult } from '../types.js';

// Reusable editable schema-group card. Read-only by default; the pencil toggles edit mode
// (live controls + Annuleer/Opslaan). Read-only fields stay read-only in edit mode. Fields
// are grouped under their section_key. Saves its own {entry:{field:value}} patch and
// dispatches zm-saved. (Expert visibility is a global, server-side setting.)
function sectionLabel(key: string): string {
  const last = (key.split('.').pop() ?? key).replace(/_/g, ' ');
  return t(`section.${last}`, last ? last.charAt(0).toUpperCase() + last.slice(1) : '');
}

export class ZmGroup extends LitElement {
  @property({ attribute: false }) entry = '';
  @property({ attribute: false }) fields: Field[] = [];
  @property({ attribute: false }) config: any = null;
  @property({ attribute: false }) hass: any = null;  // when present, entity/device fields use HA's ha-selector
  @property() heading = '';
  @property() tag = '';
  @property() iconKey = '';
  // When set, edit mode shows a delete button that removes this item from the collection (e.g. "consumers", "drivers").
  @property() collection = '';
  // Single-column edit layout (one field per row) instead of the default two columns.
  @property({ type: Boolean }) singleColumn = false;
  // Key fields the save must always carry, even when they are filtered out of the display.
  @property({ attribute: false }) keyPatch: Record<string, unknown> = {};
  @property({ type: Boolean }) autoEdit = false;  // a row just added: open it and start editing right away

  @state() private _editing = false;
  @state() private _draft: Record<string, unknown> = {};
  @state() private _saving = false;
  @state() private _result: SaveResult | null = null;

  private _isEnum(f: Field) { return !!f.selector?.select; }
  private _isSecret(f: Field) { return f.selector?.text?.type === 'password'; }

  private _fieldValue(fid: string): unknown {
    return this._editing ? this._draft[fid] : readValue(this.config, this.fields.find((f) => f.id === fid)?.entry ?? this.entry, { id: fid } as Field);
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
  // hidden fields stay in this.fields (and in the draft) so visible_when can key on them, but are never drawn;
  // an action button and a one-shot input are only meaningful while editing, so they stay out of the read view
  private get _visible() { return this.fields.filter((f) => !f.hidden && !(f.readonly && f.selector?.entity) && (this._editing || !(f.selector?.button || f.transient)) && this._visibleWhen(f)); }
  private _sections(): [string, Field[]][] {
    const map = new Map<string, Field[]>();
    for (const f of this._visible) { const k = f.section_key ?? ''; if (!map.has(k)) map.set(k, []); map.get(k)!.push(f); }
    return [...map.entries()];
  }

  private _value(f: Field): unknown {
    return this._editing ? this._draft[f.id] : readValue(this.config, f.entry ?? this.entry, f);
  }

  private _startEdit() {
    const d: Record<string, unknown> = {};
    for (const f of this.fields) d[f.id] = readValue(this.config, f.entry ?? this.entry, f);
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

  private async _save() {
    // a patch per entry: a card can carry the fields of a second entry (the price in the ZenMaster card), each goes back where it came from
    const patches: Record<string, Record<string, unknown>> = {};
    for (const f of this.fields) {
      if (f.readonly) continue;
      if (JSON.stringify(this._draft[f.id]) !== JSON.stringify(readValue(this.config, f.entry ?? this.entry, f))) (patches[f.entry ?? this.entry] ??= {})[f.id] = this._draft[f.id];
    }
    if (!Object.keys(patches).length) { this._editing = false; return; }
    for (const f of this.fields) if (f.is_key && patches[f.entry ?? this.entry]) patches[f.entry ?? this.entry][f.id] = readValue(this.config, f.entry ?? this.entry, f);  // the backend finds the object on its key field
    if (patches[this.entry]) Object.assign(patches[this.entry], this.keyPatch);
    this._saving = true;
    this._result = null;
    try {
      // a collection item is an HA subentry addressed by its id; a singleton goes through the settings save
      let res: SaveResult = { ok: true };
      for (const [entry, patch] of Object.entries(patches)) {
        if (res.ok) res = this.collection ? await api.updateSubentry(entry, patch) : await api.saveSchema('edit', entry, patch);
      }
      this._result = res;
      if (res.ok) { this._editing = false; this.dispatchEvent(new CustomEvent('zm-saved', { bubbles: true, composed: true })); }
    } catch (e: any) {
      this._result = { ok: false, errors: [e?.message ?? String(e)] };
    } finally {
      this._saving = false;
    }
  }

  private async _delete() {
    if (!confirm(t('ui.group.confirm_delete', '{name} verwijderen?', { name: this.heading || this.entry }))) return;
    this._saving = true;
    try {
      await api.deleteSubentry(this.entry);
      this.dispatchEvent(new CustomEvent('zm-saved', { bubbles: true, composed: true }));
    } catch { /* ignore */ }
    finally { this._saving = false; }
  }

  static styles = [
    tokens,
    css`
      :host { display: block; }
      .card { background: var(--zm-card); border: 1px solid var(--zm-border); border-radius: 18px; box-shadow: var(--zm-shadow); padding: 15px 16px; animation: zm-rise 0.4s var(--zm-ease) both; }
      .head { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
      .ic { flex: none; width: 34px; height: 34px; border-radius: 11px; display: flex; align-items: center; justify-content: center; }
      h4 { margin: 0; font-family: var(--zm-font-display); font-weight: 600; font-size: var(--zm-fs-l); color: var(--zm-text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .tag { font-family: var(--zm-font-body); font-size: var(--zm-fs-s); font-weight: 700; color: var(--zm-text-faint); background: var(--zm-card-2); padding: 3px 9px; border-radius: 999px; }
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
      .result { font-family: var(--zm-font-body); font-size: var(--zm-fs-m); padding: 8px 11px; border-radius: 10px; margin-bottom: 10px; }
      .result.ok { color: #13967d; background: var(--zm-teal-soft); }
      .result.err { color: #e8645a; background: rgba(232,100,90,0.12); }
      .result ul { margin: 3px 0 0; padding-left: 16px; }
      .shead { font-family: var(--zm-font-body); font-size: var(--zm-fs-s); font-weight: 700; text-transform: uppercase; letter-spacing: 0.07em; color: var(--zm-text-faint); margin: 12px 0 6px; }
      .shead:first-child { margin-top: 2px; }
      .fgrid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; align-items: start; }
      .fgrid.one { grid-template-columns: minmax(0, 1fr); }
      .field { display: flex; align-items: center; gap: 8px; min-width: 0; background: var(--zm-card-2); border-radius: 9px; padding: 2px 10px; box-sizing: border-box; }
      .field .hint { display: block; white-space: normal; font-size: var(--zm-fs-s); font-weight: 400; color: var(--zm-text-faint); line-height: 1.3; margin-top: 2px; max-width: 240px; }
      .field.pick { grid-column: 1 / -1; }
      .field .id { flex: 0 0 33%; min-width: 0; font-family: var(--zm-font-body); font-size: var(--zm-fs-s); font-weight: 700; color: var(--zm-text-dim); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .ctl { flex: 1 1 auto; min-width: 0; display: flex; align-items: center; justify-content: flex-end; }
      input.zin, select.zin, .ro, ha-selector { width: 100%; }
      ha-selector { display: block; }
      input.zin, select.zin { background: var(--zm-card); color: var(--zm-text); border: 1px solid var(--zm-border); border-radius: 7px; font-family: var(--zm-font-body); font-weight: 700; font-size: var(--zm-fs-m); padding: 4px 9px; text-align: right; box-sizing: border-box; }
      select.zin { text-align: left; }
      input.zin:focus, select.zin:focus { outline: none; border-color: var(--zm-primary); }
      .actionrow { grid-column: 1 / -1; display: flex; padding: 2px 0; }
      .ghostbtn { display: inline-flex; align-items: center; gap: 6px; border: 1px solid var(--zm-border); background: transparent; color: var(--zm-text-dim); border-radius: 999px; padding: 6px 13px; font-family: var(--zm-font-body); font-size: var(--zm-fs-s); font-weight: 800; }
      .ghostbtn:hover { color: var(--zm-text); border-color: var(--zm-text-faint); }
      .ghostbtn:disabled { opacity: 0.5; cursor: default; }
      .ghostbtn svg { flex: none; }
      .ro { display: block; background: var(--zm-card); color: var(--zm-text); border: 1px solid var(--zm-border-2); border-radius: 7px; font-family: var(--zm-font-body); font-weight: 700; font-size: var(--zm-fs-m); padding: 4px 9px; text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; box-sizing: border-box; }
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
      system: { bg: 'var(--zm-orange-soft)', color: '#f47a20', path: svg`<path d="M4 7h9M17 7h3M4 17h3M11 17h9"></path><circle cx="15" cy="7" r="2.3"></circle><circle cx="8" cy="17" r="2.3"></circle>` },
      price: { bg: 'var(--zm-amber-soft)', color: '#f5a623', path: svg`<path d="M16.5 8a5 5 0 100 8M4 11h8M4 14h7"></path>` },
      forecast: { bg: 'var(--zm-teal-soft)', color: '#1fb89a', path: svg`<circle cx="12" cy="12" r="4.2"></circle><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5 5l1.4 1.4M17.6 17.6l1.4 1.4M19 5l-1.4 1.4M6.4 17.6l-1.4 1.4"></path>` },
      zendure: { bg: 'var(--zm-teal-soft)', color: '#3aa0c9', path: svg`<rect x="3" y="7" width="15" height="10" rx="2.5"></rect><path d="M21 10v4"></path>` },
      device: { bg: 'var(--zm-teal-soft)', color: '#1fb89a', path: svg`<rect x="3" y="7" width="15" height="10" rx="2.5"></rect><path d="M21 10v4"></path>` },
      hp: { bg: 'var(--zm-orange-soft)', color: '#f47a20', path: svg`<path d="M3 8h10a3 3 0 1 0-3-3M3 12h14a3 3 0 1 1-3 3M3 16h8a2.5 2.5 0 1 1-2.5 2.5"></path>` },
      boiler: { bg: 'var(--zm-amber-soft)', color: '#f5a623', path: svg`<path d="M12 3s6 6 6 11a6 6 0 0 1-12 0c0-5 6-11 6-11z"></path>` },
      ev: { bg: 'var(--zm-teal-soft)', color: '#1fb89a', path: svg`<path d="M13 2L5 13h5l-1 9 9-12h-6z"></path>` },
    };
    const m = map[this.iconKey] ?? { bg: 'var(--zm-orange-soft)', color: '#f47a20', path: svg`<path d="M9 2v6M15 2v6M6 8h12v4a6 6 0 01-12 0z"></path><path d="M12 18v4"></path>` };
    return html`<span class="ic" style="background:${m.bg}"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="${m.color}" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round">${m.path}</svg></span>`;
  }

  private _display(f: Field, v: unknown) {
    if (f.selector?.boolean) {
      const on = v === true;
      return html`<span class="swro"><span class="track" style="position:absolute;inset:0;border-radius:999px;background:${on ? 'var(--zm-primary)' : 'var(--zm-border)'};opacity:0.7"></span><span style="position:absolute;top:3px;left:${on ? '18px' : '3px'};width:15px;height:15px;border-radius:50%;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,0.2)"></span></span>`;
    }
    if (this._isSecret(f)) {
      return html`<span class="ro">${v == null || v === '' ? '-' : '••••••••'}</span>`;
    }
    if (this._isEnum(f)) {
      const opt = (f.selector.select?.options ?? []).find((o) => o.value === String(v));
      return html`<span class="ro" style="text-align:left">${opt ? t(`option.${f.id}.${opt.value}`, opt.label) : (v == null ? '-' : String(v))}</span>`;
    }
    if (f.selector?.device) {
      const id = v == null || v === '' ? '' : String(v);
      const dev = id ? this.hass?.devices?.[id] : null;
      const label = dev ? (dev.name_by_user || dev.name || id) : (id || '-');
      return html`<span class="ro" style="text-align:left" title=${id}>${label}</span>`;
    }
    if (f.selector?.entity) {
      const id = v == null || v === '' ? '' : String(v);
      const label = (id ? this.hass?.states?.[id]?.attributes?.friendly_name : '') || id || '-';
      return html`<span class="ro" style="text-align:left" title=${id}>${label}</span>`;
    }
    return html`<span class="ro">${v == null || v === '' ? '-' : String(v)}</span>`;
  }

  private _control(f: Field) {
    const v = this._draft[f.id];
    if (f.readonly) return this._display(f, v);
    if (this._usesPicker(f)) {
      return html`<ha-selector .hass=${this.hass} .selector=${f.selector} .value=${v ?? ''} @value-changed=${(e: any) => this._set(f.id, e.detail?.value ?? '')}></ha-selector>`;
    }
    // Fallback for the standalone UI, where HA's ha-selector never loads.
    if (f.selector?.boolean) {
      return html`<label class="sw"><input type="checkbox" .checked=${v === true} @change=${(e: Event) => this._set(f.id, (e.target as HTMLInputElement).checked)} /><span class="track"></span><span class="knob"></span></label>`;
    }
    if (this._isEnum(f)) {
      return html`<select class="zin" @change=${(e: Event) => this._set(f.id, (e.target as HTMLSelectElement).value)}>${(f.selector.select?.options ?? []).map((o) => html`<option value=${o.value} ?selected=${String(v) === o.value}>${t(`option.${f.id}.${o.value}`, o.label)}</option>`)}</select>`;
    }
    const num = f.selector?.number;
    if (num) {
      return html`<input class="zin" type="number" step=${num.step ?? 1} .value=${v == null ? '' : String(v)} min=${num.min ?? ''} max=${num.max ?? ''} @change=${(e: Event) => { const raw = (e.target as HTMLInputElement).value; this._set(f.id, raw === '' ? null : Number(raw)); }} />`;
    }
    return html`<input class="zin" type=${this._isSecret(f) ? 'password' : 'text'} autocomplete="new-password" placeholder=${f.selector?.entity ? 'sensor.…' : ''} .value=${v == null ? '' : String(v)} @change=${(e: Event) => this._set(f.id, (e.target as HTMLInputElement).value)} />`;
  }

  private _usesPicker(f: Field) { return (!!f.selector?.entity || !!f.selector?.device) && this.hass && haPickersReady(); }

  // A setting that lives as an HA entity on the device (switch/number/select/time): its value in the config is the
  // entity id. It is shown live and written straight through the entity's service, in read and edit mode alike.
  private _live(f: Field): { eid: string; st: any } | null {
    const v = readValue(this.config, this.entry, f);
    if (typeof v !== 'string' || !/^(switch|number|select|time)\./.test(v)) return null;
    return { eid: v, st: this.hass?.states?.[v] };
  }

  private _liveControl(f: Field, eid: string, st: any) {
    const call = (domain: string, service: string, data: Record<string, unknown>) => this.hass?.callService?.(domain, service, { entity_id: eid, ...data });
    const domain = eid.split('.')[0];
    const state = st?.state;
    const known = state != null && state !== 'unknown' && state !== 'unavailable';
    if (domain === 'switch') {
      return html`<label class="sw"><input type="checkbox" .checked=${state === 'on'} @change=${(e: Event) => call('switch', (e.target as HTMLInputElement).checked ? 'turn_on' : 'turn_off', {})} /><span class="track"></span><span class="knob"></span></label>`;
    }
    if (domain === 'select') {
      return html`<select class="zin" @change=${(e: Event) => call('select', 'select_option', { option: (e.target as HTMLSelectElement).value })}>${(st?.attributes?.options ?? []).map((o: string) => html`<option value=${o} ?selected=${o === state}>${o}</option>`)}</select>`;
    }
    if (domain === 'time') {
      return html`<input class="zin" type="time" .value=${known ? String(state).slice(0, 5) : ''} @change=${(e: Event) => { const v = (e.target as HTMLInputElement).value; if (v) call('time', 'set_value', { time: v.length === 5 ? `${v}:00` : v }); }} />`;
    }
    const num = f.selector?.number;
    return html`<input class="zin" type="number" step=${num?.step ?? st?.attributes?.step ?? 1} min=${st?.attributes?.min ?? ''} max=${st?.attributes?.max ?? ''} .value=${known ? String(state) : ''} @change=${(e: Event) => { const raw = (e.target as HTMLInputElement).value; if (raw !== '') call('number', 'set_value', { value: Number(raw) }); }} />`;
  }

  private _field(f: Field) {
    const btn = f.selector?.button;
    if (btn) {
      const icon = btn.icon === 'mail' ? svg`<path d="M3 7l9 6 9-6M3 5h18v14H3z"></path>` : nothing;
      return html`<div class="actionrow"><button class="ghostbtn" ?disabled=${this._saving} @click=${() => this._command(btn.command)}>${icon === nothing ? nothing : html`<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${icon}</svg>`}${t(`button.${f.id}`, btn.label)}</button></div>`;
    }
    const live = this._live(f);
    const name = t(`field.${f.id}.name`, f.id);
    const hint = t(`field.${f.id}.description`, '');
    return html`<div class="field ${this._usesPicker(f) ? 'pick' : ''}"><span class="id" title=${hint || f.id}>${name}${hint && this._editing ? html`<span class="hint">${hint}</span>` : nothing}</span><span class="ctl">${live ? this._liveControl(f, live.eid, live.st) : this._editing ? this._control(f) : this._display(f, this._value(f))}</span></div>`;
  }

  private async _command(cmd: string) {
    this._saving = true;
    this._result = null;
    try {
      const res = await api.command(cmd);
      if (res?.patch) this._draft = { ...this._draft, ...res.patch };  // e.g. needcode: the code field appears
      if (!res?.ok) this._result = { ok: false, errors: [t('ui.group.command_failed', '{cmd}: mislukt', { cmd })] };
      this.dispatchEvent(new CustomEvent('zm-saved', { bubbles: true, composed: true }));
    } catch (e: any) {
      this._result = { ok: false, errors: [e?.message ?? String(e)] };
    } finally {
      this._saving = false;
    }
  }

  protected firstUpdated() {
    this._loadPickers();
    if (this.autoEdit) this._startEdit();
  }
  protected updated(ch: Map<string, unknown>) { if (ch.has('hass')) this._loadPickers(); }
  private _loadPickers() { if (this.hass && !haPickersReady()) ensureHaPickers().then(() => this.requestUpdate()); }

  private _openEdit = (e: Event) => {
    e.stopPropagation();
    this._startEdit();
  };

  private _headButtons() {
    return this._editing
      ? html`<div class="cluster" @click=${(e: Event) => e.stopPropagation()}>
          ${this.collection ? html`<button class="iconbtn" title=${t('ui.common.delete', 'Verwijderen')} ?disabled=${this._saving} @click=${this._delete}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"></path></svg></button>` : nothing}
          <button class="iconbtn" title=${t('ui.common.cancel', 'Annuleren')} ?disabled=${this._saving} @click=${this._cancel}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"></path></svg></button>
          <button class="iconbtn primary" title=${t('ui.common.save', 'Opslaan')} ?disabled=${this._saving} @click=${this._save}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M5 13l4 4L19 7"></path></svg></button>
        </div>`
      : html`<button class="iconbtn" title=${t('ui.common.edit', 'Bewerken')} @click=${this._openEdit}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"></path><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"></path></svg></button>`;
  }

  private _body() {
    return this._sections().map(([key, fields]) => html`
      ${key ? html`<div class="shead">${sectionLabel(key)}</div>` : nothing}
      <div class="fgrid ${this.singleColumn ? 'one' : ''}">${fields.map((f) => this._field(f))}</div>
    `);
  }

  render() {
    if (!this._visible.length) return nothing;
    const r = this._result;
    const err = r && !r.ok ? html`<div class="result err">${t('ui.group.not_saved', 'niet opgeslagen:')}<ul>${(r.errors ?? []).map((e) => html`<li>${e}</li>`)}</ul></div>` : nothing;
    return html`
      <div class="card">
        <div class="head">
          ${this._icon()}
          <h4>${this.heading || this.entry}</h4>
          ${this.tag ? html`<span class="tag">${this.tag}</span>` : nothing}
          <span class="sp"></span>
          ${this._headButtons()}
        </div>
        ${err}
        ${this._body()}
      </div>
    `;
  }
}

if (!customElements.get('zm-group')) customElements.define('zm-group', ZmGroup);
