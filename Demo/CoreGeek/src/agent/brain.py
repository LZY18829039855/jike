from __future__ import annotations

from itertools import permutations
from typing import Any

from .grid import next_step
from .evolve import (
    pick_task,
    should_prioritize,
    solve as solve_evolve_task,
    treasure_may_interrupt,
)
from .intel import (
    MEM,
    bad_build_cells,
    can_prompt,
    dump_ore,
    failed_cells,
    hold_ore,
    mark_prompt,
    mine_rank,
    missing_ritual,
    note_summon_used,
    observe,
    remember_commands,
    remember_task_accept,
    should_abandon_task,
    summon_budget_left,
    threat_anchor,
    wall_zone_seeds,
    weapon_ready,
    weapon_zone_seeds,
    treasure_imminent,
    treasure_prompt,
    treasure_ready,
)
from .protocol import (
    BOMB,
    BOSS_SUMMON,
    COPPER,
    DIZZY,
    IRON,
    LARGE_SUMMON,
    MEDICINE,
    MIDDLE_SUMMON,
    Pos,
    SMALL_SUMMON,
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
    remove_command,
    sell_command,
    station_footprint,
    summon_treasure_command,
    use_command,
)

TOWER_LOADOUT = ("rocket", "rocket", "rocket")
STONE_RESERVE = 12
STONE_BATCH = 10
SUMMON_ORDERS = (BOSS_SUMMON, LARGE_SUMMON, MIDDLE_SUMMON, SMALL_SUMMON)
ROBOT_ATTACK = {
    "smallRobot": 5,
    "middleRobot": 10,
    "largeRobot": 20,
    "bossRobot": 40,
}
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
        prompt, execute_cmd = _night(turn, commands)
    remember_commands(commands)
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
    order = _wall_order(turn, seal=_should_seal(turn))
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
    banned = bad_build_cells()
    free_towers = [
        pos for pos in towers_missing if pos not in occupied and pos not in banned
    ]
    free_walls = [
        pos for pos in walls_missing if pos not in occupied and pos not in banned
    ]
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
        if role.unit_id not in commands and not _holding_line(turn, role):
            _idle_work(turn, role, claimed, commands)
    return prompt, execute_cmd


def _holding_line(turn: Turn, role: Unit) -> bool:
    """入夜前已经站到塔边的角色不要再被支使去采矿。"""
    if not turn.near_night:
        return False
    return any(distance(role.pos, tower.pos) <= 1 for tower in turn.weapons())


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

    # 2) 手里已有升级券优先用掉（尤其是武器升级券）
    if _try_use_upgrade(turn, role, commands):
        return budget

    # 2.5) 手里的召唤令立刻用掉，作用于对手下个夜晚
    if _try_use_summon(role, commands):
        return budget

    # 3) 天亮后拆掉夜里封上的缺口，否则全队出不了门
    if not turn.near_night and _open_gate(turn, role, claimed, commands):
        return budget

    # 4) 夜间临近：回防塔位
    if turn.near_night and turn.weapons():
        if _man_tower(turn, role, claimed, commands):
            return budget

    # 5) 开局并行：先凑满 3 座火箭炮，避免整晚无火力
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
                turn, role, site, TOWER_LOADOUT[min(index, 2)], claimed, commands,
            ):
                if distance(role.pos, site) <= 1 and role.pos != site:
                    return budget - WEAPON_BUILD_COST
                return budget

    # 6) 先砌来敌面全长 + 上下各一半，再去做别的
    if walls_missing and _wall_work(
        turn, role, walls_missing, claimed, commands,
    ):
        return budget

    # 6.5) 核心墙完成后，再升级武器
    if _prefer_weapon_upgrade(turn, role, budget):
        spent = _buy_weapon_upgrade(turn, role, claimed, commands, budget)
        if spent is not None:
            return budget - spent

    # 7) 有铜/铁/涨价矿就尽快卖掉换成金币
    if _should_sell(turn, role) and _sell_or_walk(turn, role, claimed, commands):
        return budget

    # 8) 买武器升级券等；买完由步骤 2 在后续回合使用
    spent = _buy_or_walk(turn, role, claimed, commands, budget)
    if spent is not None:
        return budget - spent

    # 9) 经济采集：涨价矿 > 铜 > 铁；石头只在仍缺墙时保底
    if _mine_economy(turn, role, claimed, commands, keep_stone=bool(walls_missing)):
        return budget
    return budget


def _prefer_weapon_upgrade(turn: Turn, role: Unit, budget: int) -> bool:
    """核心墙已齐、有塔且金币够时，去买武器升级券。"""
    weapons = turn.weapons()
    if not weapons:
        return False
    if role.find_item(WEAPON_UPGRADE_1) or role.find_item(WEAPON_UPGRADE_2):
        return False
    if any(tower.level == 1 for tower in weapons):
        return budget >= turn.shop_price(WEAPON_UPGRADE_1)
    if any(tower.level == 2 for tower in weapons):
        return budget >= turn.shop_price(WEAPON_UPGRADE_2)
    return False


def _buy_weapon_upgrade(
    turn: Turn,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    budget: int,
) -> int | None:
    weapons = turn.weapons()
    name = None
    if any(tower.level == 1 for tower in weapons):
        name = WEAPON_UPGRADE_1
    elif any(tower.level == 2 for tower in weapons):
        name = WEAPON_UPGRADE_2
    if name is None:
        return None
    price = turn.shop_price(name)
    if budget < price:
        return None
    if _buy_named_or_walk(turn, role, name, claimed, commands, budget):
        shop = turn.shop_pos()
        if shop is not None and distance(role.pos, shop) <= 1:
            return price
        return 0
    return None


def _try_use_summon(role: Unit, commands: dict[int, dict[str, Any]]) -> bool:
    if summon_budget_left() <= 0:
        return False
    for name in SUMMON_ORDERS:
        if role.has_item(name):
            commands[role.unit_id] = use_command(name)
            note_summon_used()
            return True
    return False


def _should_seal(turn: Turn) -> bool:
    """墙已基本合围、且所有角色都退回基地一圈内，才把缺口砌死。"""
    if not turn.near_night:
        return False
    station = turn.station()
    if station is None:
        return False
    if _ring_progress(turn)[1] < 0.6:
        return False
    footprint = station_footprint(station.pos)
    roles = turn.controllable()
    if not roles:
        return False
    return all(_footprint_distance(role.pos, footprint) <= 2 for role in roles)


def _open_gate(
    turn: Turn,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> bool:
    gate = _gate_cell(turn)
    if gate is None:
        return False
    wall = next((unit for unit in turn.walls() if unit.pos == gate), None)
    if wall is None:
        return False
    if role.pos != gate and distance(role.pos, gate) <= 1:
        commands[role.unit_id] = remove_command(gate)
        return True
    step = _step_toward(turn, role, gate, claimed)
    if step is not None:
        commands[role.unit_id] = move_command(step)
        return True
    return False


def _wall_work(
    turn: Turn,
    role: Unit,
    walls_missing: list[Pos],
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    hunt_stone: bool = True,
) -> bool:
    stones = role.item_count(WALL_MATERIAL)
    mine = _adjacent_mine(turn, role, WALL_MATERIAL)
    if mine is not None and stones < STONE_BATCH:
        commands[role.unit_id] = collect_command(mine)
        claimed.add(mine)
        return True
    if stones:
        pool = [site for site in walls_missing if site not in claimed]
        # 能立刻动手的格子优先，别为了「理想墙位」空跑一路
        adjacent = [
            site for site in pool
            if site != role.pos and distance(role.pos, site) <= 1
        ]
        for site in adjacent or pool:
            if _build_or_walk(turn, role, site, WALL, claimed, commands):
                return True
    if not hunt_stone:
        return False
    return _mine_kind(turn, role, WALL_MATERIAL, claimed, commands)


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

    # —— 开拓者金币主线：能开宝藏就开；否则刷任务点 ——
    if turn.phase_task.strip():
        # 先求解/交卷，避免超时离点时把刚拿到的答案扔掉
        prompt, execute_cmd = solve_evolve_task(turn, role, commands)
        submitted = (commands.get(role.unit_id) or {}).get("action") == "submitAnswer"
        if submitted:
            return prompt, execute_cmd
        if should_abandon_task(turn):
            if _leave_task(turn, role, claimed, commands):
                return "", ""
            return "", ""
        _glue_to_task(turn, role, claimed, commands)
        if role.unit_id in commands or prompt or execute_cmd:
            return prompt, execute_cmd
        return "", ""

    prompt = _maybe_treasure_prompt(turn)

    # 条件齐全就开宝藏（积分/金币大头）
    if _hunt_treasure(turn, role, claimed, commands):
        return prompt, ""

    # 宝藏将开但缺祭品：先去买齐
    if (
        turn.weapons()
        and not turn.near_night
        and treasure_imminent(turn)
        and _buy_ritual_or_walk(turn, role, claimed, commands, turn.gold) is not None
    ):
        return prompt, ""

    # 否则刷任务点换金币/积分（冷却期也持续 accept，敌方同款）
    if should_prioritize(turn):
        if _accept_or_approach(turn, role, claimed, commands):
            return prompt, ""

    # 非紧急时也可慢慢备齐祭品
    if (
        turn.weapons()
        and not turn.near_night
        and not treasure_imminent(turn)
        and _buy_ritual_or_walk(turn, role, claimed, commands, turn.gold) is not None
    ):
        return prompt, ""

    if turn.weapons():
        _man_tower(turn, role, claimed, commands, prefer_inside=True)
    elif towers_missing:
        _step_or_idle(turn, role, towers_missing[0], claimed, commands)
    return prompt, ""


def _accept_or_approach(
    turn: Turn,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> bool:
    """有就绪任务才接；冷却中走近任务点等待，不空刷 accept。"""
    task = pick_task(turn, role)
    if task is not None:
        if distance(role.pos, task.pos) <= 1:
            commands[role.unit_id] = accept_task_command()
            remember_task_accept(task, turn.round_no)
            return True
        step = _step_toward(turn, role, task.pos, claimed)
        if step is not None:
            commands[role.unit_id] = move_command(step)
            return True
        return False

    points = list(turn.our_task_points()) or [item.pos for item in turn.tasks if item.valid]
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


def _leave_task(
    turn: Turn,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> bool:
    station = turn.station()
    target = station.pos if station else role.pos
    weapons = turn.weapons()
    if weapons:
        target = min(weapons, key=lambda unit: distance(role.pos, unit.pos)).pos
    step = _step_toward(turn, role, target, claimed)
    if step is not None:
        commands[role.unit_id] = move_command(step)
        return True
    for dx, dy in _NEIGHBOUR_STEPS:
        pos = Pos(role.pos.x + dx, role.pos.y + dy)
        if turn.land(pos) and pos not in claimed:
            commands[role.unit_id] = move_command(pos)
            claimed.add(pos)
            return True
    return False


def _idle_work(
    turn: Turn,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> None:
    if turn.near_night and _man_tower(turn, role, claimed, commands):
        return
    if _mine_economy(turn, role, claimed, commands, keep_stone=False):
        return
    if turn.weapons():
        _man_tower(turn, role, claimed, commands)


def _stay_on_task(
    turn: Turn,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> bool:
    return _glue_to_task(turn, role, claimed, commands)


def _hold_near_task(
    turn: Turn,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> bool:
    return _glue_to_task(turn, role, claimed, commands)


def _glue_to_task(
    turn: Turn,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> bool:
    """钉在当前接取任务点 1 格内；走出 1 格任务会被强制结束。"""
    if role.unit_id in commands:
        return False
    anchor = MEM.task_pos or MEM.pending_task_pos
    if anchor is None:
        return False
    dist = distance(role.pos, anchor)
    if dist == 0:
        return False
    if dist == 1:
        if turn.land(anchor) and anchor not in claimed:
            commands[role.unit_id] = move_command(anchor)
            claimed.add(anchor)
            return True
        return False
    step = _step_toward(turn, role, anchor, claimed)
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
    return solve_evolve_task(turn, role, commands)


def _night(turn: Turn, commands: dict[int, dict[str, Any]]) -> tuple[str, str]:
    claimed: set[Pos] = set()
    used_controllers: set[int] = set()
    prompt = ""
    execute_cmd = ""
    pioneer = turn.pioneer()

    # 夜里：已接任务继续做完；否则能开夜宝藏就开；否则继续刷任务点；再否则当炮手
    if pioneer is not None and turn.phase_task.strip():
        prompt, execute_cmd = solve_evolve_task(turn, pioneer, commands)
        submitted = (commands.get(pioneer.unit_id) or {}).get("action") == "submitAnswer"
        if submitted:
            used_controllers.add(pioneer.unit_id)
        elif should_abandon_task(turn):
            if _leave_task(turn, pioneer, claimed, commands):
                used_controllers.add(pioneer.unit_id)
        else:
            _glue_to_task(turn, pioneer, claimed, commands)
            if pioneer.unit_id in commands or prompt or execute_cmd:
                used_controllers.add(pioneer.unit_id)
    elif pioneer is not None and treasure_ready(turn) and MEM.treasure.phase == "night":
        if _hunt_treasure(turn, pioneer, claimed, commands):
            used_controllers.add(pioneer.unit_id)
    elif pioneer is not None and should_prioritize(turn):
        if _accept_or_approach(turn, pioneer, claimed, commands):
            used_controllers.add(pioneer.unit_id)
    else:
        prompt = _maybe_treasure_prompt(turn)

    # 无任务时开拓者专职炮手（敌方日志 gunner=10011）
    if (
        pioneer is not None
        and pioneer.unit_id not in used_controllers
        and pioneer.unit_id not in commands
        and turn.weapons()
    ):
        if _man_tower(turn, pioneer, claimed, commands):
            used_controllers.add(pioneer.unit_id)

    pairs = _assign_towers(turn, used_controllers)
    for role, _ in pairs:
        if role.unit_id in commands:
            continue
        if role.health <= 100:
            med = role.find_item(MEDICINE)
            if med:
                commands[role.unit_id] = use_command(med)
                used_controllers.add(role.unit_id)
                continue
        if _try_combat_item(turn, role, commands):
            used_controllers.add(role.unit_id)

    fired: set[int] = set()
    for role, tower in pairs:
        if role.unit_id in used_controllers or role.unit_id in commands:
            continue
        if distance(role.pos, tower.pos) > 1:
            step = _step_toward(turn, role, tower.pos, claimed)
            if step is not None:
                commands[role.unit_id] = move_command(step)
            continue

        # 火箭有 3 回合冷却，冷却期改控身边任意一座就绪的塔
        ready = [
            unit for unit in turn.weapons()
            if weapon_ready(turn, unit)
            and unit.unit_id not in fired
            and distance(role.pos, unit.pos) <= 1
        ]
        if not ready:
            if _try_combat_item(turn, role, commands, relaxed=True):
                used_controllers.add(role.unit_id)
            continue
        pick = next(
            (unit for unit in ready if unit.unit_id == tower.unit_id), ready[0],
        )
        targets = _attack_targets(turn, pick)
        if targets:
            commands[pick.unit_id] = attack_command(role.unit_id, *targets)
            fired.add(pick.unit_id)
            used_controllers.add(role.unit_id)
    return prompt, execute_cmd


def _assign_towers(
    turn: Turn, exclude: set[int] | None = None,
) -> list[tuple[Unit, Unit]]:
    banned = exclude or set()
    roles = [role for role in turn.controllable() if role.unit_id not in banned]
    towers = list(turn.weapons())
    if not roles or not towers:
        return []
    if len(roles) >= len(towers):
        candidates = (
            list(zip(assignment, towers))
            for assignment in permutations(roles, len(towers))
        )
    else:
        candidates = (
            list(zip(roles, assignment))
            for assignment in permutations(towers, len(roles))
        )

    def key(pairs: list[tuple[Unit, Unit]]) -> tuple:
        distances = [distance(role.pos, tower.pos) for role, tower in pairs]
        return (
            sum(distances),
            max(distances, default=0),
            tuple((role.unit_id, tower.unit_id) for role, tower in pairs),
        )

    return min(candidates, key=key)


def _try_combat_item(
    turn: Turn,
    role: Unit,
    commands: dict[int, dict[str, Any]],
    relaxed: bool = False,
) -> bool:
    station = turn.station()
    if station is None:
        return False
    hostiles = [
        robot for robot in turn.hostile_robots()
        if not robot.dizzy and distance(station.pos, robot.pos) <= 12
    ]
    min_pack = 2 if relaxed else 3
    if len(hostiles) < min_pack and not any(
        robot.kind in {"bossRobot", "largeRobot"} for robot in hostiles
    ):
        return False

    cluster = _best_cluster(hostiles)
    if cluster is None:
        return False
    center, count = cluster
    if count < (1 if relaxed else 2):
        return False

    bomb = role.find_item(BOMB)
    if bomb and count >= (2 if relaxed else 3):
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
        return []

    shots = tower.shot_count()
    if tower.kind == "railgun":
        target = max(candidates, key=lambda robot: _railgun_score(turn, tower, robot))
        return [target.pos]
    if tower.kind == "gatling":
        return _gatling_targets(turn, tower, candidates, shots)
    return _rocket_targets(turn, tower, candidates, shots)


def _threat_key(turn: Turn, robot: Robot) -> tuple:
    station = turn.station()
    base_dist = distance(station.pos, robot.pos) if station else 99
    attack = ROBOT_ATTACK.get(robot.kind, 5)
    immediate = 1 if base_dist <= 4 else 0
    finishable = 1 if robot.health <= 60 else 0
    return (
        immediate,
        attack,
        -base_dist,
        finishable,
        robot.kill_score,
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
    turn: Turn, tower: Unit, candidates: list[Robot], shots: int,
) -> list[Pos]:
    ordered = sorted(
        candidates,
        key=lambda robot: _threat_key(turn, robot),
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
            if wall.health < _wall_max_hp(wall) * 4 // 5
            and distance(role.pos, wall.pos) <= 1
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


def _wall_max_hp(wall: Unit) -> int:
    # level1/2/3 分别 1000/1500/2000
    return 500 * (min(max(wall.level, 1), 3) + 1)


def _should_sell(turn: Turn, role: Unit) -> bool:
    ores = role.ore_counts()
    if not ores:
        return False
    if role.backpack_full:
        return True
    # 只把铜/铁当卖金钱；石头默认留着建墙
    copper = ores.get(COPPER, 0)
    iron = ores.get(IRON, 0)
    # 敌方同款：铜铁积到就整包大额卖，阈值尽量低
    if copper > 0 or iron > 0:
        value = copper * turn.ore_price(COPPER) + iron * turn.ore_price(IRON)
        if dump_ore(turn, COPPER) or dump_ore(turn, IRON):
            return True
        if value >= 5:
            return True
        if turn.gold < WEAPON_BUILD_COST and value >= 1:
            return True
    value = 0
    for name, count in ores.items():
        if name == WALL_MATERIAL:
            continue
        if count <= 0:
            continue
        if hold_ore(turn, name) and not dump_ore(turn, name):
            continue
        if dump_ore(turn, name):
            return True
        value += count * turn.ore_price(name)
    if value >= 15:
        return True
    if len(turn.weapons()) < 3 and value >= max(5, WEAPON_BUILD_COST - turn.gold):
        return True
    if turn.gold < 100 and value >= 10:
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
    # 卖货顺序：涨价矿 > 铜 > 铁；石头仅在背包满且没有铜铁时才卖超额部分
    sell_name = None
    sell_num = 0
    best_key = (-1, -1, -1)
    for name in (COPPER, IRON, WALL_MATERIAL):
        count = ores.get(name, 0)
        if name == WALL_MATERIAL:
            if not role.backpack_full:
                continue
            # 满包时仍尽量留砌墙库存
            count = max(0, count - STONE_RESERVE)
            if ores.get(COPPER, 0) > 0 or ores.get(IRON, 0) > 0:
                continue
        if count <= 0:
            continue
        if hold_ore(turn, name) and not role.backpack_full:
            continue
        price = turn.ore_price(name)
        hot = 1 if dump_ore(turn, name) else 0
        # 铜优先于铁（同价/非涨价时）
        kind_rank = 2 if name == COPPER else 1 if name == IRON else 0
        key = (hot, price, kind_rank)
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
    if shop is None:
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
    """白天购货顺序：武器升级（优先）→ 基地保命 → 墙券/修复 → 其它。"""
    weapons = turn.weapons()
    walls = turn.walls()
    station = turn.station()

    def can_buy(name: str, stack: int = 1) -> tuple[str, int] | None:
        price = turn.shop_price(name)
        if budget >= price and role.item_count(name) < stack:
            return name, price
        return None

    # 1) 有塔则火力升级最优先（每座 L1→L2 / L2→L3）
    if any(tower.level == 1 for tower in weapons):
        item = can_buy(WEAPON_UPGRADE_1)
        if item:
            return item
    if any(tower.level == 2 for tower in weapons):
        item = can_buy(WEAPON_UPGRADE_2)
        if item:
            return item
    # 2) 基地保命升到 L2/L3
    if station is not None and station.level == 1:
        item = can_buy(STATION_UPGRADE_1)
        if item:
            return item
    if station is not None and station.level == 2:
        item = can_buy(STATION_UPGRADE_2)
        if item:
            return item
    # 3) 再升级围墙
    if any(wall.level == 1 for wall in walls):
        item = can_buy(WALL_UPGRADE_1)
        if item:
            return item
    if any(wall.level == 2 for wall in walls):
        item = can_buy(WALL_UPGRADE_2)
        if item:
            return item
    # 4) 残墙先补血，比重建便宜（阈值放宽，尽早买 WallFixer）
    if any(wall.health * 5 < _wall_max_hp(wall) * 4 for wall in walls):
        item = can_buy(WALL_FIXER, stack=2)
        if item:
            return item
    need = missing_ritual(turn, role)
    if need:
        item = can_buy(need[0])
        if item:
            return item
    if role.health < 150:
        item = can_buy(MEDICINE, stack=2)
        if item:
            return item
    # 落后终局或对方基地残血时，把金币直接转换为进攻压力。
    if _aggressive_endgame(turn):
        order = _summon_pick(turn, budget)
        if order is not None:
            item = can_buy(order, stack=1)
            if item:
                return item
    # 5) 炸弹 100 金在 3x3 内打 100 点，可以囤
    if turn.day_no >= 3:
        item = can_buy(BOMB, stack=2)
        if item:
            return item
    if turn.day_no >= 3:
        item = can_buy(DIZZY, stack=1)
        if item:
            return item
    # 6) 生存与升级都到位后，用余钱压对手：其基地被毁我方直接胜
    order = _summon_pick(turn, budget)
    if order is not None:
        item = can_buy(order, stack=1)
        if item:
            return item
    return None


def _aggressive_endgame(turn: Turn) -> bool:
    enemy_station = turn.enemy_station()
    return bool(
        (enemy_station is not None and enemy_station.health <= 1800)
        or (turn.day_no >= 7 and turn.score_gap < 0)
        or turn.day_no >= 9
    )


def _summon_pick(turn: Turn, budget: int) -> str | None:
    enemy_station = turn.enemy_station()
    enemy_weak = enemy_station is not None and enemy_station.health <= 1800
    behind_late = turn.day_no >= 7 and turn.score_gap < 0
    if (
        summon_budget_left() <= 0
        or (turn.day_no < 3 and not enemy_weak)
    ):
        return None
    station = turn.station()
    fully_upgraded = (
        station is not None
        and station.level >= 3
        and all(tower.level >= 2 for tower in turn.weapons())
    )
    if enemy_weak or behind_late or turn.day_no >= 9:
        reserve = 40
    else:
        reserve = 60 if fully_upgraded else 180
    spare = budget - reserve
    for name in SUMMON_ORDERS:
        if spare >= turn.shop_price(name):
            return name
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
    if target in bad_build_cells():
        return False
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
    avoid = set(failed_cells(role.unit_id)) | claimed
    for stand in _stand_cells(turn, role, target, claimed, inside_only):
        if stand == role.pos:
            return None
        if stand in avoid and stand != target:
            continue
        step = next_step(turn, role, stand, avoid)
        if step is None or step in avoid:
            continue
        claimed.add(step)
        return step
    step = next_step(turn, role, target, avoid)
    if step is not None and step not in avoid:
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
    banned = bad_build_cells()
    seeds = weapon_zone_seeds()
    anchor = threat_anchor(turn)
    cells = [
        pos for pos in _cells_at_distance(station.pos, 1)
        if turn.land(pos) and pos not in banned
    ]
    # 塔位朝机器人实际来向摆，射程才不浪费；已验证过的格子优先
    cells.sort(
        key=lambda pos: (
            0 if pos in seeds else 1,
            distance(pos, anchor),
            pos.x,
            pos.y,
        ),
    )
    return tuple(cells[:3])


def _base_is_northwest(turn: Turn) -> bool:
    """基地在地图左上（挑战者常见）还是右下。"""
    station = turn.station()
    if station is None:
        return True
    return station.pos.x < turn.width // 2


def _wall_face_cells(
    turn: Turn,
) -> tuple[list[Pos], list[Pos], list[Pos], list[Pos]]:
    """按基地 2x2 外扩一圈，拆成 front/top/bottom/rear 四面。

    左上基地：机器人从右往左打 → front=右墙；右下基地：从左往右 → front=左墙。
    """
    station = turn.station()
    if station is None:
        return [], [], [], []
    footprint = station_footprint(station.pos)
    xs = [pos.x for pos in footprint]
    ys = [pos.y for pos in footprint]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)

    bottom = [Pos(x, ymin - 2) for x in range(xmax + 2, xmin - 3, -1)]
    left = [Pos(xmin - 2, y) for y in range(ymin - 1, ymax + 2)]
    top = [Pos(x, ymax + 2) for x in range(xmin - 2, xmax + 3)]
    right = [Pos(xmax + 2, y) for y in range(ymax + 1, ymin - 2, -1)]

    if _base_is_northwest(turn):
        return right, top, bottom, left
    return left, top, bottom, right


def _half_toward_front(cells: list[Pos], northwest: bool) -> list[Pos]:
    """上下墙只砌靠来敌一侧的一半长度。"""
    if not cells:
        return []
    ordered = sorted(cells, key=lambda pos: (pos.x, pos.y))
    half = max(1, (len(ordered) + 1) // 2)
    if northwest:
        # 来敌在右：取右半段
        return ordered[-half:]
    # 来敌在左：取左半段
    return ordered[:half]


def _wall_build_plan(turn: Turn) -> tuple[list[Pos], list[Pos], list[Pos]]:
    """实际要砌的格子：来敌面全长 + 上下各一半（靠来敌侧）。"""
    front, top, bottom, _rear = _wall_face_cells(turn)
    nw = _base_is_northwest(turn)
    return front, _half_toward_front(top, nw), _half_toward_front(bottom, nw)


def _wall_ring(turn: Turn) -> tuple[Pos, ...]:
    """来敌面全长 + 上下半墙，不含背面与远端半段。"""
    front, top, bottom = _wall_build_plan(turn)
    seen: set[Pos] = set()
    out: list[Pos] = []
    for pos in (*front, *top, *bottom):
        if pos in seen or not turn.land(pos):
            continue
        seen.add(pos)
        out.append(pos)
    return tuple(out)


def _ring_progress(turn: Turn) -> tuple[tuple[Pos, ...], float]:
    ring = _wall_ring(turn)
    if not ring:
        return (), 0.0
    legal = [pos for pos in ring if pos not in bad_build_cells()]
    if not legal:
        return ring, 0.0
    standing = {unit.pos for unit in turn.walls()}
    built = sum(1 for pos in legal if pos in standing)
    return ring, built / len(legal)


def _gate_cell(turn: Turn) -> Pos | None:
    """背面永久开口作出入口，不封第四面。"""
    return None


def _wall_order(turn: Turn, seal: bool = False) -> tuple[Pos, ...]:
    """建造顺序：来敌面全长 → 上半墙 → 下半墙；完成后才去升级武器。"""
    del seal
    station = turn.station()
    if station is None:
        return ()
    front, top, bottom = _wall_build_plan(turn)
    face_rank: dict[Pos, int] = {}
    for pos in front:
        face_rank[pos] = 0
    for pos in top:
        face_rank.setdefault(pos, 1)
    for pos in bottom:
        face_rank.setdefault(pos, 2)

    ring = _wall_ring(turn)
    seeds = wall_zone_seeds()
    frontier = {
        pos for seed in seeds for pos in _neighbours(seed)
        if turn.land(pos) and pos not in seeds
    }
    banned = bad_build_cells()
    anchor = threat_anchor(turn)
    footprint = station_footprint(station.pos)
    core = set(face_rank)

    filtered: list[Pos] = []
    for pos in {*ring, *frontier}:
        if pos in banned:
            continue
        # 只收核心计划内格子，或紧贴核心、仍靠来敌侧的邻格（黄区试探）
        if pos in core:
            filtered.append(pos)
            continue
        if pos not in frontier:
            continue
        if any(distance(pos, cell) <= 1 for cell in core):
            filtered.append(pos)

    filtered.sort(
        key=lambda pos: (
            face_rank.get(pos, 3),
            0 if pos in ring else 1,
            distance(pos, anchor),
            _footprint_distance(pos, footprint),
            pos.x,
            pos.y,
        ),
    )
    return tuple(dict.fromkeys(filtered))[:20]


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
