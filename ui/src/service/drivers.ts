// The websocket payload is a flat map keyed by device id; every entry carries its class name in `kind`.
export function byKind(config: any, kind: string): Record<string, any> {
  const out: Record<string, any> = {};
  for (const [id, entry] of Object.entries(config ?? {})) if ((entry as any)?.kind === kind) out[id] = entry;
  return out;
}

// the battery fleet: every device the backend marks as a DistributionDevice, whatever its class
export function fleetDevices(config: any): Record<string, any> {
  const out: Record<string, any> = {};
  for (const [id, entry] of Object.entries(config ?? {})) if ((entry as any)?.fleet) out[id] = entry;
  return out;
}
