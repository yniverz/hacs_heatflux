# Heat Flux

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![Validate](https://github.com/yniverz/hacs_heatflux/actions/workflows/validate.yml/badge.svg)](https://github.com/yniverz/hacs_heatflux/actions/workflows/validate.yml)
[![Tests](https://github.com/yniverz/hacs_heatflux/actions/workflows/tests.yml/badge.svg)](https://github.com/yniverz/hacs_heatflux/actions/workflows/tests.yml)

The **net heat flow into or out of a room**, in watts, live. It uses only the
power of the air conditioner and the room temperature. You don't need
insulation values, a building model or an outdoor sensor for the heat flow
itself.

The room's **heat capacity** is learned automatically from the moments the AC
power jumps (compressor starts, stops, big modulation steps).

## How it works

Energy balance of the room:

```
C × dT/dt = P_ac − Q_loss      →      Q_loss = P_ac − C × dT/dt
```

| Symbol   | Meaning                                                          |
|----------|------------------------------------------------------------------|
| `P_ac`   | heat the AC puts into the room (W, negative while cooling)       |
| `dT/dt`  | how fast the room temperature changes (K/h)                      |
| `C`      | effective heat capacity of the room (Wh/K), learned              |
| `Q_loss` | **net** heat flow out of the room: walls, windows, air leakage, sun, people and devices together. Positive: the room loses heat. Negative: it gains heat. |

### Learning the heat capacity

`Q_loss` changes slowly. Across a sudden step of the AC power it stays about
the same, so

```
C = ΔP_ac / Δ(dT/dt)
```

A **step event** works like this:

1. **Waiting.** The AC power was stable (±10 %, at least ±60 W) for 20 minutes
   and then leaves that band. The temperature slope of those 20 minutes (linear
   regression, which copes with a 0.1 °C sensor) is the slope *before*.
2. **Settling.** The first 4 minutes after the step are skipped: air flow
   settles and inverters ramp up.
3. **Measuring.** The next 15 minutes give the slope *after*. The power must
   be stable in there and at least 300 W away from before, otherwise the event
   is dropped.

Each valid event gives one `C`. The last 30 are kept across restarts. The
heat capacity is their median after dropping outliers.

The measuring window is kept short on purpose. The longer you measure after a
step, the more walls and furniture take part and the bigger `C` gets. The
short window matches the timescale of the live calculation.

### Live calculation

Every 30 seconds, over the last 20 minutes:

```
net heat loss = mean(P_ac) − C × slope(T)
```

The AC power is averaged over the same window as the temperature slope so both
belong to the same time.

## Electrical power and COP

Most ACs report the **electrical** power, which has no sign. So:

- **Direction** comes from the AC's climate entity: `hvac_action`
  (heating / cooling / drying / idle / defrosting), otherwise the hvac mode.
  In `auto` mode without `hvac_action` the indoor coil temperature decides.
- **Heat output** = electrical power × COP. When the compressor sensor is
  *off*, the heat output is 0: standby and fan power are not heat.

### The COP model

A heat pump reaches a fairly constant share η of the Carnot limit. That limit
depends on the refrigerant temperatures:

| Mode    | Carnot COP                   | condensing          | evaporating         |
|---------|------------------------------|---------------------|---------------------|
| heating | `T_cond / (T_cond − T_evap)` | coil or room + 15 K | outdoor − 6 K       |
| cooling | `T_evap / (T_cond − T_evap)` | outdoor + 12 K      | coil or room − 12 K |

(temperatures in kelvin)

- **η** is fixed once from the rated values you enter at their reference
  temperatures.
- **The COP** then follows the room, the outdoor and, if configured, the
  **indoor coil** temperature. The coil is the real condensing temperature
  while heating and the real evaporating temperature while cooling, so the
  model also follows part load. Results are clamped to 1–10.
- **Outdoor temperature** comes from [Open-Meteo](https://open-meteo.com) for
  the Home Assistant location by default: no account, updated every 15
  minutes. You can pick a sensor or a weather entity instead. Avoid AC
  outdoor-unit sensors that sit in the sun.
- **Without an outdoor temperature** (or with COP *Fixed*), the rated values
  are used as they are.

**Where to get the values:** every AC sold in the EU has an energy label with
**SCOP** (heating) and **SEER** (cooling). You can also look it up in
[EPREL](https://eprel.ec.europa.eu). The seasonal values roughly correspond
to about **7 °C** (SCOP) and **25 °C** (SEER) outdoors, which are the
defaults. If you have EN 14511 rated COP / EER instead, use 7 °C and 35 °C.

**How much the COP error matters:** a constant COP error scales `C` and the
net heat loss by the same factor. The curve shape, the sign and the zero
crossings stay correct. What would distort the result is a COP that changes
with the weather, and that is what the model covers. The heat capacity can't
tell a wrong COP apart on its own: with only these two sensors the method
measures `C / COP`.

## Installation

### HACS (custom repository)

1. In Home Assistant open **HACS**.
2. Open the menu (⋮, top right) → **Custom repositories**.
3. Add `https://github.com/yniverz/hacs_heatflux` with type **Integration**.
4. Search for **Heat Flux**, open it and click **Download**.
5. Restart Home Assistant.

### Manual

Copy `custom_components/heatflux` into `<config>/custom_components/` and
restart Home Assistant.

## Setup

**Settings → Devices & services → Add integration → Heat Flux**, one entry
per room.

| Field                   | Notes |
|-------------------------|-------|
| Room temperature        | A sensor you trust, away from the AC's air flow. Not the AC's own sensor. |
| AC power                | W or kW. Preferably the whole unit's electrical power: rated COP / SEER values include the fans. |
| The AC power is         | *Electrical input* (× COP) or *Thermal output*. Thermal output must be signed (+ heating, − cooling) unless a climate entity is given. |
| AC climate entity       | Needed for electrical power (heating or cooling?). |
| Compressor running      | Optional. Off means no heat output. |
| Indoor coil temperature | Optional. Better COP, defrost detection, direction in auto mode. |

Under **Configure**:

- **Outdoor temperature source.**
- **Pause calibration while on:** binary sensors, switches, input booleans or
  `sun.sun` (paused while the sun is above the horizon). For example a window
  contact, or sun on the window.
- **Efficiency:** COP model or fixed, and the rated values.
- **Heat capacity calibration:**
  - thresholds and windows, see above;
  - which events count (heating and cooling, or only one of them);
  - how many valid events count as *calibrated*.
- **Live calculation:**
  - the averaging window;
  - a **manual heat capacity** (0 = the learned one).

### Example: Kältebringer KBR290 (12000 BTU, R290)

```
Room temperature        sensor.<room sensor>            (not the AC's)
AC power                sensor.kaltebringer_12k_real_time_power
The AC power is         Electrical input
AC climate entity       climate.kaltebringer_12k
Compressor running      binary_sensor.kaltebringer_12k_ac_compressor_running
Indoor coil temperature sensor.kaltebringer_12k_indoor_coil_temperature
Outdoor temperature     Open-Meteo   (the unit's outdoor sensor sits in the sun)
Heating COP             4.1 at 7 °C   (SCOP, EPREL 2396170)
Cooling EER             7.0 at 25 °C  (SEER)
```

## Entities

For a room named *Living room*:

| Entity | Unit | Notes |
|--------|------|-------|
| `sensor.living_room_net_heat_loss` | W | Net heat flow out of the room; negative: the room gains heat. Needs a heat capacity. |
| `sensor.living_room_temperature_rate` | K/h | Temperature slope over the averaging window. |
| `sensor.living_room_heat_capacity` | Wh/K | Heat capacity in use. Attributes: events, valid events, std dev, heating / cooling medians, last event. |
| `sensor.living_room_ac_heat_output` | W | Thermal power of the AC (signed). Attributes: mode, defrost, input power, COP. |
| `sensor.living_room_cop` | – | Diagnostic, electrical power only. Attributes: refrigerant temperatures, η. |
| `sensor.living_room_outdoor_temperature` | °C | Diagnostic, the value the COP model uses. |
| `sensor.living_room_calibration_status` | – | Waiting / Settling / Measuring / Paused. Attributes: last result and why events were dropped. |
| `binary_sensor.living_room_heat_capacity_calibrated` | – | On after enough valid events, or with a manual heat capacity. |
| `button.living_room_reset_calibration` | – | Forget all learned events. |

## Caveats

- **Cooling includes dehumidification.** Part of the cooling power condenses
  water instead of cooling air, so cooling events tend to give a smaller `C`.
  Heating and cooling medians are shown separately, and you can limit
  calibration to heating.
- **The sun or an opened window during an event distorts `C`.** Use the pause
  entities.
- **Defrost cycles** (heating, coil colder than the room, or `hvac_action:
  defrosting`) count as 0 W and interrupt calibration.
- **Portable single-hose ACs** pull outside air into the room while running.
  Part of the "loss" is then caused by the AC itself.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements_test.txt
.venv/bin/pytest
```

The step detection and heat capacity estimation are plain Python in
`custom_components/heatflux/physics.py` and are tested with synthetic rooms
(sensor resolution, noise, ramps, gaps, pauses).
