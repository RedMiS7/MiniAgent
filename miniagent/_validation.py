"""Shared validation policy for internal data contracts."""
from pydantic import BaseModel, ConfigDict


class ContractModel(BaseModel):
    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", validate_default=True,
        allow_inf_nan=False, hide_input_in_errors=True,
    )
