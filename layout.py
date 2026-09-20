"""Geometry and names shared by every script of the demo.

Coordinates are metres in the SUMO network frame (y up). Anything that names an
edge, a stop or a file lives here so that `build_network.py`, `asv_operator.py`,
`travellers.py` and `run.py` cannot drift apart.

中文说明：本文件只有常量，没有逻辑。所有"边名 / 站名 / 坐标 / 文件路径"都集中在这里，
因为它们至少被三个脚本同时用到，写三遍迟早不一致。改场景几何先改这里。
"""

from __future__ import annotations

import os

# --- 文件路径：全部相对本文件所在目录，从哪个目录启动脚本都一样 ---------------------
HERE = os.path.dirname(os.path.abspath(__file__))
NET_FILE = os.path.join(HERE, "net.net.xml")          # netconvert 生成的路网
ROUTE_FILE = os.path.join(HERE, "pt_lines.rou.xml")   # 公交 / 地铁线路（默认班距）
STOP_FILE = os.path.join(HERE, "stops.add.xml")       # 车站
CFG_FILE = os.path.join(HERE, "sim.sumocfg")          # SUMO 配置
VIEW_FILE = os.path.join(HERE, "view.xml")            # sumo-gui 显示设置
RESULTS_DIR = os.path.join(HERE, "results")

# --- land (SUMO) 陆上 ----------------------------------------------------------------
HOME_EDGE = "road_in"            # 住宅路：乘客在它的人行道上出生
HOME_POS = (20.0, 700.0)         # 家分布在离路起点 20~700 m 处（离枢纽 1.2~1.9 km，所以值得坐公交）
BUS_STOPS = {"bs_home": (60, 90), "bs_mid": (460, 490), "bs_hub": (1900, 1940)}   # 站名 -> 路上的位置区间 [m]
PIER_EDGE = "pier"               # 西码头：等船的边
PIER_WAIT_POS = 8.0              # 在码头边上第 8 m 处站住
EAST_WALK_EDGE = "E_out"         # 东岸下船后的步道
LANDING_POS = 2.0                # 下船的人在这条步道上重新出现的位置 [m]
OFFICE_EDGE = "office"           # 所有人的目的地边
OFFICE_POS = 100.0               # 目的地在该边上的位置 [m]
METRO_STOPS = {"ms_W": "st_W", "ms_E": "st_E"}   # 地铁站名 -> 所在轨道边
BUS_LINE, METRO_LINE = "B1", "M1"
WALK_SPEED = 1.3                 # 估算东岸步行时间用的步速 [m/s]

# --- water (own model) 水上：船不在 SUMO 里，坐标只给自己的点模型和画图用 ----------------
WATER = [(616, -70), (824, -70), (824, 70), (616, 70)]    # 水面多边形（只用来画）
DOCKS = {
    # pos: 泊位坐标；wait_edge: 哪条 SUMO 边上的等待者算作在本码头排队（None = 这里没人上船）；
    # land_edge: 在本码头下船的人被放回 SUMO 的哪条边
    "W": {"pos": (622.0, 0.0), "wait_edge": PIER_EDGE, "land_edge": None},
    "E": {"pos": (818.0, 0.0), "wait_edge": None, "land_edge": EAST_WALK_EDGE},
}
# 两个码头之间的中间航点：去程走南边 y=-40，回程走北边 y=+40，对开的船不会画在一条线上
SEA_LANES = {("W", "E"): [(640.0, -40.0), (800.0, -40.0)],
             ("E", "W"): [(800.0, 40.0), (640.0, 40.0)]}
GUI_VIEW = (440, -100, 960, 190)                            # sumo-gui 视野 xmin, ymin, xmax, ymax
