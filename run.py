"""Integrated land-water simulation: travellers choose, vessels operate.

Three pieces advance in lock-step, one simulated second at a time:

    land        SUMO           bus line B1, metro line M1, pedestrians, stops
    water       asv_operator   vessel fleet (point model from asv.py), docks, policies
    behaviour   travellers     demand -> mode choice -> tour, injected through TraCI

Every `--dt` seconds (30 s by default) the operator and the traveller model act
as one batch, following the time-based framework of Zhou et al. (2026):
travellers who depart in the coming interval receive the operator's current
expected wait, choose between Bus->ASV->Walk and Bus->Metro->Walk and are
added to SUMO; then the operator decides for each docked vessel whether it
leaves, with whom on board, and for which dock.

    python run.py --fleet 2 --policy demand
    python run.py --fleet 1 --policy timetable --headway 300 --metro-period 600
    python run.py --gui ...

中文说明：主程序。陆上（SUMO）、水上（asv_operator）、行为（travellers）三个模型每秒对齐一次；
每 dt 秒做一批决策：先让即将出发的乘客拿着运营商发布的期望等待选链并注入 SUMO，
再让运营商决定每艘停着的船走不走、带谁、去哪。上船 = 从 SUMO 删人，下船 = 在东岸重建。
"""

from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import asdict

import numpy as np
import sumolib
import traci
import traci.constants as tc
from sumolib import checkBinary

from asv_operator import DemandPolicy, Operator, TimetablePolicy, Waiting
from build_network import write_lines
from layout import (BUS_LINE, CFG_FILE, DOCKS, EAST_WALK_EDGE, GUI_VIEW, HERE, LANDING_POS,
                    METRO_LINE, NET_FILE, OFFICE_EDGE, OFFICE_POS, PIER_EDGE, RESULTS_DIR,
                    SEA_LANES, VIEW_FILE, WALK_SPEED, WATER)
from travellers import (ASV, METRO, ChoiceParams, choose_mode, create_tour, generate_demand,
                        plan_alternatives)

DEFAULT_END = 3600
GIF_STEP = 4


def gif_frame_times(gif_seconds: int, end_seconds: int) -> list[int]:
    """Simulation times rendered into a GIF (4 simulated seconds per frame)."""
    return list(range(0, min(gif_seconds, end_seconds), GIF_STEP))


def animation_crowd_counts(travellers, metro_waiting: int, now: float) -> tuple[int, int, int]:
    """People waiting at the pier/station and people who have reached the office."""
    pier_waiting = sum(trav.mode == ASV and trav.t_pier is not None and trav.t_pier <= now
                       and (trav.t_board is None or trav.t_board > now) for trav in travellers)
    office_arrived = sum(trav.t_arrive is not None and trav.t_arrive <= now for trav in travellers)
    return pier_waiting, metro_waiting, office_arrived


# ------------------------------------------------------------------------ scenario 场景参数
def parse_args() -> argparse.Namespace:
    """四组参数，对应四个模块：水上、陆上、行为、耦合框架。"""
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--gui", action="store_true")
    water = p.add_argument_group("water side")
    water.add_argument("--fleet", type=int, default=2, help="number of vessels")
    water.add_argument("--capacity", type=int, default=12, help="passengers per vessel")
    water.add_argument("--cruise", type=float, default=3.0, help="cruise speed [m/s]")
    water.add_argument("--policy", choices=["timetable", "demand"], default="demand")
    water.add_argument("--headway", type=float, default=300,
                       help="timetable: departure interval from the west dock [s]")
    water.add_argument("--max-wait", type=float, default=120,
                       help="demand: leave when the first traveller has waited this long [s]")
    land = p.add_argument_group("land side")
    land.add_argument("--bus-period", type=int, default=300, help="bus B1 headway [s]")
    land.add_argument("--metro-period", type=int, default=300, help="metro M1 headway [s]")
    behaviour = p.add_argument_group("behaviour")
    behaviour.add_argument("--travellers", type=int, default=300)
    behaviour.add_argument("--demand-end", type=int, default=2400, help="last departure [s]")
    behaviour.add_argument("--asc-asv", type=float, default=0.0,
                           help="constant in favour of the vessel chain [utils]")
    frame = p.add_argument_group("framework")
    frame.add_argument("--dt", type=int, default=30, help="operator / traveller batch interval [s]")
    frame.add_argument("--end", type=int, default=DEFAULT_END, help="simulated duration [s]")
    frame.add_argument("--seed", type=int, default=1)
    frame.add_argument("--screenshot", type=int, default=0,
                       help="with --gui: save results/shot_<tag>_t<T>.png at time T [s]")
    frame.add_argument("--gif", type=int, default=0,
                       help="animate the first N simulated seconds to results/anim_<tag>.gif")
    return p.parse_args()


def scenario_tag(a: argparse.Namespace) -> str:
    """File-name stem with every parameter that changes the outcome.
    文件名里带上所有影响结果的参数，并行跑多个场景不会互相覆盖。"""
    tag = f"{a.policy}_fleet{a.fleet}_cap{a.capacity}_metro{a.metro_period}"
    if a.asc_asv:
        tag += f"_asc{a.asc_asv:g}"
    if a.seed != 1:
        tag += f"_seed{a.seed}"
    if a.end != DEFAULT_END:          # 截图用的短跑不能覆盖正式结果
        tag += f"_end{a.end}"
    return tag


def make_policy(a: argparse.Namespace):
    """按 --policy 造策略对象。demand_prior 是场景知识（早高峰需求都在西岸），不属于策略本身。"""
    if a.policy == "timetable":
        return TimetablePolicy(a.headway)
    return DemandPolicy(a.max_wait, demand_prior={"W": 1.0, "E": 0.0})


def crossing_estimate(cruise: float) -> float:
    """Expected crossing time published to travellers: sea lane length at cruise
    speed plus an allowance for accelerating and braking at the berths.
    告诉乘客的过河时间：航线折线长度 / 巡航速度 + 20 s 起停。从几何算，改了 layout 不用改这里。"""
    pts = [DOCKS["W"]["pos"]] + SEA_LANES[("W", "E")] + [DOCKS["E"]["pos"]]
    length = sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
    return length / cruise + 20.0


def east_walk_estimate() -> float:
    """Expected walk from the landing point to the office [s], read off the network.
    下船点到办公区的步行时间，从路网里读边长算出来。"""
    net = sumolib.net.readNet(NET_FILE)
    return (net.getEdge(EAST_WALK_EDGE).getLength() - LANDING_POS + OFFICE_POS) / WALK_SPEED


# ------------------------------------------------------------------------ SUMO side 启动与背景
def start_sumo(a: argparse.Namespace) -> None:
    """Write the timetable for this scenario and launch SUMO with it.
    先按本场景的班距写一份线路文件，再用 --route-files 覆盖 sumocfg 里的默认文件。"""
    lines = write_lines(a.bus_period, a.metro_period, a.end,
                        path=os.path.join(HERE, f"_lines_b{a.bus_period}_m{a.metro_period}.rou.xml"))
    cmd = [checkBinary("sumo-gui" if a.gui else "sumo"), "-c", CFG_FILE, "--route-files", lines,
           "--start", "--quit-on-end", "--no-warnings", "--end", str(a.end)]
    if a.gui:
        cmd += ["--window-size", "1600,700", "--window-pos", "0,0", "--delay", "50",
                "--gui-settings-file", VIEW_FILE]
    traci.start(cmd)


def draw_scenery(op: Operator, gui: bool) -> None:
    """Water, docks and vessels are decoration: SUMO has no edges on the water.
    水面、码头、船都只是画在界面上的装饰，SUMO 不知道水上有东西。"""
    traci.polygon.add("water", WATER, (150, 200, 255, 255), fill=True, layer=-2)
    for name, dock in DOCKS.items():
        traci.poi.add(f"dock_{name}", *dock["pos"], (90, 60, 30, 255), poiType="dock",
                      layer=1, width=6, height=6)
    for v in op.vessels:
        traci.poi.add(v.name, v.asv.x, v.asv.y, (220, 40, 40, 255), poiType="asv",
                      layer=5, width=7, height=7)
    if gui:
        traci.gui.setBoundary("View #0", *GUI_VIEW)


# ------------------------------------------------------------------------ the coupling 耦合
class Simulation:
    """Owns the three models and the handover between them.
    持有三个模型和运行期状态；方法分三组：交接（SUMO ↔ 船）、批处理的两半、主循环。"""

    def __init__(self, a: argparse.Namespace) -> None:
        self.a = a
        self.tag = scenario_tag(a)
        self.rng = np.random.default_rng(a.seed)
        self.op = Operator(a.fleet, a.capacity, make_policy(a), cruise=a.cruise)
        self.params = ChoiceParams(asc_asv=a.asc_asv, asv_crossing=crossing_estimate(a.cruise),
                                   east_walk=east_walk_estimate())
        self.travellers = generate_demand(a.travellers, a.demand_end, self.rng)
        self.by_pid = {t.pid: t for t in self.travellers}
        self.upcoming = list(self.travellers)     # 还没出发的人，按出发时刻排好，逐批取走
        self.pier_since: dict[str, float] = {}    # pid -> 到码头的时刻
        self.removed: set[str] = set()            # 因上船而从 SUMO 删掉的人
        self.log = {"t": [], "queue": [], "offer": [], "share_asv": [],
                    "traj": {v.name: [] for v in self.op.vessels}}
        if a.gif:
            self.log.update({"bus_traj": [], "metro_traj": [], "metro_waiting": []})

    # -- handover between SUMO and the water side 陆水交接 -------------------------------
    def pier_queue(self, now: float) -> list[Waiting]:
        """Travellers waiting on the pier, first arrival first.
        码头上等船的人 = 在 pier 边上且当前 stage 是"等待"的人；先到先上。"""
        ids = [pid for pid in traci.edge.getLastStepPersonIDs(PIER_EDGE)
               if traci.person.getStage(pid, 0).type == tc.STAGE_WAITING]
        for pid in ids:
            if pid not in self.pier_since:        # 第一次见到他：记下到达时刻
                self.pier_since[pid] = now
                self.by_pid[pid].t_pier = now
        return sorted((Waiting(pid, self.pier_since[pid]) for pid in ids), key=lambda w: w.since)

    def board(self, waiting: list[Waiting], now: float) -> None:
        """Boarding is leaving SUMO: from now on the traveller is a name on a vessel.
        上船 = 从 SUMO 删掉；此后这个人只是船的 onboard 列表里的一个名字。"""
        for w in waiting:
            traci.person.remove(w.pid)
            self.removed.add(w.pid)
            self.by_pid[w.pid].t_board = now

    def land(self, pids: list[str], now: float) -> None:
        """Landing is re-entering SUMO on the east footpath and walking to the office.
        下船 = 在东岸步道上重建一个人（id 加 _e 避免重名），安排他走到办公区。"""
        for pid in pids:
            traci.person.add(pid + "_e", EAST_WALK_EDGE, LANDING_POS)
            traci.person.appendWalkingStage(pid + "_e", [EAST_WALK_EDGE, OFFICE_EDGE],
                                            arrivalPos=OFFICE_POS)
            self.by_pid[pid].t_land = now

    def record_arrivals(self, now: float) -> None:
        """Persons who finished their plan this second reached the office.
        Boarded persons also show up here (they were removed), hence the filter.
        这一秒走完计划、消失的人 = 到了办公区。被 remove 的上船者也会出现在这个名单里，要滤掉。"""
        for pid in traci.simulation.getArrivedPersonIDList():
            base = pid.removesuffix("_e")         # 东岸重建的人叫 p17_e，剥掉后缀找回原记录
            if pid not in self.removed and base in self.by_pid:
                self.by_pid[base].t_arrive = now

    # -- the two halves of a batch 一批决策的两半 ------------------------------------------
    def travellers_decide(self, now: float) -> None:
        """Everyone departing before the next batch chooses a chain and enters SUMO.
        下一批之前出发的人：问两条链各要多久 → Logit 选链 → 注入 SUMO。"""
        while self.upcoming and self.upcoming[0].t_depart < now + self.a.dt:
            trav = self.upcoming.pop(0)
            alts = plan_alternatives(trav, self.op.offer(), self.params)   # 用的是此刻发布的等待
            if alts:
                choose_mode(trav, alts, self.params, self.rng)
                create_tour(trav, alts, now)

    def operator_dispatches(self, now: float, queue: list[Waiting]) -> None:
        """The operator decides per dock; boarding is executed here, on the SUMO side.
        运营商逐码头决策；先 board()（SUMO 里的人消失）再 op.depart()（名字进船），两边不会同时有这个人。"""
        for dep in self.op.batch(now, {"W": queue, "E": []}):     # 东码头没人上船 → 空队列
            self.board(dep.boarding, now)
            self.op.depart(dep, now)
            print(f"[{now:5.0f}s] {dep.vessel.name} {dep.vessel.dock}->{dep.target} "
                  f"with {len(dep.boarding):2d} pax, {len(queue) - len(dep.boarding):3d} left, "
                  f"offer now {self.op.offer():4.0f} s")

    def log_state(self, now: float, queue: list[Waiting]) -> None:
        """每批记一次：时间、码头队列、发布的期望等待、累计选船比例。"""
        decided = [t for t in self.travellers if t.mode]
        self.log["t"].append(now)
        self.log["queue"].append(len(queue))
        self.log["offer"].append(self.op.offer())
        self.log["share_asv"].append(sum(t.mode == ASV for t in decided) / max(1, len(decided)))

    def log_transit_positions(self) -> None:
        """Record transit positions and the west-station queue for optional animation."""
        positions = {BUS_LINE: [], METRO_LINE: []}
        for vid in traci.vehicle.getIDList():
            line = traci.vehicle.getLine(vid)
            if line in positions:
                x, y = traci.vehicle.getPosition(vid)
                positions[line].append((round(x, 1), round(y, 1)))
        self.log["bus_traj"].append(positions[BUS_LINE])
        self.log["metro_traj"].append(positions[METRO_LINE])
        self.log["metro_waiting"].append(traci.busstop.getPersonCount("ms_W"))

    # -- main loop 主循环 -------------------------------------------------------------------
    def run(self) -> None:
        """每秒：SUMO 走 → 收到达 → 数队列 → 船走并卸客 → 同步界面；每 dt 秒再做一批决策。
        顺序很重要：先乘客后运营，乘客拿到的是上一批之后更新过的 offer，运营看到的队列不含刚生成的人。"""
        a = self.a
        start_sumo(a)
        draw_scenery(self.op, a.gui)
        for step in range(a.end):
            traci.simulationStep()                                  # ① 陆：SUMO 走 1 s
            now = traci.simulation.getTime()
            self.record_arrivals(now)                               # ② 谁到办公区了
            queue = self.pier_queue(now)                            # ③ 码头队列（最新位置）
            for v in self.op.step(now, 1.0):                        # ④ 水：船走 1 s，到岸的卸客
                self.land(v.onboard, now)
                v.onboard = []
            for v in self.op.vessels:                               #    同步 POI、记航迹
                traci.poi.setPosition(v.name, v.asv.x, v.asv.y)
                self.log["traj"][v.name].append((round(v.asv.x, 1), round(v.asv.y, 1)))
            if a.gif:
                self.log_transit_positions()
            if step % a.dt == 0:                                    # ⑤ 批处理
                self.travellers_decide(now)
                self.operator_dispatches(now, queue)
                self.log_state(now, queue)                          # ⑥ 记录
            if a.gui and a.screenshot and step == a.screenshot:
                self.screenshot(step)
        traci.close()

    def screenshot(self, step: int) -> None:
        traci.gui.setBoundary("View #0", *GUI_VIEW)
        traci.gui.screenshot("View #0", os.path.join(RESULTS_DIR, f"shot_{self.tag}_t{step}.png"))
        traci.simulationStep()                      # sumo-gui 在下一帧才真正写文件


# ------------------------------------------------------------------------ results 结果
def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def kpis(travellers, op: Operator) -> dict:
    """乘客侧指标（分担率、等待、门到门、计划时间）+ 船队侧指标（summary()，键加前缀 fleet_）。"""
    asv = [t for t in travellers if t.mode == ASV]
    metro = [t for t in travellers if t.mode == METRO]
    waits = [t.t_board - t.t_pier for t in asv if t.t_board is not None]
    return {
        "travellers": len(travellers),
        "share_asv": len(asv) / max(1, len(travellers)),
        "asv_boarded": len(waits),
        "asv_left_on_pier": sum(t.t_pier is not None and t.t_board is None for t in asv),   # 到了码头没上船
        "pier_wait_mean": _mean(waits),
        "pier_wait_max": max(waits, default=None),
        "door_to_door_asv": _mean([t.t_arrive - t.t_depart for t in asv if t.t_arrive is not None]),
        "door_to_door_metro": _mean([t.t_arrive - t.t_depart for t in metro if t.t_arrive is not None]),
        "planned_asv": _mean([t.expected[ASV]["total"] for t in asv if ASV in t.expected]),
        "planned_metro": _mean([t.expected[METRO]["total"] for t in metro if METRO in t.expected]),
        "arrived": sum(t.t_arrive is not None for t in travellers),
        **{f"fleet_{k}": v for k, v in op.summary().items()},
    }


def plot_run(sim: Simulation, k: dict, path: str) -> None:
    """Three panels: pier queue vs published wait, door-to-door by chain, vessel tracks.
    左：码头队列（左轴）与发布的期望等待（右轴），看反馈回路；中：两条链门到门分布；右：航迹。"""
    import matplotlib
    matplotlib.use("Agg")                       # 只画到文件，不弹窗
    import matplotlib.pyplot as plt

    a, log, op = sim.a, sim.log, sim.op
    fig, ax = plt.subplots(1, 3, figsize=(16, 4.2))

    ax[0].plot(log["t"], log["queue"], color="C0", label="waiting on the pier")
    ax[0].set(xlabel="time [s]", ylabel="travellers on pier",
              title="Pier queue and the wait the operator publishes")
    twin = ax[0].twinx()
    twin.plot(log["t"], log["offer"], color="C3", ls="--", label="published expected wait")
    twin.set_ylabel("expected wait [s]", color="C3")
    ax[0].legend(loc="upper left", fontsize=7)
    twin.legend(loc="upper right", fontsize=7)

    arrived = [t for t in sim.travellers if t.t_arrive is not None]
    d2d = {m: [t.t_arrive - t.t_depart for t in arrived if t.mode == m] for m in (ASV, METRO)}
    ax[1].hist([d2d[ASV], d2d[METRO]], bins=20,
               label=[f"Bus-ASV-Walk (n={len(d2d[ASV])})", f"Bus-Metro-Walk (n={len(d2d[METRO])})"])
    ax[1].set(xlabel="door-to-door time [s]", ylabel="travellers",
              title=f"Mode share ASV {100 * k['share_asv']:.0f} %, "
                    f"pier wait {k['pier_wait_mean'] or 0:.0f} s")
    ax[1].legend(fontsize=7)

    ax[2].fill(*zip(*WATER), color="#bcd8ff")
    for name, dock in DOCKS.items():
        ax[2].plot(*dock["pos"], "s", color="#5a3c1e", ms=8)
        ax[2].text(dock["pos"][0], dock["pos"][1] + 12, f"dock {name}", ha="center", fontsize=8)
    for v in op.vessels:
        xs, ys = zip(*log["traj"][v.name])
        ax[2].plot(xs, ys, lw=0.7, label=f"{v.name}: {len(v.trips)} trips")
    ax[2].set(aspect="equal", xlabel="x [m]", ylabel="y [m]",
              title=f"Vessel tracks, empty sailing "
                    f"{100 * k['fleet_empty_distance_share']:.0f} % of distance")
    ax[2].legend(fontsize=7)

    fig.suptitle(f"{a.policy} policy, {a.fleet} vessel(s) x {a.capacity} pax, "
                 f"metro every {a.metro_period} s, dt = {a.dt} s", fontsize=11)
    fig.tight_layout()
    fig.savefig(path, dpi=120)


def render_gif(sim: Simulation, path: str) -> None:
    """Animate recorded land and water movements without re-running the simulation."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation, PillowWriter

    a, log = sim.a, sim.log
    frames = gif_frame_times(a.gif, a.end)
    if not frames:
        return

    def display_position(point: tuple[float, float]) -> tuple[float, float]:
        """Compress the long residential road and tall rail loop for a readable overview."""
        x, y = point
        if x < 560:
            x = 430 + (x + 1400) * 130 / 1960
        elif x > 900:
            x = 900 + (x - 900) * 0.25
        if y > 70:
            y = 70 + (y - 70) * 0.4
        return x, y

    def offsets(points) -> np.ndarray:
        return np.asarray([display_position(point) for point in points], dtype=float).reshape(-1, 2)

    def crowd(n: int, x0: float, y0: float, dx: float, cols: int = 6) -> np.ndarray:
        points = [(x0 + dx * (i // cols), y0 - 8 - 2.6 * (i % cols)) for i in range(n)]
        return np.asarray(points, dtype=float).reshape(-1, 2)

    fig, ax = plt.subplots(figsize=(11, 4.4))
    ax.set(xlim=(410, 970), ylim=(-85, 165), aspect="equal")
    ax.axis("off")

    water = [display_position(point) for point in WATER]
    ax.fill(*zip(*water), color="#bcd8ff", zorder=0)
    net = sumolib.net.readNet(NET_FILE)

    def draw_edges(edge_ids, color: str, width: float, zorder: int = 1) -> None:
        for edge_id in edge_ids:
            shape = [display_position(point) for point in net.getEdge(edge_id).getShape()]
            ax.plot(*zip(*shape), color=color, lw=width, solid_capstyle="round", zorder=zorder)

    draw_edges(("link_pier", "pier", "E_out", "office", "link_metro", "link_metroE"),
               "#999999", 2.5)
    draw_edges(("road_in", "road_out"), "#777777", 4)
    draw_edges(("st_W", "rail_1", "rail_2", "rail_3", "st_E", "rail_4", "rail_5", "rail_6"),
               "#556270", 2.5)
    draw_edges(("st_W", "st_E"), "#8064a2", 5, zorder=2)

    for start, end in ((60, 90), (460, 490), (1900, 1940)):
        x1, y1 = display_position((-1400 + start, 40))
        x2, y2 = display_position((-1400 + end, 40))
        ax.plot([x1, x2], [y1 + 5, y2 + 5], color="orange", lw=3, zorder=3)

    for name, dock in DOCKS.items():
        x, y = display_position(dock["pos"])
        ax.plot(x, y, "s", color="#5a3c1e", ms=8, zorder=4)
        ax.text(x, y + 10, f"dock {name}", ha="center", fontsize=7)
    ax.text(486, 53, "homes + bus B1", ha="center", fontsize=8)
    ax.text(680, 151, "metro M1 loop", ha="center", fontsize=8)
    ax.text(600, 113, "metro station waiting", ha="center", fontsize=7)
    ax.text(604, 15, "pier\nwaiting", ha="center", fontsize=7)
    ax.text(852, 15, "east bank", ha="center", fontsize=7)
    ax.text(925, 15, "office\narrived", ha="center", fontsize=7)

    buses = ax.scatter([], [], s=75, marker="s", color="orange", edgecolor="black", zorder=6)
    metros = ax.scatter([], [], s=65, marker="D", color="#8064a2", edgecolor="black", zorder=6)
    pier_waiting_dots = ax.scatter([], [], s=7, color="black", zorder=5)
    metro_waiting_dots = ax.scatter([], [], s=7, color="#8064a2", zorder=5)
    office_dots = ax.scatter([], [], s=7, color="gray", zorder=5)
    vessel_dots = [ax.plot([], [], "o", color="#df3030", ms=9, zorder=6)[0]
                   for _ in sim.op.vessels]
    wakes = [ax.plot([], [], color="#df3030", lw=0.8, alpha=0.65, zorder=3)[0]
             for _ in sim.op.vessels]
    title = ax.set_title("", fontsize=9)

    def draw(frame_index: int):
        t = frames[frame_index]
        sample = min(t, len(log["bus_traj"]) - 1)
        pier_waiting, metro_waiting, office_arrived = animation_crowd_counts(
            sim.travellers, log["metro_waiting"][sample], t)
        pier_waiting_dots.set_offsets(crowd(pier_waiting, 609, 0, -2.6))
        metro_waiting_dots.set_offsets(crowd(metro_waiting, 585, 108, 2.6))
        office_dots.set_offsets(crowd(min(office_arrived, 72), 908, 0, 2.6))
        buses.set_offsets(offsets(log["bus_traj"][sample]))
        metros.set_offsets(offsets(log["metro_traj"][sample]))

        for dot, wake, vessel in zip(vessel_dots, wakes, sim.op.vessels):
            trajectory = log["traj"][vessel.name]
            x, y = display_position(trajectory[sample])
            dot.set_data([x], [y])
            tail = trajectory[max(0, sample - 60):sample + 1]
            wake_points = [display_position(point) for point in tail]
            wake.set_data([point[0] for point in wake_points], [point[1] for point in wake_points])

        title.set_text(f"t = {t:4d} s    pier waiting: {pier_waiting:3d}    "
                       f"metro waiting: {metro_waiting:3d}    office arrived: {office_arrived:3d}\n"
                       f"[{a.policy}, fleet {a.fleet}, cap {a.capacity}, "
                       f"bus {a.bus_period} s, metro {a.metro_period} s]")
        return vessel_dots + wakes + [pier_waiting_dots, metro_waiting_dots, office_dots,
                                      buses, metros, title]

    animation = FuncAnimation(fig, draw, frames=len(frames), blit=False)

    def report_progress(index: int, total: int) -> None:
        if index % 50 == 0 or index + 1 == total:
            print(f"GIF frame {index + 1}/{total}", flush=True)

    animation.save(path, writer=PillowWriter(fps=12), dpi=80, progress_callback=report_progress)
    plt.close(fig)
    print(f"-> {path} ({len(frames)} frames)")


def write_results(sim: Simulation) -> None:
    """One PNG, one JSON (parameters and results together) and one summary line.
    参数和结果同存一个 JSON，compare.py 靠 args 给场景起名。"""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    k = kpis(sim.travellers, sim.op)
    plot_run(sim, k, os.path.join(RESULTS_DIR, f"result_{sim.tag}.png"))
    out = {"args": vars(sim.a), "kpis": k,
           "series": {key: sim.log[key] for key in ("t", "queue", "offer", "share_asv")},
           "travellers": [t.to_dict() for t in sim.travellers],
           "trips": [asdict(t) for v in sim.op.vessels for t in v.trips]}
    json.dump(out, open(os.path.join(RESULTS_DIR, f"result_{sim.tag}.json"), "w"), indent=0)
    print(f"\n{sim.tag}: ASV share {100 * k['share_asv']:.0f} %, boarded {k['asv_boarded']}, "
          f"left on pier {k['asv_left_on_pier']}, pier wait {k['pier_wait_mean'] or 0:.0f} s, "
          f"door-to-door ASV {k['door_to_door_asv'] or 0:.0f} s / "
          f"metro {k['door_to_door_metro'] or 0:.0f} s, "
          f"{k['fleet_trips']} sailings ({k['fleet_empty_trips']} empty), "
          f"load factor {100 * k['fleet_load_factor']:.0f} % -> results/result_{sim.tag}.png")


def main() -> None:
    sim = Simulation(parse_args())
    sim.run()
    write_results(sim)
    if sim.a.gif:
        render_gif(sim, os.path.join(RESULTS_DIR, f"anim_{sim.tag}.gif"))


if __name__ == "__main__":
    main()
