"""Draw the network as built: nodes with coordinates, edges by type, stops, docks, sea lanes.

    python plot_network.py      ->  results/network_layout.png + .svg (editable in Inkscape / Figma)

Reads the same dictionaries as build_network.py, so the figure can never drift from the network.

中文说明：把 NODES / EDGES / 车站 / 码头 / 航线 画成一张带图例的示意图：上图全貌（1960 m 住宅路一目了然），
下图枢纽放大，每个节点标名字和坐标。改了 build_network.py 或 layout.py 重跑一次即可。
"""

from __future__ import annotations

import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Polygon, Rectangle

from build_network import EDGES, FOOT, NODES, RAIL, ROAD, STATION
from layout import BUS_STOPS, DOCKS, METRO_STOPS, RESULTS_DIR, SEA_LANES, WATER

# 拥挤处的节点标签偏移 (points)；不在表里的用默认右上
LABEL_OFFSET = {"Wd": (-56, -18), "P": (-6, 10), "Ed": (-4, 10), "E1": (4, -16),
                "Mw0": (-74, -16), "Me0": (-74, -16), "R4": (4, -16)}
SKIP_EDGE_LABEL = {"pier"}                  # 太短，标了也看不清
EDGE_LABEL_DY = {"E_out": -12, "office": 6, "link_pier": 6, "link_metroE": 6, "st_W": -13, "st_E": -13}   # 边名标签的 y 偏移 [m]

STYLE = {  # 边的属性字符串 -> 画法
    ROAD: dict(color="#777777", lw=6, ls="-", label="bus road (sidewalk + bus lane)"),
    FOOT: dict(color="#999999", lw=3, ls="-", label="footpath (pedestrians only)"),
    RAIL: dict(color="#783cc8", lw=2, ls="--", label="rail (one-way loop)"),
    STATION: dict(color="#783cc8", lw=5, ls="-", label="metro station (rail + platform)"),
}


def point_on(edge_id: str, pos: float) -> tuple[float, float]:
    """Position `pos` metres from the start of a straight edge. 直线边上距起点 pos 米处的坐标。"""
    _, a, b, _ = next(e for e in EDGES if e[0] == edge_id)
    (x0, y0), (x1, y1) = NODES[a], NODES[b]
    length = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
    f = pos / length
    return x0 + f * (x1 - x0), y0 + f * (y1 - y0)


def draw(ax, label_nodes: bool, xlim, ylim) -> None:
    ax.add_patch(Polygon(WATER, closed=True, color="#bcd8ff", zorder=0))
    for eid, a, b, attrs in EDGES:                                   # 边
        (x0, y0), (x1, y1) = NODES[a], NODES[b]
        s = STYLE[attrs]
        ax.plot([x0, x1], [y0, y1], color=s["color"], lw=s["lw"], ls=s["ls"], zorder=1,
                solid_capstyle="butt")
        if attrs == RAIL:                                            # 单向环线：画方向箭头
            ax.annotate("", xy=((x0 + x1) / 2, (y0 + y1) / 2), xytext=(x0, y0),
                        arrowprops=dict(arrowstyle="->", color=s["color"], lw=1.2))
        if label_nodes and eid not in SKIP_EDGE_LABEL:
            ax.text((x0 + x1) / 2, (y0 + y1) / 2 + EDGE_LABEL_DY.get(eid, 6), eid, fontsize=7,
                    color=s["color"], ha="center", style="italic")
    for sid, (a, b) in BUS_STOPS.items():                            # 公交站：路上的橙色区间
        xa, ya = point_on("road_in", a)
        xb, _ = point_on("road_in", b)
        ax.add_patch(Rectangle((xa, ya - 3), xb - xa, 6, color="#ffc800", zorder=3))
        if label_nodes or sid != "bs_hub":
            ax.text((xa + xb) / 2, ya - 12, f"{sid}\n{a}–{b} m", fontsize=7, ha="center", va="top")
    for sid, edge in METRO_STOPS.items():                            # 地铁站：站边上的绿色区间
        xa, ya = point_on(edge, 10)
        xb, _ = point_on(edge, 60)
        ax.add_patch(Rectangle((xa, ya + 3), xb - xa, 5, color="#2ca02c", zorder=3))
        if label_nodes:
            ax.text((xa + xb) / 2, ya + 12, f"{sid} (10–60 m)", fontsize=7, ha="center", color="#2ca02c")
    for name, d in DOCKS.items():                                    # 码头 + 航线
        ax.plot(*d["pos"], "s", color="#5a3c1e", ms=8, zorder=4)
        if label_nodes:
            ax.text(d["pos"][0], d["pos"][1] - 14, f"dock {name}\n({d['pos'][0]:.0f}, {d['pos'][1]:.0f})",
                    fontsize=7, ha="center", va="top")
    for (frm, to), pts in SEA_LANES.items():
        xs = [DOCKS[frm]["pos"][0]] + [p[0] for p in pts] + [DOCKS[to]["pos"][0]]
        ys = [DOCKS[frm]["pos"][1]] + [p[1] for p in pts] + [DOCKS[to]["pos"][1]]
        ax.plot(xs, ys, color="#d62728", lw=1.5, zorder=2)
        ax.annotate("", xy=(xs[2], ys[2]), xytext=(xs[1], ys[1]),
                    arrowprops=dict(arrowstyle="->", color="#d62728", lw=1.5))
    for nid, (x, y) in NODES.items():                                # 节点：黑点 + 名字 + 坐标
        ax.plot(x, y, "o", color="black", ms=4, zorder=5)
        if label_nodes:
            ax.annotate(f"{nid} ({x}, {y})", (x, y), textcoords="offset points",
                        xytext=LABEL_OFFSET.get(nid, (4, 4)), fontsize=7.5, fontweight="bold")
        elif nid in ("T0", "J"):
            ax.annotate(f"{nid} ({x}, {y})", (x, y), textcoords="offset points", xytext=(4, 6), fontsize=8)
    ax.set(xlim=xlim, ylim=ylim, aspect="equal", xlabel="x [m]", ylabel="y [m]")
    ax.grid(True, lw=0.3, alpha=0.5)


def main() -> None:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    fig, (top, bottom) = plt.subplots(2, 1, figsize=(15, 9.5),
                                      gridspec_kw={"height_ratios": [1, 2.6]})
    draw(top, label_nodes=False, xlim=(-1450, 1150), ylim=(-100, 300))
    top.set_title("Whole network: 1960 m residential road (bus B1) feeding the hub J", fontsize=10)
    draw(bottom, label_nodes=True, xlim=(430, 1130), ylim=(-95, 290))
    bottom.set_title("Hub area to scale: every node with its (x, y), edges by type, stops, docks, sea lanes",
                     fontsize=10)
    handles = [Line2D([], [], **{k: v for k, v in s.items() if k != "label"}, label=s["label"])
               for s in STYLE.values()]
    handles += [Patch(color="#ffc800", label="bus stop (startPos–endPos on lane road_in_1)"),
                Patch(color="#2ca02c", label="metro stop (10–60 m on lane st_*_1)"),
                Line2D([], [], marker="s", color="#5a3c1e", ls="", ms=8, label="dock (berth position)"),
                Line2D([], [], color="#d62728", lw=1.5, label="sea lane (eastbound south, westbound north)"),
                Patch(color="#bcd8ff", label="water (drawn only, no SUMO edges)"),
                Line2D([], [], marker="o", color="black", ls="", ms=4, label="node = SUMO junction, (x, y) in metres")]
    fig.legend(handles=handles, loc="lower center", fontsize=8, ncol=3, framealpha=0.95)   # 图例放在图外下方
    fig.subplots_adjust(left=0.06, right=0.98, top=0.96, bottom=0.13, hspace=0.3)
    for ext in ("png", "svg"):
        fig.savefig(os.path.join(RESULTS_DIR, f"network_layout.{ext}"), dpi=130)
    print(f"-> {RESULTS_DIR}/network_layout.png / .svg")


if __name__ == "__main__":
    main()
