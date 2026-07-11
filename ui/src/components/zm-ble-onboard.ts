import { LitElement, html, css, nothing } from 'lit';
import { property, state } from 'lit/decorators.js';
import { tokens } from '../tokens.js';
import { api } from '../service/api.js';
import { ZendureBle } from '../service/ble.js';
import { driverList } from '../service/drivers.js';

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
    const l = driverList(this.config);
    return l.find((d: any) => d.id === this.driverId) ?? l[0] ?? {};
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
      .head h3 { margin: 0; flex: 1 1 auto; font-family: var(--zm-font-display); font-weight: 600; font-size: 17px; color: var(--zm-text); }
      .x { flex: none; border: none; background: var(--zm-card-2); width: 30px; height: 30px; border-radius: 50%; cursor: pointer; color: var(--zm-text-dim); display: flex; align-items: center; justify-content: center; }
      .row { display: flex; flex-direction: column; gap: 6px; margin-bottom: 10px; }
      .row label { font-family: var(--zm-font-body); font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em; color: var(--zm-text-faint); }
      .seg { display: flex; gap: 8px; }
      .seg button { flex: 1 1 0; border: 1px solid var(--zm-border); background: var(--zm-card-2); color: var(--zm-text-dim); border-radius: 8px; padding: 9px; font-family: var(--zm-font-body); font-weight: 700; font-size: 13px; cursor: pointer; }
      .seg button.on { background: var(--zm-primary); color: #fff; border-color: var(--zm-primary); }
      .seg button:disabled { opacity: 0.5; cursor: default; }
      .hint { font-family: var(--zm-font-body); font-size: 12px; color: var(--zm-text-dim); margin: 4px 0 12px; word-break: break-all; }
      .btn { width: 100%; border: none; border-radius: 11px; background: var(--zm-primary); color: #fff; font-family: var(--zm-font-display); font-weight: 600; font-size: 14px; padding: 11px; cursor: pointer; box-shadow: 0 2px 8px var(--zm-primary-soft); }
      .btn:disabled { opacity: 0.5; cursor: default; }
      .log { margin-top: 12px; max-height: 220px; overflow: auto; background: var(--zm-card-2); border-radius: 10px; padding: 8px 10px; font-family: ui-monospace, monospace; font-size: 11px; color: var(--zm-text-dim); white-space: pre-wrap; word-break: break-all; }
      .unavail { font-family: var(--zm-font-body); font-size: 13px; color: #e8645a; }
    `,
  ];

  private _say(m: string) { this._log = [...this._log, m]; }
  private _close() {
    if (this._done) this.dispatchEvent(new CustomEvent('zm-saved', { bubbles: true, composed: true }));  // refresh pas bij sluiten, niet tijdens
    this.dispatchEvent(new CustomEvent('close', { bubbles: true, composed: true }));
  }

  private async _start() {
    this._busy = true; this._log = []; this._done = false;
    const iotUrl = this._iotUrl, ssid = this._ssid, pwd = this._pwd;
    let deviceId = '', deviceSn = '';
    this._ble.onSend = (m) => this._say('→ ' + JSON.stringify(m));
    this._ble.onMessage = (m) => {
      if (m && (m.deviceId || m.deviceSn)) { deviceId = m.deviceId ?? deviceId; deviceSn = m.deviceSn ?? deviceSn; }
      this._say('← ' + (typeof m === 'string' ? m : JSON.stringify(m)));
    };
    try {
      this._say(`Broker (${this._mode}): ${iotUrl || '(leeg)'}`);
      this._say('Verbinden… (kies het apparaat in de browser)');
      await this._ble.connect(this.bleName || undefined);
      this._say('Verbonden: ' + (this._ble.name || '?'));
      await this._ble.write({ messageId: 101, method: 'deviceInfo', slaveSN: 'jump', timeZone: 'GMT+08:00' });
      await this._ble.write({ AM: 3, homeId: 64847, iotUrl, messageId: 1002, method: 'token', password: pwd, ssid, timeZone: 'GMT+08:00' });
      if (!this._ble.connected) { this._say('Herverbinden voor station…'); await this._ble.reconnect(); }
      this._say('Station-command versturen…');
      await this._ble.write({ messageId: 1003, method: 'station' });
      this._say('Provisioning verstuurd, wachten op apparaat…');
      const deadline = Date.now() + 90000;
      while (!deviceId && Date.now() < deadline) {
        try {
          if (!this._ble.connected) { this._say('Herverbinden…'); await this._ble.reconnect(); }
          await this._ble.write({ messageId: '1', method: 'getInfo', timestamp: Date.now() });
        } catch (e: any) {
          this._say('Wacht op herverbinding… (' + (e?.message ?? e) + ')');
        }
        await new Promise((r) => setTimeout(r, 1500));
      }
      if (!deviceId) throw new Error('geen device-info ontvangen');
      this._say('Device: ' + deviceId + (deviceSn ? ' / ' + deviceSn : ''));
      this._say('Wachten op MQTT-registratie…');
      const { token } = await api.registerToken(deviceId, 60);
      this._say('Register-token: ' + (token ?? '(geen)'));
      if (token) {
        try {
          if (!this._ble.connected) await this._ble.reconnect();
          await this._ble.write({ AM: 3, homeId: 64847, iotUrl, messageId: 1002, method: 'token', password: pwd, ssid, timeZone: 'GMT+08:00', token });
          this._say('Register-token teruggeschreven.');
        } catch (e: any) {
          this._say('Token-writeback mislukt: ' + (e?.message ?? e));
        }
      } else {
        this._say('Geen register-token (timeout); device mogelijk al gekoppeld.');
      }
      await api.saveSchema('add', 'devices', { id: deviceId, sn: deviceSn, driver: this.driverId });
      this._say('Klaar, apparaat toegevoegd.');
      this._done = true;
    } catch (e: any) {
      this._say('Fout: ' + (e?.message ?? String(e)));
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
            <h3>Apparaat koppelen via Bluetooth</h3>
            <button class="x" @click=${this._close}><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"></path></svg></button>
          </div>
          ${!available
            ? html`<div class="unavail">Web Bluetooth is hier niet beschikbaar. Vereist HTTPS (of localhost) en Chrome/Edge.</div>`
            : html`
                <div class="row">
                  <label>Broker</label>
                  <div class="seg">
                    <button class=${this._mode === 'local' ? 'on' : ''} ?disabled=${this._busy} @click=${() => (this._mode = 'local')}>Lokaal</button>
                    <button class=${this._mode === 'global' ? 'on' : ''} ?disabled=${this._busy} @click=${() => (this._mode = 'global')}>Cloud</button>
                  </div>
                </div>
                <div class="hint">iotUrl: ${this._iotUrl || '(niet beschikbaar — cloud-sync nog niet gedraaid)'}${this.bleName ? ' · ' + this.bleName : ''}</div>
                <button class="btn" ?disabled=${this._busy} @click=${this._start}>${this._busy ? 'Bezig…' : this._done ? 'Opnieuw' : 'Verbinden'}</button>
                ${this._log.length ? html`<div class="log">${this._log.join('\n')}</div>` : nothing}
              `}
        </div>
      </div>
    `;
  }
}

if (!customElements.get('zm-ble-onboard')) customElements.define('zm-ble-onboard', ZmBleOnboard);
