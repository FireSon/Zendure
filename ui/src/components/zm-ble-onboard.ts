import { LitElement, html, css, nothing } from 'lit';
import { property, state } from 'lit/decorators.js';
import { tokens } from '../tokens.js';
import { ZendureBle } from '../service/ble.js';
import { byKind } from '../service/drivers.js';
import { t } from '../service/i18n.js';

// BLE onboarding: provisions a Zendure (WiFi + MQTT broker) over Web Bluetooth, then registers it.
// MQTT/WiFi come from the driver config; the user only picks local vs cloud broker.
export class ZmBleOnboard extends LitElement {
  @property({ attribute: false }) config: any = null;
  @property() bleName = '';
  @property() driverId = '';
  @state() private _mode: 'local' | 'global' = 'local';
  @state() private _log: string[] = [];
  @state() private _busy = false;
  @state() private _done = false;

  private _ble = new ZendureBle();

  private get _driver(): any {
    const drivers = byKind(this.config, 'ZendureDriver');
    return drivers[this.driverId] ?? Object.values(drivers)[0] ?? {};
  }
  private get _iotUrl(): string {
    return this._mode === 'local' ? (this._driver.mqtt_host ?? '') : (this._driver.cloud_url ?? '');
  }
  private get _ssid(): string { return this._driver.wifi_ssid ?? ''; }
  private get _pwd(): string { return this._driver.wifi_psw ?? ''; }

  static styles = [
    tokens,
    css`
      :host { display: block; }
      .pop-bg { position: fixed; inset: 0; background: rgba(20,18,14,0.34); backdrop-filter: blur(2px); display: flex; align-items: center; justify-content: center; z-index: 200; padding: 20px; }
      .pop { background: var(--zm-card); border: 1px solid var(--zm-border); border-radius: 20px; box-shadow: 0 24px 60px rgba(0,0,0,0.28); padding: 20px 22px; width: 380px; max-width: 100%; animation: zm-rise 0.22s ease both; }
      .head { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
      .head h3 { margin: 0; flex: 1 1 auto; font-family: var(--zm-font-display); font-weight: 600; font-size: var(--zm-fs-l); color: var(--zm-text); }
      .x { flex: none; border: none; background: var(--zm-card-2); width: 30px; height: 30px; border-radius: 50%; cursor: pointer; color: var(--zm-text-dim); display: flex; align-items: center; justify-content: center; }
      .row { display: flex; flex-direction: column; gap: 6px; margin-bottom: 10px; }
      .row label { font-family: var(--zm-font-body); font-size: var(--zm-fs-s); font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em; color: var(--zm-text-faint); }
      .seg { display: flex; gap: 8px; }
      .seg button { flex: 1 1 0; border: 1px solid var(--zm-border); background: var(--zm-card-2); color: var(--zm-text-dim); border-radius: 8px; padding: 9px; font-family: var(--zm-font-body); font-weight: 700; font-size: var(--zm-fs-m); cursor: pointer; }
      .seg button.on { background: var(--zm-primary); color: #fff; border-color: var(--zm-primary); }
      .seg button:disabled { opacity: 0.5; cursor: default; }
      .hint { font-family: var(--zm-font-body); font-size: var(--zm-fs-m); color: var(--zm-text-dim); margin: 4px 0 12px; word-break: break-all; }
      .btn { width: 100%; border: none; border-radius: 11px; background: var(--zm-primary); color: #fff; font-family: var(--zm-font-display); font-weight: 600; font-size: var(--zm-fs-m); padding: 11px; cursor: pointer; box-shadow: 0 2px 8px var(--zm-primary-soft); }
      .btn:disabled { opacity: 0.5; cursor: default; }
      .log { margin-top: 12px; max-height: 220px; overflow: auto; background: var(--zm-card-2); border-radius: 10px; padding: 8px 10px; font-family: ui-monospace, monospace; font-size: var(--zm-fs-s); color: var(--zm-text-dim); white-space: pre-wrap; word-break: break-all; }
      .unavail { font-family: var(--zm-font-body); font-size: var(--zm-fs-m); color: #e8645a; }
    `,
  ];

  private _say(m: string) { this._log = [...this._log, m]; }
  private _close() {
    if (this._done) this.dispatchEvent(new CustomEvent('zm-saved', { bubbles: true, composed: true }));  // refresh only on close, not while onboarding
    this.dispatchEvent(new CustomEvent('close', { bubbles: true, composed: true }));
  }

  private async _start() {
    this._busy = true; this._log = []; this._done = false;
    const iotUrl = this._iotUrl, ssid = this._ssid, pwd = this._pwd;
    const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
    const token = Array.from({ length: 16 }, () => chars[Math.floor(Math.random() * chars.length)]).join('');
    const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
    let deviceId = '', deviceSn = '';
    this._ble.onSend = (m) => this._say('→ ' + JSON.stringify(m));
    this._ble.onMessage = (m) => {
      if (m && (m.deviceId || m.deviceSn)) { deviceId = m.deviceId ?? deviceId; deviceSn = m.deviceSn ?? deviceSn; }
      if (m?.method === 'BLESPP') void this._ble.write({ method: 'BLESPP_OK' });
      this._say('← ' + (typeof m === 'string' ? m : JSON.stringify(m)));
    };
    try {
      this._say(t('ui.ble.log_broker', 'Broker ({mode}): {url}', { mode: this._mode, url: iotUrl || t('ui.ble.empty', '(leeg)') }));
      this._say(t('ui.ble.log_connecting', 'Verbinden… (kies het apparaat in de browser)'));
      await this._ble.connect(this.bleName || undefined);
      this._say(t('ui.ble.log_connected', 'Verbonden: {name}', { name: this._ble.name || '?' }));
      await sleep(2000);
      await this._ble.write({ messageId: 101, method: 'deviceInfo', slaveSN: 'jump', timeZone: 'GMT+08:00' });
      await sleep(2000);
      await this._ble.write({ AM: 3, homeId: 64847, iotUrl, messageId: 1002, method: 'token', password: pwd, ssid, timeZone: 'GMT+08:00', token });
      await sleep(2000);
      if (!this._ble.connected) { this._say(t('ui.ble.log_reconnect_station', 'Herverbinden voor station…')); await this._ble.reconnect(); }
      this._say(t('ui.ble.log_station', 'Station-command versturen…'));
      await this._ble.write({ messageId: 1003, method: 'station' });
      this._say(t('ui.ble.log_provisioned', 'Provisioning verstuurd; 25 s wachten terwijl het apparaat omschakelt…'));
      await sleep(25000);
      const deadline = Date.now() + 90000;
      while (!deviceId && Date.now() < deadline) {
        try {
          if (!this._ble.connected) { this._say(t('ui.ble.log_reconnect', 'Herverbinden…')); await this._ble.reconnect(); }
          await this._ble.write({ messageId: '1', method: 'getInfo', timestamp: Date.now() });
        } catch (e: any) {
          this._say(t('ui.ble.log_wait', 'Wacht op herverbinding… ({error})', { error: e?.message ?? e }));
        }
        await new Promise((r) => setTimeout(r, 1500));
      }
      if (!deviceId) throw new Error(t('ui.ble.no_info', 'geen device-info ontvangen'));
      this._say(t('ui.ble.log_device', 'Apparaat: {id}', { id: deviceId + (deviceSn ? ' / ' + deviceSn : '') }));
      this._say(t('ui.ble.log_done', 'Klaar: het apparaat verschijnt zodra het zich op de broker meldt.'));  // the driver builds it from its first broker report
      this._done = true;
    } catch (e: any) {
      this._say(t('ui.ble.log_error', 'Fout: {error}', { error: e?.message ?? String(e) }));
    } finally {
      this._ble.disconnect();
      this._busy = false;
    }
  }

  render() {
    const available = ZendureBle.available();
    return html`
      <div class="pop-bg" @click=${this._close}>
        <div class="pop" @click=${(e: Event) => e.stopPropagation()}>
          <div class="head">
            <h3>${t('ui.ble.title', 'Apparaat koppelen via Bluetooth')}</h3>
            <button class="x" @click=${this._close}><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"></path></svg></button>
          </div>
          ${!available
            ? html`<div class="unavail">${t('ui.ble.unavailable', 'Web Bluetooth is hier niet beschikbaar. Vereist HTTPS (of localhost) en Chrome/Edge.')}</div>`
            : html`
                <div class="row">
                  <label>${t('ui.ble.broker', 'Broker')}</label>
                  <div class="seg">
                    <button class=${this._mode === 'local' ? 'on' : ''} ?disabled=${this._busy} @click=${() => (this._mode = 'local')}>${t('ui.ble.local', 'Lokaal')}</button>
                    <button class=${this._mode === 'global' ? 'on' : ''} ?disabled=${this._busy} @click=${() => (this._mode = 'global')}>${t('ui.ble.cloud', 'Cloud')}</button>
                  </div>
                </div>
                <div class="hint">iotUrl: ${this._iotUrl || t('ui.ble.no_url', '(niet beschikbaar, cloud-sync nog niet gedraaid)')}${this.bleName ? ' · ' + this.bleName : ''}</div>
                <button class="btn" ?disabled=${this._busy} @click=${this._start}>${this._busy ? t('ui.ble.busy', 'Bezig…') : this._done ? t('ui.app.retry', 'Opnieuw') : t('ui.ble.connect', 'Verbinden')}</button>
                ${this._log.length ? html`<div class="log">${this._log.join('\n')}</div>` : nothing}
              `}
        </div>
      </div>
    `;
  }
}

if (!customElements.get('zm-ble-onboard')) customElements.define('zm-ble-onboard', ZmBleOnboard);
