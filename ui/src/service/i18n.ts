// Field names, descriptions and section headers come from the integration's own translations/<lang>.json,
// so the backend ships only technical keys. Loaded once per session; the key is the fallback.
let resources: Record<string, string> = {};

export async function loadTranslations(hass: any): Promise<void> {
  const language = hass?.locale?.language || hass?.language || 'en';
  try {
    const res = await hass.callWS({ type: 'frontend/get_translations', language, category: 'config_panel', integration: 'zenmaster' });
    resources = res?.resources ?? {};
  } catch {
    resources = {};  // standalone or an older core: every label falls back to its key
  }
}

// {name} placeholders are filled from params, in the translation and in the fallback alike
export function t(key: string, fallback: string, params: Record<string, string | number> = {}): string {
  const s = resources[`component.zenmaster.config_panel.${key}`] ?? fallback;
  return s.replace(/\{(\w+)\}/g, (m, p) => (p in params ? String(params[p]) : m));
}
