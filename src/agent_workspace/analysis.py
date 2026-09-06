"""Provider-neutral, bounded data-analysis request contracts (no compute imports)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ANALYSIS_TOOL_NAME = "analyze_data"
PYTHON_ANALYSIS_TOOL_NAME = "run_python_analysis"
ANALYSIS_SCHEMA = "pi-agent-data-analysis/v1"
MAX_ANALYSIS_CELLS = 1_000_000
MAX_ANALYSIS_ROWS = 200_000
MAX_ANALYSIS_COLUMNS = 256
MAX_RESULT_ROWS = 5_000
MAX_RESULT_BYTES = 2 * 1024 * 1024


class AnalysisError(RuntimeError):
    """Safe public code; never includes source contents or host paths."""

    def __init__(self, code: str, details: dict[str, str] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.details = details or {}


class AnalysisFilter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    column: str = Field(min_length=1, max_length=200)
    operation: Literal["eq", "ne", "gt", "gte", "lt", "lte", "is_null", "not_null"]
    value: str | int | float | bool | None = None


class AnalysisMetric(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    column: str = Field(min_length=1, max_length=200)
    operation: Literal["sum", "count", "mean", "min", "max", "median"]


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    file_id: str = Field(min_length=1, max_length=100, pattern=r"^[\w-]+$")
    action: Literal["inspect", "profile", "aggregate", "timeseries", "chart"]
    sheet: str | None = Field(default=None, min_length=1, max_length=200)
    encoding: Literal["utf-8-sig", "utf-8", "gb18030"] = "utf-8-sig"
    header_row: int = Field(default=0, ge=0, le=100)
    columns: list[str] = Field(default_factory=list, max_length=MAX_ANALYSIS_COLUMNS)
    filters: list[AnalysisFilter] = Field(default_factory=list, max_length=10)
    group_by: list[str] = Field(default_factory=list, max_length=4)
    metrics: list[AnalysisMetric] = Field(default_factory=list, max_length=10)
    sort_by: str | None = Field(default=None, max_length=200)
    descending: bool = True
    limit: int = Field(default=100, ge=1, le=MAX_RESULT_ROWS)
    date_column: str | None = Field(default=None, max_length=200)
    date_format: str = Field(default="ISO8601", min_length=1, max_length=64)
    frequency: Literal["day", "week", "month", "year"] = "month"
    chart_type: Literal["bar", "line", "histogram", "scatter"] = "bar"
    x: str | None = Field(default=None, max_length=200)
    y: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_operation(self) -> AnalysisRequest:
        if any(not name or len(name) > 200 for name in [*self.columns, *self.group_by]):
            raise ValueError("invalid column name")
        if self.action in {"aggregate", "timeseries"} and not self.metrics:
            raise ValueError("metrics are required")
        if self.action == "timeseries" and not self.date_column:
            raise ValueError("date_column is required")
        if self.action == "chart" and (
            not self.x or (self.chart_type != "histogram" and not self.y)
        ):
            raise ValueError("chart columns are required")
        if len({(m.column, m.operation) for m in self.metrics}) != len(self.metrics):
            raise ValueError("duplicate metrics")
        if any(len(set(names)) != len(names) for names in (self.columns, self.group_by)):
            raise ValueError("duplicate columns")
        if self.action == "chart" and self.chart_type != "histogram" and self.x == self.y:
            raise ValueError("chart X and Y must be different columns")
        return self


class PythonAnalysisRequest(BaseModel):
    """Code is accepted only by the isolated Python runner, never the fixed worker."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    action: Literal["python"] = "python"
    file_id: str = Field(min_length=1, max_length=100, pattern=r"^[\w-]+$")
    code: str = Field(min_length=1, max_length=16_000)
    sheet: str | None = Field(default=None, min_length=1, max_length=200)
    encoding: Literal["utf-8-sig", "utf-8", "gb18030"] = "utf-8-sig"
    header_row: int = Field(default=0, ge=0, le=100)
    limit: int = Field(default=100, ge=1, le=MAX_RESULT_ROWS)


AnalysisJobRequest = AnalysisRequest | PythonAnalysisRequest


def parse_analysis_request(value: dict[str, object]) -> AnalysisJobRequest:
    if value.get("action") == "python":
        return PythonAnalysisRequest.model_validate(value)
    return AnalysisRequest.model_validate(value)
