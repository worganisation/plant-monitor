"""Overview, plant history and navigation on the Grow HAT's 160 × 80 screen."""

import logging
from collections.abc import Sequence
from colorsys import hsv_to_rgb
from time import monotonic
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw, ImageFont

if TYPE_CHECKING:
    from collections import deque

    from ST7735 import ST7735

    from plant_monitor.config import ChannelState

LOGGER = logging.getLogger(__name__)
SMALL_FONT = ImageFont.load_default(size=10)
NAME_FONT = ImageFont.load_default(size=12)
VALUE_FONT = ImageFont.load_default(size=23)
BUTTONS = {5: "A", 6: "B", 16: "X", 24: "Y"}
HISTORY_SAMPLES = 60


def moisture_colour(value: float) -> tuple[int, int, int]:
    """Blend dry red through green to wet blue."""
    red, green, blue = hsv_to_rgb(value / 100 * 0.56, 0.7, 1)
    return round(red * 255), round(green * 255), round(blue * 255)


def draw_controls(draw: ImageDraw.ImageDraw, connected: bool, automatic: bool) -> None:
    """Place button hints beside their physical positions on the HAT."""
    colour = "#c6d3df"
    draw.polygon(((5, 5), (14, 11), (5, 17)), fill=colour)
    draw.polygon(((14, 63), (5, 69), (14, 75)), fill=colour)

    for left, top in ((143, 9), (148, 5), (153, 12)):
        draw.rectangle((left, top, left + 2, 18), fill=colour)

    if automatic:
        draw.rectangle((144, 63, 147, 75), fill=colour)
        draw.rectangle((151, 63, 154, 75), fill=colour)
    else:
        draw.polygon(((145, 63), (154, 69), (145, 75)), fill=colour)

    draw.ellipse((146, 37, 152, 43), fill="#79d6b0" if connected else "#ffba75")


def render_overview(moisture: dict[int, float], connected: bool, automatic: bool) -> Image.Image:
    """Show the three numbered moisture columns used in the product demo."""
    image = Image.new("RGB", (160, 80), "#101820")
    draw = ImageDraw.Draw(image)
    draw_controls(draw, connected, automatic)

    for channel in range(1, 4):
        left = 29 + (channel - 1) * 35
        value = moisture.get(channel)
        draw.text((left + 15, 2), str(channel), font=SMALL_FONT, fill="#edf5fa", anchor="mt")
        draw.rectangle((left, 15, left + 29, 64), fill="#2b3945")
        if value is not None and value > 0:
            height = max(1, round(50 * value / 100))
            draw.rectangle((left, 65 - height, left + 29, 64), fill=moisture_colour(value))

        draw.text(
            (left + 15, 68),
            "--" if value is None else f"{value:.0f}%",
            font=SMALL_FONT,
            fill="#edf5fa",
            anchor="mt",
        )

    return image


def render_channel(
    state: ChannelState,
    moisture: dict[int, float],
    connected: bool,
    automatic: bool,
    channel: int,
    history: Sequence[float | None],
) -> Image.Image:
    """Show a plant's name, current moisture and its recent samples."""
    image = Image.new("RGB", (160, 80), "#101820")
    draw = ImageDraw.Draw(image)
    draw_controls(draw, connected, automatic)

    label = f"{channel} {state.channels[channel - 1].name}"
    if draw.textlength(label, font=NAME_FONT) > 110:
        while draw.textlength(label + "...", font=NAME_FONT) > 110:
            label = label[:-1]

        label += "..."

    value = moisture.get(channel)
    draw.text((80, 3), label, font=NAME_FONT, fill="#edf5fa", anchor="mt")
    draw.text(
        (80, 20),
        "--" if value is None else f"{value:.0f}%",
        font=VALUE_FONT,
        fill=moisture_colour(value) if value is not None else "#74818d",
        anchor="mt",
    )

    draw.rectangle((25, 45, 134, 75), fill="#1d2a35")
    draw.line((25, 60, 134, 60), fill="#334351")
    previous: tuple[int, int] | None = None
    for index, sample in enumerate(history):
        if sample is None:
            previous = None
            continue

        point = (
            134 - round((len(history) - index - 1) * 109 / (HISTORY_SAMPLES - 1)),
            75 - round(sample * 0.3),
        )
        if previous is not None:
            draw.line((previous, point), fill="#69c6ef", width=1)
        else:
            draw.point(point, fill="#69c6ef")

        previous = point

    return image


class Display:
    """Own the screen and navigation buttons without interrupting monitoring."""

    def __init__(self) -> None:
        self.panel: ST7735 | None = None
        self.page = 0
        self.automatic = True
        self.next_page = monotonic() + 5
        self.buttons_ready = False
        self.pressed: set[int] = set()
        self.last_press = 0.0

        try:
            from ST7735 import ST7735

            self.panel = ST7735(port=0, cs=1, dc=9, backlight=12, rotation=270)
        except ImportError, OSError, RuntimeError:
            LOGGER.exception("Screen unavailable; continuing plant monitoring")
            return

        try:
            import RPi.GPIO as GPIO

            GPIO.setup(list(BUTTONS), GPIO.IN, pull_up_down=GPIO.PUD_UP)
            self.buttons_ready = True
        except ImportError, OSError, RuntimeError:
            LOGGER.exception("Screen buttons unavailable; automatic rotation remains available")

    def poll_buttons(self) -> bool:
        """Handle one press per release, with debounce, on the monitor thread."""
        if not self.buttons_ready:
            return False

        import RPi.GPIO as GPIO

        try:
            pressed = {pin for pin in BUTTONS if GPIO.input(pin) == GPIO.LOW}
        except OSError, RuntimeError:
            LOGGER.exception("Unable to read screen buttons")
            self.buttons_ready = False
            return False

        new_presses = pressed - self.pressed
        self.pressed = pressed
        now = monotonic()
        if not new_presses or now - self.last_press < 0.2:
            return False

        self.last_press = now
        button = next(BUTTONS[pin] for pin in BUTTONS if pin in new_presses)
        if button == "Y":
            self.automatic = not self.automatic
        else:
            self.automatic = False
            if button == "X":
                self.page = 0
            else:
                self.page = (self.page + (1 if button == "A" else -1)) % 4

        self.next_page = now + 5
        return True

    def update(
        self,
        state: ChannelState,
        moisture: dict[int, float],
        connected: bool,
        history: dict[int, deque[float | None]],
    ) -> None:
        """Refresh the selected page, rotating only when automatic mode is enabled."""
        if self.panel is None:
            return

        if self.automatic and monotonic() >= self.next_page:
            self.page = (self.page + 1) % 4
            self.next_page = monotonic() + 5

        image = (
            render_overview(moisture, connected, self.automatic)
            if self.page == 0
            else render_channel(
                state, moisture, connected, self.automatic, self.page, list(history[self.page])
            )
        )
        try:
            self.panel.display(image)
        except OSError, RuntimeError:
            LOGGER.exception("Screen update failed; continuing plant monitoring")
            self.close()
            self.panel = None

    def close(self) -> None:
        """Release the button pins, turn off the backlight and blank the screen."""
        if self.buttons_ready:
            import RPi.GPIO as GPIO

            try:
                GPIO.cleanup(list(BUTTONS))
            except OSError, RuntimeError:
                LOGGER.warning("Unable to release screen buttons", exc_info=True)

            self.buttons_ready = False

        if self.panel is None:
            return

        try:
            self.panel.set_backlight(False)
            self.panel.display(Image.new("RGB", (160, 80)))
        except OSError, RuntimeError:
            LOGGER.warning("Unable to clear the screen", exc_info=True)
