import type { Field } from './types.js';
import { driverList, allDevices } from './service/drivers.js';

// Read a CURRENT value for a settings field. Singleton groups live under config[group];
// drivers/devices/consumers/solar-panels under their own collections.
export function readValue(config: any, entryType: string, field: Field): unknown {
  const fid = field.id;
  if (entryType === 'forecast' && fid === 'solar_panels') return config?.solar_panels;
  if (entryType.startsWith('solar_panel:')) return config?.solar_panels?.[Number(entryType.split(':')[1])]?.[fid];
  const dev = allDevices(config)[entryType];
  if (dev) return dev[fid];
  const drv = driverList(config).find((x: any) => x.id === entryType);
  if (drv) return drv[fid];
  const c = (config?.consumers ?? []).find((x: any) => x.id === entryType);
  if (c) return c[fid];
  return config?.[entryType]?.[fid];  // singleton groups: system/price/forecast
}
