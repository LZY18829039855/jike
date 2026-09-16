from __future__ import annotations

import re
from typing import Any

from .grid import next_step
from .intel import (
    MEM,
    can_prompt,
    dump_ore,
    hold_ore,
    mark_prompt,
    mine_rank,
    missing_ritual,
    observe,
    parse_sandbox_answer,
    treasure_imminent,
    treasure_prompt,
    treasure_ready,
)
from .protocol import (
    BOMB,
    COPPER,
    DIZZY,
    IRON,
    MEDICINE,
    Pos,
    STATION_UPGRADE_1,
    STATION_UPGRADE_2,
    Turn,
    Unit,
    WALL,
    WALL_FIXER,
    WALL_MATERIAL,
    WALL_UPGRADE_1,
    WALL_UPGRADE_2,
    WEAPON_BUILD_COST,
    WEAPON_UPGRADE_1,
    WEAPON_UPGRADE_2,
    Robot,
    accept_task_command,
    attack_command,
    build_command,
    buy_command,
    collect_command,
    distance,
    move_command,
    sell_command,
    station_footprint,
    submit_answer_command,
    summon_treasure_command,
    use_command,
)

TOWER_LOADOUT = ("gatling", "railgun", "rocket")
STONE_RESERVE = 4
STONE_BATCH = 6
_NEIGHBOUR_STEPS = (
    (-1, -1), (-1, 0), (-1, 1),
    (0, -1), (0, 1),
    (1, -1), (1, 0), (1, 1),
)


def decide(payload: dict[str, Any]) -> dict[str, Any]:
    turn = Turn.load(payload)
    observe(turn)
    commands: dict[int, dict[str, Any]] = {}
    prompt = ""
    execute_cmd = ""
    if turn.is_day:
        prompt, execute_cmd = _day(turn, commands)
    else:
        prompt = _night(turn, commands)
    return {
        "roleCommandMap": {
            str(key): value for key, value in commands.items()
        },
        "prompt": prompt,
        "executeCmd": execute_cmd,
    }



def _day(
    turn: Turn, commands: dict[int, dict[str, Any]],
) -> tuple[str, str]:
    sites = _tower_sites(turn)
    order = _wall_order(turn)
    standing_towers = {unit.pos for unit in turn.weapons()}
    standing_walls = {unit.pos for unit in turn.walls()}
    occupied = turn.occupied_cells()
    # 全局最多 3 座：已有三塔则不再追建「偏好塔位」
    if len(standing_towers) >= 3:
        towers_missing: list[Pos] = []
    else:
        towers_missing = [
            pos for pos in sites if pos not in standing_towers
        ][: 3 - len(standing_towers)]
    walls_missing = [pos for pos in order if pos not in standing_walls]
    free_towers = [pos for pos in towers_missing if pos not in occupied]
    free_walls = [pos for pos in walls_missing if pos not in occupied]
    budget = turn.gold
    claimed: set[Pos] = set()

    prompt, execute_cmd = _pioneer_day(
        turn, sites, free_towers, free_walls, claimed, commands,
    )

    for role in turn.workers():
        if role.unit_id in commands:
            continue
        budget = _worker_day(
            turn,
            role,
            sites,
            free_towers,
            free_walls,
            claimed,
            commands,
            budget,
        )
    return prompt, execute_cmd


def _worker_day(
    turn: Turn,
    role: Unit,
    sites: tuple[Pos, ...],
    towers_missing: list[Pos],
    walls_missing: list[Pos],
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    budget: int,
) -> int:
    # 1) 紧急回血
    if role.health <= 80:
        med = role.find_item(MEDICINE)
        if med:
            commands[role.unit_id] = use_command(med)
            return budget

    # 2) 使用已有升级券 / 修复包
    if _try_use_upgrade(turn, role, commands):
        return budget

    # 3) 夜间临近：回防塔位
    if turn.near_night and turn.weapons():
        if _man_tower(turn, role, claimed, commands):
            return budget

    # 4) 优先建满三塔（先就近可立刻建造的格子）
    if towers_missing and budget >= WEAPON_BUILD_COST:
        candidates = [
            (index, site) for index, site in enumerate(sites)
            if site in towers_missing and site not in claimed
        ]
        if candidates:
            adjacent = [
                item for item in candidates
                if distance(role.pos, item[1]) <= 1 and role.pos != item[1]
            ]
            pool = adjacent or candidates
            index, site = min(
                pool,
                key=lambda item: (
                    0 if item in adjacent else 1,
                    distance(role.pos, item[1]),
                    item[0],
                ),
            )
            if _build_or_walk(
                turn, role, site, TOWER_LOADOUT[index], claimed, commands,
            ):
                if distance(role.pos, site) <= 1 and role.pos != site:
                    return budget - WEAPON_BUILD_COST
                return budget

    # 5) 背包矿石：高价或背包紧时去卖
    if _should_sell(turn, role) and _sell_or_walk(turn, role, claimed, commands):
        return budget

    # 6) 买升级券 / 消耗品
    spent = _buy_or_walk(turn, role, claimed, commands, budget)
    if spent is not None:
        return budget - spent

    # 7) 砌墙
    if walls_missing:
        stones = role.item_count(WALL_MATERIAL)
        mine = _adjacent_mine(turn, role, WALL_MATERIAL)
        if mine is not None and stones < STONE_BATCH:
            commands[role.unit_id] = collect_command(mine)
            claimed.add(mine)
            return budget
        if stones:
            for site in walls_missing:
                if site not in claimed:
                    _build_or_walk(turn, role, site, WALL, claimed, commands)
                    return budget
        if _mine_kind(turn, role, WALL_MATERIAL, claimed, commands):
            return budget

    # 8) 经济采集：铜/铁优先，保留少量石头
    if _mine_economy(turn, role, claimed, commands, keep_stone=bool(walls_missing)):
        return budget
    return budget


def _pioneer_day(
    turn: Turn,
    sites: tuple[Pos, ...],
    towers_missing: list[Pos],
    walls_missing: list[Pos],
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> tuple[str, str]:
    role = turn.pioneer()
    if role is None:
        return "", ""

    if role.health <= 60:
        med = role.find_item(MEDICINE)
        if med:
            commands[role.unit_id] = use_command(med)
            return "", ""

    if turn.phase_task.strip():
        prompt, execute_cmd = _solve_task(turn, role, commands)
        if role.unit_id in commands or prompt or execute_cmd:
            return prompt, execute_cmd
        if _stay_on_task(turn, role, claimed, commands):
            return "", ""

    prompt = _maybe_treasure_prompt(turn)

    if _hunt_treasure(turn, role, claimed, commands):
        return prompt, ""

    if (
        turn.weapons()
        and not turn.near_night
        and not treasure_imminent(turn)
        and _buy_ritual_or_walk(turn, role, claimed, commands, turn.gold) is not None
    ):
        return prompt, ""

    tasks = turn.available_tasks()
    skip_task = turn.near_night or (
        treasure_imminent(turn) and MEM.treasure.pos is not None
    )
    if tasks and not skip_task:
        task = max(tasks, key=lambda item: (item.score_reward, item.gold_reward))
        if distance(role.pos, task.pos) <= 1:
            commands[role.unit_id] = accept_task_command()
            return prompt, ""
        step = _step_toward(turn, role, task.pos, claimed)
        if step is not None:
            commands[role.unit_id] = move_command(step)
            return prompt, ""

    if turn.weapons():
        _man_tower(turn, role, claimed, commands, prefer_inside=True)
    elif towers_missing:
        _step_or_idle(turn, role, towers_missing[0], claimed, commands)
    return prompt, ""


def _stay_on_task(
    turn: Turn,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> bool:
    points = turn.our_task_points() or tuple(task.pos for task in turn.tasks)
    if not points:
        return False
    nearest = min(points, key=lambda pos: distance(role.pos, pos))
    if distance(role.pos, nearest) <= 1:
        return False
    step = _step_toward(turn, role, nearest, claimed)
    if step is not None:
        commands[role.unit_id] = move_command(step)
        return True
    return False


def _maybe_treasure_prompt(turn: Turn) -> str:
    guess = MEM.treasure
    if guess.done or turn.phase_task.strip() or not can_prompt(turn):
        return ""
    missing = guess.pos is None or guess.weak or not guess.items or guess.day is None
    new_legend = bool(turn.folk_legends.strip())
    if not (missing and new_legend and MEM.folk):
        return ""
    if MEM.llm_used >= 1 and MEM.treasure.last_result not in {2, 3}:
        return ""
    MEM.awaiting_treasure = True
    mark_prompt(turn)
    return treasure_prompt(turn)


def _hunt_treasure(
    turn: Turn,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> bool:
    guess = MEM.treasure
    if guess.done or guess.pos is None:
        return False
    if guess.day is not None and turn.day_no < guess.day - 1:
        return False
    need = missing_ritual(turn, role)
    if need:
        return _buy_named_or_walk(turn, role, need[0], claimed, commands, turn.gold)
    if not treasure_ready(turn):
        return False
    if distance(role.pos, guess.pos) <= 1 and role.pos != guess.pos:
        commands[role.unit_id] = summon_treasure_command(guess.pos, list(guess.items))
        return True
    step = _step_toward(turn, role, guess.pos, claimed)
    if step is not None:
        commands[role.unit_id] = move_command(step)
        return True
    return False


def _buy_ritual_or_walk(
    turn: Turn,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    budget: int,
) -> int | None:
    need = missing_ritual(turn, role)
    if not need:
        return None
    return 0 if _buy_named_or_walk(
        turn, role, need[0], claimed, commands, budget,
    ) else None


def _buy_named_or_walk(
    turn: Turn,
    role: Unit,
    name: str,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    budget: int,
) -> bool:
    shop = turn.shop_pos()
    if shop is None:
        return False
    price = turn.shop_price(name)
    if budget < price:
        return False
    if distance(role.pos, shop) <= 1:
        commands[role.unit_id] = buy_command(name, 1)
        return True
    step = _step_toward(turn, role, shop, claimed)
    if step is not None:
        commands[role.unit_id] = move_command(step)
        return True
    return False


def _solve_task(
    turn: Turn, role: Unit, commands: dict[int, dict[str, Any]],
) -> tuple[str, str]:
    resp = turn.llm_resp.strip()
    if resp:
        answer = _extract_tag(resp, "ANSWER")
        if answer:
            commands[role.unit_id] = submit_answer_command(answer)
            MEM.awaiting_task = False
            return "", ""
        cmd = _extract_tag(resp, "CMD")
        if cmd:
            MEM.awaiting_task = False
            return "", _safe_cmd(cmd)

    sandbox = parse_sandbox_answer(turn.last_cmd_result)
    if sandbox:
        commands[role.unit_id] = submit_answer_command(sandbox)
        MEM.awaiting_task = False
        return "", ""

    result = turn.last_cmd_result.strip()
    if result and not MEM.awaiting_task:
        MEM.awaiting_task = True
        MEM.prompted_task = turn.phase_task
        mark_prompt(turn)
        return _task_prompt(turn, sandbox=result), ""

    if MEM.prompted_task != turn.phase_task or not MEM.awaiting_task:
        MEM.prompted_task = turn.phase_task
        MEM.awaiting_task = True
        mark_prompt(turn)
        return _task_prompt(turn), ""
    return "", ""


def _safe_cmd(cmd: str) -> str:
    cmd = cmd.strip().strip("`")
    if re.search(r"rm\s+-rf|shutdown|reboot|mkfs|dd\s+if=", cmd, re.I):
        return "python3 -c \"print('')\""
    return cmd[:2000]


def _task_prompt(turn: Turn, sandbox: str = "") -> str:
    folk = "\n".join(MEM.folk)
    parts = [
        "你是《未来战争》参赛 Agent 的任务求解器。沙盒无外网，可执行 shell 与 python。",
        "若已得到最终答案，只输出一行：ANSWER:<最终答案>",
        "若还需在沙盒执行命令，只输出一行：CMD:<单条命令>",
        "不要输出其它解释。优先写可复用的 python3 -c 或脚本。",
        "",
        f"【当前任务】\n{turn.phase_task}",
    ]
    if sandbox:
        parts.append(f"【上轮沙盒输出】\n{sandbox}")
    if folk:
        parts.append(f"【民间传闻累计】\n{folk}")
    if turn.official_news:
        parts.append(f"【官方消息】\n{turn.official_news}")
    return "\n".join(parts)


def _extract_tag(text: str, tag: str) -> str:
    match = re.search(rf"{tag}\s*:\s*(.+)", text, flags=re.IGNORECASE)
    if not match:
        return ""
    return match.group(1).strip().strip("`").strip()


def _night(turn: Turn, commands: dict[int, dict[str, Any]]) -> str:
    claimed: set[Pos] = set()
    used_controllers: set[int] = set()
    prompt = ""
    pioneer = turn.pioneer()
    if pioneer is not None and treasure_ready(turn) and MEM.treasure.phase == "night":
        if _hunt_treasure(turn, pioneer, claimed, commands):
            used_controllers.add(pioneer.unit_id)
    else:
        prompt = _maybe_treasure_prompt(turn)

    pairs = _assign_towers(turn, used_controllers)
    for role, _ in pairs:
        if role.unit_id in commands:
            continue
        if role.health <= 60:
            med = role.find_item(MEDICINE)
            if med:
                commands[role.unit_id] = use_command(med)
                used_controllers.add(role.unit_id)
                continue
        if _try_combat_item(turn, role, commands):
            used_controllers.add(role.unit_id)

    for role, tower in pairs:
        if role.unit_id in used_controllers or role.unit_id in commands:
            continue
        if distance(role.pos, tower.pos) <= 1:
            if tower.cooldown > 0:
                continue
            targets = _attack_targets(turn, tower)
            if targets:
                commands[tower.unit_id] = attack_command(role.unit_id, *targets)
                used_controllers.add(role.unit_id)
            continue
        step = _step_toward(turn, role, tower.pos, claimed)
        if step is not None:
            commands[role.unit_id] = move_command(step)
    return prompt


def _assign_towers(
    turn: Turn, exclude: set[int] | None = None,
) -> list[tuple[Unit, Unit]]:
    banned = exclude or set()
    roles = [role for role in turn.controllable() if role.unit_id not in banned]
    towers = list(turn.weapons())
    if not roles or not towers:
        return []
    pairs: list[tuple[Unit, Unit]] = []
    remaining = set(range(len(roles)))
    for tower in towers:
        best_idx = min(
            remaining,
            key=lambda idx: (
                distance(roles[idx].pos, tower.pos),
                roles[idx].unit_id,
            ),
        )
        pairs.append((roles[best_idx], tower))
        remaining.remove(best_idx)
        if not remaining:
            break
    return pairs


def _try_combat_item(
    turn: Turn, role: Unit, commands: dict[int, dict[str, Any]],
) -> bool:
    station = turn.station()
    if station is None:
        return False
    hostiles = [
        robot for robot in turn.hostile_robots()
        if not robot.dizzy and distance(station.pos, robot.pos) <= 12
    ]
    if len(hostiles) < 3 and not any(
        robot.kind in {"bossRobot", "largeRobot"} for robot in hostiles
    ):
        return False

    cluster = _best_cluster(hostiles)
    if cluster is None:
        return False
    center, count = cluster
    if count < 2:
        return False

    bomb = role.find_item(BOMB)
    if bomb and count >= 3:
        commands[role.unit_id] = use_command(bomb, center)
        return True
    dizzy = role.find_item(DIZZY)
    if dizzy and any(
        robot.kind in {"bossRobot", "largeRobot"}
        and distance(center, robot.pos) <= 1
        for robot in hostiles
    ):
        commands[role.unit_id] = use_command(dizzy, center)
        return True
    return False


def _best_cluster(robots: list[Robot]) -> tuple[Pos, int] | None:
    if not robots:
        return None
    best_pos = robots[0].pos
    best_key = (-1, -1)
    for robot in robots:
        count = sum(1 for other in robots if distance(robot.pos, other.pos) <= 1)
        key = (count, robot.kill_score)
        if key > best_key:
            best_key = key
            best_pos = robot.pos
    return best_pos, best_key[0]


def _attack_targets(turn: Turn, tower: Unit) -> list[Pos]:
    reach = tower.range_of_attack()
    candidates = [
        robot for robot in turn.hostile_robots()
        if distance(tower.pos, robot.pos) <= reach
    ]
    if not candidates:
        # 打任意可见机器人（含打对面的），赚击杀分
        candidates = [
            robot for robot in turn.robots
            if robot.health > 0 and distance(tower.pos, robot.pos) <= reach
        ]
    if not candidates:
        return []

    shots = tower.shot_count()
    if tower.kind == "railgun":
        target = max(candidates, key=lambda robot: _railgun_score(turn, tower, robot))
        return [target.pos]
    if tower.kind == "gatling":
        return _gatling_targets(tower, candidates, shots)
    return _rocket_targets(turn, tower, candidates, shots)


def _threat_key(turn: Turn, robot: Robot) -> tuple:
    station = turn.station()
    base_dist = distance(station.pos, robot.pos) if station else 0
    finishable = 1 if robot.health <= 40 else 0
    return (
        robot.kill_score,
        finishable,
        -base_dist,
        -robot.health,
        -robot.robot_id,
    )


def _railgun_score(turn: Turn, tower: Unit, robot: Robot) -> tuple:
    path_value = 0
    line = set(_chebyshev_line(tower.pos, robot.pos))
    energy = max(tower.level, 1) * 10
    for other in sorted(
        turn.hostile_robots(),
        key=lambda item: distance(tower.pos, item.pos),
    ):
        if other.pos not in line:
            continue
        if distance(tower.pos, other.pos) > tower.range_of_attack():
            continue
        dealt = min(energy, other.health)
        path_value += other.kill_score * 20 + dealt
        energy -= dealt
        if energy <= 0:
            break
    return (*_threat_key(turn, robot), path_value)


def _chebyshev_line(origin: Pos, target: Pos) -> list[Pos]:
    steps = distance(origin, target)
    if steps == 0:
        return [origin]
    line = []
    for index in range(steps + 1):
        x = origin.x + round(index * (target.x - origin.x) / steps)
        y = origin.y + round(index * (target.y - origin.y) / steps)
        line.append(Pos(x, y))
    return line


def _on_fire_line(origin: Pos, target: Pos, point: Pos) -> bool:
    return point in set(_chebyshev_line(origin, target))


def _gatling_targets(
    tower: Unit, candidates: list[Robot], shots: int,
) -> list[Pos]:
    ordered = sorted(
        candidates,
        key=lambda robot: (
            robot.kill_score,
            -robot.health,
            -distance(tower.pos, robot.pos),
            -robot.robot_id,
        ),
        reverse=True,
    )
    primary = ordered[0]
    chosen = [primary]
    for robot in ordered[1:]:
        if len(chosen) >= shots:
            break
        if _cone_ok(tower.pos, [item.pos for item in chosen] + [robot.pos]):
            chosen.append(robot)
    while len(chosen) < shots:
        chosen.append(primary)
    return [robot.pos for robot in chosen[:shots]]


def _cone_ok(origin: Pos, points: list[Pos]) -> bool:
    for i, first in enumerate(points):
        for second in points[i + 1:]:
            if not _angle_ok(origin, first, second):
                return False
    return True


def _angle_ok(origin: Pos, first: Pos, second: Pos) -> bool:
    ax, ay = first.x - origin.x, first.y - origin.y
    bx, by = second.x - origin.x, second.y - origin.y
    if ax == 0 and ay == 0:
        return True
    if bx == 0 and by == 0:
        return True
    dot = ax * bx + ay * by
    return dot >= 0


def _rocket_targets(
    turn: Turn, tower: Unit, candidates: list[Robot], shots: int,
) -> list[Pos]:
    reach = tower.range_of_attack()
    cells = {robot.pos for robot in candidates}
    scored: list[tuple[int, Pos]] = []
    for cell in cells:
        value = 0
        for robot in candidates:
            dist = distance(cell, robot.pos)
            if dist == 0:
                value += robot.kill_score * 20 + min(robot.health, 20)
            elif dist == 1:
                value += robot.kill_score * 10 + min(robot.health, 10)
        scored.append((value, cell))
    scored.sort(key=lambda item: (-item[0], item[1].x, item[1].y))
    chosen: list[Pos] = []
    for _, cell in scored:
        if len(chosen) >= shots:
            break
        if any(pos == cell for pos in chosen):
            continue
        if distance(tower.pos, cell) <= reach:
            chosen.append(cell)
    if not chosen:
        chosen = [candidates[0].pos]
    while len(chosen) < shots:
        chosen.append(chosen[0])
    return chosen[:shots]


def _try_use_upgrade(
    turn: Turn, role: Unit, commands: dict[int, dict[str, Any]],
) -> bool:
    # 武器升级
    for voucher, need_level in (
        (WEAPON_UPGRADE_1, 1),
        (WEAPON_UPGRADE_2, 2),
    ):
        item = role.find_item(voucher)
        if not item:
            continue
        for tower in turn.weapons():
            if tower.level == need_level and distance(role.pos, tower.pos) <= 1:
                commands[role.unit_id] = use_command(item, tower.pos)
                return True
        # 走向待升级塔
        targets = [tower for tower in turn.weapons() if tower.level == need_level]
        if targets:
            target = min(targets, key=lambda unit: distance(role.pos, unit.pos))
            step = _step_toward(turn, role, target.pos, set())
            if step is not None:
                commands[role.unit_id] = move_command(step)
                return True

    station = turn.station()
    if station is not None:
        for voucher, need_level in (
            (STATION_UPGRADE_1, 1),
            (STATION_UPGRADE_2, 2),
        ):
            item = role.find_item(voucher)
            if not item:
                continue
            if _footprint_distance(role.pos, station_footprint(station.pos)) <= 1:
                commands[role.unit_id] = use_command(item, station.pos)
                return True
            step = _step_toward(turn, role, station.pos, set(), inside_only=True)
            if step is not None:
                commands[role.unit_id] = move_command(step)
                return True

    # 残血墙修复 / 围墙升级
    fixer = role.find_item(WALL_FIXER)
    if fixer:
        damaged = [
            wall for wall in turn.walls()
            if wall.health < 600 and distance(role.pos, wall.pos) <= 1
        ]
        if damaged:
            wall = min(damaged, key=lambda unit: unit.health)
            commands[role.unit_id] = use_command(fixer, wall.pos)
            return True

    for voucher, need_level in (
        (WALL_UPGRADE_1, 1),
        (WALL_UPGRADE_2, 2),
    ):
        item = role.find_item(voucher)
        if not item:
            continue
        walls = [
            wall for wall in turn.walls()
            if wall.level == need_level and distance(role.pos, wall.pos) <= 1
        ]
        if walls:
            wall = min(walls, key=lambda unit: unit.health)
            commands[role.unit_id] = use_command(item, wall.pos)
            return True
        targets = [wall for wall in turn.walls() if wall.level == need_level]
        if targets:
            target = min(targets, key=lambda unit: distance(role.pos, unit.pos))
            step = _step_toward(turn, role, target.pos, set())
            if step is not None:
                commands[role.unit_id] = move_command(step)
                return True
    return False


def _should_sell(turn: Turn, role: Unit) -> bool:
    ores = role.ore_counts()
    if not ores:
        return False
    if role.backpack_full:
        return True
    sellable = 0
    for name, count in ores.items():
        if name == WALL_MATERIAL:
            count = max(0, count - STONE_RESERVE)
        if count <= 0:
            continue
        if hold_ore(turn, name) and not dump_ore(turn, name):
            continue
        sellable += count
        if dump_ore(turn, name):
            return True
    if sellable >= 8:
        return True
    if ores.get(COPPER, 0) and turn.ore_price(COPPER) >= 5 and not hold_ore(turn, COPPER):
        return True
    if ores.get(IRON, 0) and turn.ore_price(IRON) >= 4 and not hold_ore(turn, IRON):
        return True
    if turn.gold < WEAPON_BUILD_COST and sellable >= 3:
        return True
    if len(turn.weapons()) >= 3 and turn.gold < 100 and sellable >= 5:
        return True
    return False


def _sell_or_walk(
    turn: Turn,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> bool:
    vendor = turn.vendor_pos()
    if vendor is None:
        return False
    ores = role.ore_counts()
    if not ores:
        return False
    # 保留砌墙用石头
    sell_name = None
    sell_num = 0
    best_key = (-1, -1)
    for name in (COPPER, IRON, WALL_MATERIAL):
        count = ores.get(name, 0)
        if name == WALL_MATERIAL:
            count = max(0, count - STONE_RESERVE)
        if count <= 0:
            continue
        if hold_ore(turn, name) and not role.backpack_full:
            continue
        price = turn.ore_price(name)
        key = (1 if dump_ore(turn, name) else 0, price)
        if key > best_key:
            best_key = key
            sell_name = name
            sell_num = count
    if not sell_name:
        return False
    if distance(role.pos, vendor) <= 1:
        commands[role.unit_id] = sell_command(sell_name, sell_num)
        return True
    step = _step_toward(turn, role, vendor, claimed)
    if step is not None:
        commands[role.unit_id] = move_command(step)
        return True
    return False


def _buy_or_walk(
    turn: Turn,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    budget: int,
) -> int | None:
    shop = turn.shop_pos()
    if shop is None or len(turn.weapons()) < 3:
        return None

    want = _wanted_purchase(turn, role, budget)
    if want is None:
        return None
    name, price = want
    if distance(role.pos, shop) <= 1:
        commands[role.unit_id] = buy_command(name, 1)
        return price
    step = _step_toward(turn, role, shop, claimed)
    if step is not None:
        commands[role.unit_id] = move_command(step)
        return 0
    return None


def _wanted_purchase(
    turn: Turn, role: Unit, budget: int,
) -> tuple[str, int] | None:
    # 已有券则先别买重复的
    weapons = turn.weapons()
    station = turn.station()

    def can_buy(name: str) -> tuple[str, int] | None:
        price = turn.shop_price(name)
        if budget >= price and not role.has_item(name):
            return name, price
        return None

    if any(tower.level == 1 for tower in weapons):
        item = can_buy(WEAPON_UPGRADE_1)
        if item:
            return item
    if station is not None and station.level == 1:
        item = can_buy(STATION_UPGRADE_1)
        if item:
            return item
    if any(tower.level == 2 for tower in weapons):
        item = can_buy(WEAPON_UPGRADE_2)
        if item:
            return item
    if station is not None and station.level == 2:
        item = can_buy(STATION_UPGRADE_2)
        if item:
            return item
    if turn.day_no >= 3 and budget >= turn.shop_price(DIZZY):
        item = can_buy(DIZZY)
        if item:
            return item
    if turn.day_no >= 4 and budget >= turn.shop_price(BOMB):
        item = can_buy(BOMB)
        if item:
            return item
    need = missing_ritual(turn, role)
    if need and budget >= turn.shop_price(need[0]):
        item = can_buy(need[0])
        if item:
            return item
    if role.health < 150:
        item = can_buy(MEDICINE)
        if item:
            return item
    if any(wall.health < 700 for wall in turn.walls()):
        item = can_buy(WALL_FIXER)
        if item:
            return item
    if turn.walls() and budget >= turn.shop_price(WALL_UPGRADE_1) + 50:
        item = can_buy(WALL_UPGRADE_1)
        if item:
            return item
    return None


def _man_tower(
    turn: Turn,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    prefer_inside: bool = False,
) -> bool:
    towers = turn.weapons()
    if not towers:
        return False
    # 找空闲塔：周围没有其他可控角色
    occupied_stands: set[Pos] = set()
    for other in turn.controllable():
        if other.unit_id == role.unit_id:
            continue
        for tower in towers:
            if distance(other.pos, tower.pos) <= 1:
                occupied_stands.add(tower.pos)
    free = [tower for tower in towers if tower.pos not in occupied_stands]
    pool = free or list(towers)
    tower = min(pool, key=lambda unit: distance(role.pos, unit.pos))
    if distance(role.pos, tower.pos) <= 1:
        return False
    step = _step_toward(
        turn, role, tower.pos, claimed, inside_only=prefer_inside,
    )
    if step is not None:
        commands[role.unit_id] = move_command(step)
        return True
    return False


def _mine_economy(
    turn: Turn,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    keep_stone: bool,
) -> bool:
    if role.backpack_full:
        return _sell_or_walk(turn, role, claimed, commands)

    ranked = mine_rank(turn, keep_stone)
    for kind in ranked:
        if _mine_kind(turn, role, kind, claimed, commands):
            return True
    return False


def _adjacent_mine(turn: Turn, role: Unit, kind: str) -> Pos | None:
    mines = sorted(
        (
            mine for mine in turn.mines(kind)
            if role.pos != mine and distance(role.pos, mine) <= 1
        ),
        key=lambda pos: (distance(role.pos, pos), pos.x, pos.y),
    )
    return mines[0] if mines else None


def _mine_kind(
    turn: Turn,
    role: Unit,
    kind: str,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> bool:
    if role.backpack_full:
        return False
    mines = sorted(
        (pos for pos in turn.mines(kind) if pos not in claimed),
        key=lambda pos: (distance(role.pos, pos), pos.x, pos.y),
    )
    for mine in mines:
        if role.pos != mine and distance(role.pos, mine) <= 1:
            commands[role.unit_id] = collect_command(mine)
            claimed.add(mine)
            return True
        step = _step_toward(turn, role, mine, claimed)
        if step is not None:
            commands[role.unit_id] = move_command(step)
            return True
    return False


def _build_or_walk(
    turn: Turn,
    role: Unit,
    target: Pos,
    name: str,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> bool:
    if role.pos != target and distance(role.pos, target) <= 1:
        commands[role.unit_id] = build_command(target, name)
        claimed.add(target)
        return True
    step = _step_toward(turn, role, target, claimed)
    if step is not None:
        commands[role.unit_id] = move_command(step)
        return True
    return False


def _step_or_idle(
    turn: Turn,
    role: Unit,
    target: Pos,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> None:
    step = _step_toward(turn, role, target, claimed)
    if step is not None:
        commands[role.unit_id] = move_command(step)


def _step_toward(
    turn: Turn,
    role: Unit,
    target: Pos,
    claimed: set[Pos],
    *,
    inside_only: bool = False,
) -> Pos | None:
    for stand in _stand_cells(turn, role, target, claimed, inside_only):
        if stand == role.pos:
            return None
        step = next_step(turn, role, stand)
        if step is None or step in claimed:
            continue
        claimed.add(step)
        return step
    return None


def _stand_cells(
    turn: Turn,
    role: Unit,
    target: Pos,
    claimed: set[Pos],
    inside_only: bool = False,
) -> list[Pos]:
    station = turn.station()
    footprint = station_footprint(station.pos) if station else ()
    blocked = turn.blocked(role)
    cells = [
        pos for pos in _neighbours(target)
        if turn.land(pos)
        and pos not in blocked
        and (pos == role.pos or pos not in claimed)
        and (
            not inside_only
            or _footprint_distance(pos, footprint) <= 1
        )
    ]
    cells.sort(
        key=lambda pos: (_footprint_distance(pos, footprint), pos.x, pos.y),
    )
    return cells


def _tower_sites(turn: Turn) -> tuple[Pos, ...]:
    station = turn.station()
    if station is None:
        return ()
    footprint = station_footprint(station.pos)
    cells = [
        pos for pos in _cells_at_distance(station.pos, 1) if turn.land(pos)
    ]
    cells.sort(
        key=lambda pos: (_footprint_distance(pos, footprint), pos.x, pos.y),
    )
    return tuple(cells[:3])


def _wall_order(turn: Turn) -> tuple[Pos, ...]:
    station = turn.station()
    if station is None:
        return ()
    footprint = station_footprint(station.pos)
    xs = [pos.x for pos in footprint]
    ys = [pos.y for pos in footprint]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    order = [
        *(Pos(x, ymin - 2) for x in range(xmax + 2, xmin - 3, -1)),
        *(Pos(xmin - 2, y) for y in range(ymin - 1, ymax + 2)),
        *(Pos(x, ymax + 2) for x in range(xmin - 2, xmax + 3)),
        *(Pos(xmax + 2, y) for y in range(ymax + 1, ymin - 2, -1)),
    ]
    entrance = Pos(xmax + 2, ymin - 1)
    return tuple(pos for pos in order if pos != entrance and turn.land(pos))


def _cells_at_distance(station_pos: Pos, radius: int) -> tuple[Pos, ...]:
    footprint = station_footprint(station_pos)
    xs = [pos.x for pos in footprint]
    ys = [pos.y for pos in footprint]
    cells = []
    for x in range(min(xs) - radius, max(xs) + radius + 1):
        for y in range(min(ys) - radius, max(ys) + radius + 1):
            pos = Pos(x, y)
            if pos in footprint:
                continue
            if _footprint_distance(pos, footprint) == radius:
                cells.append(pos)
    return tuple(cells)


def _footprint_distance(pos: Pos, footprint: tuple[Pos, ...]) -> int:
    if not footprint:
        return 0
    return min(distance(pos, cell) for cell in footprint)


def _neighbours(pos: Pos) -> tuple[Pos, ...]:
    return tuple(Pos(pos.x + dx, pos.y + dy) for dx, dy in _NEIGHBOUR_STEPS)
