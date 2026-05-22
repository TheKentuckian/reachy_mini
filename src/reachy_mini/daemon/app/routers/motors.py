"""Motors router.

Provides endpoints to get and set the motor control mode.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from reachy_mini.daemon.instrumentation import log_event, timing_event
from reachy_mini.io.protocol import MotorControlMode

from ..dependencies import get_backend

if TYPE_CHECKING:
    from ....daemon.backend.abstract import Backend

router = APIRouter(
    prefix="/motors",
)


class MotorStatus(BaseModel):
    """Represents the status of the motors.

    Exposes
    - mode: The current motor control mode (enabled, disabled, gravity_compensation).
    """

    mode: MotorControlMode


# 2S LiFePO4 pack (6.4V nominal, 2000 mAh).
# Full: 7.2V (2×3.6V charged). Empty: 5.6V (2×2.8V BMS floor).
# Power board rated input is 6.8–7.6V; voltage below 6.8V means the pack
# is below nominal but the BMS hasn't cut off yet.
# LiFePO4 has a very flat discharge curve — treat % as a coarse indicator.
_BATT_FULL_V = 7.2
_BATT_EMPTY_V = 5.6


class VoltageStatus(BaseModel):
    """Present motor bus voltage and estimated battery percentage."""

    voltage_v: float | None
    percent: int | None


@router.get("/status")
async def get_motor_status(backend: Backend = Depends(get_backend)) -> MotorStatus:
    """Get the current status of the motors."""
    return MotorStatus(mode=backend.get_motor_control_mode())


@router.get("/voltage")
async def get_motor_voltage(backend: Backend = Depends(get_backend)) -> VoltageStatus:
    """Read present bus voltage from a motor as a battery proxy.

    Percent is a linear estimate over the 2S LiFePO4 operating range
    (6.8–7.3 V). LiFePO4 discharges very flatly so treat it as a rough
    indicator, not a calibrated gauge.
    """
    voltage = backend.read_motor_voltage()
    percent: int | None = None
    if voltage is not None:
        pct = (voltage - _BATT_EMPTY_V) / (_BATT_FULL_V - _BATT_EMPTY_V) * 100.0
        percent = max(0, min(100, round(pct)))
    return VoltageStatus(voltage_v=voltage, percent=percent)


@router.post("/set_mode/{mode}")
async def set_motor_mode(
    mode: MotorControlMode,
    backend: Backend = Depends(get_backend),
) -> dict[str, str]:
    """Set the motor control mode."""
    log_event(
        "daemon.motor.mode.set.start",
        source="rest",
        endpoint="/api/motors/set_mode/{mode}",
        mode=mode.value,
    )
    with timing_event(
        "daemon.motor.mode.set",
        source="rest",
        endpoint="/api/motors/set_mode/{mode}",
        mode=mode.value,
    ):
        backend.set_motor_control_mode(mode)
    log_event(
        "daemon.motor.mode.set.complete",
        source="rest",
        endpoint="/api/motors/set_mode/{mode}",
        mode=mode.value,
    )

    return {"status": f"motors changed to {mode} mode"}
