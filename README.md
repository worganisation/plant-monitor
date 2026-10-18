# Plant monitor

Grow HAT monitoring and MQTT controls for GrowPi. One Python process owns the three moisture channels and their pumps. Home Assistant discovers each channel's moisture and frequency sensors, editable plant name, wet/dry calibration numbers, and one-second watering button.

Channel IDs and MQTT topics stay fixed when a plant is renamed. Names and calibration persist in `/var/lib/plant-monitor/channels.json`; back up that file along with the protected `.env`. Sensor readings expire when pulses stop. Watering commands must be live, finite and positive, and cannot exceed `MAX_WATER_SECONDS` (five seconds by default).

## Development

Install Python 3.14, uv, just and prek, then run:

```fish
just setup
prek install
just run
```

Hardware dependencies install on ARM64 Linux. The GPIO backend is `rpi-lgpio`, which provides the Grow HAT driver’s `RPi.GPIO` API on current kernels. Building its `lgpio` dependency requires `swig`, `liblgpio-dev` and the compiler toolchain on the Pi (`sudo apt install swig liblgpio-dev build-essential`). Running the monitor requires the actual Grow HAT libraries and GPIO access; it does not substitute simulated readings when hardware is missing. CI runs prek, Ruff and basedpyright. Renovate manages dependencies using the shared worganisation preset.

## GrowPi setup

Clone `https://github.com/worganisation/plant-monitor.git` into `/home/worgarside/plant-monitor`. Run as the `worgarside` user, which has GPIO access. Store these values in `.env` with mode `0600`:

```dotenv
MQTT_HOST=10.0.0.113
MQTT_PORT=1883
MQTT_USERNAME=growpi
MQTT_PASSWORD=<set locally>
DEVICE_ID=growpi
MAX_WATER_SECONDS=5
```

`PUBLISH_INTERVAL` defaults to 30 seconds. The systemd unit sets `STATE_FILE=/var/lib/plant-monitor/channels.json` and creates the state directory with the service user's ownership. Channels start as Channel 1–3; set their names and calibration through the discovered Home Assistant controls.

```fish
just sync
just install
just start
just status
just logs
```

The service starts only the monitor process. Pump commands are handled by that process; installing or starting the service does not issue a watering command.

## Releases and deployment

The manual Semantic Release workflow publishes a version from `main`. Publishing a stable release triggers deployment through the worganisation homelab runner to `worgarside@10.0.0.53`. The deployment workflow also accepts an existing release tag through its manual dispatch.

The `production-deploy` GitHub environment contains `DEPLOY_SSH_PRIVATE_KEY` and `DEPLOY_SSH_KNOWN_HOSTS`. The latter must contain GrowPi's independently verified host key for `10.0.0.53`. The infrastructure repository owns the runner's TCP 22 allowance to GrowPi. The repository `DEPLOY_KEY` secret authenticates Semantic Release's Git push.

Deployment refuses a dirty checkout, fetches the requested tag, stops the service, checks out the tag, syncs locked runtime dependencies, refreshes the unit and restarts it. A failed dependency sync leaves the service stopped and fails the deployment. Use `just deploy VERSION` to deploy or roll back to an existing release; mutable channel settings and credentials are preserved.

## MQTT

Discovery is retained under `homeassistant/device/growpi_plant_channel_<N>/config`. State and commands use `/homeassistant/growpi/plant_monitor/channel_<N>/...`; availability uses `/homeassistant/growpi/plant_monitor/availability`. Name and calibration edits are acknowledged by retained state after they are saved locally. Discovery and subscriptions refresh on reconnect and Home Assistant startup.
