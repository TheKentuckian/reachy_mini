"""Motors router.

Provides endpoints to get and set the motor control mode.
"""

import asyncio

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from reachy_mini.io.protocol import MotorControlMode

from ....daemon.backend.abstract import Backend
from ..dependencies import get_backend

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
    # Blocking bus read (queued behind the control loop) — keep it off the
    # event loop that also serves /ws/sdk.
    voltage = await asyncio.to_thread(backend.read_motor_voltage)
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
    backend.set_motor_control_mode(mode)

    return {"status": f"motors changed to {mode} mode"}
