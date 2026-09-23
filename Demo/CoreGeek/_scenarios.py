"""逐条验证策略改动是否真的生效。"""
import copy
import json
import sys

sys.path.insert(0, "src")

from agent import intel
from agent.brain import decide
from agent.intel import MEM, reset_memory

RAW = open("../../docs/request.txt", encoding="utf-8").read()
BASE = json.loads(RAW[RAW.index("{"):])

CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append((name, ok, detail))
    print(("PASS  " if ok else "FAIL  ") + name + ("  " + str(detail) if detail else ""))


def frame(**over):
    f = copy.deepcopy(BASE)
    f.update(over)
    return f


def role(f, rid):
    for r in f["teamOur"]["roles"]:
        if r["id"] == rid:
            return r
    raise KeyError(rid)


def cmds(out):
    return {int(k): v for k, v in out["roleCommandMap"].items()}


# ---------------------------------------------------------------- 任务不放弃
reset_memory()
f = frame(roundNo=10, phaseTask="给出 token")
f["teamOur"]["roles"] = [r for r in f["teamOur"]["roles"]]
role(f, 10011)["pos"] = {"x": 14, "y": 15}  # 站在任务点旁
decide(f)  # 第一轮出 prompt

submits = 0
left_point = 0
for i in range(1, 25):
    f = frame(roundNo=10 + i, phaseTask="给出 token")
    role(f, 10011)["pos"] = {"x": 14, "y": 15}
    f["errors"] = [{"errorCode": 2, "description": "键值比对不通过: $/token: 值不符"}]
    f["llmResp"] = 'ANSWER:{"token":"abc%d"}' % i
    out = cmds(decide(f))
    cmd = out.get(10011, {})
    if cmd.get("action") == "submitAnswer":
        submits += 1
    if cmd.get("action") == "move":
        left_point += 1

check("答错 24 次后仍在提交答案", submits >= 10, f"submits={submits}")
check("答错不会离开任务点", left_point == 0, f"moves={left_point}")
check("MEM 未标记放弃", not MEM.abandon_task)

# ---------------------------------------------------------- 超时前保底提交
reset_memory()
f = frame(roundNo=5, phaseTask="算出世界遗产数量")
role(f, 10011)["pos"] = {"x": 14, "y": 15}
decide(f)
MEM.task_timeout = 10
MEM.task_required.append("world_heritage_count")
f = frame(roundNo=13, phaseTask="算出世界遗产数量")
role(f, 10011)["pos"] = {"x": 14, "y": 15}
f["lastCmdResult"] = "[exitCode:0]\n57"
out = cmds(decide(f))
cmd = out.get(10011, {})
check(
    "临近超时会交保底答案",
    cmd.get("action") == "submitAnswer" and "world_heritage_count" in str(cmd),
    cmd,
)

# timeoutRounds 缺失时也要有保底提交
reset_memory()
f = frame(roundNo=5, phaseTask="算出世界遗产数量")
role(f, 10011)["pos"] = {"x": 14, "y": 15}
decide(f)
check("timeoutRounds 缺失时不设超时", MEM.task_timeout == 0, MEM.task_timeout)
MEM.task_required.append("world_heritage_count")
f = frame(roundNo=31, phaseTask="算出世界遗产数量")
role(f, 10011)["pos"] = {"x": 14, "y": 15}
f["lastCmdResult"] = "[exitCode:0]\n57"
out = cmds(decide(f))
cmd = out.get(10011, {})
check("无超时字段时按 25 回合兜底提交", cmd.get("action") == "submitAnswer", cmd)
check("兜底提交后也不放弃任务", not MEM.abandon_task)

# ------------------------------------------------------ 任务期附带宝藏提问
reset_memory()
f = frame(roundNo=6, phaseTask="给出 token")
role(f, 10011)["pos"] = {"x": 14, "y": 15}
out = decide(f)
check("任务 prompt 里挂了宝藏附加题", "TREASURE:" in out["prompt"], out["prompt"][:60])
check("任务期提问不消耗 LLM 额度", MEM.llm_used == 0, MEM.llm_used)

# -------------------------------------------------------------- 建造失败学习
reset_memory()
f = frame(roundNo=3)
role(f, 10010)["pos"] = {"x": 6, "y": 21}
role(f, 10010)["backpack"] = ["stone"] * 12
out = cmds(decide(f))
built = out.get(10010, {})
check("工人会砌墙", built.get("action") == "build", built)
if built.get("action") == "build":
    target = built["targetPos"][0]
    f2 = frame(roundNo=4)
    role(f2, 10010)["pos"] = {"x": 6, "y": 21}
    role(f2, 10010)["backpack"] = ["stone"] * 12
    f2["lastRoundRoleActionResults"] = {"10010": False}
    retry = cmds(decide(f2)).get(10010, {})
    f3 = frame(roundNo=5)
    role(f3, 10010)["pos"] = {"x": 6, "y": 21}
    role(f3, 10010)["backpack"] = ["stone"] * 12
    f3["lastRoundRoleActionResults"] = {"10010": False}
    decide(f3)
    from agent.intel import bad_build_cells
    from agent.protocol import Pos

    banned = bad_build_cells()
    check(
        "建造连续失败的格子进黑名单",
        Pos(target["x"], target["y"]) in banned,
        f"{target}, retry={retry} -> {sorted((p.x, p.y) for p in banned)}",
    )

# ------------------------------------------------------------ 采购优先级
reset_memory()
f = frame(roundNo=20)
f["teamOur"]["goldNum"] = 400
w = role(f, 10010)
w["pos"] = {"x": 24, "y": 20}  # 商店旁
w["backpack"] = []
out = cmds(decide(f))
buy = out.get(10010, {})
check(
    "有钱时先升级基地到 L2",
    buy.get("action") == "buy" and buy.get("name") == "StationUpgradeVoucher1",
    buy,
)

# ------------------------------------------------------------ 召唤令购买/使用
reset_memory()
f = frame(roundNo=530)  # 第 5 天
f["teamOur"]["goldNum"] = 900
for r in f["teamOur"]["roles"]:
    if r["roleType"] in {"gatling", "railgun", "rocket"}:
        r["level"] = 3
        r["health"] = 2000
    if r["roleType"] == "wall":
        r["level"] = 3
        r["health"] = 2000
    if r["roleType"] == "station":
        r["level"] = 3
        r["health"] = 4500
w = role(f, 10010)
w["pos"] = {"x": 24, "y": 20}
w["backpack"] = ["Bomb", "Bomb", "DizzyWeapon", "Medicine", "Medicine"]
out = cmds(decide(f))
buy = out.get(10010, {})
check("全升满后余钱买召唤令", buy.get("name", "").endswith("SummonOrder"), buy)

reset_memory()
f = frame(roundNo=530)
w = role(f, 10010)
w["backpack"] = ["BossRobotSummonOrder"]
out = cmds(decide(f))
use = out.get(10010, {})
check(
    "手里的召唤令立刻使用",
    use.get("action") == "use" and use.get("name") == "BossRobotSummonOrder",
    use,
)

# ------------------------------------------------ 夜晚火箭冷却后改控其它塔
reset_memory()
f = frame(roundNo=80)  # 夜晚
# 三名角色都站在三塔中间，使其同时相邻多座塔
role(f, 10010)["pos"] = {"x": 9, "y": 23}
role(f, 10012)["pos"] = {"x": 8, "y": 25}
role(f, 10011)["pos"] = {"x": 10, "y": 26}
for r in f["robot"]["roles"]:
    r["pos"] = {"x": 9, "y": 21}
    r["abnormalState"] = ""
out1 = cmds(decide(f))
attacks1 = {k: v for k, v in out1.items() if v.get("action") == "attack"}
check("夜晚三塔齐射", len(attacks1) >= 2, sorted(attacks1))
rocket_fired = 10040 in attacks1
check("火箭首回合可开火", rocket_fired, sorted(attacks1))

f2 = frame(roundNo=81)
role(f2, 10010)["pos"] = {"x": 9, "y": 23}
role(f2, 10012)["pos"] = {"x": 8, "y": 25}
role(f2, 10011)["pos"] = {"x": 10, "y": 26}
for r in f2["robot"]["roles"]:
    r["pos"] = {"x": 9, "y": 21}
    r["abnormalState"] = ""
for r in f2["teamOur"]["roles"]:
    if r["roleType"] == "rocket":
        r["cooldown"] = 3
out2 = cmds(decide(f2))
attacks2 = {k: v for k, v in out2.items() if v.get("action") == "attack"}
check("冷却回合火箭不再开火", 10040 not in attacks2, sorted(attacks2))
check("冷却回合其它塔仍有输出", len(attacks2) >= 2, sorted(attacks2))

# ---------------------------------------------------- 加特林目标数=等级
reset_memory()
f = frame(roundNo=80)
role(f, 10010)["pos"] = {"x": 9, "y": 23}
for r in f["teamOur"]["roles"]:
    if r["roleType"] == "gatling":
        r["level"] = 3
for r in f["robot"]["roles"]:
    r["pos"] = {"x": 9, "y": 21}
    r["abnormalState"] = ""
out = cmds(decide(f))
gat = out.get(10020, {})
check(
    "加特林 level3 传 3 个目标",
    gat.get("action") != "attack" or len(gat["targetPos"]) == 3,
    gat,
)

# ------------------------------------------------ 生命周期、协议与新闻
from agent.intel import parse_official, remember_task_accept
from agent.protocol import PlayerTask, Pos, Turn, station_cells

check(
    "基地严格占用 2x2 四格",
    len(station_cells(Pos(10, 24))) == 4
    and Pos(10, 25) not in station_cells(Pos(10, 24)),
)

reset_memory()
old = frame(roundNo=100)
old["worldNews"] = {"officialNews": "", "folkLegends": ""}
decide(old)
MEM.official.append((1, "上一半场残留"))
new = frame(roundNo=1)
new["worldNews"] = {"officialNews": "", "folkLegends": ""}
decide(new)
check("roundNo 回退时重置半场记忆", not MEM.official, MEM.official)

reset_memory()
news_turn = Turn.load(frame(roundNo=1))
event = parse_official(
    news_turn,
    "今天铁矿发生塌方，浅层仍可抢采，明天全面停工，修复需要2天。",
)
check(
    "今天事故、明天停采不会提前封矿",
    event is not None and event.start_day == 2 and event.end_day == 3,
    event,
)

reset_memory()
chosen = PlayerTask("短任务", Pos(14, 14), 0, 50, 30, True, 12)
remember_task_accept(chosen, 10)
active = frame(roundNo=11, phaseTask="短任务正文")
role(active, 10011)["pos"] = {"x": 14, "y": 15}
decide(active)
check(
    "任务使用实际领取点的 timeoutRounds",
    MEM.task_timeout == 12 and MEM.task_pos == Pos(14, 14),
    (MEM.task_timeout, MEM.task_pos),
)

reset_memory()
probe = frame(roundNo=20)
decide(probe)
failed = frame(roundNo=21, lastSummonTreasureResult=2)
decide(failed)
check(
    "写死宝藏失败后仍保持固定方案",
    (
        MEM.treasure.day == 8
        and not MEM.treasure.weak
        and MEM.treasure.pos == Pos(3, 3)
        and MEM.treasure.items == ["AcientTablet", "StarSand", "FlameBreath"]
    ),
    (MEM.treasure.day, MEM.treasure.weak, MEM.treasure.pos, MEM.treasure.items),
)

reset_memory()
enemy_only = frame(roundNo=80)
role(enemy_only, 10010)["pos"] = {"x": 9, "y": 23}
role(enemy_only, 10012)["pos"] = {"x": 8, "y": 25}
role(enemy_only, 10011)["pos"] = {"x": 10, "y": 26}
for robot in enemy_only["robot"]["roles"]:
    robot["pos"] = {"x": 9, "y": 21}
    robot["targetTeam"] = "defender"
out = cmds(decide(enemy_only))
check(
    "不帮助对手清理进攻其基地的机器人",
    not any(cmd.get("action") == "attack" for cmd in out.values()),
    out,
)

# --------------------------------------------------------------- 汇总
bad = [name for name, ok, _ in CHECKS if not ok]
print()
print(f"{len(CHECKS) - len(bad)}/{len(CHECKS)} passed")
if bad:
    print("FAILED:", bad)
    sys.exit(1)
