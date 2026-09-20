# Demo 方案：乘客行为 × ASV 运营 的一体化陆水仿真（小 demo）

> 代码逐行讲解见 [GUIDE_zh.md](GUIDE_zh.md)（PDF 版 [GUIDE_zh.pdf](GUIDE_zh.pdf)）。

目标只对准项目 objective (1)「integrated land-water simulation platform」，
用两个可替换的模型把陆（SUMO）和水（ASV）接起来；论文 Zhou et al. (2026, ETRR)
提供的是**耦合框架**（time-based：运营商每 Δt 与仿真器交换一次状态）和
**评价指标**（等待、车内时间、空驶、载客率、服务率）。

## 1. 场景（刻意很小）

```
住宅区 ──公交 B1──► 公交站 ─步行─► 西码头 W ~~~ ASV 舰队 ~~~ 东码头 E ─步行─► 办公区
                         └──────── 地铁 M1（过桥，固定时刻表）────────┘
```

- 早高峰 1 小时，约 300 名乘客，全部从西岸住宅区去东岸办公区。
- 每个人在两条链之间选：**Bus → ASV → Walk** 或 **Bus → Metro → Walk**。
- 陆上一切（公交、地铁、行人、车站）由 SUMO 仿真；船由自己的 Python 模型仿真。

## 2. 乘客模型 `travellers.py`（行为层，三步）

| 步骤 | 做什么 | 实现 |
|---|---|---|
| Demand generation | 泊松到达，起点路段随机，出发 0–40 min，每人抽一个时间价值/等待敏感度 | numpy 随机数，写成 request 列表 |
| Mode / route choice | 二项 Logit：V = ASC − β_ivt·车内 − β_wait·(等待+步行) − β_fare·票价 | ASV 的期望等待由运营商**实时**给出；地铁链时间用 SUMO `findIntermodalRoute` 按时刻表算 |
| Tour creation | 把选中的链写成 SUMO person 的 stage 序列 | `traci.person.add` + walk / ride(B1) / walk / [等船] 或 [ride(M1)] / walk |

## 3. ASV 运营模型 `operator.py` + `asv.py`（运营层，对应用户五问）

| 问题 | 模型 |
|---|---|
| 船在哪里 | `asv.py` 点模型：沿航点以巡航速度行驶，只有起停加减速（约 50 行；父目录的 `../asv.py` 是带 ILOS 制导和潮流的可替换版本） |
| 能坐多少人 | 容量 C；登船 = 从 SUMO 移出乘客，靠岸 = 在东岸重建乘客并步行 |
| 去哪个码头 | 空闲船去排队最长的码头；两码头时 = 是否空驶回程 / 原地等 |
| 什么时候发船 | 两种策略：`timetable`（固定间隔）vs `demand`（满载或首位等待 > max_wait） |
| 怎么调度 | 论文的 time-based 框架：每 Δt=30 s 读状态 → 决策 → 发布期望等待给乘客模型 |

## 4. 耦合 `run.py`（论文 Fig. 1）

1. SUMO 按 1 s 步进。
2. 每 Δt=30 s：新到乘客批 → 向运营商要「期望等待」→ Logit 选模式 → 注入 SUMO。
3. 同一时刻：运营商更新每艘船（发船 / 目标码头 / 空驶重定位）。
4. 每秒：船体积分、码头登船与到岸交接、记录 KPI。

## 5. 场景比较 & KPI（对应 objective 3）

- 场景轴：舰队 {1,2,3} × 策略 {timetable, demand} × 地铁间隔 {5, 10 min}。
- KPI：ASV 分担率、平均等待、门到门时间、空驶航次比例（≈ 论文 empty VKT）、平均载客率、超时未服务人数。
- 输出：`results/*.json`、`compare.png`、`summary.csv`。

## 6. 文件（6 个，约 600 行）

`build_network.py` 网络 · `travellers.py` 行为 · `operator.py` 运营 · `asv.py` 船（复用）·
`run.py` 耦合主循环 · `compare.py` 比较图表。

## 7. 明确不做（保持小）

拼车路径插入与票价优化（论文的 OP/FA 策略）、3 个以上码头、真实 OSM 地图、日间学习。
README 里作为扩展列出，代码接口（operator 策略类、travellers 效用函数）预留。

---

## 8. 运行方法（已实现）

```bash
source ../.venv/bin/activate
python build_network.py                      # 生成路网、车站、公交/地铁时刻表、sim.sumocfg
python run.py                                # 默认：2 艘船、需求响应调度、地铁每 300 s
python run.py --fleet 1 --policy timetable --headway 300
python run.py --fleet 3 --policy demand --metro-period 120 --asc-asv 0.5
python compare.py                            # results/compare.png + compare.csv
python run.py --gui                          # sumo-gui，船是红色 POI
```

参数：`--fleet --capacity --cruise`（船）、`--policy timetable|demand --headway --max-wait`（调度）、
`--bus-period --metro-period`（陆上供给）、`--travellers --demand-end --asc-asv`（行为）、`--dt`（批处理间隔）。

## 9. 结果（300 人 / 40 min 需求，仿真 1 h）

| 船数 | 策略 | ASV 分担率 | 码头平均等待 | 门到门 ASV / 地铁 | 航次（空驶） | 载客率 |
|---|---|---|---|---|---|---|
| 1 | demand | 35 % | 547 s | 1229 / 767 s | 18 (9) | 48 % |
| 1 | timetable 300 s | 43 % | 648 s | 1323 / 768 s | 24 (14) | 42 % |
| 2 | demand | 61 % | 120 s | 795 / 768 s | 36 (18) | 42 % |
| 2 | timetable 150 s | 55 % | 151 s | 835 / 766 s | 47 (32) | 29 % |
| 3 | demand | 79 % | 48 s | 740 / 747 s | 48 (24) | 41 % |
| 3 | timetable 100 s | 64 % | 106 s | 795 / 752 s | 69 (49) | 23 % |

三句话结论：
1. 供需互相反馈：1 艘船时发布的期望等待升到 9–14 min，约六成人改乘地铁；3 艘船时等待 < 1 min，ASV 链拿到 79–87 %。
2. 同样船数下，需求响应调度比固定时刻表等待更短、载客率更高、空驶更少（对应论文的 empty VKT）。
3. 换乘衔接比发车频率更重要：5 min 地铁恰好在公交到站后 40 s 发车，2 min 地铁要等 100 s，所以更密的地铁并没有抢走 ASV 的客流。

图：`results/compare.png`（分担率 / 门到门时间 / 船队效率），每个场景一张 `results/result_*.png`（码头队列 + 发布的期望等待、门到门分布、航迹）。
