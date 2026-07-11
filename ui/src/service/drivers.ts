// Drivers are a polymorphic collection: config.drivers = [{ type, id, devices, smartmeters, ... }].
// The devices/smartmeters live per driver; these helpers flatten them across all drivers for the UI.

export function driverList(config: any): any[] {
  return Array.isArray(config?.drivers) ? config.drivers : [];
}

export function primaryDriver(config: any): any | null {
  const l = driverList(config);
  return l.length ? l[0] : null;
}

export function allDevices(config: any): Record<string, any> {
  const out: Record<string, any> = {};
  for (const d of driverList(config))
    for (const [id, dev] of Object.entries(d.devices ?? {})) out[id] = { ...(dev as any), _driver: d.id };
  return out;
}

export function allSmartmeters(config: any): Record<string, any> {
  const out: Record<string, any> = {};
  for (const d of driverList(config))
    for (const [id, sm] of Object.entries(d.smartmeters ?? {})) out[id] = { ...(sm as any), _driver: d.id };
  return out;
}

// Available driver types for the "add driver" control (class name -> label).
export const DRIVER_TYPES: Record<string, string> = {
  ZendureDriver: 'Zendure',
};
