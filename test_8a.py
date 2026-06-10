"""
单次扩展调度测试（强化版）

目标：
- 在充电区保留每个充电桩的全部可用空位（即使用 `CHARGING_QUEUE_LEN` 的剩余槽位），
 允许所有空闲槽位被等候车辆一次性占用。
- 等候区放置共 20 辆车：10 辆快充（fast）+ 10 辆慢充（slow），每辆车均设置 `wait_start_time`。
- 调度目标：在两个模式下同时使得被调度进入充电区的所有车辆的累计（等待时间 + 充电时间）之和最小。

方法说明：
- 为了可扩展性，采用按模式分离的位掩码 DP（bitmask DP）来求每种模式的最优分配。
 具体而言：对每种模式（fast/slow），将该模式下的等候车辆用位掩码表示，预计算每个充电桩对任意车辆子集的代价（按桩内短作业优先 SPT 排序计算），
 然后用 DP 在桩之间合并子集以获得在该模式下填满所有空位的最小总代价。最终将两种模式的最优代价相加作为全局最优。
（当车辆数很小时位掩码 DP 可行；这是比原先全排列更具扩展性的实现。）
"""

import sys
import os
import hashlib
from datetime import datetime
import math

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import (
    init_db, get_db, get_all_chargers, get_charger_queue_requests,
    get_request
)
from scheduler import Scheduler
from billing import calculate_charging_duration
from settings import settings


def _insert_request(cursor, user_id, queue_number, mode, amount, status, charger_id, position, time_iso):
    cursor.execute(
        """INSERT INTO requests
           (user_id, queue_number, mode, request_amount, status, charger_id,
            charger_queue_position, created_at, wait_start_time, start_time)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, queue_number, mode, amount, status, charger_id, position, time_iso, time_iso,
         time_iso if status == 'charging' else None)
    )
    return cursor.lastrowid


def run_test():
    print('=' * 70)
    print('测试8a: 单次扩展调度 — 总充电时长最短 验证')
    print('=' * 70)

    # 初始化数据库并清理表
    init_db()
    conn = get_db()
    cur = conn.cursor()
    cur.execute('DELETE FROM bills')
    cur.execute('DELETE FROM system_logs')
    cur.execute('DELETE FROM requests')
    cur.execute('DELETE FROM users WHERE role != "admin"')
    cur.execute("UPDATE chargers SET status='working', total_charges=0, total_duration=0, total_energy=0, total_charge_fee=0, total_service_fee=0, total_fee=0")
    conn.commit()

    # 建立一些测试用户（足够多的用户用于插入大量请求）
    user_ids = []
    for i in range(1, 61):
        cur.execute(
            'INSERT INTO users (username, password_hash, role, battery_capacity) VALUES (?, ?, "user", ?)',
            (f'test_user_{i}', hashlib.sha256(b'123456').hexdigest(), 100)
        )
        user_ids.append(cur.lastrowid)

    conn.commit()

    # 读取充电桩信息
    chargers = {c['charger_no']: dict(c) for c in get_all_chargers()}

    base_time = datetime(2024, 1, 1, 10, 0, 0).isoformat()

    # 构造场景：在每个充电桩上保留其全部可用空位（free_slots = CHARGING_QUEUE_LEN - existing_len）
    # 在部分桩上预置已有正在充电与排队的请求，以产生剩余空位用于本次单次调度测试
    _insert_request(cur, user_ids[0], 'QF1-1', 'fast', 30.0, 'charging', chargers['F1']['id'], 0, base_time)
    _insert_request(cur, user_ids[1], 'QF1-2', 'fast', 10.0, 'queued', chargers['F1']['id'], 1, base_time)
    _insert_request(cur, user_ids[2], 'QF2-1', 'fast', 30.0, 'charging', chargers['F2']['id'], 0, base_time)
    _insert_request(cur, user_ids[3], 'QF2-2', 'fast', 10.0, 'queued', chargers['F2']['id'], 1, base_time)

    _insert_request(cur, user_ids[4], 'QT1-1', 'slow', 20.0, 'charging', chargers['T1']['id'], 0, base_time)
    _insert_request(cur, user_ids[5], 'QT1-2', 'slow', 10.0, 'queued', chargers['T1']['id'], 1, base_time)

    # 等候区车辆：包含快/慢两种模式，共 20 台（10 快 + 10 慢），每台设置 wait_start_time
    waiting_fast_ids = []
    waiting_slow_ids = []
    # 快充等候车辆（数量=10），混合不同充电量以测试 SPT 排序效果
    fast_amounts = [5.0, 10.0, 15.0, 20.0, 25.0, 8.0, 12.0, 30.0, 18.0, 22.0]
    slow_amounts = [10.0, 20.0, 5.0, 25.0, 15.0, 12.0, 18.0, 8.0, 30.0, 16.0]
    idx = 6
    for i in range(10):
        rid = _insert_request(cur, user_ids[idx], f'WF{i+1}', 'fast', fast_amounts[i], 'waiting', None, -1, base_time)
        waiting_fast_ids.append(rid)
        idx += 1

    for i in range(10):
        rid = _insert_request(cur, user_ids[idx], f'WS{i+1}', 'slow', slow_amounts[i], 'waiting', None, -1, base_time)
        waiting_slow_ids.append(rid)
        idx += 1

    conn.commit()
    conn.close()

    s = Scheduler()

    # 防止实际开启线程进行充电：替换 _start_charging 为简单记录状态函数
    original_start = s._start_charging

    def fake_start(request_id, charger_id):
        c = get_db()
        c.execute("UPDATE requests SET status='charging', start_time=? WHERE id=?", (s.get_current_time().isoformat(), request_id))
        c.commit()
        c.close()

    s._start_charging = fake_start

    try:
        # 计算调度前每个充电桩的队列持续时长与可用空位（用于联合穷举）
        from database import get_all_chargers as _gac, get_charger_queue_requests as _gq, get_waiting_requests
        target_chargers = [c for c in _gac() if c['status'] == 'working']

        # 构造每桩的现有持续序列
        def existing_seq(charger_id, power):
            seq = []
            for req in _gq(charger_id):
                if req['status'] == 'charging' and req.get('start_time'):
                    start = datetime.fromisoformat(req['start_time'])
                    elapsed = (s.get_current_time() - start).total_seconds() / 3600
                    total_needed = calculate_charging_duration(req['request_amount'], power)
                    remaining = max(0, total_needed - elapsed)
                    seq.append(remaining)
                else:
                    seq.append(calculate_charging_duration(req['request_amount'], power))
            return seq

        charger_powers = {c['id']: c['power'] for c in target_chargers}
        existing_seq_map = {c['id']: existing_seq(c['id'], c['power']) for c in target_chargers}

        # 将可用槽按模式分组，统计每个桩的空位数
        slots_by_mode = {'fast': [], 'slow': []}
        for c in target_chargers:
            q = _gq(c['id'])
            existing_len = len(q)
            free_slots = settings.get('CHARGING_QUEUE_LEN') - existing_len
            if free_slots <= 0:
                continue
            slots_by_mode[c['type']].append({'charger_id': c['id'], 'free_slots': free_slots, 'power': c['power'], 'pre_seq': list(existing_seq_map[c['id']])})

        waiting = get_waiting_requests()
        waiting_fast = [r for r in waiting if r['mode'] == 'fast']
        waiting_slow = [r for r in waiting if r['mode'] == 'slow']

        # 对每种模式单独做位掩码 DP：车辆数量 ≤ 10 时可行
        def solve_mode(waiting_list, slots_info):
            n = len(waiting_list)
            if n == 0 or len(slots_info) == 0:
                return 0.0, {}

            # 每辆车在每个桩上的处理时间
            durations = []
            for req in waiting_list:
                durations.append([calculate_charging_duration(req['request_amount'], s['power']) for s in slots_info])

            # 每个桩对每个子集的代价预计算（子集大小不能超过 free_slots）
            max_mask = 1 << n
            cost_on_charger = [dict() for _ in range(len(slots_info))]
            for j, s in enumerate(slots_info):
                cap = s['free_slots']
                pre_sum = sum(s['pre_seq'])
                # 遍历所有子集
                for mask in range(max_mask):
                    k = mask.bit_count()
                    if k == 0:
                        cost_on_charger[j][mask] = 0.0
                        continue
                    if k > cap:
                        continue
                    ps = []
                    for i in range(n):
                        if (mask >> i) & 1:
                            ps.append(durations[i][j])
                    ps.sort()
                    # cost = k * pre_sum + sum_{i=0..k-1} (k - i) * ps[i]
                    total = k * pre_sum + sum((k - idx) * p for idx, p in enumerate(ps))
                    cost_on_charger[j][mask] = total

            # DP：按桩逐步合并子集（mask 表示已分配车辆集合）
            dp = {0: 0.0}
            parent = {}
            for j in range(len(slots_info)):
                new_dp = {}
                new_parent = {}
                valid_masks = list(cost_on_charger[j].keys())
                for assigned_mask, base_cost in dp.items():
                    # 可以选择为该桩分配的子集（与已分配无交集）
                    for sub in valid_masks:
                        if assigned_mask & sub:
                            continue
                        new_mask = assigned_mask | sub
                        new_cost = base_cost + cost_on_charger[j][sub]
                        if new_mask not in new_dp or new_cost < new_dp[new_mask]:
                            new_dp[new_mask] = new_cost
                            new_parent[new_mask] = (assigned_mask, j, sub)
                dp = new_dp
                # 合并 parent 链
                parent.update(new_parent)

            # 目标：尽可能填满所有空位（即分配数量 K = min(total_slots, n)）
            total_slots = sum(s['free_slots'] for s in slots_info)
            K = min(total_slots, n)
            best = float('inf')
            best_mask = None
            for mask, cost in dp.items():
                if mask.bit_count() == K and cost < best:
                    best = cost
                    best_mask = mask

            # 重建每桩的分配
            assign_map = {s['charger_id']: [] for s in slots_info}
            if best_mask is None:
                return best, assign_map

            cur = best_mask
            # 反向追踪 parent 链，直到回到 0
            while cur and cur in parent:
                prev_mask, j, sub = parent[cur]
                # 将 sub 中的车辆归到第 j 个桩
                for i in range(n):
                    if (sub >> i) & 1:
                        assign_map[slots_info[j]['charger_id']].append(waiting_list[i]['id'])
                cur = prev_mask

            return best, assign_map

        # 求解两种模式的最优分配并累加
        best_fast_cost, fast_assign = solve_mode(waiting_fast, slots_by_mode.get('fast', []))
        best_slow_cost, slow_assign = solve_mode(waiting_slow, slots_by_mode.get('slow', []))
        best_sum = best_fast_cost + best_slow_cost
        best_mapping = {'fast': fast_assign, 'slow': slow_assign}

        # 触发扩展单次调度（会调用 trigger_scheduling）
        s.set_extended_mode('single')

        # 读取实际分配结果并计算实际总耗时（基于最终队列顺序）
        conn = get_db()
        actual_total = 0.0
        assigned_ids = set(waiting_fast_ids + waiting_slow_ids)
        for c in target_chargers:
            q = get_charger_queue_requests(c['id'])
            # 构造最终队列对应的时长序列
            seq = []
            for req in q:
                if req['status'] == 'charging' and req.get('start_time'):
                    start = datetime.fromisoformat(req['start_time'])
                    elapsed = (s.get_current_time() - start).total_seconds() / 3600
                    total_needed = calculate_charging_duration(req['request_amount'], c['power'])
                    remaining = max(0, total_needed - elapsed)
                    seq.append(remaining)
                else:
                    seq.append(calculate_charging_duration(req['request_amount'], c['power']))

            # 扫描队列，找到我们原先等候车辆被分配到的位置并计算其等待+充电时间
            for i, req in enumerate(q):
                if req['id'] in assigned_ids:
                    wait_time = sum(seq[:i])
                    own_time = calculate_charging_duration(req['request_amount'], c['power'])
                    actual_total += wait_time + own_time

        conn.close()

        print(f'穷举最小总耗时 = {best_sum:.6f} 小时')
        print(f'实际分配总耗时 = {actual_total:.6f} 小时')

        # 验证实际分配达到全局最小（允许微小浮点误差）
        assert math.isclose(actual_total, best_sum, rel_tol=1e-6, abs_tol=1e-6), (
            f'分配非最优：实际 {actual_total:.6f}，最优 {best_sum:.6f}'
        )

        print('  ✅ 单次扩展调度最短总时长测试通过')
        return True
    finally:
        s._start_charging = original_start
        # 恢复扩展调度模式，避免影响其他测试
        try:
            s.set_extended_mode(None)
        except Exception:
            pass


if __name__ == '__main__':
    ok = run_test()
    sys.exit(0 if ok else 1)
