"""Validated service settings and persistent channel configuration."""

from pathlib import Path
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Read the complete configuration before starting MQTT or GPIO."""

    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", str_strip_whitespace=True, hide_input_in_errors=True
    )

    mqtt_host: Annotated[str, Field(min_length=1)]
    mqtt_port: Annotated[int, Field(ge=1, le=65535)] = 1883
    mqtt_username: Annotated[str, Field(min_length=1)]
    mqtt_password: Annotated[SecretStr, Field(min_length=1)]
    device_id: Annotated[str, Field(pattern=r"^[a-z0-9]+(?:_[a-z0-9]+)*$")] = "growpi"
    state_file: Path = Path("/var/lib/plant-monitor/channels.json")
    publish_interval: Annotated[float, Field(ge=5, le=300, allow_inf_nan=False)] = 30
    max_water_seconds: Annotated[float, Field(ge=1, le=30, allow_inf_nan=False)] = 5


class ChannelSettings(BaseModel):
    """A channel's label and wet/dry pulse frequencies."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: Annotated[str, Field(min_length=1, max_length=64)]
    wet_point: Annotated[float, Field(ge=0, le=1000, allow_inf_nan=False)] = 0.7
    dry_point: Annotated[float, Field(ge=0, le=1000, allow_inf_nan=False)] = 27.6

    @field_validator("name")
    @classmethod
    def printable_name(cls, value: str) -> str:
        """Keep labels readable in Home Assistant."""
        if not value.isprintable():
            raise ValueError("Name must contain only printable characters")

        return value

    @model_validator(mode="after")
    def calibration_order(self) -> Self:
        """A Grow sensor's pulse frequency falls as moisture increases."""
        if self.wet_point >= self.dry_point:
            raise ValueError("Wet point must be lower than dry point")

        return self


class ChannelState(BaseModel):
    """Settings in physical channel order, independent of plant names."""

    model_config = ConfigDict(extra="forbid")

    channels: Annotated[list[ChannelSettings], Field(min_length=3, max_length=3)] = Field(
        default_factory=lambda: [
            ChannelSettings(name=f"Channel {channel}") for channel in range(1, 4)
        ]
    )

    @classmethod
    def load(cls, path: Path) -> Self:
        """Read saved settings, using defaults only on first startup."""
        if path.exists():
            return cls.model_validate_json(path.read_text(encoding="utf-8"))

        return cls()

    def save(self, path: Path) -> None:
        """Replace the state file atomically before acknowledging an edit."""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
