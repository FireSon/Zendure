// Web Bluetooth client for Zendure onboarding (service a002, c304 write / c305 notify).
// Mirrors the BLE protocol from ZendureBle/Home.razor. The browser talks to the device
// directly; the caller (onboarding wizard) sequences the JSON commands.

const SERVICE = '0000a002-0000-1000-8000-00805f9b34fb';
const CHAR_WRITE = '0000c304-0000-1000-8000-00805f9b34fb';
const CHAR_READ = '0000c305-0000-1000-8000-00805f9b34fb';

export class ZendureBle {
  private device: BluetoothDevice | null = null;
  private writeChar: BluetoothRemoteGATTCharacteristic | null = null;
  onMessage: (msg: any) => void = () => {};  // parsed notify JSON (c305)
  onSend: (obj: any) => void = () => {};  // every outgoing command (c304)

  // Only usable in a secure context (HTTPS/localhost) with a Web Bluetooth browser.
  static available(): boolean {
    return typeof navigator !== 'undefined' && !!navigator.bluetooth && window.isSecureContext;
  }

  // Must be called from a user gesture (the chooser needs transient activation).
  async connect(namePrefix?: string, retries = 3): Promise<void> {
    this.device = await navigator.bluetooth.requestDevice(
      namePrefix ? { filters: [{ namePrefix }], optionalServices: [SERVICE] } : { acceptAllDevices: true, optionalServices: [SERVICE] },
    );
    await this._setup(retries);
  }

  // Reconnect the already-chosen device (no requestDevice, so no user gesture): the device drops
  // BLE after provisioning while it reboots onto WiFi/MQTT.
  async reconnect(retries = 20): Promise<void> {
    if (!this.device) throw new Error('BLE not connected');
    await this._setup(retries);
  }

  private async _setup(retries: number): Promise<void> {
    let lastErr: unknown;
    for (let i = 0; i < retries; i++) {
      try {
        const server = await this.device!.gatt!.connect();
        const service = await server.getPrimaryService(SERVICE);
        const read = await service.getCharacteristic(CHAR_READ);
        read.addEventListener('characteristicvaluechanged', (e) => {
          const v = (e.target as BluetoothRemoteGATTCharacteristic).value;
          if (!v) return;
          const str = new TextDecoder().decode(v);
          try { this.onMessage(JSON.parse(str)); } catch { this.onMessage({ _raw: str }); }
        });
        await read.startNotifications();
        this.writeChar = await service.getCharacteristic(CHAR_WRITE);
        return;
      } catch (e) {
        lastErr = e;
        await new Promise((r) => setTimeout(r, 1000));
      }
    }
    throw lastErr;
  }

  async write(obj: Record<string, unknown>): Promise<void> {
    if (!this.writeChar) throw new Error('BLE not connected');
    this.onSend(obj);
    await this.writeChar.writeValueWithoutResponse(new TextEncoder().encode(JSON.stringify(obj)));
  }

  get name(): string { return this.device?.name ?? ''; }
  get connected(): boolean { return !!this.device?.gatt?.connected; }

  disconnect(): void { this.device?.gatt?.disconnect(); }
}
