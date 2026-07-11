import type { Schema, PlanSeries, SaveResult } from '../types.js';

// Thin client for the UI <-> backend contract. Standalone (tools/ui_server.py) uses HTTP
// POST /api/<cmd>; inside HA the panel installs a websocket transport (same payloads).

type WsSend = (type: string, payload?: Record<string, unknown>) => Promise<any>;
let wsSend: WsSend | null = null;

// Called by the HA panel entry with hass.connection.sendMessagePromise.
export function setWsTransport(fn: WsSend): void {
  wsSend = fn;
}

type WsSubscribe = (onEvent: () => void) => Promise<() => void>;
let wsSubscribe: WsSubscribe | null = null;
// Called by the HA panel entry with hass.connection.subscribeMessage (push). Null in standalone/HTTP mode.
export function setWsSubscribe(fn: WsSubscribe): void {
  wsSubscribe = fn;
}

async function post<T>(endpoint: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api/${endpoint}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const j = await res.json();
      if (j && j.error) detail = String(j.error);
    } catch { /* non-JSON error body */ }
    throw new Error(`${endpoint}: ${detail}`);
  }
  return res.json() as Promise<T>;
}

async function call<T>(endpoint: string, payload?: Record<string, unknown>): Promise<T> {
  if (wsSend) return wsSend(`energymaster/${endpoint}`, payload) as Promise<T>;
  return post<T>(endpoint, payload);
}

export type SchemaAction = 'add' | 'edit' | 'delete';

// get_schema now returns the field definitions AND the current values, both from the live
// coordinator (no config-file read). get_config is gone.
export interface SchemaBundle {
  schema: Schema;
  config: any;
}

export const api = {
  getSchema: () => call<SchemaBundle>('get_schema'),
  getPlan: () => call<PlanSeries[]>('get_plan'),
  // One change at a time: section is a singleton (edit) or a collection ("consumers"/"devices":
  // add/edit/delete); payload carries the fields and, for a collection item, its "id".
  saveSchema: (action: SchemaAction, section: string, payload?: Record<string, unknown>) =>
    call<SaveResult>('save_schema', { action, section, payload }),
  // Long-poll for the MQTT register token of a device being onboarded over BLE.
  registerToken: (device_id: string, timeout?: number) =>
    call<{ token: string | null }>('register_token', { device_id, timeout }),
  // push: subscribe to "plan updated" events; returns an unsubscribe fn, or null in standalone mode.
  subscribePlan: (onEvent: () => void): Promise<() => void> | null =>
    wsSubscribe ? wsSubscribe(onEvent) : null,
};
