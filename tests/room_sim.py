"""A simulated room with an AC cycling on a thermostat, for the tests."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import random


@dataclass
class SimRoom:
    """Two-node room (air, mass) with a delayed AC heat and a varying gain.

    Integrated with small Euler steps, independently of the model's exact
    discretization. The AC heats with `ac_power` W when the air drops below
    `low` and stops above `high`.
    """

    air_capacity: float = 150.0
    mass_capacity: float = 1500.0
    coupling: float = 850.0
    heat_delay_min: float = 4.0
    ac_power: float = 2800.0
    low: float = 20.0
    high: float = 22.5
    seed: int = 1
    air: float = 20.5
    mass: float = 20.0
    delivered: float = 0.0
    on: bool = False
    t: float = 0.0  # s
    _rng: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Seed the sensor noise."""
        self._rng = random.Random(self.seed)

    def gain(self, t: float) -> float:
        """Net heat flow into the room (W): a loss that varies over the day."""
        return -500.0 + 150.0 * math.sin(2 * math.pi * t / 86400)

    @property
    def power(self) -> float:
        """AC heat output right now (W)."""
        return self.ac_power if self.on else 0.0

    def measured(self) -> float:
        """Room sensor: noise and 0.1 °C resolution."""
        return round((self.air + self._rng.gauss(0, 0.02)) * 10) / 10

    def advance(self, seconds: float, substep: float = 5.0) -> None:
        """Run the room for some seconds (thermostat checked every substep)."""
        steps = max(1, round(seconds / substep))
        h = seconds / steps / 3600
        for _ in range(steps):
            if self.air < self.low:
                self.on = True
            elif self.air > self.high:
                self.on = False
            exchange = self.coupling * (self.mass - self.air)
            air = self.air + h * (self.delivered + exchange) / self.air_capacity
            mass = self.mass + h * (-exchange + self.gain(self.t)) / self.mass_capacity
            self.delivered += (
                h * (self.power - self.delivered) / (self.heat_delay_min / 60)
            )
            self.air, self.mass = air, mass
            self.t += seconds / steps
