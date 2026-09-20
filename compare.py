"""Put several runs side by side: one table, one figure, one CSV.

    python compare.py                          # every results/result_*.json
    python compare.py "results/result_demand*"  # a subset

The label of a run is built from the parameters that differ between the runs,
so nothing here needs to change when `run.py` gains a new knob.

中文说明：把多次 run.py 的结果放在一起看。输入是 results/result_*.json（每个文件里有 args 和 kpis 两块），
输出三样：终端里一张表、results/compare.csv、results/compare.png（三联图）。
五个小函数串成 main()：
    load_runs()    读 JSON
    labels()       给每个场景起名——只用"各次运行取值不同"的参数
    print_table()  终端表格
    write_csv()    原始数值写 CSV
    plot()         三联图：分担率 / 门到门 / 船队效率
要多看一个 KPI：在 COLUMNS 里加一行 Column(...)，表和 CSV 自动多一列；图要改 plot()。
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
from typing import NamedTuple

import matplotlib
matplotlib.use("Agg")                # 只画到文件，不弹窗；服务器上也能跑
import matplotlib.pyplot as plt

from layout import RESULTS_DIR

# 这些命令行参数不影响结果（或已经体现在别处），不进场景标签，否则标签会又长又没信息
IGNORE = {"gui", "end", "seed", "screenshot"}


class Column(NamedTuple):
    """表里的一列。四个字段：
        key    run.py 写进 kpis 字典的键，例如 "share_asv"
        label  表头文字
        scale  打印前乘的倍数：1 = 原样，100 = 小数转百分数
        unit   打印时跟在数字后面的单位（"%"、"s" 或空）
    用 NamedTuple 而不是 tuple，是为了下面能写 c.key、c.label 而不是 c[0]、c[1]。"""
    key: str
    label: str
    scale: float
    unit: str


COLUMNS = [                      # 表 / CSV 的列，从左到右
    Column("share_asv", "ASV share", 100, "%"),              # 选船链的比例
    Column("pier_wait_mean", "pier wait", 1, "s"),           # 码头平均等待
    Column("asv_left_on_pier", "left on pier", 1, ""),       # 仿真结束时还滞留在码头的人
    Column("door_to_door_asv", "door-to-door ASV", 1, "s"),  # 船链平均门到门
    Column("door_to_door_metro", "door-to-door metro", 1, "s"),
    Column("fleet_trips", "sailings", 1, ""),                # 航次数
    Column("fleet_empty_trips", "empty", 1, ""),             # 其中空驶航次
    Column("fleet_load_factor", "load factor", 100, "%"),    # 载客率
]


def load_runs(pattern: str) -> list[dict]:
    """按文件名排序读入所有匹配的 JSON；把路径也存进去，labels() 在没有可区分参数时用它当名字。"""
    runs = []
    for path in sorted(glob.glob(pattern)):
        run = json.load(open(path))
        run["path"] = path
        runs.append(run)
    if not runs:
        raise SystemExit(f"no result files match {pattern!r}")
    return runs


def labels(runs: list[dict]) -> list[str]:
    """Name each run by the parameters that are not the same in every run.

    起名规则：遍历 args 里的每个参数，如果它在所有运行里取值都一样（例如 travellers=300），就不写；
    只把取值不同的写成 "fleet=2, policy=demand, metro_period=120"。
    json.dumps 是为了把 300 和 300.0、列表和字典都变成可比较、可放进 set 的字符串。"""
    varying = [k for k in runs[0]["args"] if k not in IGNORE
               and len({json.dumps(r["args"].get(k)) for r in runs}) > 1]
    if not varying:                                    # 只有一次运行（或全部参数相同）：退回用文件名
        return [os.path.basename(r["path"]) for r in runs]
    return [", ".join(f"{k}={r['args'][k]}" for k in varying) for r in runs]


def fmt(value, col: Column) -> str:
    """一个单元格：None（例如没人坐船时的平均等待）打 "-"，否则乘倍数、取整、加单位。"""
    return "-" if value is None else f"{value * col.scale:.0f}{col.unit}"


def print_table(names: list[str], runs: list[dict]) -> None:
    """终端表格：第一列按最长的场景名对齐，其余每列右对齐 18 个字符。"""
    width = max(len(n) for n in names)
    print(f"{'scenario':{width}s} " + " ".join(f"{c.label:>18s}" for c in COLUMNS))
    for name, run in zip(names, runs):
        cells = [fmt(run["kpis"].get(c.key), c) for c in COLUMNS]
        print(f"{name:{width}s} " + " ".join(f"{v:>18s}" for v in cells))


def write_csv(names: list[str], runs: list[dict], path: str) -> None:
    """CSV 里放原始数值（不乘倍数、不取整、不带单位），列名用 kpis 的键，方便进 Excel / pandas 再算。"""
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["scenario"] + [c.key for c in COLUMNS])
        writer.writeheader()
        for name, run in zip(names, runs):
            writer.writerow({"scenario": name, **{c.key: run["kpis"].get(c.key) for c in COLUMNS}})


def plot(names: list[str], runs: list[dict], path: str) -> None:
    """Left: what travellers chose. Middle: what they experienced. Right: how the fleet did.

    三张横条图，每行一个场景，三张图共用同一套 y 位置，所以场景名只在最左边写一次：
        左  分担率：选 Bus-ASV-Walk 的比例
        中  乘客经历：船链 / 地铁链的平均门到门时间两根条并排，黑色竖线 = 码头平均等待
        右  船队效率：载客率 和 空驶里程比例 两根条并排"""
    k = [r["kpis"] for r in runs]
    y = range(len(runs))
    lo, hi = [i - 0.2 for i in y], [i + 0.2 for i in y]     # 每个场景两根并排的条：上面一根、下面一根
    fig, ax = plt.subplots(1, 3, figsize=(16, 4.5))

    ax[0].barh(y, [100 * r["share_asv"] for r in k], color="C0")
    ax[0].set(yticks=y, yticklabels=names, xlabel="travellers choosing Bus-ASV-Walk [%]",
              title="Mode share")

    ax[1].barh(lo, [r["door_to_door_asv"] or 0 for r in k], height=0.4, label="ASV chain")    # or 0：没人坐时画 0
    ax[1].barh(hi, [r["door_to_door_metro"] or 0 for r in k], height=0.4, label="metro chain")
    ax[1].plot([r["pier_wait_mean"] or 0 for r in k], y, "k|", ms=12, label="mean pier wait")   # "k|" = 黑色竖线标记
    ax[1].set(yticks=y, yticklabels=[""] * len(runs), xlabel="seconds",
              title="Door-to-door time by chain")
    ax[1].legend(fontsize=7, loc="upper center", bbox_to_anchor=(0.5, -0.18), ncol=3)        # 图例放在坐标轴下方

    ax[2].barh(lo, [100 * r["fleet_load_factor"] for r in k], height=0.4, label="load factor [%]")
    ax[2].barh(hi, [100 * r["fleet_empty_distance_share"] for r in k], height=0.4,
               label="empty sailing [% of distance]")
    ax[2].set(yticks=y, yticklabels=[""] * len(runs), xlabel="%", title="Fleet efficiency")
    ax[2].legend(fontsize=7, loc="upper center", bbox_to_anchor=(0.5, -0.18), ncol=2)

    for axis in ax:
        axis.invert_yaxis()                 # 让第一个场景画在最上面（barh 默认从下往上）
    fig.tight_layout()
    fig.savefig(path, dpi=120)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("pattern", nargs="?", default=os.path.join(RESULTS_DIR, "result_*.json"),
                   help="glob of result files; quote it so the shell does not expand it")
    p.add_argument("--out", default=os.path.join(RESULTS_DIR, "compare"),
                   help="output stem: <out>.png and <out>.csv")
    a = p.parse_args()

    runs = load_runs(a.pattern)
    names = labels(runs)
    print_table(names, runs)
    write_csv(names, runs, a.out + ".csv")
    plot(names, runs, a.out + ".png")
    print(f"-> {a.out}.png, {a.out}.csv")


if __name__ == "__main__":
    main()
