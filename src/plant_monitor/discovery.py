"""Home Assistant discovery using stable device/channel identifiers."""

from plant_monitor.config import ChannelSettings, Settings


def base_topic(settings: Settings) -> str:
    """Return the monitor's MQTT namespace."""
    return f"/homeassistant/{settings.device_id}/plant_monitor"


def channel_topic(settings: Settings, channel: int) -> str:
    """Return a physical channel's namespace."""
    return f"{base_topic(settings)}/channel_{channel}"


def discovery_payload(
    settings: Settings, channel: int, config: ChannelSettings
) -> dict[str, object]:
    """Describe sensors, editable settings and a one-second watering button."""
    base = channel_topic(settings, channel)
    identifier = f"{settings.device_id}_plant_channel_{channel}"
    components: dict[str, dict[str, object]] = {}

    for metric, name, unit, device_class in (
        ("moisture", "Soil moisture", "%", "moisture"),
        ("frequency", "Sensor frequency", "Hz", "frequency"),
    ):
        components[metric] = {
            "platform": "sensor",
            "name": name,
            "state_topic": f"{base}/state",
            "value_template": f"{{{{ value_json.{metric} }}}}",
            "unit_of_measurement": unit,
            "device_class": device_class,
            "state_class": "measurement",
            "expire_after": int(settings.publish_interval * 3),
        }

    components["name"] = {
        "platform": "text",
        "name": "Plant name",
        "state_topic": f"{base}/name/state",
        "command_topic": f"{base}/name/set",
        "min": 1,
        "max": 64,
        "entity_category": "config",
    }

    for point in ("wet_point", "dry_point"):
        components[point] = {
            "platform": "number",
            "name": point.replace("_", " ").capitalize(),
            "state_topic": f"{base}/{point}/state",
            "command_topic": f"{base}/{point}/set",
            "min": 0,
            "max": 1000,
            "step": 0.01,
            "mode": "box",
            "unit_of_measurement": "Hz",
            "entity_category": "config",
        }

    components["water"] = {
        "platform": "button",
        "name": "Water for 1 second",
        "command_topic": f"{base}/water/set",
        "payload_press": "1",
        "icon": "mdi:watering-can",
        "qos": 0,
    }

    for key, component in components.items():
        component["unique_id"] = f"{identifier}_{key}"
        component["default_entity_id"] = f"{component['platform']}.{identifier}_{key}"

    return {
        "device": {
            "identifiers": [identifier],
            "name": config.name,
            "manufacturer": "Pimoroni",
            "model": f"Grow HAT channel {channel}",
        },
        "origin": {"name": "plant-monitor"},
        "components": components,
        "availability_topic": f"{base_topic(settings)}/availability",
    }
