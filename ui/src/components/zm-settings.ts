import { LitElement, html, css } from 'lit';
import { property } from 'lit/decorators.js';
import { tokens } from '../tokens.js';
import type { Schema } from '../types.js';
import { readValue } from '../fields.js';
import { t } from '../service/i18n.js';
import './zm-group.js';

// Per-group metadata for the headline groups. Each card is an independently editable
// zm-group (readonly default + pencil -> edit). Used by the Settings tab.
export const GROUP_META: Record<string, { title: string; tag: string; icon: string }> = {
  distribution: { title: 'ZenMaster', tag: 'sturing', icon: 'system' },
  zendure: { title: 'Zendure', tag: 'driver', icon: 'zendure' },
};

export class ZmSettings extends LitElement {
  @property({ attribute: false }) schema: Schema | null = null;
  @property({ attribute: false }) config: any = null;
  @property({ attribute: false }) hass: any = null;

  static styles = [
    tokens,
    css`
      :host { display: block; }
      .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(500px, 1fr)); gap: 14px; }
      .empty { padding: 48px; text-align: center; color: var(--zm-text-faint); font-family: var(--zm-font-body); }
    `,
  ];

  render() {
    if (!this.schema) return html`<div class="empty">${t('ui.settings.loading', 'Schema laden…')}</div>`;
    const groups = ['distribution', 'zendure'].filter((k) => (this.schema![k]?.length ?? 0) > 0);
    if (!groups.length) return html`<div class="empty">${t('ui.settings.empty', 'Geen instellingen in het schema.')}</div>`;
    return html`
      <div class="grid">
        ${groups.map((k) => {
          const m = GROUP_META[k] ?? { title: k, tag: '', icon: '' };
          const fields = this.schema![k].filter((f) => !f.is_key && !f.expert && !(f.readonly && f.selector?.entity));
          // the ZenMaster card carries the dynamic price in a section of its own; those fields read and save on the price entry, its key rides along hidden
          if (k === 'distribution') fields.push(...(this.schema!['price'] ?? []).filter((f) => !f.expert && !(f.readonly && f.selector?.entity)).map((f) => ({ ...f, entry: 'price', section_key: 'price', hidden: f.hidden || f.is_key })));
          const keyField = this.schema![k].find((f) => f.is_key);
          const keyPatch = keyField ? { [keyField.id]: readValue(this.config, k, keyField) } : {};
          return html`<zm-group .entry=${k} .fields=${fields} .config=${this.config} .hass=${this.hass} .keyPatch=${keyPatch} heading=${t(`ui.group.${k}.title`, m.title)} tag=${t(`ui.group.${k}.tag`, m.tag)} iconKey=${m.icon}></zm-group>`;
        })}
      </div>
    `;
  }
}

if (!customElements.get('zm-settings')) customElements.define('zm-settings', ZmSettings);
