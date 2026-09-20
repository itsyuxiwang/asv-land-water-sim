"""ASV fleet operator: the water side of the platform.

Answers, for every vessel and every operator batch, the five operational
questions of the demo:

    where is the vessel         `Vessel.asv` (point model in asv.py: waypoints, speed ramp)
    how many can it carry       `Vessel.capacity`
    which dock does it go to    the dock a `Policy.depart()` returns
    when does it leave          `TimetablePolicy` / `DemandPolicy`
    how is the fleet dispatched `Operator.batch()`, called every `dt` seconds

The operator never touches SUMO. `run.py` hands it the queues standing on the
piers and executes the decisions (boarding = removing persons from SUMO,
landing = re-creating them on the other bank). That split is what makes the
policies testable on their own and swappable for e.g. an optimisation model.

中文说明：运营商回答五个问题——船在哪（Vessel.asv）、能坐多少人（capacity）、
去哪个码头（策略 depart() 的返回值）、什么时候发船（两种策略）、怎么调度（batch()，每 dt 秒一次）。
运营商从不碰 SUMO：run.py 把码头队列交给它，再执行它的决策（上船 = 从 SUMO 删人，下船 = 在对岸重建）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Protocol

from asv import ASV
from layout import DOCKS, SEA_LANES

MIN_DWELL = 20.0       # 在码头至少停这么久（卸客 + 上客）[s]
BERTH_SPACING = 9.0    # 同一码头的多艘船沿 y 错开的间距 [m]，画图时不重叠
EMA_ALPHA = 0.3        # "发布的期望等待"的指数移动平均系数：越大对最近一次上船的等待越敏感


# --------------------------------------------------------------------------- data 数据类
@dataclass
class Waiting:
    """One traveller standing on a pier (what `run.py` reads out of SUMO). 码头上的一个人。"""
    pid: str
    since: float          # 他到码头的时刻（仿真秒）


@dataclass
class Trip:
    """One sailing from `origin` to `target`. 一个航次。"""
    vessel: str
    origin: str
    target: str
    dep: float
    pax: int              # 载了几个人（0 = 空驶）
    arr: float | None = None   # 到岸后才填


@dataclass
class Vessel:
    """一艘船的运营状态；物理位置在 `asv` 里。"""
    name: str
    asv: ASV
    capacity: int
    berth: int                                  # 泊位编号，在每个码头都一样
    dock: str                                   # 停靠的码头；航行中表示出发码头
    state: str = "docked"                       # docked（停着）| sailing（在开）
    target: str | None = None                   # 航行中的目的码头
    docked_since: float = 0.0                   # 这次停靠开始的时刻
    onboard: list[str] = field(default_factory=list)   # 船上的人：只有名字，人本身已从 SUMO 删除
    distance: float = 0.0                       # 总航程 [m]
    occupied_distance: float = 0.0              # 有人在船上时的航程 [m]，两者之差 = 空驶
    trips: list[Trip] = field(default_factory=list)

    def berth_pos(self, dock: str) -> tuple[float, float]:
        """本船在某码头的泊位坐标（按 berth 编号沿 y 错开）。"""
        x, y = DOCKS[dock]["pos"]
        return (x, y + BERTH_SPACING * self.berth)

    def ready_at(self, dock: str, now: float) -> bool:
        """Docked at `dock` long enough to have unloaded and boarded.
        停在 dock、且已停够 MIN_DWELL：这艘船现在"可以"开走。"""
        return self.state == "docked" and self.dock == dock and now - self.docked_since >= MIN_DWELL


@dataclass
class Departure:
    """A decision of the operator that `run.py` has to execute. 一条发船决策。"""
    vessel: Vessel
    target: str
    boarding: list[Waiting]      # 这次带走的人（run.py 负责把他们从 SUMO 删掉）


def other_dock(dock: str) -> str:
    """两码头场景下的"对岸"。"""
    return next(d for d in DOCKS if d != dock)


# --------------------------------------------------------------------------- policies 发船策略
class Policy(Protocol):
    """When does a docked vessel leave, and for which dock?
    策略接口：只要实现这两个方法，就能塞进 Operator。"""
    name: str

    def prior_wait(self) -> float:
        """Expected wait published before any traveller has boarded [s]. 还没人上过船时发布的期望等待。"""

    def depart(self, vessel: Vessel, now: float, queue: list[Waiting],
               queues: dict[str, list[Waiting]]) -> str | None:
        """Target dock if the vessel should leave now, else None. 现在该走就返回目的码头，否则 None。"""


class TimetablePolicy:
    """Leave the main dock at fixed slots (every `headway` s), turn around at
    the far dock after the minimum dwell. Full or empty does not matter.
    时刻表：主码头按固定班距发船，对岸停够最短时间就返航，有没有人无关。"""

    name = "timetable"

    def __init__(self, headway: float, main_dock: str = "W") -> None:
        self.headway, self.main_dock = headway, main_dock
        self.next_slot = 0.0              # 下一班的时刻

    def prior_wait(self) -> float:
        return self.headway / 2           # 随机到达的乘客平均等半个班距

    def depart(self, vessel, now, queue, queues):
        if vessel.dock == self.main_dock:
            if now < self.next_slot:      # 还没到班次时刻
                return None
            self.next_slot = (math.floor(now / self.headway) + 1) * self.headway   # 下一个整倍数
        return other_dock(vessel.dock)


class DemandPolicy:
    """Leave when full, or when the first traveller in the queue has waited
    `max_wait`. An idle vessel with nobody to carry repositions to the dock
    where the demand is (longest queue, else the dock with the higher expected
    demand), which is the reactive idle-vehicle assignment of Zhou et al.
    需求响应：满员或队首等够 max_wait 就走；没人时空驶去需求所在的码头（论文的空车重定位）。"""

    name = "demand"

    def __init__(self, max_wait: float, demand_prior: dict[str, float]) -> None:
        self.max_wait, self.demand_prior = max_wait, demand_prior   # demand_prior：各码头的先验需求权重

    def prior_wait(self) -> float:
        return self.max_wait / 2

    def depart(self, vessel, now, queue, queues):
        if queue:
            full = len(queue) >= vessel.capacity
            impatient = now - queue[0].since >= self.max_wait     # 队首那个人等够了
            return other_dock(vessel.dock) if (full or impatient) else None
        # 没人等：找"队列最长、其次先验需求最高"的码头；不是当前码头且值得跑一趟就空驶过去
        best = max(queues, key=lambda d: (len(queues[d]), self.demand_prior[d]))
        worth_moving = bool(queues[best]) or self.demand_prior[best] > self.demand_prior[vessel.dock]
        return best if best != vessel.dock and worth_moving else None


# --------------------------------------------------------------------------- operator 运营商
class Operator:
    def __init__(self, fleet: int, capacity: int, policy: Policy, cruise: float = 3.0,
                 home_dock: str = "W") -> None:
        self.policy, self.capacity = policy, capacity
        x, y = DOCKS[home_dock]["pos"]
        # 所有船一开始停在主码头，按泊位编号排开
        self.vessels = [Vessel(f"asv{k}", ASV(x, y + BERTH_SPACING * k, name=f"asv{k}", cruise=cruise),
                               capacity, berth=k, dock=home_dock) for k in range(fleet)]
        self.expected_wait = policy.prior_wait()     # 发布给乘客的期望等待 [s]，初值来自策略
        self.waits: list[float] = []                 # 每个上船者实际等了多久

    # -- information published to the traveller model 发布给乘客模型的信息 -------------------
    def offer(self) -> float:
        """Expected waiting time at the main pier: an exponential moving average
        of the waits realised so far (what a real-time app would show).
        主码头的期望等待：最近上船者实际等待的指数移动平均，相当于 app 上显示的"预计等待"。"""
        return self.expected_wait

    def record_wait(self, wait: float) -> None:
        self.waits.append(wait)
        self.expected_wait += EMA_ALPHA * (wait - self.expected_wait)   # 向新观测靠近 30 %

    # -- the time-based dispatch batch 每 dt 秒一次的批处理 ---------------------------------
    def batch(self, now: float, queues: dict[str, list[Waiting]]) -> list[Departure]:
        """Called every dt seconds with the queue on every pier. At each dock only
        the vessel that has been there longest boards; the others hold.
        每个码头一次只让停得最久的那艘船做决定，其余排队等——这就是"泊位一次服务一艘船"。"""
        decisions = []
        for dock, queue in queues.items():
            ready = [v for v in self.vessels if v.ready_at(dock, now)]
            if not ready:
                continue
            vessel = min(ready, key=lambda v: v.docked_since)          # 停得最久的
            target = self.policy.depart(vessel, now, queue, queues)    # 策略说走不走、去哪
            if target is not None:
                decisions.append(Departure(vessel, target, queue[:vessel.capacity]))   # 先到先上，最多 capacity 人
        return decisions

    def depart(self, dep: Departure, now: float) -> None:
        """Execute a departure: passengers are on board, set course to the target.
        执行发船：记名单、记等待、开一个航次、给船一串航点（中间航道 + 目的泊位）。"""
        v = dep.vessel
        v.onboard = [w.pid for w in dep.boarding]
        for w in dep.boarding:
            self.record_wait(now - w.since)
        v.trips.append(Trip(v.name, v.dock, dep.target, dep=now, pax=len(v.onboard)))
        v.asv.set_route(SEA_LANES[(v.dock, dep.target)] + [v.berth_pos(dep.target)])
        v.state, v.target = "sailing", dep.target

    # -- physics 船的运动 ------------------------------------------------------------------
    def step(self, now: float, dt: float) -> list[Vessel]:
        """Advance every vessel by dt. Returns the vessels that just arrived (their
        `onboard` list is what `run.py` has to land).
        推进所有航行中的船 dt 秒；返回刚到岸的船，run.py 负责把它们的 onboard 放回 SUMO。"""
        arrived = []
        for v in self.vessels:
            if v.state != "sailing":
                continue
            v.asv.step(dt)
            sailed = v.asv.u * dt
            v.distance += sailed
            if v.onboard:
                v.occupied_distance += sailed          # 有人在船上的里程，用来算空驶比例
            if v.asv.arrived():
                v.state, v.dock, v.target = "docked", v.target, None
                v.docked_since = now
                v.trips[-1].arr = now
                arrived.append(v)
        return arrived

    # -- KPIs 指标 ---------------------------------------------------------------------------
    def trips(self) -> list[Trip]:
        """All completed sailings of the fleet. 全船队已完成的航次。"""
        return [t for v in self.vessels for t in v.trips if t.arr is not None]

    def summary(self) -> dict:
        trips = self.trips()
        n, pax = len(trips), sum(t.pax for t in trips)
        dist = sum(v.distance for v in self.vessels)
        occupied = sum(v.occupied_distance for v in self.vessels)
        return {
            "trips": n,
            "empty_trips": sum(1 for t in trips if t.pax == 0),          # 空驶航次
            "pax_per_trip": pax / max(1, n),
            "load_factor": pax / max(1, n * self.capacity),              # 载客率 = 人次 / (航次 × 容量)
            "distance_km": dist / 1000,
            "empty_distance_share": (dist - occupied) / dist if dist else 0.0,   # 空驶里程比例（≈ 论文 empty VKT）
            "crossing_time": sum(t.arr - t.dep for t in trips) / n if n else 0.0,
        }
