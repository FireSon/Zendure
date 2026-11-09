# Testing an Anker SOLIX Solarbank with ZenMaster

ZenMaster steers a Solarbank Max AC, XE AC, Max, XE or 4 E5000 Pro over Modbus TCP: operating mode Third-Party Control (3),
then one signed power setpoint. The registers come from Anker's own integration; the driver is tested against a
simulated device only. These steps check it on a real one.

## Preparation

1. Update the Solarbank to firmware 1.0.2.24 or later: older firmware drops local Modbus TCP when the internet
   connection fails (anker-charging/ha-anker-solix-official#76, #150).
2. In the Anker app: device, settings (gear icon), **Three-Party Control Settings**, switch **Modbus TCP** on and note
   the IP address. Give that address a fixed lease in the router.
3. Remove Anker's own integration for this Solarbank, or at least make sure it does not steer: only one Modbus
   client may be connected.
4. Install ZenMaster and turn on debug logging for the Anker part in `configuration.yaml`:

   ```yaml
   logger:
     default: warning
     logs:
       custom_components.zenmaster.anker: debug
   ```

5. Restart Home Assistant. The Solarbank should appear on its own as `MaxAC_xx`, `XEAC_xx`, `Max_xx`, `XE_xx` or
   `E5000Pro_xx`. If it
   does not, add it under Settings, Devices and services, ZenMaster, **Add Anker device**, with its IP address and
   port 502.

## What to check

1. **Readings.** Without ZenMaster steering, compare the sensors in Home Assistant with the Anker app: state of
   charge, `home_output` / `home_input`, temperature, capacity (`kwh`) and the soc limits (`soc_min`, `soc_max`).
2. **Setpoint.** Set the ZenMaster mode to **Manual**, set **Manual** to `+300` W, then to `-300` W. At `+300` the
   Solarbank should discharge, at `-300` charge. Note the seconds until the measured power reaches the setpoint.
3. **Hand back.** Switch the ZenMaster optimisation off. The operating mode in the Anker app must return to the one
   it had before (self-consumption, dynamic pricing, ...).
4. **Restart.** With a setpoint of `+300` W running, restart Home Assistant. The Solarbank must return to its own
   mode when Home Assistant stops: it has no timeout of its own and would otherwise keep discharging.
5. **E5000 Pro only.** With sun on the panels, compare `home_output` with what the Anker app shows going to the
   house: ZenMaster counts the PV on top of the pack power for this model.
6. **Max and XE only.** With sun on the panels, compare `home_output` with what the Anker app shows going to the
   house: ZenMaster counts the pack power only for these models. A difference about the size of the PV power means
   their PV sits behind the pack, like on the E5000 Pro.

## What to send back

- The answers to points 2 to 6.
- The log from the restart through point 4 (the lines with `zenmaster.anker`).
- The model, the firmware version and the serial number.
