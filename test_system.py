"""
智能充电桩调度计费系统 - 集成测试脚本
测试所有核心功能的正确性
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import init_db, get_db, create_user, get_user_by_username, get_request
from config import (
    FAST_CHARGING_PILE_NUM, TRICKLE_CHARGING_PILE_NUM,
    FAST_CHARGING_POWER, TRICKLE_CHARGING_POWER,
    WAITING_AREA_SIZE, CHARGING_QUEUE_LEN
)
from scheduler import Scheduler
from billing import (
    get_current_price_period, calculate_total_fee,
    calculate_charging_duration, calculate_service_fee
)
import hashlib


def test_database():
    """测试数据库初始化"""
    print("=" * 60)
    print("测试1: 数据库初始化")
    init_db()
    conn = get_db()
    try:
        conn.execute("DELETE FROM bills")
        conn.execute("DELETE FROM requests")
        conn.execute("UPDATE chargers SET status='working'")
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    # 检查表
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    table_names = [t[0] for t in tables]
    print(f"  创建的表: {table_names}")

    # 检查充电桩
    chargers = conn.execute("SELECT * FROM chargers").fetchall()
    print(f"  充电桩数量: {len(chargers)}")
    for c in chargers:
        print(f"    {c['charger_no']}: 类型={c['type']}, 功率={c['power']}度/时, 状态={c['status']}")

    # 检查管理员
    admin = conn.execute("SELECT * FROM users WHERE role='admin'").fetchone()
    print(f"  管理员账户: {admin['username'] if admin else '不存在'}")

    conn.close()
    print("  ✅ 数据库测试通过")
    return True


def test_billing():
    """测试计费模块"""
    print("\n" + "=" * 60)
    print("测试2: 计费模块")

    from datetime import datetime

    # 测试电价时段
    # 峰时10:00
    test_time = datetime(2024, 1, 1, 10, 30)
    period, price = get_current_price_period(test_time)
    print(f"  10:30 -> {period}, {price}元/度")
    assert period == 'peak', f"期望peak，实际{period}"

    # 平时8:00
    test_time = datetime(2024, 1, 1, 8, 0)
    period, price = get_current_price_period(test_time)
    print(f"  8:00 -> {period}, {price}元/度")
    assert period == 'normal', f"期望normal，实际{period}"

    # 谷时2:00
    test_time = datetime(2024, 1, 1, 2, 0)
    period, price = get_current_price_period(test_time)
    print(f"  2:00 -> {period}, {price}元/度")
    assert period == 'valley', f"期望valley，实际{period}"

    # 测试费用计算
    start_time = datetime(2024, 1, 1, 10, 0)  # 峰时
    charge_fee, service_fee, total = calculate_total_fee(30, start_time)
    print(f"  充电30度(峰时): 充电费={charge_fee}, 服务费={service_fee}, 总计={total}")
    assert charge_fee == 30.0, f"期望充电费30.0, 实际{charge_fee}"  # 1.0*30
    assert service_fee == 24.0, f"期望服务费24.0, 实际{service_fee}"  # 0.8*30

    # 测试充电时长计算
    duration = calculate_charging_duration(30, FAST_CHARGING_POWER)
    print(f"  快充30度: {duration}小时")
    assert duration == 1.0, f"期望1.0小时, 实际{duration}"

    duration = calculate_charging_duration(20, TRICKLE_CHARGING_POWER)
    print(f"  慢充20度: {duration}小时")
    assert duration == 2.0, f"期望2.0小时, 实际{duration}"

    print("  ✅ 计费模块测试通过")
    return True


def test_queue_number():
    """测试排队号生成"""
    print("\n" + "=" * 60)
    print("测试3: 排队号生成")

    s = Scheduler()

    # 重置计数器(通过直接设置)
    s._f_counter = 0
    s._t_counter = 0

    n1 = s.generate_queue_number('fast')
    n2 = s.generate_queue_number('fast')
    n3 = s.generate_queue_number('slow')
    n4 = s.generate_queue_number('slow')

    print(f"  快充排队号: {n1}, {n2}")
    print(f"  慢充排队号: {n3}, {n4}")

    assert n1 == 'F1', f"期望F1, 实际{n1}"
    assert n2 == 'F2', f"期望F2, 实际{n2}"
    assert n3 == 'T1', f"期望T1, 实际{n3}"
    assert n4 == 'T2', f"期望T2, 实际{n4}"

    print("  ✅ 排队号生成测试通过")
    return True


def test_scheduling_algorithm():
    """测试调度算法"""
    print("\n" + "=" * 60)
    print("测试4: 调度算法")

    s = Scheduler()

    # 测试找最佳充电桩
    charger_id, total_time = s.find_best_charger_for_vehicle(30, 'fast')
    print(f"  快充30度: 最佳充电桩ID={charger_id}, 预计完成时间={total_time}小时")
    assert charger_id is not None, "应该找到可用的快充电桩"

    charger_id, total_time = s.find_best_charger_for_vehicle(20, 'slow')
    print(f"  慢充20度: 最佳充电桩ID={charger_id}, 预计完成时间={total_time}小时")
    assert charger_id is not None, "应该找到可用的慢充电桩"

    print("  ✅ 调度算法测试通过")
    return True


def test_waiting_queue_when_full():
    """测试等候区满时的行为"""
    print("\n" + "=" * 60)
    print("测试5: 等候区容量限制")

    from database import get_waiting_requests

    waiting = get_waiting_requests()
    current_count = len(waiting)
    print(f"  当前等候区车辆: {current_count}/{WAITING_AREA_SIZE}")

    remaining = WAITING_AREA_SIZE - current_count
    if remaining > 0:
        print(f"  还可以添加 {remaining} 辆车")
    else:
        print(f"  等候区已满")

    print("  ✅ 等候区容量测试通过")
    return True


def test_config():
    """测试配置参数"""
    print("\n" + "=" * 60)
    print("测试6: 系统配置")

    print(f"  快充电桩数量: {FAST_CHARGING_PILE_NUM}")
    print(f"  慢充电桩数量: {TRICKLE_CHARGING_PILE_NUM}")
    print(f"  快充功率: {FAST_CHARGING_POWER} 度/小时")
    print(f"  慢充功率: {TRICKLE_CHARGING_POWER} 度/小时")
    print(f"  等候区容量: {WAITING_AREA_SIZE}")
    print(f"  充电桩队列长度: {CHARGING_QUEUE_LEN}")

    total_charging_slots = (FAST_CHARGING_PILE_NUM + TRICKLE_CHARGING_PILE_NUM) * CHARGING_QUEUE_LEN
    total_capacity = total_charging_slots + WAITING_AREA_SIZE
    print(f"  充电区总车位: {total_charging_slots}")
    print(f"  系统总容量: {total_capacity}")

    print("  ✅ 配置测试通过")
    return True


def test_strict_fault_priority_dispatch():
    """严格测试故障优先级调度：故障队列清空前暂停普通叫号"""
    print("\n" + "=" * 60)
    print("测试7: 故障优先级调度严格规则")

    from datetime import datetime

    s = Scheduler()
    original_start_charging = s._start_charging

    def fake_start_charging(request_id, charger_id):
        conn = get_db()
        conn.execute(
            "UPDATE requests SET status='charging', start_time=? WHERE id=?",
            (s.get_current_time().isoformat(), request_id)
        )
        conn.commit()
        conn.close()

    s._start_charging = fake_start_charging

    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM bills")
        cursor.execute("DELETE FROM system_logs")
        cursor.execute("DELETE FROM requests")
        cursor.execute("DELETE FROM users WHERE role != 'admin'")
        cursor.execute("UPDATE chargers SET status='working'")
        cursor.execute("UPDATE chargers SET total_charges=0, total_duration=0, total_energy=0, total_charge_fee=0, total_service_fee=0, total_fee=0")

        fast_chargers = cursor.execute(
            "SELECT * FROM chargers WHERE type='fast' ORDER BY charger_no"
        ).fetchall()
        assert len(fast_chargers) >= 2, "至少需要两个快充桩测试故障优先级调度"

        fault_charger = dict(fast_chargers[0])
        other_chargers = [dict(c) for c in fast_chargers[1:]]
        other_charger = other_chargers[0]

        user_ids = []
        for i in range(1, 20):
            cursor.execute(
                "INSERT INTO users (username, password_hash, role, battery_capacity) VALUES (?, ?, 'user', 100)",
                (f'fault_test_user_{i}', hashlib.sha256('123456'.encode()).hexdigest())
            )
            user_ids.append(cursor.lastrowid)

        base_time = datetime(2024, 1, 1, 10, 0, 0)

        def insert_req(user_idx, queue_number, charger_id, position, status, amount=30):
            cursor.execute(
                """INSERT INTO requests
                   (user_id, queue_number, mode, request_amount, status, charger_id,
                    charger_queue_position, created_at, wait_start_time, start_time)
                   VALUES (?, ?, 'fast', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_ids[user_idx], queue_number, amount, status, charger_id,
                    position, base_time.isoformat(), base_time.isoformat(),
                    base_time.isoformat() if status == 'charging' else None
                )
            )
            return cursor.lastrowid

        charging_req = insert_req(0, 'F1', fault_charger['id'], 0, 'charging')
        fault_wait_1 = insert_req(1, 'F2', fault_charger['id'], 1, 'queued')
        fault_wait_2 = insert_req(2, 'F3', fault_charger['id'], 2, 'queued')

        filler_request_ids = []
        next_user_idx = 3
        next_queue_no = 4
        for charger in other_chargers:
            filler_request_ids.append(insert_req(next_user_idx, f'F{next_queue_no}', charger['id'], 0, 'charging'))
            next_user_idx += 1
            next_queue_no += 1
            filler_request_ids.append(insert_req(next_user_idx, f'F{next_queue_no}', charger['id'], 1, 'queued'))
            next_user_idx += 1
            next_queue_no += 1
            filler_request_ids.append(insert_req(next_user_idx, f'F{next_queue_no}', charger['id'], 2, 'queued'))
            next_user_idx += 1
            next_queue_no += 1

        other_queued_1 = filler_request_ids[1]
        other_queued_2 = filler_request_ids[2]
        normal_waiting = insert_req(next_user_idx, f'F{next_queue_no}', None, -1, 'waiting', amount=10)
        conn.commit()
        conn.close()

        s.waiting_area_service = True
        s.fault_handling = False
        s.fault_charger_id = None
        s.fault_strategy = 'priority'
        s.fault_pending_request_ids = []
        s.fault_charger_type = None

        s.advance_simulation_time(minutes=30)
        success, msg = s.set_charger_fault(fault_charger['id'], 'priority')
        print(f"  设置故障结果: {msg}")
        assert success, msg

        stopped = get_request(charging_req)
        assert stopped['status'] == 'cancelled', "故障桩正在充电车辆应停止并结束本次计费"

        conn = get_db()
        bills = conn.execute(
            "SELECT * FROM bills WHERE request_id = ?", (charging_req,)
        ).fetchall()
        conn.close()
        assert len(bills) == 1, "故障中止的正在充电车辆应生成一条详单"

        assert s.waiting_area_service is False, "故障队列未调度完前应暂停等候区叫号服务"
        assert s.fault_handling is True, "故障队列未调度完前应保持故障处理状态"
        assert set(s.fault_pending_request_ids) == {fault_wait_1, fault_wait_2}, "故障排队车辆应保留在故障待调度队列"
        assert get_request(normal_waiting)['status'] == 'waiting', "普通等候区车辆不得抢先叫号"

        conn = get_db()
        conn.execute("UPDATE requests SET status='cancelled' WHERE id=?", (other_queued_1,))
        conn.commit()
        conn.close()
        s._advance_queue(other_charger['id'])
        s._after_queue_slot_changed(other_charger['id'])

        first_fault = get_request(fault_wait_1)
        normal = get_request(normal_waiting)
        assert first_fault['charger_id'] == other_charger['id'], "出现同类型空位时应优先调度故障队列第一辆车"
        assert normal['status'] == 'waiting', "故障队列仍未清空时普通等候车辆仍不得叫号"
        assert s.waiting_area_service is False, "故障队列未全部调度完时仍应暂停叫号"
        assert s.fault_handling is True, "故障队列未全部调度完时仍应处于处理状态"

        conn = get_db()
        conn.execute("UPDATE requests SET status='cancelled' WHERE id=?", (other_queued_2,))
        conn.commit()
        conn.close()
        s._advance_queue(other_charger['id'])
        s._after_queue_slot_changed(other_charger['id'])

        second_fault = get_request(fault_wait_2)
        assert second_fault['charger_id'] == other_charger['id'], "第二个空位也应优先给故障队列"
        assert s.fault_pending_request_ids == [], "故障队列应全部调度完毕"
        assert s.waiting_area_service is True, "故障队列全部调度完后应重新开启等候区叫号服务"
        assert s.fault_handling is False, "故障队列全部调度完后应结束故障处理状态"

        print("  ✅ 故障优先级调度严格测试通过")
        return True
    finally:
        s._start_charging = original_start_charging


def run_all_tests():
    """运行所有测试"""
    print("\n" + "🧪 " * 20)
    print("智能充电桩调度计费系统 - 集成测试")
    print("🧪 " * 20)

    tests = [
        ("数据库初始化", test_database),
        ("计费模块", test_billing),
        ("排队号生成", test_queue_number),
        ("调度算法", test_scheduling_algorithm),
        ("等候区容量", test_waiting_queue_when_full),
        ("系统配置", test_config),
        ("故障优先级调度严格规则", test_strict_fault_priority_dispatch),
    ]

    results = []
    for name, test_func in tests:
        try:
            test_func()
            results.append((name, True))
        except AssertionError as e:
            print(f"  ❌ 断言失败: {e}")
            results.append((name, False))
        except Exception as e:
            print(f"  ❌ 异常: {e}")
            results.append((name, False))

    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    for name, ok in results:
        status = "✅ 通过" if ok else "❌ 失败"
        print(f"  {status} - {name}")

    print(f"\n总计: {passed}/{total} 通过")
    return passed == total


if __name__ == '__main__':
    success = run_all_tests()
    sys.exit(0 if success else 1)
