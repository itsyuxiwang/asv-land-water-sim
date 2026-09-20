"""Traveller model: the behavioural side of the platform.

    demand generation   `generate_demand`   who wants to travel, from where, when,
                                            and how impatient they are
    mode / route choice `plan_alternatives` + `choose_mode`
                                            Bus -> ASV -> Walk   versus   Bus -> Metro -> Walk,
                                            binary logit on expected time components
    tour creation       `create_tour`       the chosen chain becomes a SUMO person plan

The expected times of the bus and metro legs come from SUMO's own intermodal
router (`findIntermodalRoute`, which reads the timetables of the two lines). The
expected wait for a vessel is published by the operator (`Operator.offer`), so
the choice reacts to how the fleet is actually performing, as in the
time-based framework of Zhou et al. (2026).

中文说明：乘客模型三步，对应本文件的三段——
    1. 需求生成 generate_demand()：谁出门、什么时候、家在哪、多不耐烦（每人一个随机系数）。
    2. 方式选择 plan_alternatives() + choose_mode()：
       先问 SUMO 的换乘路径规划器"两条链各要多久"（公交、地铁按时刻表算），
       再补上 SUMO 不知道的三段（等船 = 运营商实时发布值、过河、东岸步行），
       最后按二项 Logit 抽签。
    3. 行程生成 create_tour()：把选中的链写成 SUMO person 的 stage 序列，注入仿真。
    等船时间用的是运营商实时发布的值，所以船队表现差 → 发布值升高 → 选船的人变少 → 队列变短，
    这就是供需之间的反馈回路。

    run.py 只调用三个函数：generate_demand()（开始时一次）、plan_alternatives() + choose_mode()
    + create_tour()（每批对即将出发的人各调一次）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import traci
import traci.constants as tc

from layout import (HOME_EDGE, HOME_POS, METRO_LINE, OFFICE_EDGE, OFFICE_POS, PIER_EDGE,
                    PIER_WAIT_POS)

ASV, METRO = "asv", "metro"          # 两个备选方案的名字；全文件用常量，避免手打字符串拼错


@dataclass
class ChoiceParams:
    """Utility weights of the logit model (utils per second / per euro).

    Logit 效用系数。约定：全部是正数，表示"每秒 / 每欧元损失多少效用"，负号统一在 utility() 里加。
    数值取交通行为研究里的常见量级：等待和步行比坐在车里更难受（系数 > 1 倍），票价 0.4 util/欧元。
    改这里就改了所有人的口味；个体差异另由 Traveller.impatience 承担。"""
    asc_asv: float = 0.0            # 船链的常数项（alternative-specific constant）：装下模型没写出来的偏好，
                                    # 正数 = 喜欢坐船。命令行 --asc-asv 改它，用来做敏感性分析
    b_ivt: float = 1.0 / 60         # 车内每秒：1 分钟 = 1 util（其余系数都以它为基准）
    b_wait: float = 1.6 / 60        # 等待每秒（公交站、站台、码头都算）：比车内难受 1.6 倍
    b_walk: float = 1.4 / 60        # 步行每秒：1.4 倍
    b_fare: float = 0.4             # 每欧元 0.4 util，即 1 欧元 ≈ 24 s 车内时间
    fare_asv: float = 2.0           # 两边票价相同 → 默认互相抵消；留给"票价策略"扩展（论文的 pricing）
    fare_metro: float = 2.0
    asv_crossing: float = 110.0     # 过河预计时间 [s]；run.py 用 crossing_estimate() 按航线几何重算后覆盖
    east_walk: float = 260.0        # 东码头 → 办公区步行 [s]；run.py 用 east_walk_estimate() 按路网重算后覆盖


@dataclass
class Traveller:
    """一个乘客 = 一行日志。前四个字段是需求生成时定的；mode / p_asv / expected 在决策时填；
    四个时间戳在仿真过程中由 run.py 逐个填上（地铁链的人只有 t_depart 和 t_arrive）。"""
    pid: str                        # SUMO 里的 person id，形如 "p17"；东岸重建时会变成 "p17_e"
    t_depart: float                 # 出发时刻 [s]
    home_pos: float                 # 家在住宅路 road_in 上的位置 [m]
    impatience: float               # 乘在 b_wait 上的个体系数：1 = 平均水平，2 = 等待难受两倍
    mode: str | None = None         # 选了哪条链：ASV | METRO；None = 还没决策
    p_asv: float | None = None      # 决策时算出的选船概率（抽签前）
    expected: dict = field(default_factory=dict)   # 决策时两条链的时间拆分 {mode: {walk, ivt, wait, fare, total}}
    t_pier: float | None = None     # 到西码头（run.py 第一次在码头边上看到他）
    t_board: float | None = None    # 上船（被 board() 从 SUMO 删掉）
    t_land: float | None = None     # 在东岸下船（被 land() 重建）
    t_arrive: float | None = None   # 到办公区（SUMO 报告他走完了全部计划）

    def to_dict(self) -> dict:
        """写 JSON 用：去掉体积大的 expected，只留所选链的计划总时间，方便和实际门到门时间比。"""
        row = {k: v for k, v in self.__dict__.items() if k != "expected"}
        row["planned_total"] = self.expected.get(self.mode, {}).get("total")
        return row


# ------------------------------------------------------------------ demand generation 需求生成
def generate_demand(n: int, t_end: float, rng: np.random.Generator) -> list[Traveller]:
    """Poisson arrivals over [0, t_end] (given N, the arrival times are i.i.d.
    uniform), homes spread along the residential road, log-normal impatience.

    三行随机数、一行打包。rng 由 run.py 按 --seed 创建，所以同一个 seed 出同一批人。"""
    # 泊松过程的性质：给定总人数 N，N 个到达时刻 = N 个 [0, t_end] 上的均匀分布再排序
    times = np.sort(rng.uniform(0, t_end, n))
    # 家均匀分布在住宅路上 20~700 m 处：离枢纽 1.2~1.9 km，坐公交才划算
    homes = rng.uniform(*HOME_POS, n)
    # 不耐烦程度取对数正态：中位数 1，大多数人在 0.6~1.8。没有这个异质性，
    # 同一批人面对同一个发布值会全部选同一边，分担率只会在 0 和 100 % 之间跳
    impatience = rng.lognormal(0.0, 0.4, n)
    return [Traveller(f"p{i}", float(times[i]), float(homes[i]), float(impatience[i]))
            for i in range(n)]


# ------------------------------------------------------------------ mode / route choice 方式选择
def _route(trav: Traveller, to_edge: str, arrival_pos: float) -> list:
    """Ask SUMO's intermodal router for the fastest public-transport chain.

    问 SUMO：这个人从家（road_in 上 home_pos 处）在 t_depart 出发、允许坐公共交通，怎么到 to_edge？
    返回 Stage 列表，每个 Stage 有 type（2 步行 / 3 乘车）、line（B1 / M1）、depart（开始时刻）、
    travelTime（时长）、destStop（在哪一站下）。SUMO 按两条线的时刻表算，包括赶不上这班要等下一班。
    两条链的差别只在终点：船链到码头边 pier，地铁链直接到办公区 office。"""
    return traci.simulation.findIntermodalRoute(HOME_EDGE, to_edge, modes="public",
                                                depart=trav.t_depart, departPos=trav.home_pos,
                                                arrivalPos=arrival_pos)


def _components(stages) -> dict:
    """Split a SUMO stage list into walking, in-vehicle and (timetable) waiting time.

    把 Stage 列表拆成 Logit 需要的三个数：
        walk  所有步行段的时长之和
        ivt   所有乘车段的时长之和（in-vehicle time）
        wait  等车时间 = 乘车段的开始时刻 − 上一段的结束时刻（规划器按时刻表算出来的"赶班车"等待）
    例：走 168 s 到站（200→368），车 660 s 才来 → wait = 660 − 368 = 292 s。"""
    walk = ivt = wait = 0.0
    clock = None                      # 上一段结束的时刻；第一段之前没有等待
    for s in stages:
        if s.type == tc.STAGE_DRIVING:
            if clock is not None:
                wait += max(0.0, s.depart - clock)
            ivt += s.travelTime
        else:
            walk += s.travelTime
        clock = s.depart + s.travelTime
    return {"walk": walk, "ivt": ivt, "wait": wait}


def plan_alternatives(trav: Traveller, offer_wait: float, params: ChoiceParams) -> dict:
    """Both chains with their time components. SUMO knows the bus and metro legs;
    the vessel wait (operator offer), the crossing and the east walk are added here.

    返回 {ASV: {...}, METRO: {...}}，每个方案有 walk / ivt / wait / fare / total 和原始 stages。
    某条链拼不出来（例如地铁末班车已过）就不出现在字典里，choose_mode() 会直接选另一条。"""
    alts = {}
    # --- 船链：SUMO 只能规划到码头；等船、过河、东岸步行三段 SUMO 不知道，在这里补上
    to_pier = _route(trav, PIER_EDGE, PIER_WAIT_POS)
    if to_pier:
        c = _components(to_pier)
        c["wait"] += offer_wait           # 运营商此刻发布的期望等待（反馈回路的入口）
        c["ivt"] += params.asv_crossing   # 过河按车内时间计
        c["walk"] += params.east_walk     # 下船后走到办公区
        alts[ASV] = c | {"fare": params.fare_asv, "stages": to_pier}      # dict 合并：加票价和原始 stage
    # --- 地铁链：直接问家 → 办公区，规划器自己拼出 走 → B1 → 走 → M1 → 走 五段
    to_office = _route(trav, OFFICE_EDGE, OFFICE_POS)
    if any(s.line == METRO_LINE for s in to_office):        # 确认里面真有地铁；纯步行到不了对岸
        alts[METRO] = _components(to_office) | {"fare": params.fare_metro, "stages": to_office}
    for c in alts.values():
        c["total"] = c["walk"] + c["ivt"] + c["wait"]       # 计划的门到门时间，事后和实际比
    return alts


def utility(c: dict, trav: Traveller, params: ChoiceParams, asc: float) -> float:
    """V = ASC − β_ivt·车内 − β_wait·impatience·等待 − β_walk·步行 − β_fare·票价
    只有等待项乘了个体系数 impatience：假设人们对等待的忍耐力差异最大。"""

    # asc: Alternative-Specific Constant，中文通常称为“方案专属常数”
    # positive: 喜欢坐船 → 船链的效用加上 asc_asv；地铁链是参照方案，asc = 0
    return (asc - params.b_ivt * c["ivt"] - params.b_wait * trav.impatience * c["wait"]
            - params.b_walk * c["walk"] - params.b_fare * c["fare"])


def choose_mode(trav: Traveller, alts: dict, params: ChoiceParams,
                rng: np.random.Generator) -> str:
    """Binary logit between the two chains; a missing alternative is never chosen.

    二项 Logit：P(ASV) = 1 / (1 + exp(V_metro − V_asv))。效用差 1 util ≈ 73 % 对 27 %。
    量级感：发布的等船时间每多 1 分钟，V_asv 少 1.6 × impatience util；
    典型乘客发布值 60 s 时 P(ASV) ≈ 0.6，180 s 时掉到 0.06（GUIDE 5.4 节有手算表）。
    副作用：把两条链的时间拆分存进 trav.expected，把概率和结果存进 trav.p_asv / trav.mode。"""
    trav.expected = {m: {k: v for k, v in c.items() if k != "stages"} for m, c in alts.items()}
    if len(alts) == 1:                              # 只剩一条链可选（例如地铁末班车已过）→ 不抽签
        trav.mode = next(iter(alts))
        trav.p_asv = float(trav.mode == ASV)
        return trav.mode
    v_asv = utility(alts[ASV], trav, params, params.asc_asv)
    v_metro = utility(alts[METRO], trav, params, 0.0)     # 地铁链是参照方案，常数项为 0
    trav.p_asv = 1.0 / (1.0 + math.exp(v_metro - v_asv))
    trav.mode = ASV if rng.random() < trav.p_asv else METRO   # 抽签：同一个人换个 seed 可能选另一边
    return trav.mode


# ------------------------------------------------------------------ tour creation 行程生成
def create_tour(trav: Traveller, alts: dict, now: float) -> None:
    """Turn the chosen chain into a SUMO person. The vessel leg is a waiting stage
    on the pier that only the operator can end (boarding removes the person).

    把选中的链变成 SUMO person，分三步：
        1. person.add：在家所在的边上创建人，出发时刻 = 他自己的 t_depart
        2. appendStage：规划器返回的 Stage 可以原样挂回（findIntermodalRoute 的输出就是 appendStage 的输入）
        3. 船链再加一个 10 万秒的"等船"stage：人到码头后站住，只有运营商把他删掉才会结束；
           地铁链什么都不加，走完最后一段步行会自动"到达"并消失，run.py 用 getArrivedPersonIDList() 捕捉。"""
    # 批处理在 t=1 处理 t∈[0,30) 出发的人，有人的出发时刻已经过去；SUMO 不接受过去的时间，所以取 max
    traci.person.add(trav.pid, HOME_EDGE, trav.home_pos, depart=max(trav.t_depart, now))
    for stage in alts[trav.mode]["stages"]:
        traci.person.appendStage(trav.pid, stage)
    if trav.mode == ASV:
        traci.person.appendWaitingStage(trav.pid, 100000, "waiting for ASV")
