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
    FIXED_RITUAL_BUY_FROM_DAY,
    FIXED_IRON_STOCKPILE_UNTIL,
    bad_build_cells,
    basic_wall_cells,
    dump_ore,
    failed_cells,
    hold_idle,
    hold_ore,
    is_idle_hold,
    mine_rank,
    missing_ritual,
    need_ritual_prep,
    note_basic_wall,
    note_purchase,
    note_summon_used,
    observe,
    oscillation_bans,
    prefer_day1_copper,
    purchase_busy,
    remember_commands,
    remember_task_accept,
    should_abandon_task,
    summon_budget_left,
    threat_anchor,
    wall_zone_seeds,
    weapon_ready,
    treasure_ready,
    use_failures,
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
STONE_TARGET = 5  # 全队常备约 5 块石头，墙被摧毁后立即补建
DAY1_STONE_GOAL = 16  # 第一天先囤约 16 石再统一砌墙
STONE_RESERVE = 5
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
    _refill_idle_roles(turn, commands)
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
    _lock_basic_wall(turn)
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
    # 基本围墙缺口永远排在白天施工最前，确保每天优先保全
    basic_gaps = _basic_wall_gaps(turn)
    walls_missing = list(dict.fromkeys(
        [*basic_gaps, *[pos for pos in order if pos not in standing_walls]],
    ))
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
    mason_ids = _pick_mason_ids(turn, free_walls)
    wall_lanes = _day1_wall_lanes(turn, workers, free_walls)
    # 临夜只需 1 人贴塔（与夜间单炮手一致），另一人继续挖矿
    night_gunner_id = _pick_day_gunner_id(turn, workers)

    for role in workers:
        worker_walls = wall_lanes.get(role.unit_id, free_walls)
        budget = _worker_day(
            turn,
            role,
            sites,
            free_towers,
            worker_walls,
            claimed,
            commands,
            budget,
            job="mason" if role.unit_id in mason_ids else "miner",
            night_gunner=(role.unit_id == night_gunner_id),
        )
        if role.unit_id not in commands:
            if _rebuild_destroyed_walls(turn, role, claimed, commands):
                continue
            if _need_early_walls(turn):
                # Day1 墙未齐：囤石未满才采石，够了只砌墙
                if _day1_stockpiling_stone(turn):
                    _mine_kind(turn, role, WALL_MATERIAL, claimed, commands)
                elif worker_walls and _wall_work(
                    turn, role, worker_walls, claimed, commands, hunt_stone=False,
                ):
                    pass
                elif (
                    worker_walls is not free_walls
                    and free_walls
                    and _wall_work(
                        turn, role, free_walls, claimed, commands, hunt_stone=False,
                    )
                ):
                    # 本区剩余墙位暂时不可达时，立即跨区支援，不能困在墙内空转。
                    pass
                elif (
                    _day1_stone_accounted(turn) < DAY1_STONE_GOAL
                    and role.item_count(WALL_MATERIAL) == 0
                ):
                    _mine_kind(turn, role, WALL_MATERIAL, claimed, commands)
                else:
                    # 无可施工墙位时改采高价值矿，不原地待命。
                    _fill_idle_mine(turn, role, claimed, commands)
            else:
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


def _destroyed_wall_sites(turn: Turn) -> list[Pos]:
    """基本围墙缺口：第一天锁定的墙位变空地后必须补回。"""
    return _basic_wall_gaps(turn)


def _full_wall_ring(turn: Turn) -> tuple[Pos, ...]:
    """第一天规格的三面满墙（迎敌面 + 上下满长），炮旁留通行格。"""
    front, top, bottom, _rear = _wall_face_cells(turn)
    reserved = _weapon_keep_open(turn)
    seen: set[Pos] = set()
    out: list[Pos] = []
    for pos in (*front, *top, *bottom):
        if pos in seen or not turn.land(pos) or pos in reserved:
            continue
        seen.add(pos)
        out.append(pos)
    return tuple(out)


def _lock_basic_wall(turn: Turn) -> None:
    """第一天把满墙计划锁定为基本围墙；之后只增不改。"""
    if turn.day_no == 1:
        note_basic_wall(set(_full_wall_ring(turn)))
        note_basic_wall({wall.pos for wall in turn.walls()})
        return
    if not basic_wall_cells():
        # 中途重启记忆时，用满墙规格 + 曾站住的墙位回填
        note_basic_wall(set(_full_wall_ring(turn)))
        note_basic_wall(set(wall_zone_seeds()))


def _basic_wall_gaps(turn: Turn) -> list[Pos]:
    """基本围墙位上当前没有墙的格子，白天优先补齐。"""
    _lock_basic_wall(turn)
    basic = basic_wall_cells()
    if not basic:
        return []
    standing = {wall.pos for wall in turn.walls()}
    reserved = _weapon_keep_open(turn)
    return sorted(
        (
            pos for pos in basic
            if pos not in standing
            and pos not in reserved
            and pos not in bad_build_cells()
            and turn.land(pos)
        ),
        key=lambda pos: (_wall_priority(turn, pos), pos.x, pos.y),
    )


def _rebuild_destroyed_walls(
    turn: Turn,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> bool:
    """基本围墙变空地后立刻新建：有石头去砌，没石头去采。"""
    if role.kind != WORKER:
        return False
    sites = _basic_wall_gaps(turn)
    if not sites:
        return False
    # 还没砌出过墙：走固定施工链，不要把「从未建过」当成被砸缺口从前墙开砌
    if not MEM.good_wall:
        return False
    if role.item_count(WALL_MATERIAL) > 0 and _wall_work(
        turn, role, sites, claimed, commands, hunt_stone=False,
    ):
        return True
    # 全队缺墙时人人去采石补缺口，不要求自己包里已有石头
    return _mine_kind(turn, role, WALL_MATERIAL, claimed, commands)


def _day1_wall_progress(turn: Turn) -> float:
    """迎敌面 + 上下两面（满长合计约 16 格）的完工比例。"""
    _, progress = _ring_progress(turn)
    return progress


def _need_early_walls(turn: Turn) -> bool:
    """第一天三面墙未齐前，优先囤石砌墙，再挖铁。"""
    if turn.day_no > 1:
        return False
    return _day1_wall_progress(turn) < 0.95


def _day1_stone_accounted(turn: Turn) -> int:
    """背包里的石头 + 已经砌上的三面墙，避免砌掉后又把目标刷回 16。"""
    ring = set(_wall_ring(turn))
    built = sum(1 for unit in turn.walls() if unit.pos in ring)
    return _team_stone(turn) + built


def _day1_stockpiling_stone(turn: Turn) -> bool:
    """Day1：累计约 16 石（含已砌）之前先采石；够了就停，不再回矿补货。"""
    if not _need_early_walls(turn):
        return False
    if _day1_stone_accounted(turn) >= DAY1_STONE_GOAL:
        return False
    return _team_stone(turn) < DAY1_STONE_GOAL


def _walls_safe(turn: Turn, walls_missing: list[Pos]) -> bool:
    """基本围墙完整、且不临夜缺墙时，视为围墙无风险。"""
    if _basic_wall_gaps(turn):
        return False
    if not walls_missing:
        return True
    if _wall_completion_phase(turn) and walls_missing:
        return False
    if turn.day_no <= 1 and walls_missing:
        return False
    if _need_early_walls(turn):
        return False
    front = _front_wall_cells(turn)
    front_missing = [pos for pos in walls_missing if pos in front]
    if turn.near_night and front_missing:
        return False
    return len(front_missing) == 0


def _pick_mason_ids(turn: Turn, walls_missing: list[Pos]) -> set[int]:
    """基本围墙有缺口或 Day1 未齐：全员石匠；墙稳后最多留 1 人补石砌墙。"""
    workers = list(turn.workers())
    if not workers:
        return set()
    if _basic_wall_gaps(turn):
        return {role.unit_id for role in workers}
    if _wall_completion_phase(turn) and walls_missing:
        return {role.unit_id for role in workers}
    if _need_early_walls(turn):
        return {role.unit_id for role in workers}
    if _walls_safe(turn, walls_missing) and _team_stone(turn) >= STONE_TARGET:
        return set()
    if _walls_safe(turn, walls_missing) and not walls_missing:
        return set()
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

    ranked = sorted(workers, key=key)
    return {ranked[0].unit_id}


def _day1_wall_lanes(
    turn: Turn,
    workers: list[Unit],
    walls_missing: list[Pos],
) -> dict[int, list[Pos]]:
    """两名工人固定从上/下边最外侧开工，沿圈砌到迎敌左右面碰头。"""
    if len(workers) < 2 or not walls_missing:
        return {}
    if (
        turn.day_no > 1
        and not _need_early_walls(turn)
        and not _wall_completion_phase(turn)
    ):
        return {}
    workers = sorted(workers[:2], key=lambda role: role.unit_id)
    worker_ids = {role.unit_id for role in workers}
    lanes_valid = (
        set(MEM.wall_lane) == worker_ids
        and set(MEM.wall_lane.values()) == {"top", "bottom"}
    )
    top_chain, bottom_chain = _mason_chains(turn)
    if not top_chain or not bottom_chain:
        return {}

    if not lanes_valid:
        first, second = workers
        top_stand = _preferred_wall_stand(turn, top_chain[0]) or top_chain[0]
        bottom_stand = _preferred_wall_stand(turn, bottom_chain[0]) or bottom_chain[0]
        direct = distance(first.pos, top_stand) + distance(second.pos, bottom_stand)
        swapped = distance(first.pos, bottom_stand) + distance(second.pos, top_stand)
        if direct <= swapped:
            MEM.wall_lane = {first.unit_id: "top", second.unit_id: "bottom"}
        else:
            MEM.wall_lane = {first.unit_id: "bottom", second.unit_id: "top"}

    missing = set(walls_missing)
    assigned: dict[int, list[Pos]] = {}
    for role in workers:
        lane = MEM.wall_lane[role.unit_id]
        own = top_chain if lane == "top" else bottom_chain
        other = bottom_chain if lane == "top" else top_chain
        assigned[role.unit_id] = [pos for pos in own if pos in missing] or [
            pos for pos in reversed(other) if pos in missing
        ]
    return assigned


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

    # 1.5) 围墙被打烂：先于卖矿、升炮、用券、购物，立刻新建缺口。
    if _rebuild_destroyed_walls(turn, role, claimed, commands):
        return budget

    # Day3 先清空铜铁再做升级和采购；协议每回合只能卖一种矿，
    # 所以会先卖涨价铁，下一回合继续卖铜。
    if _day3_ores_left(turn, role) and _sell_or_walk(
        turn, role, claimed, commands,
    ):
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

    # 5) 凑满 3 座火箭。囤石阶段只在贴着炮位时建，囤够后走去固定炮位
    early_walls = _need_early_walls(turn)
    stockpiling = _day1_stockpiling_stone(turn)
    if towers_missing and budget >= WEAPON_BUILD_COST and not stockpiling:
        candidates = [
            (index, site) for index, site in enumerate(sites)
            if site in towers_missing and site not in claimed
        ]
        if candidates:
            adjacent = [
                item for item in candidates
                if distance(role.pos, item[1]) <= 1 and role.pos != item[1]
            ]
            # 墙还没齐时：手里有石头先留着砌墙，没石头的人去建炮
            if role.item_count(WALL_MATERIAL) > 0 and early_walls and not adjacent:
                candidates = []
            if candidates:
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

    # 5.5) Day1：先囤约 16 石 → 砌墙；累计够 16 后不再回矿
    fire_ready = _firepower_ready(turn)
    if early_walls:
        if stockpiling:
            if _mine_kind(turn, role, WALL_MATERIAL, claimed, commands):
                return budget
            return budget
        if walls_missing and _wall_work(
            turn, role, walls_missing, claimed, commands, hunt_stone=False,
        ):
            return budget
        if (
            _day1_stone_accounted(turn) < DAY1_STONE_GOAL
            and role.item_count(WALL_MATERIAL) == 0
            and _mine_kind(turn, role, WALL_MATERIAL, claimed, commands)
        ):
            return budget
        return budget

    # Day1 墙已齐：改挖铁为主；升炮/购物仍可做
    day1_iron = turn.day_no <= 1 and not early_walls

    # 手里还有未用券/修复包，或采矿批次未满：先做完，不去商店。
    if not _should_defer_shop(turn, role):
        spent = _buy_wall_supplies(turn, role, claimed, commands, budget)
        if spent is not None:
            return budget - spent

    if len(turn.weapons()) >= 3 and not fire_ready:
        if _prefer_weapon_upgrade(turn, role, budget):
            spent = _buy_weapon_upgrade(turn, role, claimed, commands, budget)
            if spent is not None:
                return budget - spent
        if not day1_iron and not _wall_completion_phase(turn):
            # 非 Day1、尚未进入建全阶段：升级未完成时只砌迎敌面
            front = _front_wall_cells(turn)
            walls_missing = [pos for pos in walls_missing if pos in front]

    # 6) 石匠：砌墙 + 补石头
    if not day1_iron and job == "mason" and walls_missing and _wall_work(
        turn, role, walls_missing, claimed, commands,
    ):
        return budget
    if not day1_iron and job == "mason" and _team_stone(turn) < STONE_TARGET:
        if _mine_kind(turn, role, WALL_MATERIAL, claimed, commands):
            return budget

    # 6.5) 火力已达标后再追更高等级券
    if fire_ready and _prefer_weapon_upgrade(turn, role, budget):
        spent = _buy_weapon_upgrade(turn, role, claimed, commands, budget)
        if spent is not None:
            return budget - spent

    # 7) 涨价日优先出铁；其它时候满包/急需金币再卖
    if _should_sell(turn, role) and _sell_or_walk(turn, role, claimed, commands):
        return budget

    # 8) 买武器升级券等；买完由步骤 2 在后续回合使用
    if not _should_defer_shop(turn, role):
        spent = _buy_or_walk(turn, role, claimed, commands, budget)
        if spent is not None:
            return budget - spent

    # 9) 经济采集：Day1 墙齐后铁优先；石匠在仍需墙时才掺石头
    keep_stone = (
        not day1_iron
        and job == "mason"
        and not _walls_safe(turn, walls_missing)
        and bool(walls_missing)
    )
    if _mine_economy(turn, role, claimed, commands, keep_stone=keep_stone):
        return budget
    return budget


PIONEER_TASK_DAYS = frozenset({1, 8})


def _firepower_ready(turn: Turn) -> bool:
    """三门火箭均达到二级，才算核心火力成形。"""
    weapons = turn.weapons()
    return len(weapons) >= 3 and all(tower.level >= 2 for tower in weapons)


def _has_level3_weapon(turn: Turn) -> bool:
    return any(tower.level >= 3 for tower in turn.weapons())


def _wall_completion_phase(turn: Turn) -> bool:
    """第三天出现三级火箭后：把三面墙砌满并升级。"""
    return turn.day_no >= 3 and _has_level3_weapon(turn)


def _pioneer_is_shopper(turn: Turn) -> bool:
    """除第1、8天做任务外，白天由开拓者统一采购。"""
    return (
        turn.is_day
        and turn.day_no not in PIONEER_TASK_DAYS
        and turn.pioneer() is not None
    )


def _role_may_shop(turn: Turn, role: Unit) -> bool:
    if not _pioneer_is_shopper(turn):
        return True
    pioneer = turn.pioneer()
    return pioneer is not None and role.unit_id == pioneer.unit_id


def _walls_finished(turn: Turn) -> bool:
    """计划圈已砌满，且圈内墙都至少二级。"""
    ring = [
        pos for pos in _wall_ring(turn)
        if pos not in bad_build_cells() and turn.land(pos)
    ]
    if not ring:
        return True
    standing = {wall.pos: wall for wall in turn.walls()}
    if any(pos not in standing for pos in ring):
        return False
    return all(standing[pos].level >= 2 for pos in ring)


def _front_wall_cells(turn: Turn) -> set[Pos]:
    front, _top, _bottom = _wall_build_plan(turn)
    return set(front)


def _next_weapon_voucher(turn: Turn) -> str | None:
    """按阶段目标选择下一张武器券：Day2 两座 L2，Day3 先出一座 L3。"""
    weapons = turn.weapons()
    if not weapons:
        return None
    upgraded = sum(1 for tower in weapons if tower.level >= 2)
    if upgraded < 2 and any(tower.level == 1 for tower in weapons):
        return WEAPON_UPGRADE_1
    # 第二天两座 L2 达标后停手，把第一张三级券留到第三天再买。
    if turn.day_no < 3:
        return None
    # 第三天优先让其中一座 L2 升到 L3。
    if not _has_level3_weapon(turn) and any(tower.level == 2 for tower in weapons):
        return WEAPON_UPGRADE_2
    # 三级达标后先建全并升级围墙，墙完成前不再追买武器券。
    if _wall_completion_phase(turn) and not _walls_finished(turn):
        return None
    if any(tower.level == 1 for tower in weapons):
        return WEAPON_UPGRADE_1
    if any(tower.level == 2 for tower in weapons):
        return WEAPON_UPGRADE_2
    return None


def _priority_weapon_goal_pending(turn: Turn) -> bool:
    """核心火力阶段目标未完成时，暂停墙体及其它非核心采购。"""
    weapons = turn.weapons()
    if len(weapons) < 3:
        return False
    if turn.day_no == 2:
        return sum(1 for tower in weapons if tower.level >= 2) < 2
    if turn.day_no == 3:
        return not any(tower.level >= 3 for tower in weapons)
    return False


def _team_item_count(turn: Turn, name: str) -> int:
    return sum(role.item_count(name) for role in turn.controllable())


def _weapon_upgrade_buyer_id(turn: Turn) -> int | None:
    """开拓者白天统一采购；第1、8天或开拓者缺席时才由工人顶上。"""
    if _next_weapon_voucher(turn) is None:
        MEM.weapon_buyer_id = None
        return None
    pioneer = turn.pioneer()
    if pioneer is not None and turn.day_no not in PIONEER_TASK_DAYS:
        MEM.weapon_buyer_id = pioneer.unit_id
        return pioneer.unit_id
    workers = list(turn.workers())
    if not workers:
        MEM.weapon_buyer_id = None
        return None
    valid_ids = {role.unit_id for role in workers}
    if MEM.weapon_buyer_id in valid_ids:
        return MEM.weapon_buyer_id
    shop = turn.shop_pos()
    buyer = min(
        workers,
        key=lambda role: (
            distance(role.pos, shop) if shop is not None else 99,
            role.unit_id,
        ),
    )
    MEM.weapon_buyer_id = buyer.unit_id
    return buyer.unit_id


def _prefer_weapon_upgrade(turn: Turn, role: Unit, budget: int) -> bool:
    """仅全局指定采购员去买券；队伍已有同类券时不重复购买。"""
    if role.unit_id != _weapon_upgrade_buyer_id(turn):
        return False
    if role.find_item(WEAPON_UPGRADE_1) or role.find_item(WEAPON_UPGRADE_2):
        return False
    name = _next_weapon_voucher(turn)
    if name is None:
        return False
    if _team_item_count(turn, name) > 0:
        return False
    return budget >= turn.shop_price(name)


def _buy_weapon_upgrade(
    turn: Turn,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    budget: int,
) -> int | None:
    if role.unit_id != _weapon_upgrade_buyer_id(turn):
        return None
    name = _next_weapon_voucher(turn)
    if name is None:
        return None
    if _team_item_count(turn, name) > 0:
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
    # Day1 囤石阶段：只采不砌，凑够约 16 块再统一建墙
    if _day1_stockpiling_stone(turn):
        return _mine_kind(turn, role, WALL_MATERIAL, claimed, commands)

    stones = role.item_count(WALL_MATERIAL)
    mine = _adjacent_mine(turn, role, WALL_MATERIAL)
    # Day1 累计够 16 后，旁边有矿也不再补采，有石头就去砌
    stock_to = 0 if turn.day_no <= 1 else STONE_BATCH
    if mine is not None and stones < stock_to:
        commands[role.unit_id] = collect_command(mine)
        claimed.add(mine)
        return True
    if stones:
        pool = [site for site in walls_missing if site not in claimed]
        # 严格按施工链：上/下边最外侧 → 左右面，不就近抢砌
        for site in pool:
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

    # 已接任务必须做完；第1、8天才主动去接新任务。
    if turn.phase_task.strip():
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

    if turn.day_no in PIONEER_TASK_DAYS:
        if should_prioritize(turn):
            # 只有就绪任务才贴点；冷却中不空走任务点，去控炮/用券。
            held = _accept_or_approach(
                turn, role, claimed, commands, hold_idle=False,
            )
            if role.unit_id in commands:
                return "", ""
            if held and turn.available_tasks():
                _fill_idle_mine(
                    turn, role, claimed, commands, adjacent_only=True,
                )
                return "", ""
        if turn.weapons() and (turn.near_night or not _near_task_point(turn, role)):
            _man_tower(turn, role, claimed, commands, prefer_inside=True)
        elif towers_missing and not _near_task_point(turn, role):
            _step_or_idle(turn, role, towers_missing[0], claimed, commands)
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

    # 其余白天：先把手里的券用掉，再采购，避免商店↔墙体空跑。
    if _try_use_upgrade(turn, role, commands, claimed):
        return "", ""

    if _try_use_summon(role, commands):
        return "", ""

    spent = _buy_weapon_upgrade(turn, role, claimed, commands, turn.gold)
    if spent is not None:
        return "", ""
    if not _has_unused_supplies(role):
        spent = _buy_wall_supplies(turn, role, claimed, commands, turn.gold)
        if spent is not None:
            return "", ""
    if (
        need_ritual_prep(turn, role)
        and turn.day_no >= FIXED_RITUAL_BUY_FROM_DAY
        and turn.day_no < FIXED_TREASURE_DAY
        and not turn.near_night
        and _buy_ritual_or_walk(turn, role, claimed, commands, turn.gold) is not None
    ):
        return "", ""
    if not _has_unused_supplies(role):
        spent = _buy_or_walk(turn, role, claimed, commands, turn.gold)
        if spent is not None:
            return "", ""

    if _try_use_upgrade(turn, role, commands, claimed):
        return "", ""

    if turn.weapons():
        _man_tower(turn, role, claimed, commands, prefer_inside=True)
    elif towers_missing:
        _step_or_idle(turn, role, towers_missing[0], claimed, commands)
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
    # 夜里有怪：任务点在正面墙外就不去，否则会贴着墙内来回挤
    if task is not None and not _behind_robot_front(turn, task.pos):
        task = None
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
            if not _night_mine_ok(turn, role, mine):
                continue
            commands[role.unit_id] = collect_command(mine)
            claimed.add(mine)
            return True
        if oscillation_bans(role.unit_id):
            return _sidestep(turn, role, claimed, commands)
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


def _has_unused_supplies(role: Unit) -> bool:
    return bool(
        role.find_item(WALL_FIXER)
        or role.find_item(WALL_UPGRADE_1)
        or role.find_item(WALL_UPGRADE_2)
        or role.find_item(WEAPON_UPGRADE_1)
        or role.find_item(WEAPON_UPGRADE_2)
        or role.find_item(STATION_UPGRADE_1)
        or role.find_item(STATION_UPGRADE_2)
    )


def _mining_unfinished(role: Unit) -> bool:
    plan = MEM.mine_quota.get(role.unit_id)
    if plan is None or role.backpack_full:
        return False
    kind, target = plan
    return role.item_count(kind) < target


def _should_defer_shop(turn: Turn, role: Unit) -> bool:
    """采矿批次或未用券没做完时，除非已经站在商店旁，否则不去采购。"""
    shop = turn.shop_pos()
    if shop is not None and distance(role.pos, shop) <= 1:
        return False
    return _has_unused_supplies(role) or _mining_unfinished(role)


def _sidestep(
    turn: Turn,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> bool:
    """防抖把来回格禁了：先贴身采矿，否则走第三方向，不交空指令。"""
    if role.unit_id in commands:
        return False
    if role.kind == WORKER and not role.backpack_full:
        for kind in mine_rank(turn, keep_stone=False):
            mine = _adjacent_mine(turn, role, kind)
            if mine is None or mine in claimed:
                continue
            if not _night_mine_ok(turn, role, mine):
                continue
            commands[role.unit_id] = collect_command(mine)
            claimed.add(mine)
            return True
    bans = (
        set(oscillation_bans(role.unit_id))
        | set(failed_cells(role.unit_id))
        | set(claimed)
        | set(turn.blocked(role))
    )
    bans.update(_robot_front_avoid(turn, role))
    last = (MEM.move_hist.get(role.unit_id) or [None])[-1]
    best: Pos | None = None
    best_key: tuple[int, int, int] | None = None
    for dx, dy in _NEIGHBOUR_STEPS:
        pos = Pos(role.pos.x + dx, role.pos.y + dy)
        if pos in bans or not turn.land(pos):
            continue
        stay_far = 0 if last is None or pos != last else 1
        key = (stay_far, abs(dx) + abs(dy), pos.x + pos.y)
        if best is None or key < best_key:
            best, best_key = pos, key
    if best is None:
        return False
    commands[role.unit_id] = move_command(best)
    claimed.add(best)
    return True


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
        if need and turn.day_no >= FIXED_RITUAL_BUY_FROM_DAY:
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
        return _sidestep(turn, role, claimed, commands)
    return False


def _solve_task(
    turn: Turn, role: Unit, commands: dict[int, dict[str, Any]],
) -> tuple[str, str]:
    return solve_evolve_task(turn, role, commands)


def _front_x(turn: Turn) -> int | None:
    front, _top, _bottom, _rear = _wall_face_cells(turn)
    if not front:
        return None
    return front[0].x


def _robots_attacking(turn: Turn) -> bool:
    return (not turn.is_day) and bool(turn.hostile_robots())


def _behind_robot_front(turn: Turn, pos: Pos) -> bool:
    """正面墙内侧。夜里有怪时，不要走到墙外的进攻路线上。"""
    if not _robots_attacking(turn):
        return True
    line = _front_x(turn)
    if line is None:
        return True
    if _base_is_northwest(turn):
        return pos.x < line
    return pos.x > line


ROBOT_DANGER_RADIUS = 3
ROBOT_EVADE_RADIUS = 5
ROBOT_SAFE_MINE_DIST = 7


def _robot_gap(turn: Turn, pos: Pos) -> int:
    robots = turn.hostile_robots()
    if not robots:
        return 99
    return min(distance(pos, robot.pos) for robot in robots)


def _robot_front_avoid(turn: Turn, role: Unit) -> set[Pos]:
    """夜里有怪时的禁行格。

    所有人：机器人身边 ROBOT_DANGER_RADIUS 格内不踩。
    墙内的人：正面墙及墙外整列也不踩。
    墙外的人不按列封：回基地那条路正是机器人的进攻路线，靠 _evade_robots 躲开。
    """
    if not _robots_attacking(turn):
        return set()
    blocked: set[Pos] = set()
    for robot in turn.hostile_robots():
        for dx in range(-ROBOT_DANGER_RADIUS, ROBOT_DANGER_RADIUS + 1):
            for dy in range(-ROBOT_DANGER_RADIUS, ROBOT_DANGER_RADIUS + 1):
                blocked.add(Pos(robot.pos.x + dx, robot.pos.y + dy))
    line = _front_x(turn)
    if line is not None and _behind_robot_front(turn, role.pos):
        east = _base_is_northwest(turn)
        for x in range(turn.width):
            if (x >= line) if east else (x <= line):
                for y in range(turn.height):
                    blocked.add(Pos(x, y))
    blocked.discard(role.pos)
    return blocked


def _night_mine_ok(turn: Turn, role: Unit, pos: Pos) -> bool:
    """墙内的人只采墙内矿；墙外的人只采离所有机器人足够远的矿。"""
    if not _robots_attacking(turn):
        return True
    if _behind_robot_front(turn, role.pos):
        return _behind_robot_front(turn, pos)
    return _robot_gap(turn, pos) >= ROBOT_SAFE_MINE_DIST


def _evade_robots(
    turn: Turn,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> bool:
    """墙外的人：机器人靠近就往远离它们的方向挪一步，不走回基地。"""
    here = _robot_gap(turn, role.pos)
    if here > ROBOT_EVADE_RADIUS:
        return False
    blocked = set(turn.blocked(role)) | claimed
    best: tuple[int, int, int] | None = None
    pick: Pos | None = None
    for dx, dy in _NEIGHBOUR_STEPS:
        pos = Pos(role.pos.x + dx, role.pos.y + dy)
        if pos in blocked or not turn.land(pos):
            continue
        gap = _robot_gap(turn, pos)
        if gap <= here:
            continue
        key = (gap, -abs(dx) - abs(dy), -pos.x - pos.y)
        if best is None or key > best:
            best, pick = key, pos
    if pick is None:
        hold_idle(role.unit_id)
        return True
    commands[role.unit_id] = move_command(pick)
    claimed.add(pick)
    return True


def _night(turn: Turn, commands: dict[int, dict[str, Any]]) -> tuple[str, str]:
    _tower_sites(turn)
    _lock_basic_wall(turn)
    claimed: set[Pos] = set()
    used_controllers: set[int] = set()
    prompt = ""
    execute_cmd = ""
    pioneer = turn.pioneer()
    # 本波怪清完后不要再钉在炮上发呆，全员去采矿、卖货、采购
    cleared = not turn.hostile_robots()
    station = turn.station()

    # 开拓者在正面墙外且机器人逼近：先躲开，不要穿过进攻路线回基地
    if (
        pioneer is not None
        and not cleared
        and not _behind_robot_front(turn, pioneer.pos)
        and _evade_robots(turn, pioneer, claimed, commands)
    ):
        used_controllers.add(pioneer.unit_id)
    # 夜里：已接任务继续做完；否则能开夜宝藏就开；否则继续刷任务点；再否则当炮手
    elif (
        pioneer is not None
        and turn.phase_task.strip()
        and not should_abandon_task(turn)
    ):
        prompt, execute_cmd = solve_evolve_task(turn, pioneer, commands)
        submitted = (commands.get(pioneer.unit_id) or {}).get("action") == "submitAnswer"
        if submitted:
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

    # 三火箭 CD=3：有怪时只需 1 人轮流控三炮；清场后不再占炮
    gunner = None if cleared else _pick_night_gunner(turn, used_controllers)
    if gunner is not None and gunner.unit_id not in commands:
        # 贴身有就绪火箭且有目标：先开炮，用券/吃药放到冷却空档
        can_fire = any(
            weapon_ready(turn, tower)
            and distance(gunner.pos, tower.pos) <= 1
            and _attack_targets(turn, tower)
            for tower in turn.weapons()
        )
        dying = gunner.health <= 100 and gunner.find_item(MEDICINE) is not None
        if can_fire and not dying and _solo_rocket_fire(turn, gunner, claimed, commands):
            used_controllers.add(gunner.unit_id)
        elif _try_use_upgrade(turn, gunner, commands, claimed, walk_walls=False):
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

    anchor = station.pos if station is not None else None
    gunner_id = gunner.unit_id if gunner is not None else None
    night_budget = turn.gold
    for role in turn.controllable():
        if role.unit_id in commands or role.unit_id in used_controllers:
            continue
        # 在正面墙外：机器人靠近就躲；远的话留在外面干活，别穿过进攻路线回基地
        if (
            not cleared
            and not _behind_robot_front(turn, role.pos)
            and _evade_robots(turn, role, claimed, commands)
        ):
            continue
        # 有怪时唯一炮手冷却空窗只贴身采；清场后和其他人一样出工
        if role.unit_id == gunner_id:
            if _fix_walls(turn, role, claimed, commands, walk=False):
                continue
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
        # 围墙被打烂先新建；残墙再拿修复包补。夜里寻路不越正面墙、不贴机器人。
        if _rebuild_destroyed_walls(turn, role, claimed, commands):
            continue
        if _fix_walls(turn, role, claimed, commands):
            continue
        if role.kind != WORKER:
            if role.ore_counts():
                _sell_or_walk(turn, role, claimed, commands)
            elif cleared:
                _buy_or_walk(turn, role, claimed, commands, turn.gold)
            continue
        # 工人夜里不守炮：已在商店或清场才采购，否则继续采矿。
        shop = turn.shop_pos()
        at_shop = shop is not None and distance(role.pos, shop) <= 1
        if (cleared or at_shop) and not _should_defer_shop(turn, role):
            spent = _buy_or_walk(
                turn, role, claimed, commands, night_budget,
            )
            if spent is not None:
                night_budget -= spent
                continue
        outside = not cleared and not _behind_robot_front(turn, role.pos)
        _fill_idle_mine(
            turn,
            role,
            claimed,
            commands,
            stay_near=None if cleared or outside else anchor,
            max_dist=99 if cleared or outside else (14 if turn.weapons() else 99),
        )
        if role.unit_id not in commands:
            _sidestep(turn, role, claimed, commands)
    return prompt, execute_cmd


def _pick_night_gunner(
    turn: Turn, exclude: set[int],
) -> Unit | None:
    """选唯一炮手：开拓者优先，工人只在开拓者忙碌时应急。"""
    roles = [
        role for role in turn.controllable() if role.unit_id not in exclude
    ]
    towers = list(turn.weapons())
    if not roles or not towers:
        return None
    cap = NIGHT_ROCKET_GUNNERS
    if cap <= 0:
        return None
    pioneer = next((role for role in roles if role.kind == "pioneer"), None)
    if pioneer is not None:
        return pioneer

    def score(role: Unit) -> tuple:
        ready_here = sum(
            1 for tower in towers
            if weapon_ready(turn, tower) and distance(role.pos, tower.pos) <= 1
        )
        near_any = sum(1 for tower in towers if distance(role.pos, tower.pos) <= 1)
        nearest = min(distance(role.pos, tower.pos) for tower in towers)
        # 墙外的人赶回炮位要穿过进攻路线，墙内有人时不选它
        outside = 0 if _behind_robot_front(turn, role.pos) else 1
        return (outside, -ready_here, -near_any, nearest, role.unit_id)

    return min(roles, key=score)


def _solo_rocket_fire(
    turn: Turn,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> bool:
    """一人轮流打三座火箭：优先钉在共用操控格，贴住三炮再开火。"""
    if role.unit_id in commands:
        return False
    towers = list(turn.weapons())
    if not towers:
        return False

    hub = _gunner_hub(turn, towers, role)
    # 必须站上共用格本身。_step_toward 只会停在邻格，而且会把目标格标成禁入。
    if hub is not None and role.pos != hub:
        step = _step_onto(turn, role, hub, claimed)
        if step is not None:
            commands[role.unit_id] = move_command(step)
            return True
        # 这一步进不去就原地打已经贴住的炮，不要改去绕邻格。

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

    # 共用格存在时不再改追单炮，避免在西侧邻格之间来回走。
    if hub is not None:
        if _try_combat_item(turn, role, commands, relaxed=True):
            return True
        return False

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
    *,
    walk_walls: bool = True,
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

    # Day2 两座 L2、Day3 一座 L3 尚未完成时，不让基地券、墙券或修复包抢回合。
    if _priority_weapon_goal_pending(turn):
        return False

    station = turn.station()
    if station is not None:
        for voucher, need_level in (
            (STATION_UPGRADE_1, 1),
            (STATION_UPGRADE_2, 2),
        ):
            item = role.find_item(voucher)
            if not item or station.level != need_level:
                continue
            # 目标格要选在自己 1 格内的基地格；选左上角可能隔 2 格，服务端判失败且不扣券
            cells = [
                cell for cell in station_footprint(station.pos)
                if use_failures(item, cell) < USE_FAIL_LIMIT
            ]
            if not cells:
                continue
            near = [cell for cell in cells if distance(role.pos, cell) <= 1]
            if near:
                target = min(near, key=lambda cell: (cell.x, cell.y))
                commands[role.unit_id] = use_command(item, target)
                return True
            step = _step_toward(turn, role, station.pos, claimed, inside_only=True)
            if step is not None:
                commands[role.unit_id] = move_command(step)
                return True

    # 围墙升级优先于修复：迎敌左右面未升完前，不升上下面。
    for voucher, need_level in (
        (WALL_UPGRADE_1, 1),
        (WALL_UPGRADE_2, 2),
    ):
        item = role.find_item(voucher)
        if not item:
            continue
        front_todo = _front_walls_needing_upgrade(turn, need_level)
        # 迎敌正面（左右面）还有待升级：只对这些墙用券/走近
        pool = front_todo or [
            wall for wall in turn.walls()
            if wall.level == need_level
        ]
        near = [
            wall for wall in pool
            if distance(role.pos, wall.pos) <= 1
        ]
        if near:
            wall = min(
                near,
                key=lambda unit: (_wall_priority(turn, unit.pos), unit.health),
            )
            MEM.use_target.pop(role.unit_id, None)
            commands[role.unit_id] = use_command(item, wall.pos)
            return True
        if not walk_walls:
            continue
        locked = MEM.use_target.get(role.unit_id)
        targets = sorted(
            (wall for wall in pool if wall.pos not in claimed),
            key=lambda unit: (
                0 if locked is not None and unit.pos == locked else 1,
                _wall_priority(turn, unit.pos),
                distance(role.pos, unit.pos),
            ),
        )
        for target in targets:
            step = _step_toward(turn, role, target.pos, claimed)
            if step is not None:
                MEM.use_target[role.unit_id] = target.pos
                claimed.add(target.pos)
                commands[role.unit_id] = move_command(step)
                return True
        if oscillation_bans(role.unit_id) and _sidestep(turn, role, claimed, commands):
            return True
    # 没有可用墙升级券时才修复残墙。
    if _fix_walls(turn, role, claimed, commands, walk=walk_walls):
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
    locked = MEM.use_target.get(role.unit_id)
    pick = min(
        free,
        key=lambda unit: (
            0 if locked is not None and unit.pos == locked else 1,
            distance(role.pos, unit.pos),
            unit.pos.x,
            unit.pos.y,
        ),
    )
    MEM.use_target[role.unit_id] = pick.pos
    return pick


def _wall_max_hp(wall: Unit) -> int:
    # level1/2/3 分别 1000/1500/2000
    return 500 * (min(max(wall.level, 1), 3) + 1)


USE_FAIL_LIMIT = 2
WALL_UPGRADE_FROM_DAY = 2
WALL_FIXER_FROM_DAY = 2
WALL_VOUCHER_BATCH = 4
FIXER_BATCH = 3
WALL_DAMAGED_RATIO = 0.8


def _wall_priority(turn: Turn, pos: Pos) -> int:
    """0=迎敌左右面，1=背面左右面，2=上下面。升级券严格先左右后上下。"""
    front, _top, _bottom, rear = _wall_face_cells(turn)
    if pos in set(front):
        return 0
    if pos in set(rear):
        return 1
    return 2


def _front_walls_needing_upgrade(turn: Turn, need_level: int) -> list[Unit]:
    """迎敌正面（左右面）上仍待升级的墙。"""
    front = _front_wall_cells(turn)
    return [
        wall for wall in turn.walls()
        if wall.level == need_level and wall.pos in front
    ]


def _wall_damaged(wall: Unit) -> bool:
    return wall.health < _wall_max_hp(wall) * WALL_DAMAGED_RATIO


def _team_items(turn: Turn, name: str) -> int:
    return sum(role.item_count(name) for role in turn.controllable())


def _wall_upgrades_owed(turn: Turn) -> int:
    """先凑齐迎敌左右面升级券；正面升完后再补其余基本围墙。"""
    if not _wall_completion_phase(turn):
        return 0
    front = _front_wall_cells(turn)
    front_todo = sum(
        1 for wall in turn.walls()
        if wall.level == 1 and wall.pos in front
    )
    if front_todo > 0:
        return max(0, front_todo - _team_items(turn, WALL_UPGRADE_1))
    basic = basic_wall_cells() or set(_wall_ring(turn))
    todo = sum(
        1 for wall in turn.walls()
        if wall.level == 1 and wall.pos in basic
    )
    return max(0, todo - _team_items(turn, WALL_UPGRADE_1))


def _fixer_owed(turn: Turn) -> bool:
    """有残墙且全队修复包不够时才买，不再每天强制跑一趟商店。"""
    if turn.day_no < WALL_FIXER_FROM_DAY:
        return False
    damaged = sum(1 for wall in turn.walls() if _wall_damaged(wall))
    if damaged <= 0:
        return False
    return _team_items(turn, WALL_FIXER) < min(damaged, FIXER_BATCH)


def _buy_wall_supplies(
    turn: Turn,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    budget: int,
) -> int | None:
    """白天去商店买墙券/修复包，返回花掉的金币；没有要买的返回 None。"""
    shop = turn.shop_pos()
    if (
        shop is None
        or not turn.is_day
        or not _role_may_shop(turn, role)
        or _priority_weapon_goal_pending(turn)
    ):
        return None
    want: tuple[str, int] | None = None
    owed = min(_wall_upgrades_owed(turn), WALL_VOUCHER_BATCH)
    # 建全阶段先买升级券；修复包只在没有待升墙时补。
    if owed > 0:
        want = (WALL_UPGRADE_1, owed)
    elif _fixer_owed(turn):
        damaged = sum(1 for wall in turn.walls() if _wall_damaged(wall))
        have = _team_items(turn, WALL_FIXER)
        want = (WALL_FIXER, max(1, min(damaged, FIXER_BATCH) - have))
    if want is None:
        return None
    name, num = want
    if purchase_busy(name, role.unit_id, turn.round_no):
        return None
    price = turn.shop_price(name)
    num = min(num, budget // price) if price > 0 else num
    if num <= 0:
        return None
    if distance(role.pos, shop) <= 1:
        commands[role.unit_id] = buy_command(name, num)
        note_purchase(name, role.unit_id, turn.round_no)
        if name == WALL_FIXER:
            MEM.fixer_day = turn.day_no
        return price * num
    step = _step_toward(turn, role, shop, claimed)
    if step is None:
        if oscillation_bans(role.unit_id) and _sidestep(turn, role, claimed, commands):
            return 0
        return None
    commands[role.unit_id] = move_command(step)
    note_purchase(name, role.unit_id, turn.round_no)
    return 0


def _fix_walls(
    turn: Turn,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    *,
    walk: bool = True,
) -> bool:
    """手里有修复包：贴身有残墙就修；否则走到最该修的残墙旁（迎敌面优先）。"""
    fixer = role.find_item(WALL_FIXER)
    if not fixer:
        return False
    damaged = [
        wall for wall in turn.walls()
        if _wall_damaged(wall)
        and not (
            turn.day_no == 2
            and wall.level == 1
            and _wall_priority(turn, wall.pos) == 0
        )
    ]
    if not damaged:
        return False
    near = [wall for wall in damaged if distance(role.pos, wall.pos) <= 1]
    if near:
        wall = min(near, key=lambda unit: unit.health)
        MEM.use_target.pop(role.unit_id, None)
        commands[role.unit_id] = use_command(fixer, wall.pos)
        return True
    if not walk:
        return False
    locked = MEM.use_target.get(role.unit_id)
    for wall in sorted(
        damaged,
        key=lambda unit: (
            0 if locked is not None and unit.pos == locked else 1,
            _wall_priority(turn, unit.pos),
            unit.health,
            distance(role.pos, unit.pos),
        ),
    ):
        if wall.pos in claimed:
            continue
        step = _step_toward(turn, role, wall.pos, claimed)
        if step is not None:
            MEM.use_target[role.unit_id] = wall.pos
            claimed.add(wall.pos)
            commands[role.unit_id] = move_command(step)
            return True
    if oscillation_bans(role.unit_id):
        return _sidestep(turn, role, claimed, commands)
    return False


def _should_sell(turn: Turn, role: Unit) -> bool:
    ores = role.ore_counts()
    if not ores:
        return False
    copper = ores.get(COPPER, 0)
    iron = ores.get(IRON, 0)
    stone = ores.get(WALL_MATERIAL, 0)
    # Day3 无条件清空铜铁，不受涨价、背包容量或当前金币限制。
    if turn.day_no == 3 and (copper > 0 or iron > 0):
        return True
    # 涨价日：有铁立刻卖
    if iron > 0 and dump_ore(turn, IRON):
        return True
    if copper > 0 and dump_ore(turn, COPPER):
        return True
    # 囤铁期：包满时只卖铜/超额石头，绝不卖铁
    if role.backpack_full:
        if hold_ore(turn, IRON) and iron > 0 and copper <= 0:
            return stone > STONE_RESERVE
        return True
    # 采卖计划未完成：先采满批次再卖（涨价日除外，上面已处理）
    plan = MEM.mine_quota.get(role.unit_id)
    if plan is not None:
        kind, target = plan
        if role.item_count(kind) < target and not role.backpack_full:
            return False
    # 急需金币建炮时才提前卖（囤铁期仍不卖铁）
    value = copper * turn.ore_price(COPPER)
    if not hold_ore(turn, IRON):
        value += iron * turn.ore_price(IRON)
    if len(turn.weapons()) < 3 and value >= max(1, WEAPON_BUILD_COST - turn.gold):
        return True
    if turn.gold < 25 and value >= 15:
        return True
    if len(role.backpack) >= 30 and value >= 10:
        return True
    return False


def _day3_ores_left(turn: Turn, role: Unit) -> bool:
    """第 3 天需要连续卖出的铜铁是否仍有剩余。"""
    if turn.day_no != 3:
        return False
    ores = role.ore_counts()
    return bool(ores.get(COPPER, 0) or ores.get(IRON, 0))


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
    # 每条 sell 指令只能卖一种；Day3 会连续调用直到铜铁清空。
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
                # 囤铁期允许卖超额石头腾背包；涨价日有铁则先卖铁
                if hold_ore(turn, IRON) and ores.get(IRON, 0) > 0 and ores.get(COPPER, 0) <= 0:
                    pass
                else:
                    continue
        if count <= 0:
            continue
        # 严格遵守囤矿：涨价前绝不卖铁
        if hold_ore(turn, name):
            continue
        price = turn.ore_price(name)
        hot = 1 if dump_ore(turn, name) else 0
        # 铜优先于铁（同价/非涨价时）；涨价铁优先
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
    if shop is None or not _role_may_shop(turn, role):
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
        _sidestep(turn, role, claimed, commands)
        return 0
    return None


def _wanted_purchase(
    turn: Turn, role: Unit, budget: int,
) -> tuple[str, int] | None:
    """白天购货顺序：武器升级（优先）→ 基地保命 → 墙券/修复 → 其它。"""
    weapons = turn.weapons()
    walls = turn.walls()
    station = turn.station()
    # 下一张武器券的钱先留住，避免被墙券/炸弹花光
    reserve = 0
    next_voucher = _next_weapon_voucher(turn)
    if next_voucher is not None and (
        len(weapons) >= 3 and not _firepower_ready(turn)
        or next_voucher == WEAPON_UPGRADE_2
    ):
        reserve = min(WEAPON_UPGRADE_RESERVE, turn.shop_price(next_voucher))

    def can_buy(name: str, stack: int = 1, *, core: bool = False) -> tuple[str, int] | None:
        if purchase_busy(name, role.unit_id, turn.round_no):
            return None
        price = turn.shop_price(name)
        pool = budget if core else budget - reserve
        if pool >= price and role.item_count(name) < stack:
            return name, price
        return None

    # 1) 有塔则火力升级最优先，但只允许全局指定采购员购买。
    if (
        next_voucher is not None
        and role.unit_id == _weapon_upgrade_buyer_id(turn)
        and _team_item_count(turn, next_voucher) == 0
    ):
        item = can_buy(next_voucher, core=True)
        if item:
            return item
    # Day2 两座 L2、Day3 一座 L3 未完成前，所有钱只留给武器升级。
    if _priority_weapon_goal_pending(turn):
        return None
    # 2) 基地保命升到 L2/L3
    if (
        station is not None and station.level == 1
        and _team_items(turn, STATION_UPGRADE_1) == 0
    ):
        item = can_buy(STATION_UPGRADE_1)
        if item:
            return item
    if (
        station is not None and station.level == 2
        and _team_items(turn, STATION_UPGRADE_2) == 0
    ):
        item = can_buy(STATION_UPGRADE_2)
        if item:
            return item
    # 3) 三级火箭后建全围墙，再买墙升级券
    if _wall_completion_phase(turn):
        if any(wall.level == 1 for wall in walls):
            item = can_buy(WALL_UPGRADE_1)
            if item:
                return item
        if any(wall.level == 2 for wall in walls):
            item = can_buy(WALL_UPGRADE_2)
            if item:
                return item
    # 4) 残墙且全队没有修复包才买，避免每人各买 1 个来回跑
    if _team_items(turn, WALL_FIXER) == 0 and any(
        _wall_damaged(wall) for wall in walls
    ):
        item = can_buy(WALL_FIXER, stack=1)
        if item:
            return item
    need = missing_ritual(turn, role)
    if need and turn.day_no >= FIXED_RITUAL_BUY_FROM_DAY:
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
    towers = list(turn.weapons())
    if not towers:
        return False
    hub = _gunner_hub(turn, towers, role)
    if hub is not None and role.pos != hub:
        step = _step_onto(turn, role, hub, claimed)
        if step is not None:
            commands[role.unit_id] = move_command(step)
            return True
        return False
    if hub is not None and role.pos == hub:
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
        # Day1 附近无铁：丢掉远处铁配额，改走铜优先
        if kind == IRON and prefer_day1_copper(turn):
            MEM.mine_quota.pop(role.unit_id, None)
            plan = None
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
            batch = MINE_BATCH
            # 囤铁期尽量采满背包，涨价日一次出清（Day1 附近无铁时不锁铁配额）
            if (
                kind == IRON
                and turn.day_no <= FIXED_IRON_STOCKPILE_UNTIL
                and not prefer_day1_copper(turn)
            ):
                batch = max(MINE_BATCH, role.capacity)
            MEM.mine_quota[role.unit_id] = (kind, batch)
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
            and _night_mine_ok(turn, role, pos)
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
        claimed.discard(mine)
    if oscillation_bans(role.unit_id):
        return _sidestep(turn, role, claimed, commands)
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
    outside_only = name == WALL
    can_build_here = (
        role.pos != target
        and distance(role.pos, target) <= 1
        and (
            not outside_only
            or _is_outside_wall_stand(turn, role.pos, target)
        )
    )
    if can_build_here:
        commands[role.unit_id] = build_command(target, name)
        claimed.add(target)
        return True
    claimed.add(target)
    if outside_only:
        stand = _preferred_wall_stand(turn, target)
        step = None
        if stand is not None and stand != role.pos:
            step = _step_onto(turn, role, stand, claimed)
        if step is None:
            step = _step_toward(
                turn, role, target, claimed, outside_only=True,
            )
    else:
        step = _step_toward(
            turn, role, target, claimed, outside_only=False,
        )
    if step is not None:
        commands[role.unit_id] = move_command(step)
        return True
    claimed.discard(target)
    if oscillation_bans(role.unit_id):
        return _sidestep(turn, role, claimed, commands)
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
        return
    _sidestep(turn, role, claimed, commands)


def _step_onto(
    turn: Turn,
    role: Unit,
    target: Pos,
    claimed: set[Pos],
) -> Pos | None:
    """走进目标格。不能走 _step_toward：那只会停在旁边，还会把目标格禁掉。"""
    if role.pos == target:
        return None
    avoid = set(failed_cells(role.unit_id)) | set(oscillation_bans(role.unit_id)) | set(claimed)
    avoid.update(_robot_front_avoid(turn, role))
    avoid.discard(role.pos)
    if _behind_robot_front(turn, target):
        avoid.discard(target)
    greedy = not _robots_attacking(turn)
    step = next_step(turn, role, target, avoid, allow_greedy=greedy)
    if step is None or (step != target and step in avoid):
        return None
    claimed.add(step)
    claimed.add(target)
    return step


def _step_toward(
    turn: Turn,
    role: Unit,
    target: Pos,
    claimed: set[Pos],
    *,
    inside_only: bool = False,
    outside_only: bool = False,
) -> Pos | None:
    avoid = set(failed_cells(role.unit_id)) | set(oscillation_bans(role.unit_id)) | claimed
    avoid.update(_robot_front_avoid(turn, role))
    if outside_only:
        station = turn.station()
        if station is not None:
            footprint = station_footprint(station.pos)
            wall_radius = _footprint_distance(target, footprint)
            if _footprint_distance(role.pos, footprint) > wall_radius:
                # 已在墙外后，寻路也不得再穿过围墙内部抄近路。
                avoid.update(
                    Pos(x, y)
                    for x in range(turn.width)
                    for y in range(turn.height)
                    if _footprint_distance(Pos(x, y), footprint) <= wall_radius
                )
    avoid.discard(role.pos)
    # 夜里有怪时 A* 找不到路就原地待命；贪心乱走会挤进墙内贴着正面墙来回晃
    greedy = not _robots_attacking(turn)
    for stand in _stand_cells(
        turn, role, target, claimed, inside_only, outside_only,
    ):
        if stand == role.pos:
            return None
        if stand in avoid and stand != target:
            continue
        step = next_step(turn, role, stand, avoid, allow_greedy=greedy)
        if step is None or step in avoid:
            continue
        claimed.add(step)
        if stand != role.pos:
            claimed.add(stand)
        return step
    if outside_only:
        # 砌墙绝不退回内侧或直接追墙位，避免角色被封进围墙。
        return None
    step = next_step(turn, role, target, avoid, allow_greedy=greedy)
    if step is not None and step not in avoid:
        claimed.add(step)
        return step
    # 防抖把两极都禁了：尝试仅禁失败格再走一步，仍不行则待命
    soft = set(failed_cells(role.unit_id)) | claimed
    soft.update(_robot_front_avoid(turn, role))
    soft.discard(role.pos)
    step = next_step(turn, role, target, soft, allow_greedy=greedy)
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


def _refill_idle_roles(
    turn: Turn, commands: dict[int, dict[str, Any]],
) -> None:
    """去重或寻路失败后补一条就近动作，避免整回合交空指令。"""
    claimed: set[Pos] = set()
    controllers: set[int] = set()
    for command in commands.values():
        if command.get("action") == "attack":
            raw_id = command.get("controllerId")
            if raw_id is not None:
                controllers.add(int(raw_id))
        targets = command.get("targetPos") or ()
        if not targets:
            continue
        raw = targets[0]
        claimed.add(Pos(int(raw["x"]), int(raw["y"])))
    pioneer = turn.pioneer()
    for role in turn.controllable():
        if role.unit_id in commands or role.unit_id in controllers:
            continue
        if (
            pioneer is not None
            and role.unit_id == pioneer.unit_id
            and turn.phase_task.strip()
            and _near_task_point(turn, role)
        ):
            continue
        at_tower = any(
            distance(role.pos, tower.pos) <= 1 for tower in turn.weapons()
        )
        if not turn.is_day and at_tower:
            _fill_idle_mine(
                turn, role, claimed, commands, adjacent_only=True,
            )
            continue
        if _fill_idle_mine(turn, role, claimed, commands, adjacent_only=True):
            continue
        if not turn.is_day and _robots_attacking(turn) and not at_tower:
            continue
        _sidestep(turn, role, claimed, commands)


def _stand_cells(
    turn: Turn,
    role: Unit,
    target: Pos,
    claimed: set[Pos],
    inside_only: bool = False,
    outside_only: bool = False,
) -> list[Pos]:
    station = turn.station()
    footprint = station_footprint(station.pos) if station else ()
    blocked = turn.blocked(role)
    planned = _planned_wall_cells(turn) if outside_only else set()
    preferred = _preferred_wall_stand(turn, target) if outside_only else None
    cells = [
        pos for pos in _neighbours(target)
        if turn.land(pos)
        and pos not in blocked
        and pos not in planned
        and (pos == role.pos or pos not in claimed)
        and (
            not inside_only
            or _footprint_distance(pos, footprint) <= 1
        )
        and (
            not outside_only
            or _footprint_distance(pos, footprint)
            > _footprint_distance(target, footprint)
        )
    ]
    cells.sort(
        key=lambda pos: (
            0 if preferred is not None and pos == preferred else 1,
            (
                -_footprint_distance(pos, footprint)
                if outside_only
                else _footprint_distance(pos, footprint)
            ),
            pos.x,
            pos.y,
        ),
    )
    return cells


def _planned_wall_cells(turn: Turn) -> set[Pos]:
    """计划墙位（含基本围墙）禁止当作站位，避免踩上去再下来。"""
    front, top, bottom, rear = _wall_face_cells(turn)
    return set(front) | set(top) | set(bottom) | set(rear) | set(basic_wall_cells())


def _preferred_wall_stand(turn: Turn, wall: Pos) -> Pos | None:
    """墙格正外侧一格：上墙朝北、下墙朝南、右墙朝东、左墙朝西。"""
    station = turn.station()
    if station is None:
        return None
    footprint = station_footprint(station.pos)
    xs = [cell.x for cell in footprint]
    ys = [cell.y for cell in footprint]
    xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)
    if wall.y == ymax + 2:
        stand = Pos(wall.x, wall.y + 1)
    elif wall.y == ymin - 2:
        stand = Pos(wall.x, wall.y - 1)
    elif wall.x == xmax + 2:
        stand = Pos(wall.x + 1, wall.y)
    elif wall.x == xmin - 2:
        stand = Pos(wall.x - 1, wall.y)
    else:
        axial = (
            Pos(wall.x + 1, wall.y),
            Pos(wall.x - 1, wall.y),
            Pos(wall.x, wall.y + 1),
            Pos(wall.x, wall.y - 1),
        )
        stand = max(axial, key=lambda pos: _footprint_distance(pos, footprint))
        if _footprint_distance(stand, footprint) <= _footprint_distance(wall, footprint):
            return None
    if not turn.land(stand):
        return None
    return stand


def _mason_chains(turn: Turn) -> tuple[list[Pos], list[Pos]]:
    """写死施工链：上边最外侧 → 迎敌左右面；下边最外侧 → 迎敌左右面，中间碰头。"""
    front, top, bottom = _wall_build_plan(turn)
    reserved = _weapon_keep_open(turn)
    banned = bad_build_cells()
    nw = _base_is_northwest(turn)

    def clean(cells: list[Pos]) -> list[Pos]:
        seen: set[Pos] = set()
        out: list[Pos] = []
        for pos in cells:
            if pos in seen or pos in reserved or pos in banned:
                continue
            if not turn.land(pos):
                continue
            seen.add(pos)
            out.append(pos)
        return out

    if nw:
        top_arm = sorted(top, key=lambda pos: (pos.x, pos.y))
        bottom_arm = sorted(bottom, key=lambda pos: (pos.x, -pos.y))
        front_down = sorted(front, key=lambda pos: (-pos.y, pos.x))
    else:
        top_arm = sorted(top, key=lambda pos: (-pos.x, pos.y))
        bottom_arm = sorted(bottom, key=lambda pos: (-pos.x, -pos.y))
        front_down = sorted(front, key=lambda pos: (-pos.y, -pos.x))

    mid = (len(front_down) + 1) // 2
    top_front = front_down[:mid]
    bottom_front = list(reversed(front_down[mid:]))
    return clean([*top_arm, *top_front]), clean([*bottom_arm, *bottom_front])


def _is_outside_wall_stand(turn: Turn, stand: Pos, wall: Pos) -> bool:
    if stand == wall or stand in _planned_wall_cells(turn):
        return False
    station = turn.station()
    if station is None:
        return True
    footprint = station_footprint(station.pos)
    return (
        _footprint_distance(stand, footprint)
        > _footprint_distance(wall, footprint)
    )


def _tower_sites(turn: Turn) -> tuple[Pos, ...]:
    """三炮贴住基地一角，角色站缺口格同时贴住三座。

    左上基地（左上角 sx,sy）：炮 (sx-1,sy-1)、(sx-1,sy+1)、(sx,sy+1)，站 (sx-1,sy)。
    右下基地取中心对称。
    """
    station = turn.station()
    if station is None:
        return ()
    sx, sy = station.pos.x, station.pos.y
    if _base_is_northwest(turn):
        towers = (
            Pos(sx - 1, sy - 1),
            Pos(sx - 1, sy + 1),
            Pos(sx, sy + 1),
        )
        hub = Pos(sx - 1, sy)
    else:
        towers = (
            Pos(sx + 2, sy),
            Pos(sx + 2, sy - 2),
            Pos(sx + 1, sy - 2),
        )
        hub = Pos(sx + 2, sy - 1)
    MEM.tower_plan = towers
    MEM.tower_hub = hub
    return towers


def _hub_of_l(cells: list[Pos]) -> Pos | None:
    """直角三格的缺角 = 共用操控格。"""
    if not _is_compact_l(cells):
        return None
    xs = {pos.x for pos in cells}
    ys = {pos.y for pos in cells}
    for x in range(min(xs), max(xs) + 1):
        for y in range(min(ys), max(ys) + 1):
            hub = Pos(x, y)
            if hub not in cells:
                return hub
    return None


def _is_compact_l(cells: list[Pos]) -> bool:
    """三格落在某个 2x2 里（缺一角）= 直角布局。"""
    if len(cells) != 3:
        return False
    xs = [pos.x for pos in cells]
    ys = [pos.y for pos in cells]
    return max(xs) - min(xs) == 1 and max(ys) - min(ys) == 1


def _gunner_hub(turn: Turn, towers: list[Unit], role: Unit) -> Pos | None:
    """夜战站位：优先锁定方案的缺角格，否则取能贴住最多炮的格子。"""
    if not towers:
        return None
    blocked = turn.blocked(role)
    planned = MEM.tower_hub
    if planned is not None:
        if (
            0 <= planned.x < turn.width
            and 0 <= planned.y < turn.height
            and turn.land(planned)
            and (planned not in blocked or planned == role.pos)
            and all(distance(planned, tower.pos) <= 1 for tower in towers)
        ):
            return planned

    # 若现有炮已是直角，用其缺角
    positions = [tower.pos for tower in towers]
    if len(positions) == 3:
        natural = _hub_of_l(positions)
        if natural is not None and (
            natural not in blocked or natural == role.pos
        ) and turn.land(natural):
            return natural

    candidates: set[Pos] = set()
    for tower in towers:
        for nbr in _neighbours(tower.pos):
            if not (0 <= nbr.x < turn.width and 0 <= nbr.y < turn.height):
                continue
            if not turn.land(nbr):
                continue
            if nbr in blocked and nbr != role.pos:
                continue
            if any(nbr == other.pos for other in towers):
                continue
            candidates.add(nbr)
    best: Pos | None = None
    best_key: tuple | None = None
    anchor = threat_anchor(turn)
    for pos in candidates:
        covered = sum(1 for tower in towers if distance(pos, tower.pos) <= 1)
        key = (covered, -distance(pos, anchor), -pos.x, -pos.y)
        if best_key is None or key > best_key:
            best_key = key
            best = pos
    if best_key is None or best_key[0] < 2:
        return None
    return best


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


def _weapon_side_wall(turn: Turn) -> list[Pos]:
    """靠近两座火箭的那条边，固定砌满 6 格。"""
    _front, top, bottom, _rear = _wall_face_cells(turn)
    if _base_is_northwest(turn):
        return list(top)
    return list(bottom)


def _wall_build_plan(turn: Turn) -> tuple[list[Pos], list[Pos], list[Pos]]:
    """实际要砌的格子。

    Day1：迎敌面 + 上下两面满长（约 4+6+6=16 格）。
    第三天三级火箭后：再次砌满三面。
    其间：远离双炮的一侧缩短；靠近两座火箭的那条边始终砌满 6 格。
    """
    front, top, bottom, _rear = _wall_face_cells(turn)
    if turn.day_no <= 1 or _wall_completion_phase(turn):
        return front, list(top), list(bottom)
    nw = _base_is_northwest(turn)
    trim = _side_trim_count(turn)
    if nw:
        far = _trim_side_walls(_half_toward_front(bottom, nw), nw, trim)
        return front, list(top), far
    far = _trim_side_walls(_half_toward_front(top, nw), nw, trim)
    return front, far, list(bottom)


def _weapon_keep_open(turn: Turn) -> set[Pos]:
    """炮位、操炮格及其邻格不砌墙，留给角色走进去控炮。

    靠近两座火箭的那条 6 格墙不让路，必须砌满。
    """
    if not MEM.tower_plan:
        _tower_sites(turn)
    anchors: set[Pos] = set(MEM.tower_plan)
    if MEM.tower_hub is not None:
        anchors.add(MEM.tower_hub)
    for weapon in turn.weapons():
        anchors.add(weapon.pos)
    open_cells = set(anchors)
    for pos in anchors:
        open_cells.update(_neighbours(pos))
    for pos in _weapon_side_wall(turn):
        open_cells.discard(pos)
    return open_cells


def _wall_ring(turn: Turn) -> tuple[Pos, ...]:
    """来敌面 + 上下侧（Day1 满长，其后半墙），不含背面；炮旁留出通行格。"""
    front, top, bottom = _wall_build_plan(turn)
    reserved = _weapon_keep_open(turn)
    seen: set[Pos] = set()
    out: list[Pos] = []
    for pos in (*front, *top, *bottom):
        if pos in seen or not turn.land(pos) or pos in reserved:
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
    """建造顺序：上边最外侧、下边最外侧各一条链，砌到迎敌左右面碰头。"""
    del seal
    if turn.station() is None:
        return ()
    top_chain, bottom_chain = _mason_chains(turn)
    return tuple(dict.fromkeys([*top_chain, *bottom_chain]))[:20]


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
