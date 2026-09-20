"""自进化任务：优先接取，沙盒探索，沉淀 SOP，复用求解。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .intel import (
    MEM,
    force_submit_now,
    mark_prompt,
    parse_sandbox_answer,
    patch_task_answer,
    remember_answer,
    task_rounds_left,
    treasure_imminent,
    treasure_ready,
    treasure_rider,
)
from .protocol import PlayerTask, Turn, Unit, distance, submit_answer_command


_JUNK_ANSWER = re.compile(
    r"(建议|可复用|SOP|SKILL|整理成|后续可能|形成固定|自进化|"
    r"please\s+form|reusable|document\s+the\s+process)",
    re.I,
)
_FILE_HINT = re.compile(
    r"([A-Za-z0-9_./-]+\.(?:md|txt|json|py|yml|yaml|csv|ini|conf))",
    re.I,
)
_SAFE_BLOCK = re.compile(
    r"rm\s+-rf|shutdown|reboot|mkfs|dd\s+if=|:\(\)\s*\{",
    re.I,
)


@dataclass
class Skill:
    """同一任务族沉淀出的可复用流程。"""

    family: str
    explore_cmds: list[str] = field(default_factory=list)
    good_cmds: list[str] = field(default_factory=list)
    last_answer: str = ""
    sandbox_notes: str = ""


@dataclass
class EvolveState:
    family: str = ""
    explore_i: int = 0
    last_cmd: str = ""
    last_sandbox: str = ""
    asked_llm: bool = False
    reused_skill: bool = False


_SKILLS: dict[str, Skill] = {}
_STATE = EvolveState()


def reset() -> None:
    _SKILLS.clear()
    _STATE.family = ""
    _STATE.explore_i = 0
    _STATE.last_cmd = ""
    _STATE.last_sandbox = ""
    _STATE.asked_llm = False
    _STATE.reused_skill = False


def on_task_text(task: str) -> None:
    """phaseTask 文本变化时切换子题，保留超时计时，重置求解态。"""
    family = task_family(task)
    if family != _STATE.family:
        _STATE.family = family
        _STATE.explore_i = 0
        _STATE.last_cmd = ""
        _STATE.last_sandbox = ""
        _STATE.asked_llm = False
        _STATE.reused_skill = False
        MEM.awaiting_task = False


def task_family(text: str) -> str:
    """把同类变体（换城市/文件名）归到同一 family，便于 SOP 复用。"""
    raw = (text or "").strip().lower()
    if not raw:
        return ""
    normalized = re.sub(r"\d+", "#", raw)
    normalized = re.sub(
        r"(北京|上海|广州|深圳|杭州|成都|重庆|武汉|西安|南京|"
        r"beijing|shanghai|guangzhou|shenzhen|hangzhou)",
        "<city>",
        normalized,
        flags=re.I,
    )
    normalized = re.sub(r"[a-z0-9_.-]+\.(md|txt|json|py)", "<file>.\\1", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
    tip = normalized[:48]
    return f"{digest}:{tip}"


def should_prioritize(turn: Turn) -> bool:
    """白天能接/能做自进化任务时，开拓者优先去做。"""
    if turn.phase_task.strip():
        return True
    if not turn.is_day:
        return False
    if turn.round_no < MEM.skip_task_until:
        return False
    # 入夜前 10 回合让路回防；其余白天优先任务
    if turn.near_night:
        return False
    return bool(turn.available_tasks())


def pick_task(turn: Turn, role: Unit) -> PlayerTask | None:
    tasks = turn.available_tasks()
    if not tasks:
        return None
    return max(tasks, key=lambda item: _task_score(turn, role, item))


def _task_score(turn: Turn, role: Unit, task: PlayerTask) -> tuple[float, float, int]:
    travel = max(0, distance(role.pos, task.pos) - 1)
    timeout = task.timeout_rounds or 25
    solve_rounds = min(10, max(3, timeout // 4))
    elapsed = max(1, travel + solve_rounds)
    speed_bonus = 5.0 * timeout / solve_rounds if task.timeout_rounds else 0.0
    family = task_family(task.task_type)
    reuse = 8.0 if family in _SKILLS and _SKILLS[family].good_cmds else 0.0
    expected = task.score_reward + speed_bonus + task.gold_reward * 0.5 + reuse
    return (expected / elapsed, expected, -travel)


def treasure_may_interrupt(turn: Turn) -> bool:
    """只有宝藏窗口已到且本任务已交过至少一次，才允许短暂打断。"""
    return (
        MEM.task_submits > 0
        and treasure_ready(turn)
        and treasure_imminent(turn)
        and MEM.treasure.pos is not None
    )


def solve(
    turn: Turn, role: Unit, commands: dict[int, dict[str, Any]],
) -> tuple[str, str]:
    task = turn.phase_task.strip()
    if not task:
        return "", ""
    on_task_text(task)
    family = _STATE.family or task_family(task)
    skill = _SKILLS.setdefault(family, Skill(family=family))

    # 1) 消化 LLM 回复
    resp = turn.llm_resp.strip()
    if resp and MEM.awaiting_task:
        answer = _extract_tag(resp, "ANSWER")
        if answer and not is_junk_answer(answer):
            payload = patch_task_answer(answer, turn)
            _commit_answer(role, commands, payload, skill)
            return "", ""
        cmd = _extract_tag(resp, "CMD")
        if cmd:
            MEM.awaiting_task = False
            _STATE.asked_llm = False
            safe = safe_cmd(cmd)
            _STATE.last_cmd = safe
            if safe not in skill.good_cmds:
                skill.good_cmds.append(safe)
            return "", safe
        # LLM 给了垃圾答案：丢掉等待态，继续探索/重问
        if answer and is_junk_answer(answer):
            MEM.awaiting_task = False
            _STATE.asked_llm = False

    # 2) 消化上轮沙盒输出
    raw_result = turn.last_cmd_result.strip()
    if raw_result:
        _STATE.last_sandbox = _clip(raw_result, 2500)
        skill.sandbox_notes = _STATE.last_sandbox[-1200:]
        concrete = concrete_sandbox_answer(raw_result)
        if concrete and not is_junk_answer(concrete):
            payload = patch_task_answer(concrete, turn)
            if _STATE.last_cmd and _STATE.last_cmd not in skill.good_cmds:
                skill.good_cmds.append(_STATE.last_cmd)
            _commit_answer(role, commands, payload, skill)
            return "", ""

    # 3) 判题缺键：就地修补再交
    if MEM.task_fails and (MEM.task_required or MEM.task_forbidden) and MEM.task_answer:
        payload = patch_task_answer(MEM.task_answer, turn)
        if payload != MEM.task_answer:
            _commit_answer(role, commands, payload, skill)
            return "", ""

    # 4) 复用已有 SOP：同类题直接跑沉淀命令
    if not _STATE.reused_skill and skill.good_cmds:
        cmd = _adapt_cmd(skill.good_cmds[-1], task, skill)
        _STATE.reused_skill = True
        _STATE.last_cmd = cmd
        return "", cmd

    # 5) 确定性沙盒探索（不依赖 LLM）
    boot = next_bootstrap_cmd(task, skill)
    if boot:
        _STATE.last_cmd = boot
        if boot not in skill.explore_cmds:
            skill.explore_cmds.append(boot)
        return "", boot

    # 6) 快超时保底
    if force_submit_now(turn):
        payload = patch_task_answer(_fallback_answer(turn), turn)
        if payload and not is_junk_answer(payload):
            _commit_answer(role, commands, payload, skill)
            return "", ""
        if MEM.task_required:
            payload = patch_task_answer("{}", turn)
            _commit_answer(role, commands, payload, skill)
            return "", ""

    # 7) 问 LLM（任务期间不占每日额度）
    if not MEM.awaiting_task:
        MEM.prompted_task = task
        MEM.awaiting_task = True
        _STATE.asked_llm = True
        mark_prompt(turn)
        return build_prompt(turn, skill), ""

    return "", ""


def next_bootstrap_cmd(task: str, skill: Skill) -> str | None:
    steps: list[str] = []
    files = _FILE_HINT.findall(task)
    steps.append("pwd; ls -la")
    steps.append("find . -maxdepth 3 -type f 2>/dev/null | head -80")
    for name in files:
        steps.append(f"sed -n '1,200p' {name} 2>/dev/null || cat {name}")
    steps.append(
        "python3 - <<'PY'\n"
        "import os,glob\n"
        "for p in sorted(glob.glob('**/*', recursive=True))[:80]:\n"
        "    if os.path.isfile(p):\n"
        "        print(p)\n"
        "PY"
    )
    # 常见文档名兜底
    for name in ("API_DOCS.md", "README.md", "api.md", "docs.md", "task.md"):
        if name.lower() not in task.lower():
            steps.append(f"test -f {name} && sed -n '1,220p' {name}")

    while _STATE.explore_i < len(steps):
        cmd = steps[_STATE.explore_i]
        _STATE.explore_i += 1
        if cmd in skill.explore_cmds and _STATE.explore_i < len(steps):
            continue
        return cmd
    return None


def concrete_sandbox_answer(raw: str) -> str:
    """只把真正像答案的沙盒输出当提交内容，避免把 ls 结果交上去。"""
    text = parse_sandbox_answer(raw)
    if not text:
        # 允许 ANSWER: 行即使 exitCode 非 0 以外的规范输出
        match = re.search(r"ANSWER\s*:\s*(.+)", raw, re.I)
        if match:
            text = match.group(1).strip()
        else:
            return ""
    if is_junk_answer(text):
        return ""
    if text.startswith("{") and text.endswith("}"):
        try:
            data = json.loads(text)
            if isinstance(data, dict) and data:
                return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        except json.JSONDecodeError:
            return ""
    if re.fullmatch(r"-?\d+(\.\d+)?", text):
        return text
    if len(text) <= 120 and not re.search(r"[\n\r]", text):
        if re.search(r"total\s+\d+|drwx|command not found|traceback", text, re.I):
            return ""
        # 短文本且不像目录列表，可能是 token/天气摘要
        if re.search(r"[:：].+", text) or re.search(r"[A-Za-z0-9_\-]{4,}", text):
            return text
    return ""


def is_junk_answer(text: str) -> bool:
    blob = (text or "").strip()
    if not blob:
        return True
    if _JUNK_ANSWER.search(blob):
        return True
    if blob.startswith("- ") and ("建议" in blob or "SOP" in blob.upper()):
        return True
    return False


def safe_cmd(cmd: str) -> str:
    cmd = cmd.strip().strip("`")
    if _SAFE_BLOCK.search(cmd):
        return "python3 -c \"print('')\""
    return cmd[:2000]


def build_prompt(turn: Turn, skill: Skill) -> str:
    parts = [
        "你是《未来战争》自进化任务求解器。沙盒无外网，可跑 shell 与 python。",
        "目标：产出任务要求的【具体答案】，不是写建议、不是写 SOP 说明。",
        "禁止输出「建议整理成 SOP」这类元描述。",
        "若已得到最终答案，只输出一行：ANSWER:<最终答案，优先合法 JSON>",
        "若还需执行命令，只输出一行：CMD:<单条命令>",
        "不要输出其它解释。JSON 键必须与题目要求一致，不能多不能少。",
        "",
        f"【当前任务】\n{turn.phase_task}",
        f"【剩余回合】{task_rounds_left(turn)}",
    ]
    if skill.good_cmds:
        parts.append("【已沉淀可复用命令】\n" + "\n".join(skill.good_cmds[-3:]))
    if skill.last_answer:
        parts.append(f"【同类题上次正确答案】\n{skill.last_answer}")
    if MEM.task_required:
        parts.append("【必须包含的键】 " + ", ".join(MEM.task_required))
    if MEM.task_forbidden:
        parts.append("【禁止出现的键】 " + ", ".join(MEM.task_forbidden))
    if turn.error_msgs:
        parts.append("【上轮判题错误】\n" + "\n".join(turn.error_msgs))
    if _STATE.last_sandbox:
        parts.append(f"【上轮沙盒输出】\n{_clip(_STATE.last_sandbox, 1800)}")
    elif skill.sandbox_notes:
        parts.append(f"【历史沙盒摘录】\n{_clip(skill.sandbox_notes, 1200)}")
    if MEM.task_answer:
        parts.append(f"【上次提交】\n{MEM.task_answer}")
    rider = treasure_rider(turn)
    if rider:
        parts.append(rider)
    return "\n".join(parts)


def _commit_answer(
    role: Unit,
    commands: dict[int, dict[str, Any]],
    payload: str,
    skill: Skill,
) -> None:
    remember_answer(payload)
    skill.last_answer = payload
    commands[role.unit_id] = submit_answer_command(payload)
    MEM.awaiting_task = False


def _fallback_answer(turn: Turn) -> str:
    for source in (
        MEM.task_answer,
        _extract_tag(turn.llm_resp, "ANSWER"),
        concrete_sandbox_answer(turn.last_cmd_result),
    ):
        if source and source.strip() and not is_junk_answer(source):
            return source.strip()
    skill = _SKILLS.get(_STATE.family)
    if skill and skill.last_answer and not is_junk_answer(skill.last_answer):
        return skill.last_answer
    if MEM.task_required:
        return "{}"
    return ""


def _adapt_cmd(cmd: str, task: str, skill: Skill) -> str:
    """简单变量替换：城市名、文件名随题目更新。"""
    adapted = cmd
    cities = re.findall(
        r"(北京|上海|广州|深圳|杭州|成都|重庆|武汉|西安|南京|"
        r"Beijing|Shanghai|Guangzhou|Shenzhen|Hangzhou)",
        task,
        flags=re.I,
    )
    if cities:
        new_city = cities[0]
        adapted = re.sub(
            r"(北京|上海|广州|深圳|杭州|成都|重庆|武汉|西安|南京|"
            r"Beijing|Shanghai|Guangzhou|Shenzhen|Hangzhou)",
            new_city,
            adapted,
            count=1,
            flags=re.I,
        )
    files = _FILE_HINT.findall(task)
    if files:
        adapted = re.sub(
            r"[A-Za-z0-9_./-]+\.(?:md|txt|json|py)",
            files[0],
            adapted,
            count=1,
        )
    return safe_cmd(adapted)


def _extract_tag(text: str, tag: str) -> str:
    match = re.search(rf"{tag}\s*:\s*(.+)", text, flags=re.IGNORECASE)
    if not match:
        return ""
    return match.group(1).strip().strip("`").strip()


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n...<truncated>...\n" + text[-20:]
