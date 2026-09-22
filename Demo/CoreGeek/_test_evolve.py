import json
import sys

sys.path.insert(0, "src")

from agent.brain import decide
from agent.evolve import (
    is_junk_answer,
    task_family,
    concrete_sandbox_answer,
    try_preset_answer,
    reset,
    Skill,
    _answer_from_sandbox,
    _classify,
)
from agent.intel import reset_memory

assert is_junk_answer("./proc/1/schedstat")
assert is_junk_answer("- 建议将修复过程整理成可复用的 SOP")
assert not is_junk_answer('{"temp":26}')
assert concrete_sandbox_answer("[exitCode:0]\n./proc/1/schedstat") == ""
assert concrete_sandbox_answer("[exitCode:0]\ntotal 12") == ""
got = concrete_sandbox_answer('[exitCode:0]\nANSWER:{"city":"北京","temp":26}')
assert "北京" in got and "26" in got
assert task_family("查询北京天气，见 API_DOCS.md") == task_family(
    "查询上海天气，见 API_DOCS.md"
)
assert _classify("请阅读task_1_alpha.md，获取任务信息") == "engineering-fix"
assert _classify("请阅读task_1_beijing.md，获取任务信息") == "unknown-api"
assert task_family("请阅读task_1_beijing.md") == task_family("请阅读task_2_nanjing.md")

# 题干里直接给出的 token 仍可秒交；城市文物不再写死
skill = Skill(family="t")
token_ans = try_preset_answer("请提交 token=fc1e78eb2a5a", skill)
assert '"token"' in token_ans and "fc1e78eb2a5a" in token_ans
auth_ans = try_preset_answer("请提交认证码 0de1b57493cf", skill)
assert "0de1b57493cf" in auth_ans and '"token"' in auth_ans
bare_ans = try_preset_answer("提交以下字符串完成校验：c8be2288b213", skill)
assert "c8be2288b213" in bare_ans
assert try_preset_answer("请阅读task_1_beijing.md，获取任务信息", skill) == ""
assert try_preset_answer("查询北京世界遗产与文物统计", skill) == ""
assert try_preset_answer('请提交 {"token":"xxx"}', skill) == ""
assert is_junk_answer('{"token":"xxx"}')
assert _answer_from_sandbox(
    '[exitCode:0]\nFWBUNDLE1 {"ok":true,"task_path":"./task_1_alpha.md",'
    '"task":"submit {\\"token\\":\\"xxx\\"}"}',
    "请阅读task_1_alpha.md，获取任务信息",
) == ""
assert "offset" in __import__("agent.evolve", fromlist=["_http_probe_cmd"])._http_probe_cmd("北京")

# check TOKEN → 交卷
check_out = "[exitCode:0]\n[ OK ] 全部通过 (6/6) | TOKEN: fc1e78eb2a5a |"
got = _answer_from_sandbox(check_out, "请阅读task_1_alpha.md")
assert "fc1e78eb2a5a" in got

raw = open("../../docs/request.txt", encoding="utf-8").read()
payload = json.loads(raw[raw.index("{") :])

# 白天可接任务：开拓者应优先走向/领取任务
reset_memory()
payload["roundNo"] = 5
payload["phaseTask"] = ""
out = decide(payload)
cmds = out["roleCommandMap"]
pioneer = cmds.get("10011") or cmds.get(10011)
assert pioneer is not None, cmds
assert pioneer["action"] in {"move", "acceptTask"}, pioneer

# 阅读 md：先 bootstrap，不写死交卷
reset_memory()
reset()
payload["phaseTask"] = "请阅读task_1_beijing.md，获取任务信息"
payload["llmResp"] = ""
payload["lastCmdResult"] = ""
out = decide(payload)
cmd = out["roleCommandMap"].get("10011") or {}
assert cmd.get("action") != "submitAnswer", out
assert "python3" in (out.get("executeCmd") or ""), out
assert "FWBUNDLE1" in out["executeCmd"]

# 沙盒给出 TOKEN 后应 submitAnswer
reset_memory()
reset()
payload["phaseTask"] = "请阅读task_1_alpha.md，获取任务信息"
payload["lastCmdResult"] = (
    '[exitCode:0]\n[ OK ] 全部通过 (6/6) | TOKEN: 0de1b57493cf |'
)
out = decide(payload)
cmd = out["roleCommandMap"].get("10011") or {}
assert cmd.get("action") == "submitAnswer", out
assert "0de1b57493cf" in cmd.get("taskAnswer", ""), cmd
assert out["executeCmd"] == ""

# HTTP 成功体应统计后交卷
reset_memory()
reset()
payload["phaseTask"] = "请阅读task_2_nanjing.md，获取任务信息"
payload["lastCmdResult"] = (
    '[exitCode:0] | FWHTTP1 {"ok": true, "status": 200, '
    '"body": "{\\"code\\": 200, \\"data\\": {\\"records\\": ['
    '{\\"id\\": \\"NJ001\\", \\"name\\": \\"明孝陵\\", \\"type\\": \\"陵墓\\", '
    '\\"era\\": \\"明\\", \\"protected_level\\": \\"世界遗产\\"}, '
    '{\\"id\\": \\"NJ002\\", \\"name\\": \\"鸡鸣寺\\", \\"type\\": \\"宗教建筑\\", '
    '\\"era\\": \\"南北朝\\", \\"protected_level\\": \\"全国重点\\"}]}}"} |'
)
out = decide(payload)
cmd = out["roleCommandMap"].get("10011") or {}
assert cmd.get("action") == "submitAnswer", out
answer = cmd.get("taskAnswer", "")
assert "南京" in answer or "total_count" in answer, cmd

# 单页满 10 条且无 ANSWER：不得当全量交卷
reset_memory()
reset()
payload["phaseTask"] = "请阅读task_1_beijing.md，获取任务信息"
ten = ",".join(
    [
        f'{{\\"id\\": \\"BJ{i:03d}\\", \\"name\\": \\"遗址{i}\\", '
        f'\\"type\\": \\"遗址\\", \\"era\\": \\"明\\", \\"protected_level\\": \\"全国重点\\"}}'
        for i in range(10)
    ]
)
payload["lastCmdResult"] = (
    '[exitCode:0] | FWHTTP1 {"ok": true, "status": 200, '
    f'"body": "{{\\"code\\": 200, \\"data\\": {{\\"records\\": [{ten}]}}}}"'
    " } |"
)
out = decide(payload)
cmd = out["roleCommandMap"].get("10011") or {}
assert cmd.get("action") != "submitAnswer", out
assert "python3" in (out.get("executeCmd") or "") or out.get("prompt"), out

# 垃圾 LLM 答案不得直接提交
reset_memory()
reset()
payload["phaseTask"] = "查询未知星球天气"
payload["llmResp"] = "ANSWER: - 建议将修复过程整理成可复用的 SOP"
payload["lastCmdResult"] = ""
out = decide(payload)
action = (out["roleCommandMap"].get("10011") or {}).get("action")
answer = (out["roleCommandMap"].get("10011") or {}).get("taskAnswer", "")
assert action != "submitAnswer" or not is_junk_answer(answer), out
assert (
    "10011" in out["roleCommandMap"]
    or 10011 in out["roleCommandMap"]
    or out["executeCmd"]
    or out["prompt"]
), out

# 占位 token 不得交卷
reset_memory()
reset()
payload["phaseTask"] = "请阅读task_1_alpha.md，获取任务信息"
payload["llmResp"] = 'ANSWER:{"token":"xxx"}'
payload["lastCmdResult"] = ""
out = decide(payload)
cmd = out["roleCommandMap"].get("10011") or {}
assert cmd.get("taskAnswer") != '{"token":"xxx"}', out

# 工程题不得把上一题的文物 JSON 当答案
reset_memory()
reset()
payload["phaseTask"] = "请阅读task_1_alpha.md，获取任务信息"
payload["llmResp"] = ""
payload["lastCmdResult"] = (
    '[exitCode:0] ANSWER:{"city":"北京","total_count":15,'
    '"world_heritage_count":6,"types":["建筑"],"oldest_era":"周口店遗址"}'
)
out = decide(payload)
cmd = out["roleCommandMap"].get("10011") or {}
assert cmd.get("action") != "submitAnswer", out
assert "python3" in (out.get("executeCmd") or "") or out.get("prompt"), out

# 工程题不得复用上一题 TOKEN
reset_memory()
reset()
payload["phaseTask"] = "请阅读task_1_alpha.md，获取任务信息"
payload["llmResp"] = ""
payload["lastCmdResult"] = (
    '[exitCode:0]\n[ OK ] 全部通过 (6/6) | TOKEN: fc1e78eb2a5a |'
)
out = decide(payload)
cmd = out["roleCommandMap"].get("10011") or {}
assert "fc1e78eb2a5a" in cmd.get("taskAnswer", ""), cmd
payload["phaseTask"] = "请阅读task_2_beta.md，获取任务信息"
payload["lastCmdResult"] = ""
payload["llmResp"] = 'ANSWER:{"token":"fc1e78eb2a5a"}'
out = decide(payload)
cmd = out["roleCommandMap"].get("10011") or {}
assert cmd.get("taskAnswer", "") != '{"token":"fc1e78eb2a5a"}', out
payload["llmResp"] = ""
payload["lastCmdResult"] = (
    '[exitCode:0]\n[ OK ] 全部通过 (6/6) | TOKEN: 0de1b57493cf |'
)
out = decide(payload)
cmd = out["roleCommandMap"].get("10011") or {}
assert "0de1b57493cf" in cmd.get("taskAnswer", ""), cmd
assert "fc1e78eb2a5a" not in cmd.get("taskAnswer", ""), cmd

# 工程题：自摸索 explore（check→fix→verify），失败可再探
reset_memory()
reset()
payload["phaseTask"] = "请阅读task_1_alpha.md，获取任务信息"
payload["llmResp"] = ""
payload["lastCmdResult"] = ""
out = decide(payload)
assert "FWBUNDLE1" in (out.get("executeCmd") or ""), out
payload["lastCmdResult"] = (
    '[exitCode:0] | FWBUNDLE1 {"ok":true,'
    '"task_path":"tmp/selfEvolutionTask/1-fixed-step/2-engineering-fix/task_1_alpha.md",'
    '"task":"# 修复应用 alpha\\nworkspace ws_1/\\nport 8080\\nname alpha-app"} |'
)
out = decide(payload)
cmd1 = out.get("executeCmd") or ""
assert "PROBE" in cmd1 or "CHECK#" in cmd1 or "run_check" in cmd1 or "apply_fix" in cmd1, out
# 探索失败（无 TOKEN）后应允许再探或问 LLM，不得交旧 TOKEN
payload["lastCmdResult"] = (
    "[exitCode:0]\nPROBE task=... ws=... app=alpha\n"
    "CHECK#0 [FAIL] port mismatch expected 8080\nFIX#0 ['mkdir ...']\n"
    "CHECK#1 [FAIL] still bad\nNO_FIX\n"
)
out = decide(payload)
assert out["roleCommandMap"].get("10011", {}).get("action") != "submitAnswer", out
cmd2 = out.get("executeCmd") or ""
assert "PROBE" in cmd2 or "CHECK#" in cmd2 or out.get("prompt"), out
# 探索成功应直接交卷
payload["lastCmdResult"] = (
    "[exitCode:0]\nCHECK#0 [ OK ] 全部通过 (6/6) | TOKEN: a1b2c3d4e5f6 |\n"
    'ANSWER:{"token":"a1b2c3d4e5f6"}'
)
out = decide(payload)
cmd = out["roleCommandMap"].get("10011") or {}
assert "a1b2c3d4e5f6" in cmd.get("taskAnswer", ""), cmd

# 冷却贴点：已在任务点旁且无 phaseTask 时不应来回 move / 不应去买祭品
reset_memory()
reset()
payload["phaseTask"] = ""
payload["lastCmdResult"] = ""
payload["llmResp"] = ""
payload["roundNo"] = 40
for role in payload["teamOur"]["roles"]:
    if role.get("roleType") == "pioneer":
        role["pos"] = {"x": 14, "y": 14}
        break
out1 = decide(payload)
out2 = decide(payload)
c1 = (out1["roleCommandMap"].get("10011") or {})
c2 = (out2["roleCommandMap"].get("10011") or {})
# 两回合都 move 且目标互相翻转则失败
if c1.get("action") == "move" and c2.get("action") == "move":
    p1 = (c1["targetPos"][0]["x"], c1["targetPos"][0]["y"])
    p2 = (c2["targetPos"][0]["x"], c2["targetPos"][0]["y"])
    assert not (p1 != p2 and abs(p1[0] - p2[0]) + abs(p1[1] - p2[1]) <= 2 and
                abs(p1[0] - 14) + abs(p1[1] - 14) <= 2), (c1, c2)
# 贴点待命时不应 buy
assert c1.get("action") != "buy" and c2.get("action") != "buy", (c1, c2)

# 在任务点上不得走向另一个任务点
reset_memory()
reset()
payload["phaseTask"] = "请阅读task_2_nanjing.md，获取任务信息"
payload["lastCmdResult"] = ""
payload["llmResp"] = ""
for role in payload["teamOur"]["roles"]:
    if role.get("roleType") == "pioneer":
        role["pos"] = {"x": 14, "y": 14}
        break
out = decide(payload)
cmd = out["roleCommandMap"].get("10011") or {}
if cmd.get("action") == "move":
    dest = (cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
    assert abs(dest[0] - 14) + abs(dest[1] - 14) <= 2, cmd
    assert dest != (17, 17), cmd

# 开局工人应优先去建火箭炮（金币足够时）
reset_memory()
reset()
payload["phaseTask"] = ""
payload["roundNo"] = 1
payload["teamOur"]["goldNum"] = 80
out = decide(payload)
moves_or_builds = [
    c for c in out["roleCommandMap"].values()
    if c.get("action") in {"build", "move", "collect"}
]
assert moves_or_builds, out
worker_cmds = {
    k: v for k, v in out["roleCommandMap"].items() if k in {"10010", "10012"}
}
assert worker_cmds, out

# 全局去重：两角色同动作同目的地只留一个
from agent.brain import _command_goal_key, _dedupe_role_commands
from agent.intel import MEM, oscillation_bans
from agent.protocol import Pos, Turn

reset_memory()
payload["phaseTask"] = ""
payload["roundNo"] = 50
# 构造最小 Turn 去重
cmds = {
    10010: {"action": "collect", "targetPos": [{"x": 33, "y": 14}]},
    10012: {"action": "collect", "targetPos": [{"x": 33, "y": 14}]},
    10011: {"action": "move", "targetPos": [{"x": 27, "y": 7}]},
}
turn = Turn.load(payload)
_dedupe_role_commands(turn, cmds)
assert len([u for u, c in cmds.items() if c.get("action") == "collect"]) == 1, cmds
assert _command_goal_key(cmds[10011]) == ("move", 27, 7, "")

# 防抖：A-B-A 应禁止回到 A
reset_memory()
MEM.move_hist[10011] = [Pos(27, 7), Pos(27, 6)]
bans = oscillation_bans(10011)
assert Pos(27, 7) in bans
MEM.move_hist[10011] = [Pos(27, 7), Pos(27, 6), Pos(27, 7), Pos(27, 6)]
bans = oscillation_bans(10011)
assert Pos(27, 7) in bans and Pos(27, 6) in bans

print("evolve integration ok")
