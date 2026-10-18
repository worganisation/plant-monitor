"""Run one MQTT client and one owner of the Grow HAT hardware."""

import logging
import math
import signal
from collections import deque
from json import dumps
from queue import Empty, Full, Queue
from threading import Event
from time import monotonic
from typing import TYPE_CHECKING

from paho.mqtt.client import Client, MQTTMessage
from paho.mqtt.enums import CallbackAPIVersion, MQTTErrorCode
from pydantic import ValidationError

from plant_monitor.config import ChannelSettings, ChannelState, Settings
from plant_monitor.discovery import base_topic, channel_topic, discovery_payload
from plant_monitor.display import HISTORY_SAMPLES, Display

if TYPE_CHECKING:
    from grow.pump import Pump
    from paho.mqtt.client import ConnectFlags
    from paho.mqtt.properties import Properties
    from paho.mqtt.reasoncodes import ReasonCode

LOGGER = logging.getLogger(__name__)


class Monitor:
    """Process settings and watering commands outside the MQTT network thread."""

    def __init__(self, settings: Settings, state: ChannelState) -> None:
        from grow.moisture import Moisture

        self.settings = settings
        self.state = state
        self.stopping = Event()
        self.refresh = Event()
        self.commands: Queue[tuple[int, str, bytes, float]] = Queue(maxsize=16)
        self.sensors = {channel: Moisture(channel) for channel in range(1, 4)}
        self.display = Display()
        self.last_readings: dict[int, tuple[float, float]] = {}
        self.history: dict[int, deque[float | None]] = {
            channel: deque(maxlen=HISTORY_SAMPLES) for channel in self.sensors
        }
        self.pumps: dict[int, Pump] = {}
        self.next_watering = 0.0
        self.topics = {
            f"{channel_topic(settings, channel)}/{action}/set": (channel, action)
            for channel in self.sensors
            for action in ("name", "wet_point", "dry_point", "water")
        }

        self.client = Client(
            CallbackAPIVersion.VERSION2, client_id=f"{settings.device_id}_plant_monitor"
        )
        self.client.username_pw_set(
            settings.mqtt_username, settings.mqtt_password.get_secret_value()
        )
        self.client.will_set(f"{base_topic(settings)}/availability", "offline", qos=1, retain=True)
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client.max_queued_messages_set(64)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.enable_logger(LOGGER)

    def on_connect(
        self,
        client: Client,
        _userdata: object,
        _flags: ConnectFlags,
        reason: ReasonCode,
        _properties: Properties | None,
    ) -> None:
        """Restore subscriptions and discovery on every successful connection."""
        if reason.is_failure:
            LOGGER.error("MQTT connection rejected: %s", reason)
            return

        subscriptions = [
            (topic, 0 if action == "water" else 1) for topic, (_, action) in self.topics.items()
        ]
        subscriptions.append(("homeassistant/status", 1))
        result, _ = client.subscribe(subscriptions)
        if result != MQTTErrorCode.MQTT_ERR_SUCCESS:
            LOGGER.error("Unable to subscribe to MQTT commands: %s", result)

        self.refresh.set()
        LOGGER.info("Connected to MQTT broker")

    def on_message(self, _client: Client, _userdata: object, message: MQTTMessage) -> None:
        """Queue live commands without blocking MQTT keepalives."""
        if message.topic == "homeassistant/status":
            if message.payload == b"online":
                self.refresh.set()
            return

        if (target := self.topics.get(message.topic)) is None:
            return

        channel, action = target
        if message.retain or (action == "water" and message.dup):
            LOGGER.warning("Ignoring retained or duplicate command on %s", message.topic)
            return

        if len(message.payload) > 256:
            LOGGER.warning("Ignoring oversized command on %s", message.topic)
            return

        try:
            self.commands.put_nowait((channel, action, message.payload, monotonic()))
        except Full:
            LOGGER.warning("Command queue full; ignoring command on %s", message.topic)

    def publish_configuration(self) -> None:
        """Publish discovery and the persisted values, including on HA restart."""
        for channel, config in enumerate(self.state.channels, start=1):
            identifier = f"{self.settings.device_id}_plant_channel_{channel}"
            self.client.publish(
                f"homeassistant/device/{identifier}/config",
                dumps(discovery_payload(self.settings, channel, config)),
                qos=1,
                retain=True,
            )

            for key, value in config.model_dump().items():
                self.client.publish(
                    f"{channel_topic(self.settings, channel)}/{key}/state",
                    str(value),
                    qos=1,
                    retain=True,
                )

        self.client.publish(
            f"{base_topic(self.settings)}/availability", "online", qos=1, retain=True
        )

    def read_sensors(self) -> None:
        """Share each sample between MQTT and the screen, including while offline."""
        for channel, sensor in self.sensors.items():
            if not sensor.new_data:
                self.history[channel].append(None)
                continue

            frequency = float(sensor.moisture)
            if not math.isfinite(frequency) or frequency <= 0:
                self.history[channel].append(None)
                continue

            config = self.state.channels[channel - 1]
            saturation = (
                100 * (config.dry_point - frequency) / (config.dry_point - config.wet_point)
            )
            moisture = round(max(0, min(100, saturation)), 2)
            self.last_readings[channel] = (moisture, monotonic())
            self.history[channel].append(moisture)

            if self.client.is_connected():
                self.client.publish(
                    f"{channel_topic(self.settings, channel)}/state",
                    dumps({"frequency": round(frequency, 3), "moisture": moisture}),
                    qos=0,
                )

    def apply_command(self, channel: int, action: str, payload: bytes, received: float) -> None:
        """Validate an edit or bounded watering request before applying it."""
        value = payload.decode("utf-8")
        if action == "water":
            seconds = float(value)
            if not math.isfinite(seconds) or not 0 < seconds <= self.settings.max_water_seconds:
                raise ValueError(
                    f"Watering must last between 0 and {self.settings.max_water_seconds} seconds"
                )

            if (
                monotonic() - received > 5
                or not self.client.is_connected()
                or self.stopping.is_set()
            ):
                raise ValueError(
                    "Watering command is stale or the monitor is disconnected/stopping"
                )

            self.water(channel, seconds)
            return

        config = ChannelSettings.model_validate(
            {**self.state.channels[channel - 1].model_dump(), action: value}
        )
        channels = self.state.channels.copy()
        channels[channel - 1] = config
        updated = ChannelState(channels=channels)
        updated.save(self.settings.state_file)
        self.state = updated
        self.refresh.set()

    def water(self, channel: int, seconds: float) -> None:
        """Run one pump, always stopping it and spacing doses by 30 seconds."""
        from grow.pump import Pump

        if monotonic() < self.next_watering:
            raise ValueError("Wait 30 seconds between watering doses")

        if channel not in self.pumps:
            self.pumps[channel] = Pump(channel)

        pump = self.pumps[channel]
        if not pump.set_speed(1):
            raise ValueError("Another pump is already running")

        try:
            LOGGER.info("Watering channel %s for %.2f seconds", channel, seconds)
            self.stopping.wait(seconds)
        finally:
            pump.stop()
            self.next_watering = monotonic() + 30

    def run(self) -> None:
        """Reconnect automatically and process commands until shutdown."""
        self.client.connect_async(self.settings.mqtt_host, self.settings.mqtt_port, keepalive=30)
        self.client.loop_start()
        next_reading = monotonic() + 5
        next_screen = 0.0

        try:
            while not self.stopping.is_set():
                if self.client.is_connected():
                    if self.refresh.is_set():
                        self.refresh.clear()
                        self.publish_configuration()

                if monotonic() >= next_reading:
                    self.read_sensors()
                    next_reading = monotonic() + self.settings.publish_interval

                if self.display.poll_buttons() or monotonic() >= next_screen:
                    current = {
                        channel: value
                        for channel, (value, sampled_at) in self.last_readings.items()
                        if monotonic() - sampled_at < self.settings.publish_interval * 3
                    }
                    self.display.update(
                        self.state, current, self.client.is_connected(), self.history
                    )
                    next_screen = monotonic() + 1

                try:
                    command = self.commands.get(timeout=0.05)
                except Empty:
                    continue

                try:
                    self.apply_command(*command)
                except ValueError as exc:
                    LOGGER.warning(
                        "Rejected %s command for channel %s: %s", command[1], command[0], exc
                    )
                except OSError:
                    LOGGER.exception("Unable to save channel settings")
        finally:
            try:
                if self.client.is_connected():
                    self.client.publish(
                        f"{base_topic(self.settings)}/availability", "offline", qos=1, retain=True
                    ).wait_for_publish(timeout=2)
            finally:
                self.client.disconnect()
                self.client.loop_stop()
                self.display.close()


def main() -> None:
    """Validate settings, initialise hardware, and handle service stop signals."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        settings = Settings()  # pyright: ignore[reportCallIssue] — BaseSettings loads required fields from the environment.
        state = ChannelState.load(settings.state_file)
        state.save(settings.state_file)
    except (ValidationError, OSError) as exc:
        raise SystemExit(f"Invalid plant-monitor configuration: {exc}") from None

    monitor = Monitor(settings, state)
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda _signum, _frame: monitor.stopping.set())

    monitor.run()
