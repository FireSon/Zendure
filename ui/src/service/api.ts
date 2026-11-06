import type { Schema, PlanSeries, SaveResult, HistoryDay, RowAction } from '../types.js';

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
  if (wsSend) return wsSend(`zenmaster/${endpoint}`, payload) as Promise<T>;
  return post<T>(endpoint, payload);
}

export type SchemaAction = 'add' | 'edit' | 'delete';

// get_schema now returns the field definitions AND the current values, both from the live
// coordinator (no config-file read). get_config is gone.
export interface SchemaBundle {
  schema: Schema;
  config: any;
  actions?: Record<string, RowAction[]>;  // the row buttons each consumer offers, in backend order
  tiles?: Record<string, string[]>;  // the entity fields each consumer shows as tiles on its row
}

export const api = {
  getSchema: () => call<SchemaBundle>('get_schema'),
  getPlan: () => call<PlanSeries[]>('get_plan'),
  getHistory: () => call<{ days: HistoryDay[] }>('get_history'),
  // The settings tab (distribution, price, zendure): one singleton edit at a time.
  saveSchema: (action: SchemaAction, section: string, payload?: Record<string, unknown>) =>
    call<SaveResult>('save_schema', { action, section, payload }),
  // Consumers and solar panels are HA config subentries: the title is the name, the patch holds the fields.
  addSubentry: (subentryType: string, title: string, data?: Record<string, unknown>) =>
    call<SaveResult & { subentry_id?: string }>('subentry_add', { subentry_type: subentryType, title, data }),
  updateSubentry: (subentryId: string, patch: Record<string, unknown>) =>
    call<SaveResult>('subentry_update', { subentry_id: subentryId, patch }),
  deleteSubentry: (subentryId: string) => call<SaveResult>('subentry_delete', { subentry_id: subentryId }),
  // the action buttons on a consumer row: start, stop, skip, arm_off, arm_on
  consumerAction: (consumer: string, action: string) => call<SaveResult>('consumer_action', { consumer, action }),
  // a button field in the schema: its selector names the command, the answer may patch the card
  command: (cmd: string, payload?: Record<string, unknown>) => call<{ ok: boolean; patch?: Record<string, unknown> }>(cmd, payload),
  // push: subscribe to "plan updated" events; returns an unsubscribe fn, or null in standalone mode.
  subscribePlan: (onEvent: () => void): Promise<() => void> | null =>
    wsSubscribe ? wsSubscribe(onEvent) : null,
};
