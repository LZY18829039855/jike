from __future__ import annotations

from typing import Any

from .grid import next_step
from .evolve import (
    pick_task,
    should_prioritize,
    solve as solve_evolve_task,
)
from .intel import (
    MEM,
    FIXED_TREASURE_DAY,
    bad_build_cells,
    dump_ore,
    failed_cells,
    hold_idle,
    hold_ore,
    is_idle_hold,
    mine_rank,
    missing_ritual,
    need_ritual_prep,
    note_purchase,
    note_summon_used,
    observe,
    oscillation_bans,
    purchase_busy,
    remember_commands,
    remember_task_accept,
    should_abandon_task,
    summon_budget_left,
    threat_anchor,
    wall_zone_seeds,
    weapon_ready,
    weapon_zone_seeds,
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
    WORKER,
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
STONE_TARGET = 20  # 砌墙库存上限，够用即可
STONE_RESERVE = 20
STONE_BATCH = 10
MINE_BATCH = 8
WEAPON_UPGRADE_RESERVE = 100
# 三座火箭冷却 3 回合：一人轮流开火即可，其余专职采矿
NIGHT_ROCKET_GUNNERS = 1
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
    _dedupe_role_commands(turn, commands)
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

    workers = [role for role in turn.workers() if role.unit_id not in commands]
    mason_id = _pick_mason_id(turn, free_walls)
    # 临夜只需 1 人贴塔（与夜间单炮手一致），另一人继续挖矿
    night_gunner_id = _pick_day_gunner_id(turn, workers)

    for role in workers:
        budget = _worker_day(
            turn,
            role,
            sites,
            free_towers,
            free_walls,
            claimed,
            commands,
            budget,
            job="mason" if role.unit_id == mason_id else "miner",
            night_gunner=(role.unit_id == night_gunner_id),
        )
        if role.unit_id not in commands:
            _fill_idle_mine(
                turn,
                role,
                claimed,
                commands,
                adjacent_only=_holding_line(turn, role),
            )
    return prompt, execute_cmd


def _team_stone(turn: Turn) -> int:
    return sum(role.item_count(WALL_MATERIAL) for role in turn.controllable())


def _walls_safe(turn: Turn, walls_missing: list[Pos]) -> bool:
    """迎敌面墙基本齐、且不临夜缺墙时，视为围墙无风险。"""
    if not walls_missing:
        return True
    front = _front_wall_cells(turn)
    front_missing = [pos for pos in walls_missing if pos in front]
    if turn.near_night and front_missing:
        return False
    if turn.day_no <= 1 and front_missing:
        return False
    return len(front_missing) == 0


def _pick_mason_id(turn: Turn, walls_missing: list[Pos]) -> int | None:
    """指定一名石匠：缺墙或石头未满 20 时采石砌墙；围墙无风险则返回 None（全员挖铜铁）。"""
    workers = list(turn.workers())
    if not workers:
        return None
    if _walls_safe(turn, walls_missing) and _team_stone(turn) >= STONE_TARGET:
        return None
    if _walls_safe(turn, walls_missing) and not walls_missing:
        return None
    stone_mines = turn.stone_mines()

    def key(role: Unit) -> tuple:
        stone = role.item_count(WALL_MATERIAL)
        near_mine = min(
            (distance(role.pos, mine) for mine in stone_mines),
            default=99,
        )
        near_wall = min(
            (distance(role.pos, pos) for pos in walls_missing),
            default=99,
        ) if walls_missing else 99
        return (-stone, near_mine + near_wall, role.unit_id)

    return min(workers, key=key).unit_id


def _pick_day_gunner_id(turn: Turn, workers: list[Unit]) -> int | None:
    """临夜回防：只派离炮最近的 1 名工人。"""
    if not turn.near_night or not turn.weapons() or not workers:
        return None
    towers = turn.weapons()
    return min(
        workers,
        key=lambda role: (
            min(distance(role.pos, tower.pos) for tower in towers),
            role.unit_id,
        ),
    ).unit_id


def _holding_line(turn: Turn, role: Unit) -> bool:
    """入夜前已经站到塔边的角色不要再被支使走开去采矿。"""
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
    *,
    job: str = "miner",
    night_gunner: bool = False,
) -> int:
    # 1) 紧急回血
    if role.health <= 80:
        med = role.find_item(MEDICINE)
        if med:
            commands[role.unit_id] = use_command(med)
            return budget

    # 2) 手里已有升级券优先用掉（尤其是武器升级券）
    if _try_use_upgrade(turn, role, commands, claimed):
        return budget

    # 2.5) 手里的召唤令立刻用掉，作用于对手下个夜晚
    if _try_use_summon(role, commands):
        return budget

    # 3) 天亮后拆掉夜里封上的缺口，否则全队出不了门
    if not turn.near_night and _open_gate(turn, role, claimed, commands):
        return budget

    # 4) 夜间临近：仅指定 1 名炮手回防，另一人继续挖矿
    if turn.near_night and turn.weapons() and night_gunner:
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

    # 5.5) 三炮齐后优先升到二级，不为侧墙分散启动资金
    fire_ready = _firepower_ready(turn)
    if len(turn.weapons()) >= 3 and not fire_ready:
        if _prefer_weapon_upgrade(turn, role, budget):
            spent = _buy_weapon_upgrade(turn, role, claimed, commands, budget)
            if spent is not None:
                return budget - spent
        # 升级未完成：只砌迎敌面，上下侧暂缓
        front = _front_wall_cells(turn)
        walls_missing = [pos for pos in walls_missing if pos in front]

    # 6) 石匠：砌墙 + 补石头到 20；矿工不碰石头
    if job == "mason" and walls_missing and _wall_work(
        turn, role, walls_missing, claimed, commands,
    ):
        return budget
    if job == "mason" and _team_stone(turn) < STONE_TARGET and (
        walls_missing or turn.day_no <= 2
    ):
        if _mine_kind(turn, role, WALL_MATERIAL, claimed, commands):
            return budget

    # 6.5) 火力已达标后再追更高等级券
    if fire_ready and _prefer_weapon_upgrade(turn, role, budget):
        spent = _buy_weapon_upgrade(turn, role, claimed, commands, budget)
        if spent is not None:
            return budget - spent

    # 7) 矿价变高 / 满包 / 急需金币时再卖
    if _should_sell(turn, role) and _sell_or_walk(turn, role, claimed, commands):
        return budget

    # 8) 买武器升级券等；买完由步骤 2 在后续回合使用
    spent = _buy_or_walk(turn, role, claimed, commands, budget)
    if spent is not None:
        return budget - spent

    # 9) 经济采集：围墙无风险时铜/铁优先；石匠在仍需墙时才掺石头
    keep_stone = (
        job == "mason"
        and not _walls_safe(turn, walls_missing)
        and bool(walls_missing)
    )
    if _mine_economy(turn, role, claimed, commands, keep_stone=keep_stone):
        return budget
    return budget


def _firepower_ready(turn: Turn) -> bool:
    """三门火箭均达到二级，才算核心火力成形。"""
    weapons = turn.weapons()
    return len(weapons) >= 3 and all(tower.level >= 2 for tower in weapons)


def _front_wall_cells(turn: Turn) -> set[Pos]:
    front, _top, _bottom = _wall_build_plan(turn)
    return set(front)


def _prefer_weapon_upgrade(turn: Turn, role: Unit, budget: int) -> bool:
    """有塔且金币够时，去买武器升级券。"""
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

    # —— 写死宝藏：第8天白天直奔 (3,3) 召唤（高于一切） ——
    if (
        not MEM.treasure.done
        and turn.day_no >= FIXED_TREASURE_DAY
        and turn.is_day
        and _hunt_treasure(turn, role, claimed, commands)
    ):
        return "", ""

    # 第6-7天仍缺祭品：打断当前事务去买齐，确保第8天能开
    if (
        need_ritual_prep(turn, role)
        and turn.day_no >= 6
        and turn.day_no < FIXED_TREASURE_DAY
        and not turn.near_night
        and _buy_ritual_or_walk(turn, role, claimed, commands, turn.gold) is not None
    ):
        return "", ""

    # —— 开拓者金币主线：刷自进化任务点 ——
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

    # 第8天前空档优先买祭品（不打断正在进行的 phaseTask）
    if (
        need_ritual_prep(turn, role)
        and turn.day_no < FIXED_TREASURE_DAY
        and not turn.near_night
        and _buy_ritual_or_walk(turn, role, claimed, commands, turn.gold) is not None
    ):
        return "", ""

    # 刷任务：有就绪就接；白天冷却贴点时只贴身采，不走开错过任务
    if should_prioritize(turn):
        held = _accept_or_approach(
            turn, role, claimed, commands, hold_idle=True,
        )
        if role.unit_id in commands:
            return "", ""
        if held:
            _fill_idle_mine(
                turn, role, claimed, commands, adjacent_only=True,
            )
            return "", ""

    # 近夜或不在任务点：才去控炮
    if turn.weapons() and (turn.near_night or not _near_task_point(turn, role)):
        _man_tower(turn, role, claimed, commands, prefer_inside=True)
    elif towers_missing and not _near_task_point(turn, role):
        _step_or_idle(turn, role, towers_missing[0], claimed, commands)

    # 白天空档：开拓者也采矿（任务/宝藏/控炮都轮空时）
    if role.unit_id not in commands:
        at_tower = any(
            distance(role.pos, tower.pos) <= 1 for tower in turn.weapons()
        )
        _fill_idle_mine(
            turn,
            role,
            claimed,
            commands,
            adjacent_only=turn.near_night and at_tower,
        )
    return "", ""


def _near_task_point(turn: Turn, role: Unit) -> bool:
    points = list(turn.our_task_points()) or [
        item.pos for item in turn.tasks if item.valid
    ]
    if MEM.task_pos is not None:
        points = list(points) + [MEM.task_pos]
    if MEM.pending_task_pos is not None:
        points = list(points) + [MEM.pending_task_pos]
    return any(distance(role.pos, pos) <= 1 for pos in points)


def _accept_or_approach(
    turn: Turn,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    *,
    hold_idle: bool = True,
) -> bool:
    """有就绪任务才接；冷却中白天贴点待命，夜里不空走任务点。"""
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

    if not hold_idle:
        return False

    points = list(turn.our_task_points()) or [item.pos for item in turn.tasks if item.valid]
    if not points:
        return False
    # 已贴任意任务点：原地待命，不发 move
    if any(distance(role.pos, pos) <= 1 for pos in points):
        return True
    # 选一个固定最近点（按坐标打破平局），避免两点间来回翻
    nearest = min(points, key=lambda pos: (distance(role.pos, pos), pos.x, pos.y))
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


def _fill_idle_mine(
    turn: Turn,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    *,
    adjacent_only: bool = False,
    stay_near: Pos | None = None,
    max_dist: int = 12,
) -> bool:
    """无指令空档填采矿/卖货，避免整回合发呆。开拓者不能 collect。"""
    if role.unit_id in commands:
        return False
    # 任务书：collect 仅工人可用；开拓者空档只卖货，绝不采矿
    if role.kind != WORKER:
        if role.ore_counts():
            return _sell_or_walk(turn, role, claimed, commands)
        return False
    if adjacent_only or is_idle_hold(role.unit_id):
        if role.backpack_full:
            shop = turn.shop_pos()
            if shop is not None and distance(role.pos, shop) <= 1:
                return _sell_or_walk(turn, role, claimed, commands)
            return False
        for kind in mine_rank(turn, keep_stone=False):
            mine = _adjacent_mine(turn, role, kind)
            if mine is None or mine in claimed:
                continue
            commands[role.unit_id] = collect_command(mine)
            claimed.add(mine)
            return True
        return False
    return _mine_economy(
        turn,
        role,
        claimed,
        commands,
        keep_stone=False,
        stay_near=stay_near,
        max_dist=max_dist,
    )


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
    # 0/1 格都合法：原地待命，不要为了踩中心格来回挪
    if dist <= 1:
        return False
    step = _step_toward(turn, role, anchor, claimed)
    if step is not None:
        commands[role.unit_id] = move_command(step)
        return True
    return False


def _maybe_treasure_prompt(turn: Turn) -> str:
    return ""


def _hunt_treasure(
    turn: Turn,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> bool:
    """写死流程：仅第 8 天白天前往固定坐标召唤；此前只负责买祭品。"""
    if MEM.treasure.done:
        return False
    need = missing_ritual(turn, role)
    if turn.day_no < FIXED_TREASURE_DAY:
        if need:
            return _buy_named_or_walk(turn, role, need[0], claimed, commands, turn.gold)
        return False
    if not turn.is_day:
        return False
    if need:
        return _buy_named_or_walk(turn, role, need[0], claimed, commands, turn.gold)
    if not treasure_ready(turn):
        return False
    target = MEM.treasure.pos
    if target is None:
        return False
    if role.pos == target:
        # 站在祭坛格上先挪到邻格，下回合再召唤
        for stand in _neighbours(target):
            if stand in claimed or not turn.land(stand):
                continue
            if stand in turn.blocked(role):
                continue
            step = _step_toward(turn, role, stand, claimed)
            if step is not None:
                commands[role.unit_id] = move_command(step)
                return True
        return False
    if distance(role.pos, target) <= 1:
        commands[role.unit_id] = summon_treasure_command(target, list(MEM.treasure.items))
        return True
    step = _step_toward(turn, role, target, claimed)
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
    if purchase_busy(name, role.unit_id, turn.round_no):
        return False
    price = turn.shop_price(name)
    if budget < price:
        return False
    if distance(role.pos, shop) <= 1:
        commands[role.unit_id] = buy_command(name, 1)
        note_purchase(name, role.unit_id, turn.round_no)
        return True
    step = _step_toward(turn, role, shop, claimed)
    if step is not None:
        commands[role.unit_id] = move_command(step)
        note_purchase(name, role.unit_id, turn.round_no)
        return True
    if oscillation_bans(role.unit_id):
        hold_idle(role.unit_id)
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
        # 夜里：有就绪任务才接/走近；无任务不当贴点抖腿，留给下面当炮手
        if _accept_or_approach(
            turn, pioneer, claimed, commands, hold_idle=False,
        ):
            used_controllers.add(pioneer.unit_id)
    else:
        prompt = _maybe_treasure_prompt(turn)

    # 三火箭 CD=3：只需 1 人轮流控三炮；另外两人立刻采矿，不先贴塔
    gunner = _pick_night_gunner(turn, used_controllers)
    if gunner is not None and gunner.unit_id not in commands:
        if _try_use_upgrade(turn, gunner, commands, claimed):
            used_controllers.add(gunner.unit_id)
        elif gunner.health <= 100:
            med = gunner.find_item(MEDICINE)
            if med:
                commands[gunner.unit_id] = use_command(med)
                used_controllers.add(gunner.unit_id)
        elif _try_combat_item(turn, gunner, commands):
            used_controllers.add(gunner.unit_id)
        elif _solo_rocket_fire(turn, gunner, claimed, commands):
            # attack 挂在武器 ID 上，需标记炮手本回合已占用
            used_controllers.add(gunner.unit_id)

    station = turn.station()
    anchor = station.pos if station is not None else None
    gunner_id = gunner.unit_id if gunner is not None else None
    for role in turn.controllable():
        if role.unit_id in commands or role.unit_id in used_controllers:
            continue
        # 唯一炮手：冷却空窗只贴身采；其余角色直接挖铜/铁
        if role.unit_id == gunner_id:
            at_tower = any(
                distance(role.pos, tower.pos) <= 1 for tower in turn.weapons()
            )
            if at_tower:
                _fill_idle_mine(
                    turn, role, claimed, commands, adjacent_only=True,
                )
            else:
                _solo_rocket_fire(turn, role, claimed, commands)
            continue
        _fill_idle_mine(
            turn,
            role,
            claimed,
            commands,
            stay_near=anchor,
            max_dist=14 if turn.weapons() else 99,
        )
    return prompt, execute_cmd


def _pick_night_gunner(
    turn: Turn, exclude: set[int],
) -> Unit | None:
    """选唯一炮手：优先已贴就绪炮，否则离炮群最近的角色。"""
    roles = [
        role for role in turn.controllable() if role.unit_id not in exclude
    ]
    towers = list(turn.weapons())
    if not roles or not towers:
        return None
    cap = NIGHT_ROCKET_GUNNERS
    if cap <= 0:
        return None

    def score(role: Unit) -> tuple:
        ready_here = sum(
            1 for tower in towers
            if weapon_ready(turn, tower) and distance(role.pos, tower.pos) <= 1
        )
        near_any = sum(1 for tower in towers if distance(role.pos, tower.pos) <= 1)
        nearest = min(distance(role.pos, tower.pos) for tower in towers)
        # 工人优先当炮手，开拓者尽量留给任务/采矿
        pioneer_penalty = 1 if role.kind == "pioneer" else 0
        return (-ready_here, -near_any, nearest, pioneer_penalty, role.unit_id)

    return min(roles, key=score)


def _solo_rocket_fire(
    turn: Turn,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> bool:
    """一人轮流打三座火箭：贴就绪炮就开火，否则走向最近就绪/即将就绪的炮。"""
    if role.unit_id in commands:
        return False
    towers = list(turn.weapons())
    if not towers:
        return False

    ready_here = [
        tower for tower in towers
        if weapon_ready(turn, tower) and distance(role.pos, tower.pos) <= 1
    ]
    if ready_here:
        pick = max(
            ready_here,
            key=lambda tower: (
                len(_attack_targets(turn, tower)),
                tower.level,
                -tower.unit_id,
            ),
        )
        targets = _attack_targets(turn, pick)
        if targets:
            commands[pick.unit_id] = attack_command(role.unit_id, *targets)
            return True
        if _try_combat_item(turn, role, commands, relaxed=True):
            return True

    # 走向最近的就绪炮；都在冷却则走向冷却最短的
    def approach_key(tower: Unit) -> tuple:
        ready = 0 if weapon_ready(turn, tower) else 1
        return (
            ready,
            tower.cooldown,
            distance(role.pos, tower.pos),
            tower.pos.x,
            tower.pos.y,
        )

    target = min(towers, key=approach_key)
    if distance(role.pos, target.pos) <= 1:
        if _try_combat_item(turn, role, commands, relaxed=True):
            return True
        return False
    claimed.add(target.pos)
    step = _step_toward(turn, role, target.pos, claimed)
    if step is not None:
        commands[role.unit_id] = move_command(step)
        return True
    return False


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
    turn: Turn,
    role: Unit,
    commands: dict[int, dict[str, Any]],
    claimed: set[Pos] | None = None,
) -> bool:
    claimed = claimed if claimed is not None else set()
    # 武器升级：两人分头去不同的塔，寻路互斥
    for voucher, need_level in (
        (WEAPON_UPGRADE_1, 1),
        (WEAPON_UPGRADE_2, 2),
    ):
        item = role.find_item(voucher)
        if not item:
            continue
        tower = _pick_upgrade_tower(turn, role, need_level, claimed)
        if tower is None:
            continue
        claimed.add(tower.pos)
        if distance(role.pos, tower.pos) <= 1:
            commands[role.unit_id] = use_command(item, tower.pos)
            return True
        step = _step_toward(turn, role, tower.pos, claimed)
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
            step = _step_toward(turn, role, station.pos, claimed, inside_only=True)
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
            step = _step_toward(turn, role, target.pos, claimed)
            if step is not None:
                claimed.add(target.pos)
                commands[role.unit_id] = move_command(step)
                return True
    return False


def _pick_upgrade_tower(
    turn: Turn, role: Unit, need_level: int, claimed: set[Pos],
):
    towers = [tower for tower in turn.weapons() if tower.level == need_level]
    if not towers:
        return None
    adjacent = [
        tower for tower in towers
        if distance(role.pos, tower.pos) <= 1 and tower.pos not in claimed
    ]
    if adjacent:
        return min(adjacent, key=lambda unit: (unit.pos.x, unit.pos.y))
    free = [tower for tower in towers if tower.pos not in claimed]
    if not free:
        return None
    return min(
        free,
        key=lambda unit: (distance(role.pos, unit.pos), unit.pos.x, unit.pos.y),
    )


def _wall_max_hp(wall: Unit) -> int:
    # level1/2/3 分别 1000/1500/2000
    return 500 * (min(max(wall.level, 1), 3) + 1)


def _should_sell(turn: Turn, role: Unit) -> bool:
    ores = role.ore_counts()
    if not ores:
        return False
    if role.backpack_full:
        return True
    # 采卖计划未完成：先采满批次再卖
    plan = MEM.mine_quota.get(role.unit_id)
    if plan is not None:
        kind, target = plan
        if role.item_count(kind) < target and not role.backpack_full:
            return False
    copper = ores.get(COPPER, 0)
    iron = ores.get(IRON, 0)
    # 矿价变高（新闻短缺/当前价高于默认）优先出货
    if copper > 0 and dump_ore(turn, COPPER):
        return True
    if iron > 0 and dump_ore(turn, IRON):
        return True
    # 急需金币建炮时才提前卖
    value = copper * turn.ore_price(COPPER) + iron * turn.ore_price(IRON)
    if len(turn.weapons()) < 3 and value >= max(1, WEAPON_BUILD_COST - turn.gold):
        return True
    if turn.gold < 25 and value >= 15:
        return True
    # 背包压力大时出货，平时囤着等涨价
    if len(role.backpack) >= 30 and value >= 10:
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
    if purchase_busy(name, role.unit_id, turn.round_no):
        return None
    if distance(role.pos, shop) <= 1:
        commands[role.unit_id] = buy_command(name, 1)
        note_purchase(name, role.unit_id, turn.round_no)
        return price
    step = _step_toward(turn, role, shop, claimed)
    if step is not None:
        commands[role.unit_id] = move_command(step)
        note_purchase(name, role.unit_id, turn.round_no)
        return 0
    if oscillation_bans(role.unit_id):
        hold_idle(role.unit_id)
        return 0
    return None


def _wanted_purchase(
    turn: Turn, role: Unit, budget: int,
) -> tuple[str, int] | None:
    """白天购货顺序：武器升级（优先）→ 基地保命 → 墙券/修复 → 其它。"""
    weapons = turn.weapons()
    walls = turn.walls()
    station = turn.station()
    # 三炮未升满二级时预留升级金，避免被墙券/炸弹花光
    reserve = 0
    if len(weapons) >= 3 and not _firepower_ready(turn):
        reserve = min(WEAPON_UPGRADE_RESERVE, turn.shop_price(WEAPON_UPGRADE_1))

    def can_buy(name: str, stack: int = 1, *, core: bool = False) -> tuple[str, int] | None:
        if purchase_busy(name, role.unit_id, turn.round_no):
            return None
        price = turn.shop_price(name)
        pool = budget if core else budget - reserve
        if pool >= price and role.item_count(name) < stack:
            return name, price
        return None

    # 1) 有塔则火力升级最优先（每座 L1→L2 / L2→L3）
    if any(tower.level == 1 for tower in weapons):
        item = can_buy(WEAPON_UPGRADE_1, core=True)
        if item:
            return item
    if any(tower.level == 2 for tower in weapons):
        item = can_buy(WEAPON_UPGRADE_2, core=True)
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
    # 3) 火力成形后再升级围墙
    if _firepower_ready(turn) or len(weapons) < 3:
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
    if turn.day_no >= 3 and _firepower_ready(turn):
        item = can_buy(BOMB, stack=2)
        if item:
            return item
    if turn.day_no >= 3 and _firepower_ready(turn):
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
    free = [
        tower for tower in towers
        if tower.pos not in occupied_stands and tower.pos not in claimed
    ]
    if not free:
        return False
    tower = min(free, key=lambda unit: (distance(role.pos, unit.pos), unit.pos.x, unit.pos.y))
    if distance(role.pos, tower.pos) <= 1:
        return False
    claimed.add(tower.pos)
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
    *,
    stay_near: Pos | None = None,
    max_dist: int = 99,
) -> bool:
    # 开拓者禁止 collect，避免 COMMAND_ERROR 刷爆异常次数
    if role.kind != WORKER:
        if role.ore_counts():
            return _sell_or_walk(turn, role, claimed, commands)
        return False
    if role.backpack_full:
        MEM.mine_quota.pop(role.unit_id, None)
        return _sell_or_walk(turn, role, claimed, commands)

    plan = MEM.mine_quota.get(role.unit_id)
    if plan is not None:
        kind, target = plan
        have = role.item_count(kind)
        if have >= target:
            MEM.mine_quota.pop(role.unit_id, None)
            return _sell_or_walk(turn, role, claimed, commands)
        if _mine_kind(
            turn, role, kind, claimed, commands,
            stay_near=stay_near, max_dist=max_dist,
        ):
            return True
        # 采不到了：有货就去卖，清空计划
        if have >= 3:
            MEM.mine_quota.pop(role.unit_id, None)
            return _sell_or_walk(turn, role, claimed, commands)
        MEM.mine_quota.pop(role.unit_id, None)

    ranked = mine_rank(turn, keep_stone)
    for kind in ranked:
        if kind in {COPPER, IRON}:
            MEM.mine_quota[role.unit_id] = (kind, MINE_BATCH)
            if _mine_kind(
                turn, role, kind, claimed, commands,
                stay_near=stay_near, max_dist=max_dist,
            ):
                return True
            MEM.mine_quota.pop(role.unit_id, None)
            continue
        if _mine_kind(
            turn, role, kind, claimed, commands,
            stay_near=stay_near, max_dist=max_dist,
        ):
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
    *,
    stay_near: Pos | None = None,
    max_dist: int = 99,
) -> bool:
    if role.kind != WORKER:
        return False
    if role.backpack_full:
        return False
    mines = sorted(
        (
            pos for pos in turn.mines(kind)
            if pos not in claimed
            and (stay_near is None or distance(pos, stay_near) <= max_dist)
        ),
        key=lambda pos: (distance(role.pos, pos), pos.x, pos.y),
    )
    for mine in mines:
        if role.pos != mine and distance(role.pos, mine) <= 1:
            commands[role.unit_id] = collect_command(mine)
            claimed.add(mine)
            return True
        claimed.add(mine)
        step = _step_toward(turn, role, mine, claimed)
        if step is not None:
            commands[role.unit_id] = move_command(step)
            return True
        # 堵路/防抖：本回合待命，别换矿点来回抖
        if oscillation_bans(role.unit_id):
            hold_idle(role.unit_id)
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
    claimed.add(target)
    step = _step_toward(turn, role, target, claimed)
    if step is not None:
        commands[role.unit_id] = move_command(step)
        return True
    if oscillation_bans(role.unit_id):
        hold_idle(role.unit_id)
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
    avoid = set(failed_cells(role.unit_id)) | set(oscillation_bans(role.unit_id)) | claimed
    for stand in _stand_cells(turn, role, target, claimed, inside_only):
        if stand == role.pos:
            return None
        if stand in avoid and stand != target:
            continue
        step = next_step(turn, role, stand, avoid)
        if step is None or step in avoid:
            continue
        claimed.add(step)
        if stand != role.pos:
            claimed.add(stand)
        return step
    step = next_step(turn, role, target, avoid)
    if step is not None and step not in avoid:
        claimed.add(step)
        return step
    # 防抖把两极都禁了：尝试仅禁失败格再走一步，仍不行则待命
    soft = set(failed_cells(role.unit_id)) | claimed
    step = next_step(turn, role, target, soft)
    if step is not None and step not in soft and step not in oscillation_bans(role.unit_id):
        claimed.add(step)
        return step
    return None


def _command_goal_key(command: dict[str, Any]) -> tuple[Any, ...] | None:
    """(动作, 目的地/物品) —— 用于全局去重，避免两角色做同一件事。"""
    action = command.get("action")
    if not action:
        return None
    # 交卷/接任务/开宝藏不参与去重
    if action in {"submitAnswer", "acceptTask", "summonTreasure", "attack"}:
        return None
    targets = command.get("targetPos") or ()
    name = command.get("name") or ""
    if action in {"move", "build", "collect", "remove"} and targets:
        raw = targets[0]
        return (action, int(raw["x"]), int(raw["y"]), name)
    if action == "use" and targets:
        raw = targets[0]
        return (action, name, int(raw["x"]), int(raw["y"]))
    if action == "use":
        return (action, name)
    if action in {"buy", "sell"}:
        return (action, name, int(command.get("num") or 1))
    return (action, name)


def _unit_by_id(turn: Turn, unit_id: int) -> Unit | None:
    for role in turn.controllable():
        if role.unit_id == unit_id:
            return role
    return None


def _dedupe_role_commands(
    turn: Turn, commands: dict[int, dict[str, Any]],
) -> None:
    """同一目的动作+目的地只留一个角色；优先保留离目标更近者。"""
    buckets: dict[tuple[Any, ...], list[int]] = {}
    for unit_id, command in commands.items():
        key = _command_goal_key(command)
        if key is None:
            continue
        buckets.setdefault(key, []).append(unit_id)
    for key, unit_ids in buckets.items():
        if len(unit_ids) < 2:
            continue
        action = key[0]
        if action in {"move", "build", "collect", "remove"} and len(key) >= 3:
            try:
                goal = Pos(int(key[1]), int(key[2]))
            except (TypeError, ValueError):
                goal = None
            if goal is not None:
                def _dist(uid: int, g: Pos = goal) -> int:
                    unit = _unit_by_id(turn, uid)
                    return distance(unit.pos, g) if unit else 10**9

                unit_ids.sort(key=_dist)
            else:
                unit_ids.sort()
        elif action == "use" and len(key) >= 4:
            try:
                goal = Pos(int(key[2]), int(key[3]))
            except (TypeError, ValueError):
                goal = None
            if goal is not None:
                def _dist_use(uid: int, g: Pos = goal) -> int:
                    unit = _unit_by_id(turn, uid)
                    return distance(unit.pos, g) if unit else 10**9

                unit_ids.sort(key=_dist_use)
            else:
                unit_ids.sort()
        else:
            unit_ids.sort()
        for uid in unit_ids[1:]:
            commands.pop(uid, None)


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


def _side_trim_count(turn: Turn) -> int:
    """按全局战况缩短上下侧：早中期/低压多缩 2 格，受压少缩 1 格。"""
    robots = len(turn.hostile_robots())
    weapons = turn.weapons()
    l2 = sum(1 for tower in weapons if tower.level >= 2)
    if robots >= 6 or (turn.near_night and robots >= 3):
        return 1
    if len(weapons) < 3 or l2 < 2:
        return 2
    if turn.round_no < 100 and robots <= 2:
        return 2
    return 1


def _trim_side_walls(cells: list[Pos], northwest: bool, trim: int) -> list[Pos]:
    """从远离敌一侧再削掉 trim 格，至少保留 1 格。"""
    if not cells:
        return []
    if trim <= 0:
        return list(cells)
    ordered = sorted(cells, key=lambda pos: (pos.x, pos.y))
    keep = max(1, len(ordered) - trim)
    if northwest:
        # 半墙靠右；削左端（远端）
        return ordered[-keep:]
    return ordered[:keep]


def _wall_build_plan(turn: Turn) -> tuple[list[Pos], list[Pos], list[Pos]]:
    """实际要砌的格子：来敌面全长 + 上下半墙（再按战况缩短 1～2 格）。"""
    front, top, bottom, _rear = _wall_face_cells(turn)
    nw = _base_is_northwest(turn)
    trim = _side_trim_count(turn)
    top_h = _trim_side_walls(_half_toward_front(top, nw), nw, trim)
    bottom_h = _trim_side_walls(_half_toward_front(bottom, nw), nw, trim)
    return front, top_h, bottom_h


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
