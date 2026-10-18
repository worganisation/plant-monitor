"""Compact overview for the Grow HAT's 160 × 80 pixel display."""

import logging
from time import monotonic
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw, ImageFont

if TYPE_CHECKING:
    from ST7735 import ST7735

    from plant_monitor.config import ChannelState

LOGGER = logging.getLogger(__name__)
STATUS_FONT = ImageFont.load_default(size=10)
NAME_FONT = ImageFont.load_default(size=13)
VALUE_FONT = ImageFont.load_default(size=34)


def render_status(
    state: ChannelState, moisture: dict[int, float], connected: bool, channel: int
) -> Image.Image:
    """Draw one plant at a readable size; missing readings display as dashes."""
    image = Image.new("RGB", (160, 80), "#101820")
    draw = ImageDraw.Draw(image)
    draw.text((4, 2), f"PLANT {channel}/3", font=STATUS_FONT, fill="#c6d3df", anchor="lt")
    draw.text(
        (156, 2),
        "MQTT OK" if connected else "MQTT OFF",
        font=STATUS_FONT,
        fill="#79d6b0" if connected else "#ffba75",
        anchor="rt",
    )

    label = state.channels[channel - 1].name
    if draw.textlength(label, font=NAME_FONT) > 152:
        while draw.textlength(label + "...", font=NAME_FONT) > 152:
            label = label[:-1]

        label += "..."

    value = moisture.get(channel)
    draw.text((80, 20), label, font=NAME_FONT, fill="#edf5fa", anchor="mt")
    draw.text(
        (80, 39),
        "--" if value is None else f"{value:.0f}%",
        font=VALUE_FONT,
        fill="#69c6ef" if value is not None else "#74818d",
        anchor="mt",
    )

    for number in range(1, 4):
        left = 67 + number * 6
        draw.ellipse((left, 76, left + 2, 78), fill="#edf5fa" if number == channel else "#46535f")

    return image


class Display:
    """Use the HAT's SPI display without making monitoring depend on it."""

    def __init__(self) -> None:
        self.panel: ST7735 | None = None
        self.started = monotonic()
        try:
            from ST7735 import ST7735

            self.panel = ST7735(port=0, cs=1, dc=9, backlight=12, rotation=270)
        except ImportError, OSError, RuntimeError:
            LOGGER.exception("Screen unavailable; continuing plant monitoring")

    def update(self, state: ChannelState, moisture: dict[int, float], connected: bool) -> None:
        """Refresh the overview, disabling a failed display until service restart."""
        if self.panel is None:
            return

        try:
            channel = int((monotonic() - self.started) / 5) % 3 + 1
            self.panel.display(render_status(state, moisture, connected, channel))
        except OSError, RuntimeError:
            LOGGER.exception("Screen update failed; continuing plant monitoring")
            self.close()
            self.panel = None

    def close(self) -> None:
        """Turn off the backlight and blank the display on service shutdown."""
        if self.panel is None:
            return

        try:
            self.panel.set_backlight(False)
            self.panel.display(Image.new("RGB", (160, 80)))
        except OSError, RuntimeError:
            LOGGER.warning("Unable to clear the screen", exc_info=True)
