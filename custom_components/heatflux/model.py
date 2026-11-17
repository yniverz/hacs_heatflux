"""Two-node room model: air and mass, a Kalman filter and a parameter fit.

The room is two heat stores:

    air:   C_air  × dT_air/dt  = P_ac,delivered + H × (T_mass − T_air) + s × Q
    mass:  C_mass × dT_mass/dt = H × (T_air − T_mass) + (1 − s) × Q

P_ac,delivered follows the AC power with a first-order delay (the indoor fan
waits for a warm coil, the coil keeps heating a little after the stop). Q is
the net heat flow into the room from everything but the AC (walls, windows,
sun, people), modelled as a slow random walk.

A Kalman filter estimates T_air, T_mass, Q and the delivered AC power from
the measured (air) temperature. The parameters are fitted by maximum
likelihood of the filter's prediction errors over a few days of samples.

Plain Python on purpose (4×4 matrices): no numpy needed. Times in hours,
temperatures in °C, powers in W, heat capacities in Wh/K, H in W/K.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
import math

Matrix = list[list[float]]
Vector = list[float]

N = 4  # states: air temperature, mass temperature, gain Q, delivered AC power
AIR, MASS, GAIN, DELIVERED = range(N)

# Random walk of the two temperatures (K/√h): small model errors.
TEMPERATURE_NOISE = 0.08


@dataclass(frozen=True)
class RoomParams:
    """Parameters of the two-node model."""

    air_capacity: float = 50.0  # Wh/K
    mass_capacity: float = 400.0  # Wh/K
    coupling: float = 200.0  # W/K
    heat_delay: float = 3.0  # min, time constant of the delivered AC power
    # Share of Q that enters the air node. It doesn't change any air
    # temperature forecast (the data can't tell), but it shifts the mass
    # temperature. At 0 the air settles exactly onto the mass temperature
    # once the AC's spike has decayed, which is what "real temperature"
    # means here; that also predicted the settled temperature best.
    gain_share_air: float = 0.0
    gain_noise: float = 40.0  # W/√h, how fast Q may change
    # Only the ratio to gain_noise and TEMPERATURE_NOISE matters for the
    # filter, so this one stays fixed and gain_noise is fitted.
    sensor_noise: float = 0.3  # K

    @property
    def air_time_constant(self) -> float:
        """Minutes for the air to relax towards the mass temperature."""
        return self.air_capacity / self.coupling * 60

    @property
    def mass_time_constant(self) -> float:
        """Hours for the mass to follow the air."""
        return self.mass_capacity / self.coupling

    @property
    def total_capacity(self) -> float:
        """Air and mass together (Wh/K)."""
        return self.air_capacity + self.mass_capacity

    def as_dict(self) -> dict[str, float]:
        """For storage."""
        return {
            "air_capacity": self.air_capacity,
            "mass_capacity": self.mass_capacity,
            "coupling": self.coupling,
            "heat_delay": self.heat_delay,
            "gain_share_air": self.gain_share_air,
            "gain_noise": self.gain_noise,
            "sensor_noise": self.sensor_noise,
        }

    @classmethod
    def from_dict(cls, data: dict) -> RoomParams:
        """From storage."""
        return cls(**{k: float(data[k]) for k in cls().as_dict()})


# Fitted as logarithms.
_BOUNDS = {
    "air_capacity": (2.0, 5000.0),
    "mass_capacity": (10.0, 50000.0),
    "coupling": (2.0, 5000.0),
    "heat_delay": (0.2, 30.0),
    "gain_noise": (1.0, 5000.0),
}


def _to_vector(p: RoomParams) -> Vector:
    return [
        math.log(p.air_capacity),
        math.log(p.mass_capacity),
        math.log(p.coupling),
        math.log(p.heat_delay),
        math.log(p.gain_noise),
    ]


def _from_vector(v: Sequence[float]) -> RoomParams:
    def bounded(key: str, x: float) -> float:
        low, high = _BOUNDS[key]
        return min(max(math.exp(min(max(x, -50), 50)), low), high)

    return RoomParams(
        air_capacity=bounded("air_capacity", v[0]),
        mass_capacity=bounded("mass_capacity", v[1]),
        coupling=bounded("coupling", v[2]),
        heat_delay=bounded("heat_delay", v[3]),
        gain_noise=bounded("gain_noise", v[4]),
    )


# ------------------------------------------------------------- linear algebra


def _matmul(a: Matrix, b: Matrix) -> Matrix:
    n, m, p = len(a), len(b), len(b[0])
    return [
        [sum(a[i][k] * b[k][j] for k in range(m)) for j in range(p)] for i in range(n)
    ]


def _expm(a: Matrix) -> Matrix:
    """Matrix exponential: scaling and squaring with a Taylor series."""
    n = len(a)
    norm = max(sum(abs(x) for x in row) for row in a)
    squarings = max(0, math.ceil(math.log2(norm)) + 1) if norm > 0.5 else 0
    scale = 2.0**squarings
    scaled = [[x / scale for x in row] for row in a]
    result = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    term = [row[:] for row in result]
    for k in range(1, 16):
        term = [[x / k for x in row] for row in _matmul(term, scaled)]
        result = [
            [r + t for r, t in zip(rr, tr, strict=True)]
            for rr, tr in zip(result, term, strict=True)
        ]
    for _ in range(squarings):
        result = _matmul(result, result)
    return result


@dataclass(frozen=True)
class Discrete:
    """The model for one time step, with the steady-state Kalman gain."""

    dt: float  # h
    f: Matrix  # state transition
    g: Vector  # input (AC power) → state
    gain: Vector  # Kalman gain for the air temperature measurement
    variance: float  # innovation variance (K²)
    covariance: Matrix  # predicted state covariance (steady state)


def discretize(p: RoomParams, dt: float) -> Discrete:
    """Exact discretization of the model for a time step of dt hours."""
    ca, cm, h = p.air_capacity, p.mass_capacity, p.coupling
    s = p.gain_share_air
    tau = p.heat_delay / 60
    # Augmented [[A, B], [0, 0]] gives F and G in one exponential.
    a = [
        [-h / ca, h / ca, s / ca, 1 / ca, 0.0],
        [h / cm, -h / cm, (1 - s) / cm, 0.0, 0.0],
        [0.0, 0.0, 0.0, 0.0, 0.0],
        [0.0, 0.0, 0.0, -1 / tau, 1 / tau],
        [0.0, 0.0, 0.0, 0.0, 0.0],
    ]
    e = _expm([[x * dt for x in row] for row in a])
    f = [row[:N] for row in e[:N]]
    g = [e[i][N] for i in range(N)]

    noise = [
        TEMPERATURE_NOISE**2 * dt,
        TEMPERATURE_NOISE**2 * dt,
        p.gain_noise**2 * dt,
        1e-6,
    ]
    r = p.sensor_noise**2
    # Riccati iteration to the steady-state predicted covariance.
    cov = [[0.0] * N for _ in range(N)]
    for i in range(N):
        cov[i][i] = [1.0, 4.0, 300.0**2, 1.0][i]
    gain = [0.0] * N
    variance = r
    for _ in range(5000):
        variance = cov[0][0] + r
        gain = [cov[i][0] / variance for i in range(N)]
        updated = [
            [cov[i][j] - gain[i] * cov[0][j] for j in range(N)] for i in range(N)
        ]
        fp = _matmul(f, updated)
        new = [
            [
                sum(fp[i][k] * f[j][k] for k in range(N))
                + (noise[i] if i == j else 0.0)
                for j in range(N)
            ]
            for i in range(N)
        ]
        change = max(abs(new[i][j] - cov[i][j]) for i in range(N) for j in range(N))
        cov = new
        if change < 1e-12 * max(1.0, cov[GAIN][GAIN]):
            break
    variance = cov[0][0] + r
    gain = [cov[i][0] / variance for i in range(N)]
    return Discrete(dt, f, g, gain, variance, cov)


def _predict(d: Discrete, x: Vector, power: float) -> Vector:
    f, g = d.f, d.g
    return [
        f[i][0] * x[0] + f[i][1] * x[1] + f[i][2] * x[2] + f[i][3] * x[3] + g[i] * power
        for i in range(N)
    ]


# --------------------------------------------------------------------- filter


class RoomFilter:
    """Live estimate of air and mass temperature, net heat gain and AC heat."""

    def __init__(self, params: RoomParams) -> None:
        """Initialize; call reset() with the first temperature."""
        self.params = params
        self._discrete: dict[float, Discrete] = {}
        self.x: Vector | None = None
        self.innovation: float | None = None

    def _model(self, dt: float) -> Discrete:
        key = round(dt * 3600)
        if key not in self._discrete:
            if len(self._discrete) > 8:
                self._discrete.clear()
            self._discrete[key] = discretize(self.params, key / 3600)
        return self._discrete[key]

    def reset(
        self, temperature: float, gain: float = 0.0, delivered: float = 0.0
    ) -> None:
        """Start from a measured temperature (mass assumed at the same)."""
        self.x = [temperature, temperature, gain, delivered]

    def step(self, dt: float, power: float, measured: float | None) -> None:
        """Advance by dt hours with the AC power, then correct with a measurement."""
        if self.x is None:
            if measured is None:
                return
            self.reset(measured)
        assert self.x is not None
        d = self._model(dt)
        x = _predict(d, self.x, power)
        if measured is not None:
            e = measured - x[AIR]
            self.innovation = e
            x = [x[i] + d.gain[i] * e for i in range(N)]
        self.x = x

    def forecast(
        self, hours: float, power: float = 0.0, step: float = 1 / 60
    ) -> Vector:
        """State after `hours` with constant AC power and the current gain."""
        assert self.x is not None
        d = self._model(step)
        x = self.x
        for _ in range(max(1, round(hours / step))):
            x = _predict(d, x, power)
        return x

    @property
    def ready(self) -> bool:
        """Whether there is an estimate."""
        return self.x is not None

    @property
    def air_temperature(self) -> float | None:
        """Filtered air temperature (°C)."""
        return self.x[AIR] if self.x else None

    @property
    def mass_temperature(self) -> float | None:
        """Estimated temperature of the room's mass (°C)."""
        return self.x[MASS] if self.x else None

    @property
    def net_gain(self) -> float | None:
        """Net heat flow into the room without the AC (W)."""
        return self.x[GAIN] if self.x else None

    @property
    def delivered(self) -> float | None:
        """AC heat that reaches the room right now (W)."""
        return self.x[DELIVERED] if self.x else None

    def mass_rate(self) -> float | None:
        """Rate of change of the mass temperature (K/h)."""
        if not self.x:
            return None
        p = self.params
        x = self.x
        return (
            p.coupling * (x[AIR] - x[MASS]) + (1 - p.gain_share_air) * x[GAIN]
        ) / p.mass_capacity


def warm_state(params: RoomParams, series: Series) -> Vector | None:
    """Filter state after running over the series (warm start after a fit)."""
    f = RoomFilter(params)
    for measured, power in zip(series.temperature, series.power, strict=True):
        f.step(series.dt, power, measured)
    return f.x


# ------------------------------------------------------------------------ fit


@dataclass(frozen=True)
class Series:
    """Samples on a regular grid for fitting."""

    dt: float  # h
    temperature: list[float | None]  # measured air temperature
    power: list[float]  # thermal AC power (W)
    weight: list[bool]  # whether the sample counts for the fit

    def __len__(self) -> int:
        """Number of samples."""
        return len(self.power)


@dataclass(frozen=True)
class FitResult:
    """Fitted parameters and how well they predict."""

    params: RoomParams
    cost: float  # mean rms forecast error over the horizons (K)
    forecast_errors: list[float]  # rms error per horizon in HORIZONS (K)
    samples: int
    evaluations: int


BURN_IN_HOURS = 3.0


def negative_log_likelihood(p: RoomParams, series: Series) -> tuple[float, float, int]:
    """Gaussian negative log-likelihood of the filter's innovations.

    Returns the cost per counted sample, the rms innovation and the count.
    """
    d = discretize(p, series.dt)
    first = next((t for t in series.temperature if t is not None), None)
    if first is None:
        return math.inf, math.inf, 0
    x = [first, first, 0.0, series.power[0]]
    burn = BURN_IN_HOURS / series.dt
    f0, f1, f2, f3 = d.f
    g = d.g
    k = d.gain
    total = 0.0
    count = 0
    for i, (measured, power) in enumerate(
        zip(series.temperature, series.power, strict=True)
    ):
        x0, x1, x2, x3 = x
        x = [
            f0[0] * x0 + f0[1] * x1 + f0[2] * x2 + f0[3] * x3 + g[0] * power,
            f1[0] * x0 + f1[1] * x1 + f1[2] * x2 + f1[3] * x3 + g[1] * power,
            f2[0] * x0 + f2[1] * x1 + f2[2] * x2 + f2[3] * x3 + g[2] * power,
            f3[0] * x0 + f3[1] * x1 + f3[2] * x2 + f3[3] * x3 + g[3] * power,
        ]
        if measured is None:
            continue
        e = measured - x[0]
        x = [x[0] + k[0] * e, x[1] + k[1] * e, x[2] + k[2] * e, x[3] + k[3] * e]
        if i >= burn and series.weight[i]:
            total += e * e
            count += 1
    if count == 0:
        return math.inf, math.inf, 0
    # Steady-state filter: the innovation variance is the same for all samples.
    cost = total / count / d.variance + math.log(d.variance)
    return cost, math.sqrt(total / count), count


HORIZONS = (0.25, 0.5, 1.0)  # h: forecasts the fit optimizes for
FORECAST_EVERY = 0.25  # h between forecast starts


def prediction_errors(
    p: RoomParams,
    series: Series,
    horizons: Sequence[float] = HORIZONS,
    every: float = FORECAST_EVERY,
) -> list[list[float]]:
    """Air temperature forecast errors per horizon.

    The filter runs over the series; every `every` hours a forecast starts
    from the filtered state and runs with the real AC power (and the gain
    held constant). Its error against the measured temperature at each
    horizon is collected.
    """
    d = discretize(p, series.dt)
    steps = [max(1, round(h / series.dt)) for h in horizons]
    longest = max(steps)
    stride = max(1, round(every / series.dt))
    burn = int(BURN_IN_HOURS / series.dt)
    temps, powers, weight = series.temperature, series.power, series.weight
    first = next((t for t in temps if t is not None), None)
    errors: list[list[float]] = [[] for _ in horizons]
    if first is None:
        return errors
    f0, f1, f2, f3 = d.f
    g0, g1, g2, g3 = d.g
    k0, k1, k2, k3 = d.gain
    x0, x1, x2, x3 = first, first, 0.0, powers[0]
    n = len(powers)
    for i in range(n):
        power = powers[i]
        x0, x1, x2, x3 = (
            f0[0] * x0 + f0[1] * x1 + f0[2] * x2 + f0[3] * x3 + g0 * power,
            f1[0] * x0 + f1[1] * x1 + f1[2] * x2 + f1[3] * x3 + g1 * power,
            f2[0] * x0 + f2[1] * x1 + f2[2] * x2 + f2[3] * x3 + g2 * power,
            f3[0] * x0 + f3[1] * x1 + f3[2] * x2 + f3[3] * x3 + g3 * power,
        )
        measured = temps[i]
        if measured is not None:
            e = measured - x0
            x0, x1, x2, x3 = x0 + k0 * e, x1 + k1 * e, x2 + k2 * e, x3 + k3 * e
        if i < burn or (i - burn) % stride or i + longest >= n:
            continue
        y0, y1, y2, y3 = x0, x1, x2, x3
        h = 0
        for j in range(1, longest + 1):
            power = powers[i + j]
            y0, y1, y2, y3 = (
                f0[0] * y0 + f0[1] * y1 + f0[2] * y2 + f0[3] * y3 + g0 * power,
                f1[0] * y0 + f1[1] * y1 + f1[2] * y2 + f1[3] * y3 + g1 * power,
                f2[0] * y0 + f2[1] * y1 + f2[2] * y2 + f2[3] * y3 + g2 * power,
                f3[0] * y0 + f3[1] * y1 + f3[2] * y2 + f3[3] * y3 + g3 * power,
            )
            if j == steps[h]:
                target = temps[i + j]
                if target is not None and weight[i + j]:
                    errors[h].append(y0 - target)
                h += 1
                if h == len(steps):
                    break
    return errors


def prediction_cost(p: RoomParams, series: Series) -> tuple[float, list[float]]:
    """Mean of the rms forecast errors over the horizons, and each of them."""
    errors = prediction_errors(p, series)
    rms = [
        math.sqrt(sum(e * e for e in es) / len(es)) if es else math.inf for es in errors
    ]
    return sum(rms) / len(rms), rms


def nelder_mead(
    func: Callable[[Vector], float],
    start: Vector,
    step: float = 0.5,
    max_evaluations: int = 800,
    tolerance: float = 1e-6,
) -> tuple[Vector, float, int]:
    """Minimize func (adaptive Nelder-Mead, Gao & Han 2012)."""
    n = len(start)
    alpha, beta = 1.0, 1.0 + 2.0 / n
    gamma, delta = 0.75 - 1.0 / (2 * n), 1.0 - 1.0 / n
    simplex = [start[:]]
    for i in range(n):
        point = start[:]
        point[i] += step
        simplex.append(point)
    values = [func(p) for p in simplex]
    evaluations = n + 1
    while evaluations < max_evaluations:
        order = sorted(range(n + 1), key=lambda i: values[i])
        simplex = [simplex[i] for i in order]
        values = [values[i] for i in order]
        if abs(values[-1] - values[0]) <= tolerance * (abs(values[0]) + 1e-12):
            break
        centroid = [sum(p[j] for p in simplex[:-1]) / n for j in range(n)]
        worst = simplex[-1]

        def toward(t: float, w: Vector = worst, c: Vector = centroid) -> Vector:
            return [c[j] + t * (w[j] - c[j]) for j in range(n)]

        reflected = toward(-alpha)
        fr = func(reflected)
        evaluations += 1
        if fr < values[0]:
            expanded = toward(-alpha * beta)
            fe = func(expanded)
            evaluations += 1
            simplex[-1], values[-1] = (expanded, fe) if fe < fr else (reflected, fr)
            continue
        if fr < values[-2]:
            simplex[-1], values[-1] = reflected, fr
            continue
        outside = fr < values[-1]
        contracted = toward(-gamma if outside else gamma)
        fc = func(contracted)
        evaluations += 1
        if fc < (fr if outside else values[-1]):
            simplex[-1], values[-1] = contracted, fc
            continue
        best = simplex[0]
        for i in range(1, n + 1):
            simplex[i] = [best[j] + delta * (simplex[i][j] - best[j]) for j in range(n)]
            values[i] = func(simplex[i])
            evaluations += 1
    i = min(range(n + 1), key=lambda i: values[i])
    return simplex[i], values[i], evaluations


def initial_guesses(series: Series) -> list[RoomParams]:
    """Starting points scaled to the typical AC power of this room."""
    running = sorted(abs(p) for p in series.power if abs(p) > 1)
    typical = running[len(running) // 2] if running else 1000.0
    # A room heats its air by roughly 5-15 K/h at full AC power.
    base = RoomParams(
        air_capacity=typical / 15,
        mass_capacity=typical / 1.5,
        coupling=typical / 5,
        gain_noise=typical / 25,
    )
    return [
        base,
        replace(base, air_capacity=base.air_capacity * 3, coupling=base.coupling / 2),
        replace(base, mass_capacity=base.mass_capacity * 4, heat_delay=6.0),
    ]


def fit(
    series: Series,
    start: RoomParams | None = None,
    max_evaluations: int = 900,
    restarts: bool = True,
) -> FitResult | None:
    """Fit the parameters for the best 15-60 min forecasts (Nelder-Mead).

    One-step prediction errors would reward a filter that chases every
    wiggle with the gain; forecasts over the horizons that matter keep the
    physical parameters honest.
    """
    if len(series) < (BURN_IN_HOURS + 6) / series.dt:
        return None

    def cost(v: Vector) -> float:
        value, _ = prediction_cost(_from_vector(v), series)
        return value if math.isfinite(value) else 1e9

    starts = ([start] if start else []) + (
        initial_guesses(series) if restarts or start is None else []
    )
    best: tuple[Vector, float] | None = None
    evaluations = 0
    budget = max_evaluations // (len(starts) + 1)
    for guess in starts:
        v, value, used = nelder_mead(cost, _to_vector(guess), max_evaluations=budget)
        evaluations += used
        if best is None or value < best[1]:
            best = (v, value)
    assert best is not None
    v, value, used = nelder_mead(cost, best[0], step=0.1, max_evaluations=budget)
    evaluations += used
    params = _from_vector(v)
    value, rms = prediction_cost(params, series)
    return FitResult(
        params=params,
        cost=value,
        forecast_errors=rms,
        samples=len(series),
        evaluations=evaluations,
    )
