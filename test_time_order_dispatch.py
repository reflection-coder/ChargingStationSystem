"""
时间顺序调度故障处理测试

场景：
  快充桩1(F1): F1充电中, F4/F7排队
  快充桩2(F2): F2充电中, F5/F8排队
  快充桩3(F3): F3充电中, F6/F9排队  ← 故障
  慢充桩4(T1): F10充电中, F11排队   （不受快充故障影响）

故障策略 time_order：合并同类型未充电车辆按排队号重调度，模拟完成并输出顺序。
"""

import sys
import os
import hashlib
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import init_db, get_db, get_request, get_all_chargers
from scheduler import Scheduler


FAST_QUEUES = [
    ('F1', ['F1', 'F4', 'F7']),
    ('F2', ['F2', 'F5', 'F8']),
    ('F3', ['F3', 'F6', 'F9']),
]
SLOW_QUEUE = ('T1', ['F10', 'F11'])
CHARGE_AMOUNT = 30.0


def _charger_map():
    return {c['charger_no']: dict(c) for c in get_all_chargers()}


def _setup_db(user_ids):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        'UPDATE chargers SET total_charges=0, total_duration=0, '
        'total_energy=0, total_charge_fee=0, total_service_fee=0, total_fee=0'
    )
    conn.commit()

    chargers = _charger_map()
    base_time = datetime(2024, 1, 1, 10, 0, 0).isoformat()
    req_ids = {}

    def insert_req(user_idx, queue_number, mode, charger_no, position, status):
        charger_id = chargers[charger_no]['id']
        cursor.execute(
            """INSERT INTO requests
               (user_id, queue_number, mode, request_amount, status, charger_id,
                charger_queue_position, created_at, wait_start_time, start_time)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_ids[user_idx], queue_number, mode, CHARGE_AMOUNT, status,
                charger_id, position, base_time, base_time,
                base_time if status == 'charging' else None,
            ),
        )
        req_ids[queue_number] = cursor.lastrowid
        return cursor.lastrowid

    user_idx = 0
    for charger_no, queues in FAST_QUEUES:
        for pos, qn in enumerate(queues):
            status = 'charging' if pos == 0 else 'queued'
            insert_req(user_idx, qn, 'fast', charger_no, pos, status)
            user_idx += 1

    slow_no, slow_queues = SLOW_QUEUE
    for pos, qn in enumerate(slow_queues):
        status = 'charging' if pos == 0 else 'queued'
        insert_req(user_idx, qn, 'slow', slow_no, pos, status)
        user_idx += 1

    conn.commit()
    conn.close()
    return req_ids


def _reset_scheduler_state(scheduler):
    scheduler.waiting_area_service = True
    scheduler.fault_handling = False
    scheduler.fault_charger_id = None
    scheduler.fault_strategy = 'priority'
    scheduler.fault_pending_request_ids = []
    scheduler.fault_charger_type = None


def _queue_snapshot(charger_no):
    chargers = _charger_map()
    charger_id = chargers[charger_no]['id']
    conn = get_db()
    rows = conn.execute(
        """SELECT queue_number, status, charger_queue_position
           FROM requests WHERE charger_id = ?
           AND status IN ('charging', 'queued')
           ORDER BY charger_queue_position""",
        (charger_id,),
    ).fetchall()
    conn.close()
    return [f"{r['queue_number']}({r['status']})" for r in rows]


def _waiting_fast():
    conn = get_db()
    rows = conn.execute(
        """SELECT queue_number FROM requests
           WHERE status='waiting' AND mode='fast'
           ORDER BY queue_number"""
    ).fetchall()
    conn.close()
    return [r['queue_number'] for r in rows]


def _active_charging():
    conn = get_db()
    rows = conn.execute(
        """SELECT r.queue_number, r.mode, c.charger_no
           FROM requests r
           LEFT JOIN chargers c ON r.charger_id = c.id
           WHERE r.status='charging'
           ORDER BY r.queue_number"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _complete_one(scheduler, queue_number):
    """将指定排队号的充电请求标记完成并推进队列。"""
    conn = get_db()
    row = conn.execute(
        "SELECT id, charger_id FROM requests WHERE queue_number=? AND status='charging'",
        (queue_number,),
    ).fetchone()
    conn.close()
    if not row:
        return False

    req_id, charger_id = row['id'], row['charger_id']
    scheduler._complete_charging(req_id, charger_id)
    return True


def run_test():
    print('=' * 70)
    print('时间顺序调度故障处理测试')
    print('=' * 70)

    init_db()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM bills')
    cursor.execute('DELETE FROM system_logs')
    cursor.execute('DELETE FROM requests')
    cursor.execute('DELETE FROM users WHERE role != "admin"')
    cursor.execute("UPDATE chargers SET status='working'")
    conn.commit()

    user_ids = []
    for i in range(1, 12):
        cursor.execute(
            'INSERT INTO users (username, password_hash, role, battery_capacity) '
            'VALUES (?, ?, "user", 100)',
            (f'time_order_user_{i}', hashlib.sha256(b'123456').hexdigest()),
        )
        user_ids.append(cursor.lastrowid)
    conn.commit()
    conn.close()

    req_ids = _setup_db(user_ids)
    chargers = _charger_map()
    fault_charger = chargers['F3']

    scheduler = Scheduler()
    original_start = scheduler._start_charging

    def fake_start_charging(request_id, charger_id):
        conn = get_db()
        conn.execute(
            "UPDATE requests SET status='charging', start_time=? WHERE id=?",
            (scheduler.get_current_time().isoformat(), request_id),
        )
        conn.commit()
        conn.close()

    scheduler._start_charging = fake_start_charging
    _reset_scheduler_state(scheduler)

    try:
        print('\n【初始状态】')
        for no in ('F1', 'F2', 'F3'):
            print(f'  快充桩{no[-1]}({no}): {_queue_snapshot(no)}')
        print(f'  慢充桩4(T1): {_queue_snapshot("T1")}')

        print('\n【触发故障】快充桩3 故障，策略=时间顺序调度')
        success, msg = scheduler.set_charger_fault(fault_charger['id'], 'time_order')
        print(f'  结果: {msg}')
        assert success, msg

        f3_req = get_request(req_ids['F3'])
        assert f3_req['status'] == 'cancelled', 'F3 应停止充电并计费结束'
        conn = get_db()
        f3_bills = conn.execute(
            'SELECT COUNT(*) AS n FROM bills WHERE request_id=?', (req_ids['F3'],)
        ).fetchone()['n']
        conn.close()
        assert f3_bills == 1, 'F3 应生成详单'

        print('\n【故障后重调度结果】')
        charger_status = {c['charger_no']: c['status'] for c in get_all_chargers()}
        for no in ('F1', 'F2', 'F3'):
            status = charger_status[no]
            snap = _queue_snapshot(no) if status != 'fault' else []
            print(f'  快充桩{no[-1]}({no}, {status}): {snap}')
        print(f'  等候区快充车辆: {_waiting_fast()}')
        print(f'  慢充桩4(T1): {_queue_snapshot("T1")}（不受影响）')
        print(f'  等候区叫号服务: {"已暂停→已恢复" if scheduler.waiting_area_service else "仍暂停"}')

        assert _queue_snapshot('F1') == ['F1(charging)', 'F4(queued)', 'F5(queued)'], (
            'F4/F5 应分配到快充桩1'
        )
        assert _queue_snapshot('F2') == ['F2(charging)', 'F6(queued)', 'F7(queued)'], (
            'F6/F7 应分配到快充桩2'
        )
        assert _waiting_fast() == ['F8', 'F9'], 'F8/F9 暂无空位应在等候区'
        assert _queue_snapshot('T1') == ['F10(charging)', 'F11(queued)'], '慢充桩队列不变'

        print('\n【模拟充电完成顺序】')
        print('  规则：每次取当前充电中排队号最小的车辆完成，推进队列并触发后续调度')
        completion_log = []
        target_queues = {'F1', 'F2', 'F4', 'F5', 'F6', 'F7', 'F8', 'F9', 'F10', 'F11'}

        for _ in range(30):
            active = _active_charging()
            if not active:
                break

            pick = min(active, key=lambda r: (r['queue_number'][0], int(r['queue_number'][1:])))
            qn, charger_no = pick['queue_number'], pick['charger_no']
            _complete_one(scheduler, qn)

            req = get_request(req_ids[qn])
            if req['status'] == 'completed' and qn in target_queues:
                completion_log.append((qn, charger_no))

            if all(get_request(req_ids[qn])['status'] == 'completed' for qn in target_queues):
                break

        print(f'\n{"序号":<6}{"排队号":<10}{"充电桩":<10}')
        print('-' * 26)
        for i, (qn, charger_no) in enumerate(completion_log, 1):
            label = f'快充桩{charger_no[-1]}' if charger_no.startswith('F') else '慢充桩4'
            print(f'{i:<6}{qn:<10}{label}({charger_no})')

        print(f'\n  F3: 故障中止（不计入完成顺序），已生成详单')
        print('\n【验证】')
        for qn in target_queues:
            req = get_request(req_ids[qn])
            assert req['status'] == 'completed', f'{qn} 应已完成充电'

        # F4/F5 同在快充桩1队列，F6/F7 同在快充桩2队列，故 F5 先于 F6 完成
        expected_order = ['F1', 'F2', 'F4', 'F5', 'F6', 'F7', 'F8', 'F9', 'F10', 'F11']
        actual_order = [qn for qn, _ in completion_log]
        assert actual_order == expected_order, (
            f'完成顺序不符，期望 {expected_order}，实际 {actual_order}'
        )

        expected_chargers = {
            'F1': 'F1', 'F2': 'F2', 'F4': 'F1', 'F5': 'F1',
            'F6': 'F2', 'F7': 'F2', 'F8': 'F1', 'F9': 'F2',
            'F10': 'T1', 'F11': 'T1',
        }
        for qn, charger_no in completion_log:
            assert charger_no == expected_chargers[qn], (
                f'{qn} 应在 {expected_chargers[qn]} 完成，实际 {charger_no}'
            )

        print('  ✅ 时间顺序调度测试通过')
        return True
    finally:
        scheduler._start_charging = original_start


if __name__ == '__main__':
    ok = run_test()
    sys.exit(0 if ok else 1)
