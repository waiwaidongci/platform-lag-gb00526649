from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field
from src.constants.enums import Review

class Request(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')

class Credentials(Request):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)

class Upload(Request):
    name: str = Field(min_length=1, max_length=120)
    filename: str = Field(min_length=1, max_length=160)
    csv_text: str = Field(min_length=1, max_length=2_000_000)

class RunRequest(Request):
    dataset_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=120)
    params: dict = Field(default_factory=dict)

class ReviewRequest(Request):
    review: Review
    note: str = Field(min_length=1, max_length=500)
    decision: str = Field(default='', max_length=120)

class ScheduleRequest(RunRequest):
    recipient: str = Field(min_length=1, max_length=100)
    first_at: datetime
    repeat_daily: bool = False


class MeterRequest(Request):
    meter_code: str = Field(min_length=1, max_length=40)
    label: str = Field(min_length=1, max_length=120)
    platform: str = Field(min_length=1, max_length=80)
    is_baseline: bool = False


class BaselineRequest(Request):
    is_baseline: bool


class ComparisonItem(Request):
    meter_a: str = Field(min_length=1, max_length=40)
    meter_b: str = Field(min_length=1, max_length=40)
    deviation_low: float = Field(ge=-50, le=50)
    deviation_high: float = Field(ge=-50, le=50)


class ComparisonBatchRequest(Request):
    batch_key: str = Field(min_length=1, max_length=80)
    note: str = Field(default='', max_length=300)
    measured_at: str = Field(default='', max_length=30)
    items: list[ComparisonItem] = Field(min_length=1, max_length=200)


class WithdrawRequest(Request):
    reason: str = Field(min_length=1, max_length=300)


class PublishRequest(Request):
    expected_fingerprint: str = Field(default='', max_length=64)
