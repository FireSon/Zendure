# Testing a Sessy with ZenMaster

ZenMaster steers a Sessy over the local API of its dongle: strategy `POWER_STRATEGY_API`, then a signed setpoint.
The endpoints come from Sessy's own API documentation; the driver is tested against a simulated dongle only. These
steps check it on a real one.

## Preparation

1. Note the username and password on the sticker of the dongle and give the dongle a fixed IP address in the router.
2. Make sure no other integration steers the Sessy (ha-sessy may stay installed for reading).
3. Install ZenMaster and turn on debug logging for the Sessy part in `configuration.yaml`:

   ```yaml
   logger:
     default: warning
     logs:
       custom_components.zenmaster.sessy: debug
   ```

4. Restart Home Assistant and add the battery under Settings, Devices and services, ZenMaster, **Add Sessy
   battery**: the model (`5kWh` or `10kWh`), the host, the username and the password.

## What to check

1. **Readings.** Without ZenMaster steering, compare the sensors in Home Assistant with the Sessy app: state of
   charge, `home_output` / `home_input` and the system state.
2. **Setpoint.** Set the ZenMaster mode to **Manual**, set **Manual** to `+300` W, then to `-300` W. At `+300` the
   Sessy should deliver, at `-300` charge. Note the seconds until the measured power reaches the setpoint; from
   standby a Sessy can take up to a minute.
3. **Hand back.** Switch the ZenMaster optimisation off. The strategy in the Sessy app must return to the one it had
   before (NOM, ROI, ...).
4. **Restart.** With a setpoint of `+300` W running, restart Home Assistant. The Sessy must return to its own
   strategy when Home Assistant stops.

5. **Strategy overridden.** Note the value of the `strategy_overridden` sensor before ZenMaster steers, while it
   steers, after the hand back, and while the Sessy app or an energy supplier (Frank, Charged, ...) steers the Sessy.
   Sessy does not document who sets it; ZenMaster only shows it for now.

## What to send back

- The answers to points 2 to 5.
- The log from the restart through point 4 (the lines with `zenmaster.sessy`).
- The model and the dongle firmware version.
