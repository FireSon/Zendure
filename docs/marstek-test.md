# Testing a Marstek Venus with ZenMaster

ZenMaster steers a Marstek Venus over Modbus TCP: RS485 control mode on, then `force_mode` with the charge or
discharge power. The registers come from Omnibattery's tables; the driver is tested against a simulated device only.
These steps check it on a real one.

## Preparation

1. Connect the battery for Modbus TCP:
   - Venus E v3, Venus A, Venus D: an Ethernet cable to the battery's LAN port.
   - Venus E v1/v2: an RS485-to-TCP gateway (for example an Elfin EW11) on the battery's RS485 port.
2. Give the battery (or the gateway) a fixed IP address in the router.
3. Remove any other Modbus integration for this battery from Home Assistant: a v3 accepts one Modbus connection at
   a time.
4. Install ZenMaster and turn on debug logging for the Marstek part in `configuration.yaml`:

   ```yaml
   logger:
     default: warning
     logs:
       custom_components.zenmaster.marstek: debug
   ```

5. Restart Home Assistant and add the battery under Settings, Devices and services, ZenMaster, **Add Marstek
   device**: the register generation (`v2` for Venus E v1/v2, `v3` for Venus E v3, `vA` for Venus A, `vD` for
   Venus D), the IP address and the port (502).

## What to check

1. **Readings.** Without ZenMaster steering, compare the device's sensors in Home Assistant with the Marstek app:
   state of charge, `home_output` / `home_input` (power to and from the house), temperature and capacity (`kwh`).
   A wrong generation shows nonsense here.
2. **Sign of the power.** While the battery charges on its own, `home_input` must rise and `home_output` stay 0.
   If it is the other way round, the sign of `ac_power` is wrong.
3. **Setpoint.** Set the ZenMaster mode to **Manual**, set **Manual** to `+300` W, then to `-300` W. At `+300` the
   battery should discharge, at `-300` charge. Note the seconds until the measured power reaches the setpoint, and
   until `home_output` / `home_input` in Home Assistant show it.
4. **Hand back.** Switch the ZenMaster optimisation off. The battery should return to the mode it had before
   (self-consumption, AI, ...). Check the mode in the Marstek app.
5. **Restart.** With a setpoint of `+300` W running, restart Home Assistant. The battery should return to its own
   mode when Home Assistant stops.

## What to send back

- The answers to points 2, 3, 4 and 5.
- The log from the restart through point 5 (the lines with `zenmaster.marstek`).
- The battery model, its generation, and whether it is on Ethernet or behind a gateway.
