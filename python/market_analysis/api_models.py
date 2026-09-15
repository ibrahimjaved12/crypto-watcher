"""Versioned server-to-server input. Never accepts DB credentials or a user JWT."""
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .providers import SUPPORTED_SYMBOLS

Timestamp = Annotated[int, Field(strict=True, ge=0, le=4102444800000)]
Price = Annotated[Decimal, Field(gt=0, le=Decimal("1e20"), max_digits=40, decimal_places=20)]
Threshold = Annotated[Decimal, Field(ge=Decimal("0.1"), le=100)]
Source = Literal["Binance", "OKX", "Kraken"]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class AnalysisSettings(InputModel):
    threshold_pct: Threshold
    cooldown_minutes: Annotated[int, Field(strict=True, ge=1, le=1440)]
    monitoring_enabled: bool = Field(strict=True)


class SavedBaseline(InputModel):
    price: Price
    at_ms: Timestamp
    source: Source
    threshold: Threshold
    last_observed_ms: Timestamp
    last_up_alert_ms: Timestamp | None
    last_down_alert_ms: Timestamp | None

    @model_validator(mode="after")
    def ordered(self):
        if self.at_ms > self.last_observed_ms:
            raise ValueError("baseline time must not follow last observation")
        if self.at_ms % 60000 or self.last_observed_ms % 60000:
            raise ValueError("candle timestamps must align to one minute")
        return self


class AnalysisRequest(InputModel):
    schema_version: Literal[1]
    symbol: str = Field(min_length=5, max_length=16)
    settings: AnalysisSettings
    baseline: SavedBaseline | None

    @field_validator("symbol")
    @classmethod
    def supported(cls, value):
        if value not in SUPPORTED_SYMBOLS:
            raise ValueError("unsupported USDT symbol")
        return value
