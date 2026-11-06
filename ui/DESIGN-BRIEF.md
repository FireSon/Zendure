# ZenMaster UI design brief

Input for the frontend-design step. It captures the dashboard/chart ideas from the
three prior iterations so the design is built from accumulated intent, not a blank
sheet. Aesthetic is locked to "instrument-panel, refined" (Jan's choice). The point of
this brief: carry forward the good ideas, especially EnergyMaster_old's analytics depth,
and be explicit about what is buildable on today's data versus what needs new backend.

## 1. Aesthetic (locked)

Refined instrument-panel, dark. Tokens already in `ui/src/tokens.ts`:
- bg `#0b0e13`, surfaces `#121722`/`#1a212e`, lines `#28303d`/`#3a4554`.
- single sharp accent: lime `#cdff50`. Semantic series: solar `#f5b301`, import `#ff5470`, export `#8b5cf6`, charge `#3aa0ff`, discharge `#25d0a4`, soc lime.
- IBM Plex Mono for labels/numbers (tabular), IBM Plex Sans for body. No external font fetch (offline + HA panel constraint): system fallback stack.
- cards with top-edge highlight + depth, accent glow on active state, subtle reveal-on-load.
Keep this identity. Make it richer and denser, do not restyle to a new theme.

## 2. What we have today (the data contract)

Standalone now, HA panel later. Same 4 request-response endpoints both stages
(`tools/ui_server.py` now, HA `websocket_api` later):
- `get_schema` -> OptimizerField descriptors per entry (system, price, forecast, zendure, each consumer id). Drives settings generically.
- `get_config` / `save_config(patch)`.
- `get_plan` -> list of SolverData series, each `{id, uom, phase, source, values[]}`.

`get_plan` series available right now (single live solve, horizon 96, going to 192):
`price_buy`, `price_sell`, `load`, `solar`, `solar_lp`, `outdoor_temp`, `wind`,
`grid_buy`, `grid_sell`, `bat_charge`, `bat_discharge`, `bat_soc`, and per consumer
`<id>_power` (+ for hp/boiler also `_cop`, `_temp`, `_heat_loss`). Appliances
(dishwasher/dryer/washingmachine) are folded into `load`, not separate series.

We do NOT have yet: historical time series / statistics, a KPI engine, backtest /
scenario / Monte-Carlo, forecast-accuracy tracking, per-Zendure-device live telemetry
in the plan. These gate the richest prior features (see section 5).

Current UI (3 tabs, just reworked): dashboard (6 KPI tiles computed from the single
solve, power-flow stacked bars + dual-axis price, SoC with min/max guides), planning
(price chart, per-device swimlane, 96-row table), settings (schema-driven, editable,
save). Dev-only scenario selector (live/day/winter/...) reads static `solve_*.json`.

## 3. Prior UIs, in one line each

- **EnergyMaster_old** (vanilla-JS config panel + rich Python/WS analytics): thin rendered UI, but the deepest ANALYTICS: a 21-KPI engine across 4 categories with 30-point sparkline history + trend arrows, backtest/scenario/Monte-Carlo, forecast-accuracy (MAE/RMSE/MAPE), granular power sensors. Charts were expected to be built on top (HA Energy Dashboard / ApexCharts cards). This is the richness Jan remembers.
- **ZenMaster** (Lit + ECharts, 5 tabs): richest RENDERED charts. Gantt device-schedule, dual-axis solar forecast (multi-provider + actual overlay + price + now-marker + DataZoom), 24h colored slot-grid timelines (per consumer + baseline), accuracy table. Plus a 12-scenario simulation harness (text-only reports today).
- **Em** (Lit + ECharts, 5 tabs): most polished architecture. Power-flow stacked bars + dual price axis, schedule swimlane (custom renderItem, signed battery intensity, clickable y-axis labels that navigate), KPI tiles with click-to-expand 30-day history, 48h linked-grid forecast (confirmed vs extrapolated prices), 168h 7-day forecast with an EV-window scoring heatmap (markArea ranked by avg price, cheapest night green), Zendure device table, dynamic per-type consumer forms.

## 4. Tabs to design (target)

Dashboard, Planning, Forecast (new), Devices/Zendure (new), Settings. Forecast and
Devices come from Em/ZenMaster and are absent today. Build chart components
horizon-agnostic (96 today, 192 soon, 168h hourly for the 7-day forecast).

## 5. Feature catalogue (the synthesis)

Buildable tags: **Now** = on today's `get_plan`/config; **48h** = once the horizon
extends (data already reaches ~48h); **7d** = needs 7-day price (only solar reaches
168h today, price is day-ahead only); **Backend** = needs new server capability;
**HA-only** = needs live HA entities/statistics (works in the HA panel stage, stub offline).

| Feature | From | What it shows | Buildable |
|---|---|---|---|
| Power-flow stacked bars + dual price axis | Em/current | supply up / draw down per slot, prices on right axis | Now (have it, refine) |
| Energy-flow Sankey (solar -> battery/grid/load) | ZM_old idea | where each kW goes, current slot or day total | Now |
| Per-device swimlane, signed intensity, clickable labels | Em | battery charge(blue)/discharge(green) + consumers, opacity by magnitude, click label -> device | Now (extend current swimlane) |
| Gantt device-schedule (mode bars per device) | ZenMaster | per-device charge/discharge/idle blocks over the horizon | Partial: per-consumer Now; per-Zendure-device needs dispatch series (Backend) |
| 24h colored slot-grid timeline (consumer + baseline) | ZenMaster | 96 slots colored by power level, per consumer + house baseline | Now |
| SoC line with min/max + reserve guides | current | bat_soc with config bounds | Now (have it) |
| Now-marker + DataZoom/pan on time charts | ZenMaster | vertical "now" line, zoom into hours | Now |
| Dashboard KPI tiles | all | net cost, import/export, solar, throughput, soc-min, self-consumption, autarky | Now (cost/energy/autarky computable from series) |
| KPI tile click -> inline 30-day history sparkline | Em/ZM_old | per-KPI trend over time + up/down/stable arrow | Backend (history) / HA-only (recorder statistics) |
| 21-KPI engine, 4 categories (financial, accuracy, self-sufficiency, battery health) | ZM_old | savings, ROI, cycles, degradation, autarky, MAPE... | Backend (KPI engine + history) |
| 48h forecast: linked grids (solar bars top, price lines bottom), confirmed vs extrapolated | Em | day-ahead boundary, synced axisPointer | 48h |
| 7-day forecast + EV-window scoring heatmap (cheapest night green, others ranked red) | Em | which nights are cheapest to charge | Solar 7d: Now; price 7d: 7d (needs price forecast) |
| Solar forecast vs actual, multi-provider, accuracy overlay | ZenMaster | predicted vs measured, per provider, MAPE/safety margin | Backend (accuracy tracking; OptimizerForecast.evaluate_solar exists as a start) |
| Zendure device table (soc/output/input/solar/temp per unit) | Em | fleet status, link to HA entities | HA-only (live telemetry); config list Now |
| Backtest / scenario / Monte-Carlo result views | ZM_old | replay a period, what-if a config, uncertainty bands | Backend (was deliberately dropped in the synthesis; this is the analytics gap) |
| Schema-driven settings, per-type consumer forms | ZM_old/Em | generic OptimizerField forms, save | Now (done) |

## 6. Backend gaps (so design knows what is real vs aspirational)

Order roughly by value Jan attached to it:
1. **KPI engine + history store** (ZM_old): the single biggest "rich dashboard" driver. Needs persisted time series (the two-backend storage: JSON dev / HA statistics prod) and a KPI computation layer. Until then, KPI tiles are single-solve only and sparklines are placeholders.
2. **Forecast-accuracy tracking** (ZM_old/ZenMaster): predicted-vs-actual + MAE/RMSE/MAPE + adaptive safety margin. Partial seed exists (`OptimizerForecast.evaluate_solar`, corrections[24]).
3. **Backtest / scenario / Monte-Carlo** (ZM_old): deliberately dropped in the synthesis plan ("no simulator"). Revisit if the analytics views matter.
4. **Per-Zendure-device dispatch + telemetry series** for the Gantt and device table (HA stage gives live telemetry; the plan currently only carries the aggregate `bat_*`).
5. **7-day price** for the EV-window heatmap beyond day-ahead (needs price forecasting, flagged uncertain).

Design guidance: build the layouts and chart components for these now, fed by the
data we have, with honest "needs history / not yet wired" placeholders where the
backend is missing (Em did exactly this with its placeholders[] field). That
lets the UI land complete-looking and the backend fill in later without a redesign.

## 7. Keep / avoid (from the approved UI plan)

Keep: generic `SolverData`-driven charts (no bespoke per-view payloads), schema-driven
settings, minimal request-response endpoints, horizon-agnostic components.
Avoid (anti-patterns seen in the prior UIs): Em-style bespoke
dashboard_view/forecast_view/schedule_view payloads, hardcoded settings forms
(ZenMaster/Em), key-masking, subscription/push specials. Add a new endpoint
only with explicit approval.

## 8. Wishlist (Jan to fill / correct)

Concrete dashboard/chart ideas Jan wants, beyond the catalogue above:
- (vul aan)
- (vul aan)

Corrections to anything above:
- (vul aan)
