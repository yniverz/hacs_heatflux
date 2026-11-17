# Heat Flux

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![Validate](https://github.com/yniverz/hacs_heatflux/actions/workflows/validate.yml/badge.svg)](https://github.com/yniverz/hacs_heatflux/actions/workflows/validate.yml)
[![Tests](https://github.com/yniverz/hacs_heatflux/actions/workflows/tests.yml/badge.svg)](https://github.com/yniverz/hacs_heatflux/actions/workflows/tests.yml)

The **net heat flow into or out of a room** from its surroundings and the
room's **real temperature**, live, from only the power of the air conditioner
and the room temperature. No insulation values, building model or outdoor
sensor needed for these.

The integration learns a small model of the room (air and mass) from the data
and keeps updating it. With it, it also forecasts the room temperature and
tells how much heat the room's air and its furniture/walls hold.

## How it works

### The room: air and mass

A room isn't one heat store but two:

- **Air** (plus light things: curtains, the surface of furniture): small heat
  capacity, heated directly by the AC, reacts within minutes.
- **Mass** (furniture, wall and floor surfaces): large heat capacity, only
  heated through contact with the air.

That's why the room temperature spikes while the AC heats and drops fast
afterwards: the hot air gives its heat to the colder mass, until both are at
about the same temperature (the "knee"). After that, both cool slowly
together. The spike is the air; the slow curve is the room.

```
air:   C_air  × dT_air/dt  = P_ac,delivered + H × (T_mass − T_air)
mass:  C_mass × dT_mass/dt = H × (T_air − T_mass) + Q_gain
```

| Symbol | Meaning |
|---|---|
| `P_ac,delivered` | AC heat reaching the room (W, negative while cooling). It follows the AC power with a delay: the indoor fan waits for a warm coil. |
| `H` | how well air and mass exchange heat (W/K) |
| `C_air`, `C_mass` | heat capacities (Wh/K) |
| `Q_gain` | **net heat gain**: heat flowing into the room from everything except the AC (walls, windows, air leakage, sun, people, devices). Positive: the room gains heat. Negative: it loses heat. |

### Live estimates

Every 30 seconds a Kalman filter compares the measured temperature with what
the model expects and estimates:

- the **room temperature** (the mass temperature: where the air settles once
  the AC's spike has decayed),
- the **net heat gain**,
- the AC heat that actually reaches the room right now.

During a heating spike the estimated room temperature continues along the
slow curve; after the knee it matches the measured one. It doesn't need a
knee to happen: when the AC restarts while the temperature is still falling
fast, the estimate still knows where it would have settled.

### Learning the model

The integration keeps a per-minute history (default: 4 days, kept across
restarts). Once there are 12 hours with the AC switching on and off a few
times, it fits the model's parameters (air and mass heat capacity, coupling,
heat delay, how fast the net heat gain may change) for the **best 15, 30 and
60 minute forecasts** of the room temperature. It fits again every 6 hours; a
new fit replaces the old one only if it forecasts at least as well. Fitting
runs in the background and takes seconds (a little longer on a Raspberry Pi).

Tested on 8 days of a real room (12000 BTU split AC heating in 20-minute
cycles, 0.1 °C sensor): forecasts are within about 0.2 K at 15 to 60 minutes
(assuming the temperature stays where it is would be off by over 1 K). At
every AC stop, the estimated room temperature was within 0.16 K of where the
room actually settled an hour later.

## Electrical power and COP

Most ACs report the **electrical** power, which has no sign. So:

- **Direction** comes from the AC's climate entity: `hvac_action`
  (heating / cooling / drying / idle / defrosting), otherwise the hvac mode.
  In `auto` mode without `hvac_action` the indoor coil temperature decides.
- **Heat output** = electrical power × COP. When the compressor sensor is
  *off*, the heat output is 0: standby and fan power are not heat.
- **With an indoor coil sensor**, heat only counts once the coil is at least
  5 K warmer than the room (cooling: colder). After the compressor starts,
  the indoor fan waits for a warm coil, and during and after a defrost the
  coil is cold: the compressor draws power, but no heat reaches the room.

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

**How much the COP error matters:** a constant COP error (or a power sensor
that is off by a constant factor) scales the heat capacities, the coupling
and the net heat gain by the same factor. The **estimated room temperature,
the time constants and all forecasts are not affected at all**, and the net
heat gain keeps its shape, sign and zero crossings. What would distort the
result is a COP that changes with the weather, and that is what the COP model
covers. The model can't tell a wrong COP apart on its own: from power and
temperature alone only `C / COP` can be measured.

### Midea-based units (Kältebringer, …)

Many ACs are Midea units inside; their Home Assistant integrations
(`midea_ac_lan`, the built-in `midea`) expose both a **real-time power** and a
**compressor power**. On some models the real-time power is decoded in the
wrong number format (BCD instead of binary) and shows only about a third of
the real value, see
[midea-lan/midea-local#1035](https://github.com/midea-lan/midea-local/issues/1035).
Check: while the compressor runs, the real-time power should be a little
*above* the compressor power (fans, electronics), and a few watts on standby.
If it's lower, use the **compressor power**, or fix the decoding
(`midea_ac_lan`: customize `{"power_analysis_method": …}`; built-in `midea`:
the model needs an entry in the library's exception list).



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
| Room temperature        | A sensor you trust. The AC's own sensor sits in its air stream. |
| AC power                | W or kW, electrical input of the unit or the compressor. Midea-based units: see above. |
| The AC power is         | *Electrical input* (× COP) or *Thermal output*. Thermal output must be signed (+ heating, − cooling) unless a climate entity is given. |
| AC climate entity       | Needed for electrical power (heating or cooling?). |
| Compressor running      | Optional. Off means no heat output. |
| Indoor coil temperature | Optional but recommended: heat only counts once the coil is warm, defrost detection, better COP, direction in auto mode. |

Under **Configure**:

- **Outdoor temperature source** for the COP model.
- **Exclude from learning while on:** binary sensors, switches, input booleans
  or `sun.sun` (while above the horizon), e.g. a window contact. That time
  doesn't count for fitting; the live estimates keep running.
- **Efficiency:** COP model or fixed, and the rated values.
- **Room model:** history length, how often to fit again, and the window of
  the measured temperature rate.

The first values appear after about 12 hours of normal AC operation. Until
then the model status is *Learning*.

### Example: Kältebringer KBR290 (12000 BTU, R290)

```
Room temperature        sensor.<room sensor>            (not the AC's)
AC power                sensor.kaltebringer_12k_compressor_power
The AC power is         Electrical input
AC climate entity       climate.kaltebringer_12k
Compressor running      binary_sensor.kaltebringer_12k_ac_compressor_running
Indoor coil temperature sensor.kaltebringer_12k_indoor_coil_temperature
Outdoor temperature     Open-Meteo   (the unit's outdoor sensor sits in the sun)
Heating COP             4.1 at 7 °C   (SCOP, EPREL 2396170)
Cooling EER             7.0 at 25 °C  (SEER)
```

(`real_time_power` of this unit is affected by the Midea decoding bug above.)

## Entities

For a room named *Living room*:

| Entity | Unit | Notes |
|--------|------|-------|
| `sensor.living_room_net_heat_gain` | W | Net heat flow into the room from its surroundings, without the AC. Positive: gaining heat. Negative: losing heat. Attributes: AC heat delivered right now, total heat capacity. |
| `sensor.living_room_room_temperature_estimated` | °C | The room's real temperature (mass). Attributes: filtered air temperature, rate (K/h), the air temperature in 1 h if the AC stays off. |
| `sensor.living_room_temperature_rate` | K/h | Slope of the measured temperature over the last 20 minutes. Positive: warming. |
| `sensor.living_room_heat_capacity` | Wh/K | Air and mass together. Attributes: air and mass heat capacity, coupling (W/K), time constants, heat delay, forecast errors at 15/30/60 min. |
| `sensor.living_room_air_heat_capacity` | Wh/K | Diagnostic. |
| `sensor.living_room_mass_heat_capacity` | Wh/K | Diagnostic. |
| `sensor.living_room_air_time_constant` | min | Diagnostic: how fast the air relaxes to the mass temperature (the spike decay). |
| `sensor.living_room_ac_heat_delay` | min | Diagnostic: delay between AC power and heat in the room. |
| `sensor.living_room_forecast_error_1_h` | K | Diagnostic: RMS error of the model's 1 h forecasts on the history. |
| `sensor.living_room_ac_heat_output` | W | Thermal power of the AC (signed). Attributes: mode, defrost, coil ready, input power, COP. |
| `sensor.living_room_cop` | – | Diagnostic, electrical power only. Attributes: refrigerant temperatures, η. |
| `sensor.living_room_outdoor_temperature` | °C | Diagnostic, the value the COP model uses. |
| `sensor.living_room_model_status` | – | Learning / Fitting / Ready. Attributes: history hours, AC switches, last fit and its result. |
| `binary_sensor.living_room_room_model_ready` | – | On once the model has been fitted. |
| `button.living_room_fit_room_model_now` | – | Fit right away (e.g. after changing the COP). |
| `button.living_room_reset_room_model` | – | Forget the model and the history. |

## Caveats

- **The model needs the AC to switch on and off now and then.** Constant
  operation for days gives it nothing to learn from; the last fit is kept.
- **Cooling includes dehumidification.** Part of the cooling power condenses
  water instead of cooling air, which the model sees as a bit less cooling.
- **Changing the COP settings** changes the heat output and so the fitted
  heat capacities; press *Fit room model now* afterwards.
- **Portable single-hose ACs** pull outside air into the room while running.
  Part of the heat loss is then caused by the AC itself.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements_test.txt
.venv/bin/pytest
```

The room model (`model.py`: discretization, Kalman filter, fit) and the
history (`history.py`) are plain Python without Home Assistant and are tested
against a simulated room with known parameters (sensor resolution, noise,
heat delay, a varying heat loss).
