"""
7c 故障处理综合测试脚本
============================================================
需求原文（7c）：
  若充电桩出现故障(只考虑单一充电桩故障且正好该充电桩有车排队的情况)，
  则正在被充电的车辆停止计费，本次充电过程对应一条详单。
  此后系统重新为故障队列中的车辆进行调度。
  当充电桩故障恢复，若其它同类型充电桩中尚有车辆排队，
  则暂停等候区叫号服务，将其它同类型充电桩中尚未充电的车辆合为一组，
  按照排队号码先后顺序重新调度。调度完毕后，再重新开启等候区叫号服务。

测试场景：
  A. 时间顺序调度故障处理
  B. 优先级调度故障处理
  C. 故障恢复 —— 其它同类型桩有排队车辆（需暂停叫号重调度）
  D. 故障恢复 —— 其它同类型桩无排队车辆（直接恢复叫号）
"""

import sys
import os
import hashlib
import time as _time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import (
    init_db, get_db, get_request, get_all_chargers,
    update_charger_status, add_log
)
from scheduler import Scheduler

# ─── 公共工具 ────────────────────────────────────────────────

CHARGE_AMOUNT = 30.0
BASE_TIME = datetime(2024, 6, 1, 10, 0, 0)


def _charger_map():
    return {c['charger_no']: dict(c) for c in get_all_chargers()}


def _queue_snapshot(charger_no):
    """返回某充电桩队列快照列表，如 ['F1(charging)', 'F4(queued)']"""
    cm = _charger_map()
    if charger_no not in cm:
        return []
    charger_id = cm[charger_no]['id']
    conn = get_db()
    rows = conn.execute(
        """SELECT queue_number, status, charger_queue_position
           FROM requests WHERE charger_id = ?
             AND status IN ('charging','queued')
           ORDER BY charger_queue_position""",
        (charger_id,),
    ).fetchall()
    conn.close()
    return [f"{r['queue_number']}({r['status']})" for r in rows]


def _waiting_snapshot(mode=None):
    """返回等候区快照列表，按队列号排序"""
    conn = get_db()
    if mode:
        rows = conn.execute(
            "SELECT queue_number FROM requests WHERE status='waiting' AND mode=? "
            "ORDER BY queue_number",
            (mode,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT queue_number FROM requests WHERE status='waiting' ORDER BY queue_number"
        ).fetchall()
    conn.close()
    return [r['queue_number'] for r in rows]


def _bill_count(request_id):
    conn = get_db()
    n = conn.execute(
        "SELECT COUNT(*) FROM bills WHERE request_id=?", (request_id,)
    ).fetchone()[0]
    conn.close()
    return n


def _reset_db_for_test(cursor, conn):
    """清空测试数据（保留管理员账户和充电桩）"""
    # 关闭外键约束便于清空
    cursor.execute("PRAGMA foreign_keys = OFF")
    cursor.execute("DELETE FROM bills")
    cursor.execute("DELETE FROM system_logs")
    cursor.execute("DELETE FROM requests")
    cursor.execute("DELETE FROM users WHERE role != 'admin'")
    cursor.execute("UPDATE chargers SET status='working'")
    cursor.execute(
        "UPDATE chargers SET total_charges=0, total_duration=0, "
        "total_energy=0, total_charge_fee=0, total_service_fee=0, total_fee=0"
    )
    cursor.execute("PRAGMA foreign_keys = ON")
    conn.commit()
    _time.sleep(0.05)  # 让上一个测试的连接充分释放


def _make_users(cursor, conn, n):
    """创建 n 个测试用户，返回 user_id 列表"""
    ids = []
    pw = hashlib.sha256(b"123456").hexdigest()
    for i in range(n):
        cursor.execute(
            "INSERT INTO users (username, password_hash, role, battery_capacity) "
            "VALUES (?, ?, 'user', 100)",
            (f"7c_test_user_{i}_{os.getpid()}", pw),
        )
        ids.append(cursor.lastrowid)
    conn.commit()
    return ids


def _insert_req(cursor, conn, user_id, queue_number, mode, charger_id, position, status):
    bt = BASE_TIME.isoformat()
    cursor.execute(
        """INSERT INTO requests
           (user_id, queue_number, mode, request_amount, status, charger_id,
            charger_queue_position, created_at, wait_start_time, start_time)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user_id, queue_number, mode, CHARGE_AMOUNT, status,
            charger_id, position, bt, bt,
            bt if status == "charging" else None,
        ),
    )
    rid = cursor.lastrowid
    conn.commit()
    return rid


def _reset_scheduler(s):
    s.waiting_area_service = True
    s.fault_handling = False
    s.fault_charger_id = None
    s.fault_strategy = "priority"
    s.fault_pending_request_ids = []
    s.fault_charger_type = None


def _fake_start_charging(s):
    """替换 _start_charging 为无延迟版本（测试用）"""
    original = s._start_charging

    def fake(request_id, charger_id):
        conn = get_db()
        conn.execute(
            "UPDATE requests SET status='charging', start_time=? WHERE id=?",
            (s.get_current_time().isoformat(), request_id),
        )
        conn.commit()
        conn.close()

    s._start_charging = fake
    return original


def _complete_one(s, queue_number, req_ids):
    """直接推进某排队号到完成状态"""
    conn = get_db()
    row = conn.execute(
        "SELECT id, charger_id FROM requests WHERE queue_number=? AND status='charging'",
        (queue_number,),
    ).fetchone()
    conn.close()
    if not row:
        return False
    s._complete_charging(row["id"], row["charger_id"])
    return True


# ─── 测试 A：时间顺序调度故障处理 ─────────────────────────────

def test_A_time_order_fault():
    """
    场景：
      F1 桩: F1(充电中), F4(排队), F7(排队)
      F2 桩: F2(充电中), F5(排队), F8(排队)
      F3 桩: F3(充电中), F6(排队), F9(排队)  ← 发生故障
      T1 桩: T1(充电中)                        （慢充，不受影响）

    预期（时间顺序调度）：
      1. F3 停止计费并生成详单
      2. 合并 F1 排队{F4,F7}, F2 排队{F5,F8}, 故障队{F6,F9}
         按排队号排序: F4,F5,F6,F7,F8,F9
      3. F1 → [F1(c), F4(q), F5(q)]
         F2 → [F2(c), F6(q), F7(q)]
         等候区: F8, F9
      4. 服务暂停 → 调度完毕后恢复
    """
    print("\n" + "=" * 70)
    print("测试 A：时间顺序调度故障处理")
    print("=" * 70)

    init_db()
    conn = get_db()
    cursor = conn.cursor()
    _reset_db_for_test(cursor, conn)
    user_ids = _make_users(cursor, conn, 11)

    cm = _charger_map()
    req_ids = {}

    fast_setup = [
        ("F1", ["F1", "F4", "F7"]),
        ("F2", ["F2", "F5", "F8"]),
        ("F3", ["F3", "F6", "F9"]),
    ]
    slow_setup = [("T1", ["T1"])]

    uidx = 0
    for cno, qnos in fast_setup:
        for pos, qn in enumerate(qnos):
            st = "charging" if pos == 0 else "queued"
            req_ids[qn] = _insert_req(cursor, conn, user_ids[uidx],
                                       qn, "fast", cm[cno]["id"], pos, st)
            uidx += 1
    for cno, qnos in slow_setup:
        for pos, qn in enumerate(qnos):
            st = "charging" if pos == 0 else "queued"
            req_ids[qn] = _insert_req(cursor, conn, user_ids[uidx],
                                       qn, "slow", cm[cno]["id"], pos, st)
            uidx += 1
    conn.close()

    s = Scheduler()
    orig_start = _fake_start_charging(s)
    _reset_scheduler(s)

    try:
        print("\n[初始状态]")
        for cno in ("F1", "F2", "F3", "T1"):
            print(f"  {cno}: {_queue_snapshot(cno)}")

        print("\n[触发故障] F3 桩故障，策略=时间顺序调度")
        ok, msg = s.set_charger_fault(cm["F3"]["id"], "time_order")
        print(f"  结果: {msg}")
        assert ok, f"触发故障失败: {msg}"

        # ── 断言 1：F3 正在充电的请求停止并生成详单
        f3_req = get_request(req_ids["F3"])
        assert f3_req["status"] == "cancelled", \
            f"F3 应为 cancelled，实际 {f3_req['status']}"
        assert _bill_count(req_ids["F3"]) == 1, \
            f"F3 应生成 1 条详单，实际 {_bill_count(req_ids['F3'])}"
        print("  [OK] F3 停止充电，已生成详单")

        # ── 断言 2：重调度结果
        print("\n[故障后重调度结果]")
        for cno in ("F1", "F2", "F3"):
            print(f"  {cno}: {_queue_snapshot(cno)}")
        print(f"  等候区(快充): {_waiting_snapshot('fast')}")
        print(f"  T1: {_queue_snapshot('T1')} （不受影响）")

        assert _queue_snapshot("F1") == ["F1(charging)", "F4(queued)", "F5(queued)"], \
            f"F1 队列不符，实际 {_queue_snapshot('F1')}"
        assert _queue_snapshot("F2") == ["F2(charging)", "F6(queued)", "F7(queued)"], \
            f"F2 队列不符，实际 {_queue_snapshot('F2')}"
        assert _waiting_snapshot("fast") == ["F8", "F9"], \
            f"等候区不符，实际 {_waiting_snapshot('fast')}"
        assert _queue_snapshot("T1") == ["T1(charging)"], \
            f"T1 不应受影响，实际 {_queue_snapshot('T1')}"
        print("  [OK] 合并后按队列号重调度: F1=[F1,F4,F5], F2=[F2,F6,F7], 等候区=[F8,F9]")

        # ── 断言 3：等候区叫号服务恢复
        assert s.waiting_area_service is True, \
            f"叫号服务应已恢复，实际 waiting_area_service={s.waiting_area_service}"
        assert s.fault_handling is False, \
            "fault_handling 应为 False（故障处理完成）"
        print("  [OK] 等候区叫号服务已恢复")

        print("\n[PASS] 测试 A 通过")
        return True

    except AssertionError as e:
        print(f"\n[FAIL] 测试 A 失败: {e}")
        return False
    finally:
        s._start_charging = orig_start


# ─── 测试 B：优先级调度故障处理 ───────────────────────────────

def test_B_priority_fault():
    """
    场景（所有快充桩均满载，F3 发生故障）：
      F1 桩: F1(充电中), F4(排队), F7(排队)  → 队列满(3/3)
      F2 桩: F2(充电中), F5(排队), F8(排队)  → 队列满(3/3)
      F3 桩: F3(充电中), F6(排队), F9(排队)  ← 发生故障，队列满

    预期（优先级调度）：
      1. F3(充电中) 停止计费，生成详单
      2. F6、F9 加入 fault_pending，等候区叫号服务暂停
      3. F1 F2 均满，F6 F9 暂时留在等候区
      4. 模拟 F1 中的 F1 完成充电 → F1 空出 1 位 → F6 优先分配至 F1
      5. F9 仍无位置，服务继续暂停
      6. 模拟 F2 中的 F2 完成充电 → F2 空出 1 位 → F9 优先分配至 F2
      7. fault_pending 清空 → 服务恢复
    """
    print("\n" + "=" * 70)
    print("测试 B：优先级调度故障处理")
    print("=" * 70)

    init_db()
    conn = get_db()
    cursor = conn.cursor()
    _reset_db_for_test(cursor, conn)
    user_ids = _make_users(cursor, conn, 12)

    cm = _charger_map()
    req_ids = {}

    # F1: 满（3/3）
    req_ids["F1"] = _insert_req(cursor, conn, user_ids[0], "F1", "fast", cm["F1"]["id"], 0, "charging")
    req_ids["F4"] = _insert_req(cursor, conn, user_ids[1], "F4", "fast", cm["F1"]["id"], 1, "queued")
    req_ids["F7"] = _insert_req(cursor, conn, user_ids[2], "F7", "fast", cm["F1"]["id"], 2, "queued")
    # F2: 满（3/3）
    req_ids["F2"] = _insert_req(cursor, conn, user_ids[3], "F2", "fast", cm["F2"]["id"], 0, "charging")
    req_ids["F5"] = _insert_req(cursor, conn, user_ids[4], "F5", "fast", cm["F2"]["id"], 1, "queued")
    req_ids["F8"] = _insert_req(cursor, conn, user_ids[5], "F8", "fast", cm["F2"]["id"], 2, "queued")
    # F3 (故障桩): 满（3/3）
    req_ids["F3"] = _insert_req(cursor, conn, user_ids[6], "F3", "fast", cm["F3"]["id"], 0, "charging")
    req_ids["F6"] = _insert_req(cursor, conn, user_ids[7], "F6", "fast", cm["F3"]["id"], 1, "queued")
    req_ids["F9"] = _insert_req(cursor, conn, user_ids[8], "F9", "fast", cm["F3"]["id"], 2, "queued")
    conn.close()

    s = Scheduler()
    orig_start = _fake_start_charging(s)
    _reset_scheduler(s)

    try:
        print("\n[初始状态] 三个快充桩均满")
        for cno in ("F1", "F2", "F3"):
            print(f"  {cno}: {_queue_snapshot(cno)}")

        print("\n[触发故障] F3 桩故障，策略=优先级调度")
        ok, msg = s.set_charger_fault(cm["F3"]["id"], "priority")
        assert ok, f"触发故障失败: {msg}"
        print(f"  结果: {msg}")

        # ── 断言 1：F3 停充，详单生成
        f3_req = get_request(req_ids["F3"])
        assert f3_req["status"] == "cancelled", \
            f"F3 应为 cancelled，实际 {f3_req['status']}"
        assert _bill_count(req_ids["F3"]) == 1, "F3 应有 1 条详单"
        print("  [OK] F3(充电中) 停止充电，已生成详单")

        # ── 断言 2：F6 F9 进 fault_pending，服务暂停（F1 F2 均满，无法立刻分配）
        assert s.waiting_area_service is False, \
            f"叫号服务应已暂停，实际 waiting_area_service={s.waiting_area_service}"
        assert s.fault_handling is True, "fault_handling 应为 True"

        f6_req = get_request(req_ids["F6"])
        f9_req = get_request(req_ids["F9"])
        assert f6_req["status"] == "waiting", \
            f"F6 应为 waiting（F1 F2 已满），实际 {f6_req['status']}"
        assert f9_req["status"] == "waiting", \
            f"F9 应为 waiting（F1 F2 已满），实际 {f9_req['status']}"
        assert len(s.fault_pending_request_ids) == 2, \
            f"fault_pending 应为 2，实际 {len(s.fault_pending_request_ids)}"
        print("  [OK] F6 F9 加入 fault_pending（F1 F2 满），服务已暂停")

        # ── 步骤：模拟 F1 中的 F1 充电完成（腾出 1 位）
        print("\n[模拟 F1 完成充电，F1 桩腾出空位]")
        ok = _complete_one(s, "F1", req_ids)
        assert ok, "模拟 F1 完成充电失败"

        # _complete_charging → _advance_queue(F1) → F4 开始充电 →
        # _after_queue_slot_changed(F1) → _try_schedule_fault_pending() → F6 分配至 F1
        f6_req = get_request(req_ids["F6"])
        print(f"  F6 状态: {f6_req['status']}, charger={f6_req['charger_id']}")
        assert f6_req["status"] == "queued", \
            f"F6 应已分配（queued），实际 {f6_req['status']}"
        assert f6_req["charger_id"] == cm["F1"]["id"], \
            f"F6 应优先分配至 F1，实际 charger_id={f6_req['charger_id']}"
        # F9 仍无位置
        f9_req = get_request(req_ids["F9"])
        assert f9_req["status"] == "waiting", \
            f"F9 应仍为 waiting，实际 {f9_req['status']}"
        assert s.waiting_area_service is False, "F9 未分配，服务应继续暂停"
        print("  [OK] F6 优先分配至 F1，F9 仍等待，服务继续暂停")

        print("\n[当前状态]")
        for cno in ("F1", "F2", "F3"):
            print(f"  {cno}: {_queue_snapshot(cno)}")

        # ── 步骤：模拟 F2 中的 F2 充电完成（腾出最后 1 位）
        print("\n[模拟 F2 完成充电，F2 桩腾出空位]")
        ok = _complete_one(s, "F2", req_ids)
        assert ok, "模拟 F2 完成充电失败"

        f9_req = get_request(req_ids["F9"])
        print(f"  F9 状态: {f9_req['status']}, charger={f9_req['charger_id']}")
        assert f9_req["status"] == "queued", \
            f"F9 应已分配（queued），实际 {f9_req['status']}"
        assert f9_req["charger_id"] == cm["F2"]["id"], \
            f"F9 应分配至 F2，实际 charger_id={f9_req['charger_id']}"
        print("  [OK] F9 在 F2 腾出空位后，优先分配至 F2")

        # ── 断言 3：故障处理完成，叫号服务恢复
        assert s.waiting_area_service is True, \
            f"叫号服务应已恢复，实际 {s.waiting_area_service}"
        assert s.fault_handling is False, "fault_handling 应为 False"
        assert len(s.fault_pending_request_ids) == 0, \
            f"fault_pending 应为空，实际 {s.fault_pending_request_ids}"
        print("  [OK] 故障队列全部分配完毕，等候区叫号服务已恢复")

        print("\n[PASS] 测试 B 通过")
        return True

    except AssertionError as e:
        print(f"\n[FAIL] 测试 B 失败: {e}")
        return False
    finally:
        s._start_charging = orig_start


# ─── 测试 C：故障恢复 —— 其它同类型桩有排队车辆 ────────────────

def test_C_recovery_with_queued():
    """
    场景：
      F1 桩: F1(充电中), F4(排队), F7(排队)
      F2 桩: F2(充电中), F5(排队), F8(排队)
      F3 桩: 处于故障状态（无车辆）

    预期（故障恢复）：
      1. 系统暂停等候区叫号服务
      2. 收集 F1、F2 中所有"尚未充电"的车辆: F4, F5, F7, F8
      3. 按排队号排序: F4, F5, F7, F8
      4. 重新调度到 F1、F2、F3（F3 已恢复）
         F4 → F1(pos1), F5 → F1(pos2, F1 满)
         F7 → F2(pos1), F8 → F2(pos2, F2 满)
         F3 保持空（容量已被 F1/F2 吸收）
      5. 调度完毕后恢复叫号服务
    """
    print("\n" + "=" * 70)
    print("测试 C：故障恢复（其它同类型桩有排队车辆，需重调度）")
    print("=" * 70)

    init_db()
    conn = get_db()
    cursor = conn.cursor()
    _reset_db_for_test(cursor, conn)
    user_ids = _make_users(cursor, conn, 10)

    cm = _charger_map()
    req_ids = {}

    req_ids["F1"] = _insert_req(cursor, conn, user_ids[0], "F1", "fast", cm["F1"]["id"], 0, "charging")
    req_ids["F4"] = _insert_req(cursor, conn, user_ids[1], "F4", "fast", cm["F1"]["id"], 1, "queued")
    req_ids["F7"] = _insert_req(cursor, conn, user_ids[2], "F7", "fast", cm["F1"]["id"], 2, "queued")
    req_ids["F2"] = _insert_req(cursor, conn, user_ids[3], "F2", "fast", cm["F2"]["id"], 0, "charging")
    req_ids["F5"] = _insert_req(cursor, conn, user_ids[4], "F5", "fast", cm["F2"]["id"], 1, "queued")
    req_ids["F8"] = _insert_req(cursor, conn, user_ids[5], "F8", "fast", cm["F2"]["id"], 2, "queued")
    # 将 F3 设为故障
    update_charger_status(cm["F3"]["id"], "fault")
    conn.close()

    s = Scheduler()
    orig_start = _fake_start_charging(s)
    # 故障状态已存数据库，重置调度器内存状态以模拟"故障已在进行中"
    _reset_scheduler(s)
    s.fault_handling = False      # 故障已处理完（进入恢复阶段）
    s.waiting_area_service = True

    try:
        print("\n[初始状态] (F3 已处于故障状态)")
        for cno in ("F1", "F2", "F3"):
            cstatus = _charger_map()[cno]["status"]
            print(f"  {cno}({cstatus}): {_queue_snapshot(cno)}")

        print("\n[执行故障恢复] 恢复 F3")
        ok, msg = s.recover_charger(cm["F3"]["id"])
        print(f"  结果: {msg}")
        assert ok, f"恢复故障失败: {msg}"

        # ── 断言 1：服务恢复（重调度是同步完成的）
        assert s.waiting_area_service is True, \
            f"叫号服务应已恢复，实际 {s.waiting_area_service}"
        assert s.fault_handling is False, "fault_handling 应为 False"
        print("  [OK] 叫号服务已恢复")

        # ── 断言 2：F4 F5 F7 F8 已按排队号顺序重新分配
        print("\n[恢复后重调度结果]")
        for cno in ("F1", "F2", "F3"):
            cstatus = _charger_map()[cno]["status"]
            print(f"  {cno}({cstatus}): {_queue_snapshot(cno)}")

        f4 = get_request(req_ids["F4"])
        f5 = get_request(req_ids["F5"])
        f7 = get_request(req_ids["F7"])
        f8 = get_request(req_ids["F8"])

        assert f4["status"] == "queued", f"F4 应为 queued，实际 {f4['status']}"
        assert f5["status"] == "queued", f"F5 应为 queued，实际 {f5['status']}"
        assert f7["status"] == "queued", f"F7 应为 queued，实际 {f7['status']}"
        assert f8["status"] == "queued", f"F8 应为 queued，实际 {f8['status']}"
        print("  [OK] F4 F5 F7 F8 全部 queued")

        # 按队列号先后顺序分配：F4<F5<F7<F8 → 先到的先占位
        # F4→F1(pos1), F5→F1(pos2), F7→F2(pos1), F8→F2(pos2)
        assert _queue_snapshot("F1") == ["F1(charging)", "F4(queued)", "F5(queued)"], \
            f"F1 队列不符，实际 {_queue_snapshot('F1')}"
        assert _queue_snapshot("F2") == ["F2(charging)", "F7(queued)", "F8(queued)"], \
            f"F2 队列不符，实际 {_queue_snapshot('F2')}"
        print("  [OK] F4 F5 按队列号先后分配至 F1，F7 F8 分配至 F2")

        # F3 现在是空的（所有 queued 车辆被 F1/F2 吸收）
        assert _queue_snapshot("F3") == [], \
            f"F3 应为空，实际 {_queue_snapshot('F3')}"
        print("  [OK] F3 已恢复为工作状态（可接收新请求）")

        # ── 断言 3：F3 可以接收来自等候区的新请求（通过触发调度验证）
        assert _charger_map()["F3"]["status"] == "working", \
            "F3 应为 working 状态"
        print("  [OK] F3 状态已恢复为 working")

        print("\n[PASS] 测试 C 通过")
        return True

    except AssertionError as e:
        print(f"\n[FAIL] 测试 C 失败: {e}")
        return False
    finally:
        s._start_charging = orig_start


# ─── 测试 D：故障恢复 —— 其它同类型桩无排队车辆 ─────────────

def test_D_recovery_without_queued():
    """
    场景：
      F1 桩: F1(充电中)，无排队
      F2 桩: F2(充电中)，无排队
      F3 桩: 处于故障状态（无车辆）

    预期（故障恢复，无需重调度）：
      1. 无其它同类型桩的排队车辆
      2. 直接恢复叫号服务，不触发重调度
    """
    print("\n" + "=" * 70)
    print("测试 D：故障恢复（无排队车辆，直接恢复叫号）")
    print("=" * 70)

    init_db()
    conn = get_db()
    cursor = conn.cursor()
    _reset_db_for_test(cursor, conn)
    user_ids = _make_users(cursor, conn, 5)

    cm = _charger_map()
    req_ids = {}

    req_ids["F1"] = _insert_req(cursor, conn, user_ids[0], "F1", "fast", cm["F1"]["id"], 0, "charging")
    req_ids["F2"] = _insert_req(cursor, conn, user_ids[1], "F2", "fast", cm["F2"]["id"], 0, "charging")
    update_charger_status(cm["F3"]["id"], "fault")
    conn.close()

    s = Scheduler()
    orig_start = _fake_start_charging(s)
    _reset_scheduler(s)
    s.fault_handling = False
    s.waiting_area_service = True

    try:
        print("\n[初始状态]")
        for cno in ("F1", "F2", "F3"):
            cstatus = _charger_map()[cno]["status"]
            print(f"  {cno}({cstatus}): {_queue_snapshot(cno)}")

        print("\n[执行故障恢复] 恢复 F3（其它快充桩无排队车辆）")
        ok, msg = s.recover_charger(cm["F3"]["id"])
        print(f"  结果: {msg}")
        assert ok, f"恢复故障失败: {msg}"

        # ── 断言：直接恢复，无重调度
        assert s.waiting_area_service is True, \
            f"叫号服务应直接恢复，实际 {s.waiting_area_service}"
        assert s.fault_handling is False, "fault_handling 应为 False"
        assert _charger_map()["F3"]["status"] == "working", "F3 应为 working"
        assert _queue_snapshot("F1") == ["F1(charging)"], \
            f"F1 不应有变化，实际 {_queue_snapshot('F1')}"
        assert _queue_snapshot("F2") == ["F2(charging)"], \
            f"F2 不应有变化，实际 {_queue_snapshot('F2')}"

        print("  [OK] 无排队车辆时直接恢复，F1/F2 队列未受影响")
        print("  [OK] F3 恢复为 working 状态")

        print("\n[PASS] 测试 D 通过")
        return True

    except AssertionError as e:
        print(f"\n[FAIL] 测试 D 失败: {e}")
        return False
    finally:
        s._start_charging = orig_start


# ─── 入口 ──────────────────────────────────────────────────

def run_all_tests():
    print("=" * 70)
    print("7c 故障处理综合测试套件")
    print("=" * 70)

    results = []
    tests = [
        ("A: 时间顺序故障调度", test_A_time_order_fault),
        ("B: 优先级故障调度",   test_B_priority_fault),
        ("C: 故障恢复(有排队)", test_C_recovery_with_queued),
        ("D: 故障恢复(无排队)", test_D_recovery_without_queued),
    ]

    for name, fn in tests:
        try:
            passed = fn()
        except Exception as e:
            print(f"\n[ERROR] {name}: {e}")
            import traceback; traceback.print_exc()
            passed = False
        results.append((name, passed))

    print("\n" + "=" * 70)
    print("测试结果汇总")
    print("=" * 70)
    passed_count = 0
    for name, ok in results:
        status = "[PASS]" if ok else "[FAIL]"
        print(f"  {status} {name}")
        if ok:
            passed_count += 1
    print(f"\n总计: {passed_count}/{len(results)} 通过")
    return passed_count == len(results)


if __name__ == "__main__":
    all_ok = run_all_tests()
    sys.exit(0 if all_ok else 1)
