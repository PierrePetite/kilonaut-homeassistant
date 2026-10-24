# Kilonaut — Home Assistant integration

[![hacs][hacs-badge]][hacs] [![Validate](https://github.com/PierrePetite/kilonaut-homeassistant/actions/workflows/validate.yml/badge.svg)](https://github.com/PierrePetite/kilonaut-homeassistant/actions/workflows/validate.yml)

**Automatically track your EV wallbox charging in [Kilonaut](https://kilonaut.de) — straight from Home Assistant.** Pick your wallbox power and energy sensors, and every home charging session (kWh, duration, live power curve) lands in Kilonaut on its own. No YAML, no automations. Optionally push your odometer for cost‑per‑100 km and consumption.

Kilonaut is a cost tracker for electric‑vehicle drivers: it collects all your **charging costs and mileage in one place** — home wallbox charging and public charging across every provider (Ionity, EnBW, Aral pulse, Tesla, …). This integration is the hands‑off way to feed your **home charging** into it.

---

## What it does

- 🔌 **Detects charging automatically** from your wallbox power sensor (start/stop by power draw).
- ⚡ **Live "charging now" tile** in Kilonaut, including a charging‑power curve, while a session runs.
- 🧮 **Creates a finished session** at the end — energy is the delta of your cumulative energy counter, so it's exact.
- 🏠 **Home tariff pricing** — home charges are priced automatically by Kilonaut from your saved electricity price.
- 🚗 **Optional odometer push** — send an odometer sensor to get cost per 100 km and kWh/100 km.
- 🧩 **UI setup** — add it, paste a token, pick two sensors, done. Nothing to edit by hand.

Works with **any wallbox that exposes a power and an energy sensor in Home Assistant** — for example a [Shelly](https://kilonaut.de/hilfe/shelly) Pro EM / Plus 1PM measuring your charger, a dedicated EVSE integration, or a smart plug.

## Requirements

- Home Assistant **2024.6** or newer.
- A free **Kilonaut** account — create a token under **Settings → API & Home Assistant** at [kilonaut.de](https://kilonaut.de).
- A **power** sensor (W) and a **cumulative energy** sensor (kWh) for your wallbox.

## Installation

### Via HACS (recommended)

1. In Home Assistant, open **HACS → Integrations**.
2. Top‑right menu **⋮ → Custom repositories**.
3. Add `https://github.com/PierrePetite/kilonaut-homeassistant` with category **Integration**.
4. Install **Kilonaut**, then **restart Home Assistant**.

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=PierrePetite&repository=kilonaut-homeassistant&category=integration)

### Manual

Copy `custom_components/kilonaut` into your Home Assistant `config/custom_components/` folder and restart.

## Configuration

1. **Settings → Devices & Services → Add Integration → “Kilonaut”.**
2. Paste your **API token** (validated instantly against your account).
3. Choose your sensors:
   - **Power sensor (W)** — instantaneous charging power.
   - **Energy sensor (kWh, cumulative)** — the wallbox's total energy counter.
   - **Odometer sensor (km)** — *optional*, for mileage metrics.
   - **Charging threshold** — power above which a charge counts as active (default 1400 W).
   - **Location type** — usually *Home*.

You can change any of these later under the integration's **Configure** button.

## How it works

The integration runs **entirely on your Home Assistant** and only calls Kilonaut's public REST API over HTTPS:

- While power is above the threshold, it sends a live heartbeat every couple of minutes (`POST /charging/status`) so Kilonaut shows the "charging now" tile and power curve.
- When power stays below the threshold for 5 minutes, it posts the finished session (`POST /sessions`) with the exact kWh (energy‑counter delta) and duration. A shorter pause — opening the car, cell balancing, a brief dip — continues the same charge instead of splitting it.
- **Robust against flaky meters.** The energy counter is followed continuously, so a charge that starts while the meter is briefly unavailable (common with Shelly devices reconnecting to Wi‑Fi) still gets its exact kWh.
- **No charge is lost when Kilonaut is unreachable.** A finished session is saved on your Home Assistant before it is sent, and retried every 5 minutes (and after a restart) until Kilonaut accepts it — for up to 30 days. Retries are idempotent, so a session is never duplicated.
- If an odometer sensor is configured, its reading is pushed periodically and at the end of each charge (`POST /odometer`).

Your token is stored by Home Assistant and sent only to `kilonaut.de`. No charging or vehicle data leaves your instance except what you see above.

## Troubleshooting

- **Nothing shows up:** confirm the power sensor reports Watts and rises above the threshold while charging, and that the energy sensor is a *cumulative* counter in kWh.
- **Wrong device fields (Shelly):** make sure you selected the *power* and *energy* entities, not a switch state. See the [Shelly guide](https://kilonaut.de/hilfe/shelly).
- **Token rejected:** create a fresh token in Kilonaut → Settings → API & Home Assistant.
- **"ended without a reading" in the log:** the energy sensor was unavailable for the whole charge. Check that it reports a cumulative kWh value.
- **"charge(s) not delivered yet" in the log:** Kilonaut was unreachable or the token was rejected. Nothing to do if it was an outage; after a new token, waiting charges are sent on the next retry.
- Enable debug logs by adding to `configuration.yaml`:
  ```yaml
  logger:
    logs:
      custom_components.kilonaut: debug
  ```

## Links

- 🌐 Website: **https://kilonaut.de**
- 📖 Help & REST API docs: **https://kilonaut.de/hilfe**
- 🔌 Home Assistant guide: **https://kilonaut.de/hilfe/home-assistant**

## License

MIT — see [LICENSE](LICENSE). This integration is an open‑source API client; the Kilonaut service itself is a separate product.

---

<sub>Keywords: Home Assistant EV charging integration, electric vehicle wallbox tracker, EV charging costs, kWh energy tracking, home charging, Shelly wallbox, EVSE, Ladekosten Elektroauto tracken, Wallbox Home Assistant, Kilometerstand, odometer, cost per 100 km, HACS integration, Kilonaut.</sub>

[hacs]: https://github.com/hacs/integration
[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
