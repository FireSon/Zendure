import type { Field } from './types.js';

// Read a CURRENT value for a settings field. Singleton groups live under config[group];
// consumers/solar-panels under their own collections, devices flat under their id.
export function readValue(config: any, entryType: string, field: Field): unknown {
  const fid = field.id;
  if (entryType.startsWith('solar_panel:')) return (config?.solar_panels ?? []).find((p: any) => p.id === entryType.slice('solar_panel:'.length))?.[fid];
  const c = (config?.consumers ?? []).find((x: any) => x.id === entryType);
  if (c) return c[fid];
  return config?.[entryType]?.[fid];  // singleton groups: system/price/forecast
}
