from collections import deque
from math import sqrt

DEAD_W = 25  # measured P1 noise band, clean nights 14/15 aug
STEP_W = 300  # flank floor
STEP_SCALE = 8.0  # flank threshold: this many times the running mean movement
FAST_SOLAR_W = 500  # in the solar regime chase everything above this: the energy has to move anyway
RAMP_W_S = 100  # solar ramp floor; scales up with the running movement
RAMP_SCALE = 8.0
QUIET_S = 2.2  # no decisions while our own correction settles; set to the measured fleet lag
SOLAR_STD_W = 80  # variability above this means the solar regime
EMA_S = 120.0  # time constant of the running statistics
FAST_S = 8.0  # fast movement estimate, feeds the solar ramp; tuned on the scenario windows together with RAMP_SCALE


class Stabilizer:
    def __init__(self) -> None:
        self.target = 0.0  # the setpoint we last let through
        self.quiet_until = 0.0
        self.pending = False  # first out-of-band sample of the two-sample confirmation
        self.prev_demand: float | None = None
        self.move = 0.0  # running mean of the per-second movement
        self.fast_move = 0.0  # same, but fast: follows bursts within seconds
        self.dvar = self.dmean = 0.0  # running variance/mean of the demand
        self.svar = self.smean = 0.0  # running variance/mean of the solar signal
        self.flanks: deque[tuple[float, int]] = deque()  # (time, sign) of recent flanks
        self.regime = "quiet"

    def process(self, now: float, demand: float, solar: float, elapsed: float) -> int | None:
        # demand = p1 + output + correction: what the fleet should deliver for net zero
        a = min(1.0, elapsed / EMA_S)
        if self.prev_demand is None:
            self.prev_demand = demand
            self.dmean = demand
            self.smean = solar
        d1 = demand - self.prev_demand
        self.prev_demand = demand
        self.move += (abs(d1) / elapsed - self.move) * a
        self.fast_move += (abs(d1) / elapsed - self.fast_move) * min(1.0, elapsed / FAST_S)
        self.dmean += (demand - self.dmean) * a
        self.dvar += ((demand - self.dmean) ** 2 - self.dvar) * a
        self.smean += (solar - self.smean) * a
        self.svar += ((solar - self.smean) ** 2 - self.svar) * a

        step_thr = max(STEP_W, STEP_SCALE * self.move)
        if abs(d1) > step_thr:
            self.flanks.append((now, 1 if d1 > 0 else -1))
        while self.flanks and now - self.flanks[0][0] > 300:
            self.flanks.popleft()

        flanks = list(self.flanks)
        if len(flanks) >= 2 and any(x[1] != y[1] for x, y in zip(flanks, flanks[1:])):
            self.regime = "cycle"
        elif sqrt(self.svar) > SOLAR_STD_W or (not flanks and sqrt(self.dvar) > SOLAR_STD_W):
            self.regime = "solar"
        else:
            self.regime = "quiet"

        if now < self.quiet_until:
            return None
        error = demand - self.target
        target = None
        if self.regime == "solar":
            if abs(d1) > FAST_SOLAR_W:
                target = demand
            elif abs(error) > DEAD_W:
                ramp = max(RAMP_W_S, RAMP_SCALE * self.fast_move) * elapsed
                target = self.target + max(-ramp, min(ramp, error))
            else:
                self.pending = False
        elif self.regime == "cycle" and abs(d1) > step_thr:
            target = demand  # jump straight to the new level, no trimming
        elif abs(error) > (2 * DEAD_W if self.regime == "cycle" else DEAD_W):
            if self.pending:
                target = demand
            else:
                self.pending = True
        else:
            self.pending = False
        if target is None:
            return None
        self.pending = False
        self.quiet_until = now + QUIET_S
        self.target = target
        return int(target)
