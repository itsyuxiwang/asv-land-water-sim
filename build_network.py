"""Generate the land network, the public-transport lines and the SUMO config.

              rail return (y=260)
      ┌───────────────────────────────────────┐
      │  st_W ── bridge (y=180) ── st_E       │        metro M1 loop (rail only)
      │  ^                          │ link_metroE (100 m walk)
 T0 ══ road_in ══► J ─link_metro─┘             └► E1 ── office
 (homes, 3 bus stops)  └─link_pier─► pier ~~~~ river ~~~~ Ed ─E_out─ E1   (76 m walk)

Bus B1 shuttles T0 -> J -> T0 and stops at bs_home, bs_mid, bs_hub. From the hub
a traveller either walks to the pier (autonomous surface vessel, own model) or to
metro station st_W (train over the bridge). Both end on the office edge.

Run:  python build_network.py

中文说明：一次生成四样东西——节点/边 XML（交给 netconvert 编成路网）、车站、
带时刻表的公交与地铁线、SUMO 配置文件。乘客不在这里：乘客由 run.py 在运行时注入。
"""

from __future__ import annotations

import os
import subprocess

from sumolib import checkBinary

from layout import (BUS_LINE, BUS_STOPS, CFG_FILE, HERE, METRO_LINE, METRO_STOPS,
                    NET_FILE, ROUTE_FILE, STOP_FILE)

# 节点：名字 -> (x, y) [m]。y 向上；住宅路从 x=-1400 开始，所以路长 1960 m
NODES = {
    "T0": (-1400, 40), "J": (560, 40),                 # 公交路两端：住宅区起点、河边枢纽
    "Wd": (600, 0), "P": (616, 0),                     # 去码头的步道终点、码头末端
    "Ed": (824, 0), "E1": (900, 0), "E2": (1100, 0),   # 东岸：下船点、办公区起点（紧挨码头）、终点
    "Mw0": (560, 80), "Mw1": (640, 80),                # 地铁西站两端
    "R1": (640, 180), "R2": (880, 180),                # 铁路桥两端
    "Me0": (880, 80), "Me1": (960, 80),                # 地铁东站两端（到办公区要走 100 m）
    "R3": (960, 260), "R4": (560, 260),                # 铁路回程线两端
}
# 四种边的属性：公交路带人行道（车道 0 人行道、车道 1 机动车道）、纯步道、纯轨道、车站（轨道 + 站台）
ROAD = 'numLanes="1" speed="13.9" sidewalkWidth="2" allow="bus"' #  sidewalkWidth="2": Add a 2-metre-wide pedestrian sidewalk.
FOOT = 'numLanes="1" width="3" allow="pedestrian"'
RAIL = 'numLanes="1" speed="16" allow="rail_urban"'
STATION = RAIL + ' sidewalkWidth="3"'          # 站台 = 轨道旁的一条人行道，乘客才能走到车旁上车
EDGES = [  # (id, from, to, attributes)
    ("road_in", "T0", "J", ROAD), ("road_out", "J", "T0", ROAD),
    ("link_pier", "J", "Wd", FOOT), ("pier", "Wd", "P", FOOT),
    ("E_out", "Ed", "E1", FOOT), ("office", "E1", "E2", FOOT),
    ("link_metro", "J", "Mw0", FOOT), ("st_W", "Mw0", "Mw1", STATION), # st_W = metro station west
    ("rail_1", "Mw1", "R1", RAIL), ("rail_2", "R1", "R2", RAIL), ("rail_3", "R2", "Me0", RAIL),
    ("st_E", "Me0", "Me1", STATION), ("link_metroE", "Me1", "E1", FOOT), # st_E = metro station east
    ("rail_4", "Me1", "R3", RAIL), ("rail_5", "R3", "R4", RAIL), ("rail_6", "R4", "Mw0", RAIL),
]
METRO_ROUTE = "st_W rail_1 rail_2 rail_3 st_E rail_4 rail_5 rail_6"   # 地铁单向环线
# 时刻表：每站的 until = 该班车出发后第几秒才允许离站（车早到就等，规划器据此算换乘）
BUS_TIMETABLE = (20, 60, 190)                  # bs_home, bs_mid, bs_hub
METRO_TIMETABLE = (30, 100)                    # ms_W, ms_E


def write_network() -> None:
    """Nodes and edges as plain XML, compiled by netconvert into NET_FILE."""
    nod = "\n".join(f'  <node id="{n}" x="{x}" y="{y}"/>' for n, (x, y) in NODES.items())
    edg = "\n".join(f'  <edge id="{e}" from="{a}" to="{b}" {attrs}/>' for e, a, b, attrs in EDGES)
    nod_file, edg_file = os.path.join(HERE, "nodes.nod.xml"), os.path.join(HERE, "edges.edg.xml")
    open(nod_file, "w").write(f"<nodes>\n{nod}\n</nodes>\n")
    open(edg_file, "w").write(f"<edges>\n{edg}\n</edges>\n")
    # netconvert 默认会把路网平移到最小坐标为 (0,0)；关掉它，layout.py 里的坐标才和路网一致
    subprocess.run([checkBinary("netconvert"), "-n", nod_file, "-e", edg_file, "-o", NET_FILE,
                    "--no-warnings",
                    "--offset.disable-normalization"],
                   check=True)


def write_stops() -> None:
    """Bus stops on the driving lane of the road, metro stops on the track lane."""
    # friendlyPos：站的位置若超出车道长度就自动夹回，而不是让 SUMO 直接报错退出
    stop = ('  <busStop id="{sid}" lane="{lane}" startPos="{a}" endPos="{b}" '
            'friendlyPos="true" lines="{line}"/>')
    bus = [stop.format(sid=sid, lane="road_in_1", a=a, b=b, line=BUS_LINE)      # 车道 1 = 机动车道
           for sid, (a, b) in BUS_STOPS.items()]
    metro = [stop.format(sid=sid, lane=f"{edge}_1", a=10, b=60, line=METRO_LINE)  # 车道 1 = 轨道
             for sid, edge in METRO_STOPS.items()]
    open(STOP_FILE, "w").write("<additional>\n" + "\n".join(bus + metro) + "\n</additional>\n")


def write_lines(bus_period: int = 300, metro_period: int = 300, end: int = 3600,
                path: str = ROUTE_FILE) -> str:
    """Public-transport supply. Both lines are SUMO flows with timetabled stops
    (`until` = seconds after the vehicle's departure). SUMO's intermodal router
    only sees a line as public transport if its stops carry such times, and the
    vehicles hold at a stop until then, which is what makes it a timetable."""
    # 没有 until，SUMO 的换乘路径规划器根本不把这条线当公共交通（会返回纯步行方案）
    def stops(ids, times, dwell):
        return "".join(f'\n      <stop busStop="{s}" duration="{dwell}" until="{u}"/>'
                       for s, u in zip(ids, times))

    xml = f"""<routes>
  <vType id="bus" vClass="bus" length="12" personCapacity="60" color="255,200,0"/>
  <vType id="metro" vClass="rail_urban" length="40" personCapacity="200" color="120,60,200"/>
  <flow id="{BUS_LINE}" type="bus" line="{BUS_LINE}" begin="0" end="{end}" period="{bus_period}">
    <route edges="road_in road_out">{stops(BUS_STOPS, BUS_TIMETABLE, 10)}
    </route>
  </flow>
  <flow id="{METRO_LINE}" type="metro" line="{METRO_LINE}" begin="0" end="{end}" period="{metro_period}">
    <route edges="{METRO_ROUTE}">{stops(METRO_STOPS, METRO_TIMETABLE, 20)}
    </route>
  </flow>
</routes>
"""
    open(path, "w").write(xml)    # run.py 会按 --bus-period/--metro-period 另写一份，传 path 即可
    return path


def write_config() -> None:
    """SUMO configuration: network + lines + stops + time. No persons: they are injected at run time."""
    open(CFG_FILE, "w").write(f"""<configuration>
  <input>
    <net-file value="{os.path.basename(NET_FILE)}"/>
    <route-files value="{os.path.basename(ROUTE_FILE)}"/>
    <additional-files value="{os.path.basename(STOP_FILE)}"/>
  </input>
  <time><begin value="0"/><end value="3600"/><step-length value="1"/></time>
  <report><no-step-log value="true"/><no-warnings value="true"/></report>
</configuration>
""")


def main() -> None:
    write_network()
    write_stops()
    write_lines()
    write_config()
    print(f"network -> {NET_FILE}\nlines   -> {ROUTE_FILE}\nconfig  -> {CFG_FILE}")


if __name__ == "__main__":
    main()
