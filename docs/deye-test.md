# Testing a Deye hybrid inverter for ZenMaster

ZenMaster will steer a Deye three-phase hybrid inverter (SUN-xK-SG01HP3-EU-AM2) through its Solarman logger stick.
Before the driver is written, these steps read the inverter's registers once and test how a time-of-use (TOU)
program limits the battery. Nothing is installed in Home Assistant for this.

The script `tests/deye/deye_dump.py` only reads, unless it is started with `--tou`. The TOU test writes five
registers, and puts every one of them back when it ends, also after an error or Ctrl+C.

## Preparation

1. A PC on the same network as the inverter, with Python 3.10 or newer. The script uses only the standard library.
2. Download [`deye_dump.py`](../tests/deye/deye_dump.py) into an empty folder.
3. Note two things of the logger stick:
   - its IP address, from the router;
   - its serial number, 10 digits, on the label of the stick or in the Solarman app.
4. Stop anything else on the network that reads the stick locally, such as a Solarman integration in Home Assistant.
   The cloud connection of the stick can stay.

## 1. Register dump (read only)

```bash
python deye_dump.py 192.168.1.50 2712345678
```

Use the IP and serial of your stick. The script reads all registers in blocks and writes
`deye_<serial>_<date>_<time>.json` in the same folder. This takes about half a minute. A block the inverter does not
have shows as `error` in the file; that is normal for the battery-module blocks when fewer modules are installed.

Then open the Deye or Solarman app, go to the time-of-use settings and note the **power of program 1** as the app
shows it. Compare it with `"0x009A"` in the file:

- the same number: the scale is `1`;
- the app shows ten times the number in the file: the scale is `10`.

## 2. TOU test (writes, then restores)

The test takes the program that runs at this moment, sets its power to 1000 W, its SOC to 20% and turns its grid
charging off. It runs that program 5 minutes in **Zero Export To CT** and 5 minutes in **Export First**, and logs the
battery and grid power every 10 seconds. Then every register goes back to its old value.

Do it under these conditions, so the result is readable:

- little or no sun (evening), so PV does not mix in;
- the battery above 40%;
- a house load above 1000 W during the whole test (for example an oven or a kettle), so it shows whether the
  1000 W limits the battery.

```bash
python deye_dump.py 192.168.1.50 2712345678 --tou --scale 10
```

Use the scale from step 1. The script first reads again, shows the program it will use and the values it saved, and
asks for a `y` before it writes anything. If the current program ends within 12 minutes it stops; start it again in
the next program window.

During each phase, note in the app:

1. what the battery delivers (W);
2. what goes to or comes from the grid (W);
3. the house load (W).

At the end the script prints `Restored ...` for each of the five registers. If a line says `RESTORE FAILED`, set that
value back by hand in the app: the saved values are printed at the start and are in the file under `"tou"`.

## What to send back

- The JSON file. It holds the dump, the saved values and all samples.
- The scale from step 1.
- Your notes per phase: battery, grid and house load.
- The inverter model as on its label, and the battery (brand, number of modules).
