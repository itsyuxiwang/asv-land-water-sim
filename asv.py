"""Minimal vessel model: a point that follows waypoints at cruise speed.

Deliberately simple, because this demo is about operations (where the vessels
are, when they leave, whom they carry, which dock they head for), not about
vessel control. The only dynamics is a speed ramp: accelerate to cruise, brake
so as to arrive at the last waypoint at rest.

The operator only uses `set_route`, `arrived`, `step` and the attributes
`x, y, u`, so this class can be swapped for a real guidance/control model
without touching anything else.

中文说明：船是一个点，沿航点列表走；只有巡航速度和起停加减速，没有航向、没有潮流、
没有控制器。运营层只依赖 set_route / arrived / step / x / y / u 六个名字，
换成带制导和动力学的模型时其余代码不用改。
"""

from __future__ import annotations

import math

MIN_SPEED = 0.3     # 快到航点时的最低速度 [m/s]：不设的话 v=sqrt(2ad) 在 d→0 时趋于 0，永远到不了


class ASV:
    def __init__(self, x: float, y: float, name: str = "asv",
                 cruise: float = 3.0, accel: float = 0.5) -> None:
        self.name, self.x, self.y = name, x, y
        self.cruise, self.accel = cruise, accel     # 巡航速度 [m/s]、加/减速度 [m/s^2]
        self.u = 0.0                                # 当前速度 [m/s]
        self.wps: list[tuple[float, float]] = []    # 当前航线的航点列表
        self.k = 0                                  # 下一个要去的航点下标

    def set_route(self, wps) -> None:
        """从当前位置出发，走一串新的航点。"""
        self.wps, self.k = list(wps), 0

    def arrived(self) -> bool:
        """所有航点都走完了。"""
        return self.k >= len(self.wps)

    def remaining(self) -> float:
        """Path length still to sail along the waypoints [m]. 沿航点折线还剩多少米。"""
        pts = [(self.x, self.y)] + self.wps[self.k:]
        return sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))

    def step(self, dt: float) -> None:
        """Advance by dt seconds along the route. 沿航线前进 dt 秒。"""
        if self.arrived():
            self.u = 0.0
            return
        # 速度取三者最小：本步能加到的速度、巡航速度、从剩余路程恰好能刹停的速度 v = sqrt(2 a d)
        v_stop = math.sqrt(2.0 * self.accel * self.remaining())
        self.u = min(self.u + self.accel * dt, self.cruise, max(v_stop, MIN_SPEED))
        travel = self.u * dt
        # 用 while 而不是 if：一步 3 m 可能跨过一个很近的航点，剩余路程要继续消耗在下一段上
        while travel > 0.0 and not self.arrived():
            tx, ty = self.wps[self.k]
            d = math.dist((self.x, self.y), (tx, ty))
            if d <= travel:                 # 本步能到航点：跳到那里，余下路程留给下一段
                self.x, self.y, travel = tx, ty, travel - d
                self.k += 1
            else:                           # 到不了：沿方向走 travel 米
                self.x += travel * (tx - self.x) / d
                self.y += travel * (ty - self.y) / d
                travel = 0.0
