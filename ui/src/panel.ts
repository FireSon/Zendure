import { LitElement, html, nothing } from 'lit';
import { property, state } from 'lit/decorators.js';
import { setWsTransport, setWsSubscribe } from './service/api.js';
import { installBaseStyle } from './base-style.js';
import { STATIC_URL } from './service/ha.js';
import './zm-app.js';

// The element HA mounts as the ZenMaster sidebar panel. It receives `hass`, wires the websocket
// transport + theme, then renders the existing standalone app unchanged.
class ZenMasterPanel extends LitElement {
  @property({ attribute: false }) hass: any;
  @property({ attribute: false }) narrow = false;
  @property({ attribute: false }) panel: any;
  @state() private _ready = false;
  @state() private _theme: 'dark' | 'light' = 'light';

  // Light DOM so <zm-app> and its descendants inherit the injected theme custom properties.
  protected createRenderRoot() {
    return this;
  }

  protected willUpdate() {
    if (this.hass && !this._ready) {
      setWsTransport((type, payload) => this.hass.connection.sendMessagePromise({ type, ...(payload ?? {}) }));
      setWsSubscribe((cb) => this.hass.connection.subscribeMessage(cb, { type: 'zenmaster/subscribe_plan' }));
      installBaseStyle(`${STATIC_URL}/fonts`);
      this._ready = true;
    }
    if (this.hass) {
      this._theme = this.hass.themes?.darkMode ? 'dark' : 'light';
      const root = document.documentElement;
      root.setAttribute('data-zm-theme', this._theme);
      if (!root.getAttribute('data-zm-lead')) root.setAttribute('data-zm-lead', 'teal');
    }
  }

  protected render() {
    return this._ready ? html`<zm-app embedded .theme=${this._theme} .hass=${this.hass}></zm-app>` : nothing;
  }
}

customElements.define('zenmaster-panel', ZenMasterPanel);

// HA removes the whole custom panel during suspendWhenHidden, and its rebuild can die in an aborted
// view transition (seen on frontend 20260527.6), leaving ha-panel-custom empty until a manual refresh.
// On tab return: if our panel node is empty, detach + reattach it so its own _wasDisconnected path rebuilds it.
document.addEventListener('visibilitychange', () => {
  if (document.hidden || !location.pathname.startsWith('/zenmaster')) return;
  setTimeout(() => {
    const el = document.querySelector('home-assistant')?.shadowRoot
      ?.querySelector('home-assistant-main')?.shadowRoot
      ?.querySelector('ha-panel-custom') as any;
    if (el && el.panel && el.childElementCount === 0 && el.parentNode) {
      const parent = el.parentNode;
      parent.removeChild(el);
      parent.appendChild(el);
    }
  }, 2000);
});
