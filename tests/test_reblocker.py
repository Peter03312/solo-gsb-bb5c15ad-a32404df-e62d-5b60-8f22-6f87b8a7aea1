"""应急改配求解器单元测试（含暴力枚举交叉验证）。"""

import itertools
import random

import pytest

from app.solver import (
    Container,
    InfeasibleStowage,
    Slot,
    solve_reblock,
    solve_stowage,
)

EPS = 1e-7


def _vertical_pairs(slots):
    """与求解器一致：同堆栈按层排序后的相邻上下层对 (lower, upper)。"""
    by_stack = {}
    for s in slots:
        by_stack.setdefault(s.stack, []).append(s)
    pairs = []
    for stack_slots in by_stack.values():
        stack_slots.sort(key=lambda s: s.tier)
        pairs.extend(zip(stack_slots, stack_slots[1:]))
    return pairs


def _feasible(assign, containers, pairs, adj, incompat, min_m, max_m):
    """assign: 箱号 -> Slot；按全部硬约束检查。"""
    occ = {}
    for c in containers:
        s = assign[c.no]
        if c.weight > s.capacity + EPS:
            return False
        occ[s.code] = c
    for lower, upper in pairs:
        cu = occ.get(upper.code)
        cl = occ.get(lower.code)
        if cu is not None and cl is None:
            return False  # 悬空
        if cu is not None and cl is not None:
            if cl.weight < cu.weight - EPS:
                return False  # 下层轻于上层
            if cl.discharge_order < cu.discharge_order:
                return False  # 早卸港被压在下
    for a, b in adj:
        ca, cb = occ.get(a), occ.get(b)
        if ca is not None and cb is not None and ca.hazmat and cb.hazmat:
            if tuple(sorted((ca.hazmat, cb.hazmat))) in incompat:
                return False
    moment = sum(assign[c.no].arm * c.weight for c in containers)
    return min_m - EPS <= moment <= max_m + EPS


def _brute_force(containers, slots, adjacency, incompat, min_m, max_m,
                 original, banned):
    """枚举全部完整配载，按 (换槽数, 最高层, 箱号升序槽序列) 取最优。"""
    pairs = _vertical_pairs(slots)
    adj = {tuple(sorted(p)) for p in adjacency}
    incompat = {tuple(sorted(p)) for p in incompat}
    best_key = None
    best_assign = None
    for perm in itertools.permutations(slots, len(containers)):
        if any(s.code in banned for s in perm):
            continue
        assign = {c.no: s for c, s in zip(containers, perm)}
        if not _feasible(assign, containers, pairs, adj, incompat, min_m, max_m):
            continue
        moves = sum(1 for c in containers if assign[c.no].code != original[c.no])
        top = max(s.tier for s in perm)
        seq = tuple(assign[c.no].code
                    for c in sorted(containers, key=lambda c: c.no))
        key = (moves, top, seq)
        if best_key is None or key < best_key:
            best_key = key
            best_assign = assign
    return best_assign


def _all_feasible(containers, slots, adjacency, incompat, min_m, max_m):
    pairs = _vertical_pairs(slots)
    adj = {tuple(sorted(p)) for p in adjacency}
    incompat = {tuple(sorted(p)) for p in incompat}
    out = []
    for perm in itertools.permutations(slots, len(containers)):
        assign = {c.no: s for c, s in zip(containers, perm)}
        if _feasible(assign, containers, pairs, adj, incompat, min_m, max_m):
            out.append(assign)
    return out


def make_slots(stack_tiers=("A", "B", "C"), tiers=3, cap=100.0, arm=2.0):
    return [
        Slot(f"{s}{t:02d}", s, t, cap, arm)
        for s in stack_tiers
        for t in range(1, tiers + 1)
    ]


def test_banned_unused_slot_keeps_original_plan():
    slots = make_slots()
    containers = [
        Container("C001", 10, None, 1),
        Container("C002", 20, None, 1),
        Container("C003", 30, None, 2),
    ]
    base = solve_stowage(containers, slots, [], [], -1e9, 1e9)
    original = {a.container_no: a.slot_code for a in base.assignments}
    # 封锁未被占用的槽位：原方案仍可行且最优，一箱不动
    r = solve_reblock(containers, slots, [], [], -1e9, 1e9,
                      original=original, banned_slots=["A03"])
    assert {a.container_no: a.slot_code for a in r.assignments} == original
    assert r.max_occupied_tier == base.max_occupied_tier


def test_forced_move_min_moves_then_lex():
    slots = make_slots()
    containers = [
        Container("C001", 10, None, 1),
        Container("C002", 20, None, 1),
        Container("C003", 30, None, 2),
    ]
    base = solve_stowage(containers, slots, [], [], -1e9, 1e9)
    original = {a.container_no: a.slot_code for a in base.assignments}
    assert original == {"C001": "A01", "C002": "B01", "C003": "C01"}
    # 封锁 C01：C003 必须换槽；换槽数最少为 2（C003 无处可单层安放），
    # 同换槽数同层数下取字典序最小：C001 不动，C002->B02，C003->B01
    r = solve_reblock(containers, slots, [], [], -1e9, 1e9,
                      original=original, banned_slots=["C01"])
    mapping = {a.container_no: a.slot_code for a in r.assignments}
    assert mapping == {"C001": "A01", "C002": "B02", "C003": "B01"}
    assert r.max_occupied_tier == 2
    assert "C01" not in mapping.values()


def test_move_count_beats_lower_tier():
    """换槽数优先于最高占用层：宁可多占一层也不多换一箱。"""
    slots = [
        Slot("A01", "A", 1, 100, 2.0),
        Slot("A02", "A", 2, 100, 2.0),
        Slot("B01", "B", 1, 100, 2.0),
        Slot("C01", "C", 1, 100, 2.0),
        Slot("D01", "D", 1, 100, 2.0),
    ]
    adjacency = [("C01", "D01")]
    incompat = [("1", "5")]
    containers = [
        Container("C01", 30, "1", 1),
        Container("C02", 20, "5", 1),
        Container("C03", 40, "1", 1),
    ]
    base = solve_stowage(containers, slots, adjacency, incompat, -1e9, 1e9)
    original = {a.container_no: a.slot_code for a in base.assignments}
    assert original == {"C01": "A01", "C02": "B01", "C03": "C01"}
    # 封锁 B01 后：
    # * 换 1 箱：C02->A02（T=2）——C02 不能去 D01（与 C01 槽的 C03 危险相邻）；
    # * 换 2 箱：C01->D01、C02->A01（T=1）。
    # 规则先最小化换槽数，故应选换 1 箱、T=2 的方案。
    r = solve_reblock(containers, slots, adjacency, incompat, -1e9, 1e9,
                      original=original, banned_slots=["B01"])
    mapping = {a.container_no: a.slot_code for a in r.assignments}
    assert mapping == {"C01": "A01", "C02": "A02", "C03": "C01"}
    assert r.max_occupied_tier == 2


def test_banned_slot_never_occupied_and_capacity_kept():
    slots = [
        Slot("A01", "A", 1, 100, 2.0),
        Slot("B01", "B", 1, 15, 2.0),
        Slot("C01", "C", 1, 100, 2.0),
    ]
    containers = [Container("X", 50, None, 1), Container("Y", 10, None, 1)]
    base = solve_stowage(containers, slots, [], [], -1e9, 1e9)
    original = {a.container_no: a.slot_code for a in base.assignments}
    assert original == {"X": "A01", "Y": "B01"}
    # 封锁 B01：Y 只能去 C01（B01 承重 15 本可行，但已封锁）
    r = solve_reblock(containers, slots, [], [], -1e9, 1e9,
                      original=original, banned_slots=["B01"])
    mapping = {a.container_no: a.slot_code for a in r.assignments}
    assert mapping == {"X": "A01", "Y": "C01"}
    # 若唯一承重足够的槽也被封锁 -> 无解
    with pytest.raises(InfeasibleStowage):
        solve_reblock(containers, slots, [], [], -1e9, 1e9,
                      original=original, banned_slots=["A01", "C01"])


def test_unknown_banned_slot_rejected():
    slots = make_slots()
    containers = [Container("X", 10, None, 1)]
    base = solve_stowage(containers, slots, [], [], -1e9, 1e9)
    original = {a.container_no: a.slot_code for a in base.assignments}
    with pytest.raises(InfeasibleStowage) as exc:
        solve_reblock(containers, slots, [], [], -1e9, 1e9,
                      original=original, banned_slots=["Z99"])
    assert "不属于" in exc.value.reason


def test_too_few_available_slots():
    slots = make_slots(stack_tiers=("A", "B"), tiers=1)
    containers = [Container("X", 10, None, 1), Container("Y", 10, None, 1)]
    base = solve_stowage(containers, slots, [], [], -1e9, 1e9)
    original = {a.container_no: a.slot_code for a in base.assignments}
    with pytest.raises(InfeasibleStowage) as exc:
        solve_reblock(containers, slots, [], [], -1e9, 1e9,
                      original=original, banned_slots=["A01"])
    assert "少于箱数" in exc.value.reason


def test_moment_window_still_enforced():
    slots = make_slots(stack_tiers=("A", "B"), tiers=1, arm=2.0)
    containers = [Container("X", 10, None, 1)]
    base = solve_stowage(containers, slots, [], [], 20, 20)
    original = {a.container_no: a.slot_code for a in base.assignments}
    # 力矩区间恰为 20：X 必须留在力臂 2.0 的槽；两槽力臂相同，封锁 A01 后去 B01
    r = solve_reblock(containers, slots, [], [], 20, 20,
                      original=original, banned_slots=["A01"])
    assert r.assignments[0].slot_code == "B01"
    assert r.total_moment == 20
    # 区间不含任何可行力矩 -> 无解
    with pytest.raises(InfeasibleStowage):
        solve_reblock(containers, slots, [], [], 1000, 2000,
                      original=original, banned_slots=["A01"])


def test_empty_manifest_reblock():
    slots = make_slots()
    r = solve_reblock([], slots, [], [], -1, 1, original={}, banned_slots=["A01"])
    assert r.assignments == [] and r.total_moment == 0 and r.max_occupied_tier == 0
    with pytest.raises(InfeasibleStowage):
        solve_reblock([], slots, [], [], 1, 2, original={}, banned_slots=["A01"])


def test_randomized_against_brute_force():
    """随机小案例：与暴力枚举（同三阶段优选键）逐一比对，含无解情形。"""
    rng = random.Random(20261001)
    checked = 0
    for _ in range(150):
        n_stacks = rng.randint(2, 3)
        slots = []
        for si in range(n_stacks):
            for t in range(1, rng.randint(1, 3) + 1):
                slots.append(Slot(
                    f"S{si}T{t}", f"ST{si}", t,
                    capacity=rng.choice([15.0, 25.0, 50.0, 100.0]),
                    arm=rng.choice([-3.0, -1.0, 2.0, 4.0]),
                ))
        n_boxes = rng.randint(1, min(4, len(slots)))
        containers = [
            Container(
                f"C{i:02d}",
                rng.choice([5.0, 10.0, 15.0, 20.0, 30.0]),
                rng.choice([None, None, "1", "5"]),
                rng.randint(1, 2),
            )
            for i in range(n_boxes)
        ]
        adjacency = []
        for a in slots:
            for b in slots:
                if a.tier == b.tier and a.stack < b.stack and rng.random() < 0.3:
                    adjacency.append((a.code, b.code))
        incompat = [("1", "5")] if rng.random() < 0.5 else []
        if rng.random() < 0.7:
            min_m, max_m = -500.0, 500.0
        else:
            min_m, max_m = -60.0, 60.0

        feasible = _all_feasible(containers, slots, adjacency, incompat,
                                 min_m, max_m)
        if not feasible:
            continue
        original = {
            no: s.code for no, s in rng.choice(feasible).items()
        }
        banned = frozenset(rng.sample(
            [s.code for s in slots], rng.randint(0, min(2, len(slots) - 1))
        ))

        expected = _brute_force(containers, slots, adjacency, incompat,
                                min_m, max_m, original, banned)
        if expected is None:
            with pytest.raises(InfeasibleStowage):
                solve_reblock(containers, slots, adjacency, incompat,
                              min_m, max_m, original=original,
                              banned_slots=banned)
        else:
            r = solve_reblock(containers, slots, adjacency, incompat,
                              min_m, max_m, original=original,
                              banned_slots=banned)
            got = {a.container_no: a.slot_code for a in r.assignments}
            want = {no: s.code for no, s in expected.items()}
            assert got == want, (
                f"containers={containers}\nslots={slots}\nadj={adjacency}\n"
                f"incompat={incompat}\nwindow=({min_m},{max_m})\n"
                f"original={original}\nbanned={banned}\n"
                f"got={got}\nwant={want}"
            )
        checked += 1
    assert checked >= 100  # 确保确有足够样本参与比对
