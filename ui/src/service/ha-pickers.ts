// HA's ha-selector (and the entity/device pickers it wraps) are lazy-loaded and not registered
// in a custom panel by default. This forces them to load once via the card helpers, so our edit
// forms can render <ha-selector>. If loading fails (e.g. the standalone UI), callers fall back to a text input.

let _loading: Promise<void> | null = null;

export function haPickersReady(): boolean {
  return !!customElements.get('ha-selector');
}

async function _tryLoad(): Promise<void> {
  const helpers = await (window as any).loadCardHelpers?.();
  if (!helpers) return;
  // ha-selector lives in ha-form; different card editors pull it in on different HA versions.
  // Try a few known ones and stop as soon as ha-selector is defined.
  const candidates: any[] = [
    { type: 'entities', entities: [] },
    { type: 'tile', entity: 'sun.sun' },
    { type: 'thermostat', entity: 'sun.sun' },
    { type: 'humidifier', entity: 'sun.sun' },
  ];
  for (const cfg of candidates) {
    if (haPickersReady()) return;
    try {
      const card = await helpers.createCardElement(cfg);
      const ctor: any = card?.constructor;
      if (ctor?.getConfigElement) await ctor.getConfigElement();
    } catch {
      /* try the next candidate */
    }
  }
  // Give any async chunk a brief moment to define the element.
  await Promise.race([customElements.whenDefined('ha-selector'), new Promise((r) => setTimeout(r, 1500))]).catch(() => {});
}

export function ensureHaPickers(): Promise<void> {
  if (haPickersReady()) return Promise.resolve();
  if (!_loading) _loading = _tryLoad().catch(() => {});
  return _loading;
}
