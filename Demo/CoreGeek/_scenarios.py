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
    f["errors"] = [{"errorCode": 2, "errorMsg": "键值比对不通过: $/token: 值不符"}]
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
    decide(f2)
    from agent.intel import bad_build_cells
    from agent.protocol import Pos

    banned = bad_build_cells()
    check(
        "建造失败的格子进黑名单",
        Pos(target["x"], target["y"]) in banned,
        f"{target} -> {sorted((p.x, p.y) for p in banned)}",
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
    "有钱时先买围墙升级券",
    buy.get("action") == "buy" and buy.get("name") == "WallUpgradeVoucher1",
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
ready_at = dict(MEM.weapon_ready_at)
check("火箭开火后记下冷却", not rocket_fired or ready_at.get(10040, 0) > 80, ready_at)

f2 = frame(roundNo=81)
role(f2, 10010)["pos"] = {"x": 9, "y": 23}
role(f2, 10012)["pos"] = {"x": 8, "y": 25}
role(f2, 10011)["pos"] = {"x": 10, "y": 26}
for r in f2["robot"]["roles"]:
    r["pos"] = {"x": 9, "y": 21}
    r["abnormalState"] = ""
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

# --------------------------------------------------------------- 汇总
bad = [name for name, ok, _ in CHECKS if not ok]
print()
print(f"{len(CHECKS) - len(bad)}/{len(CHECKS)} passed")
if bad:
    print("FAILED:", bad)
    sys.exit(1)
