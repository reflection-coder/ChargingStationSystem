"""
智能充电桩调度计费系统 - 调度模块
核心调度算法：排队叫号(FIFO)、调度策略(最短完成时间选桩)、
故障处理(优先级/时间顺序)、扩展调度(单次/批量)
"""

import threading
import time as time_module
from datetime import datetime, timedelta
from settings import settings
from database import (
    get_db, get_all_chargers, get_charger, get_charger_by_no,
    get_waiting_requests, get_charger_queue_requests,
    get_request, get_active_requests,
    update_request, update_charger_status, update_charger_stats,
    create_bill, add_log
)
from billing import calculate_total_fee, calculate_charging_duration


class Scheduler:
    """充电站调度器 - 单例模式"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        # 等候区叫号服务开关
        self.waiting_area_service = True
        # 故障处理标志
        self.fault_handling = False
        # 故障充电桩ID
        self.fault_charger_id = None
        # 故障处理策略: 'priority' 或 'time_order'
        self.fault_strategy = 'priority'
        # 故障优先级调度中尚未重新分配的请求ID
        self.fault_pending_request_ids = []
        # 故障充电桩类型
        self.fault_charger_type = None
        # 扩展调度模式: None / 'single' / 'batch'
        self.extended_schedule_mode = None
        # 调度线程
        self._scheduler_thread = None
        self._stop_scheduler = False
        # 充电线程管理
        self._charging_threads = {}
        # 模拟当前时间（加速）
        self.simulation_time = datetime.now()
        self._time_lock = threading.Lock()
        # 队列号计数器
        self._f_counter = 0
        self._t_counter = 0
        self._counter_lock = threading.Lock()

    # ==================== 时间管理 ====================

    def get_current_time(self):
        """获取当前模拟时间"""
        with self._time_lock:
            return self.simulation_time

    def advance_time(self, minutes=1):
        """推进模拟时间"""
        with self._time_lock:
            self.simulation_time += timedelta(minutes=minutes)

    def set_simulation_time(self, target_time):
        """设置模拟时间到指定时刻"""
        with self._time_lock:
            self.simulation_time = target_time

    def reset_simulation_time(self):
        """将模拟时间重置为当前真实时间"""
        with self._time_lock:
            self.simulation_time = datetime.now()

    def advance_simulation_time(self, hours=0, minutes=0):
        """按小时/分钟推进模拟时间"""
        if hours == 0 and minutes == 0:
            return
        with self._time_lock:
            self.simulation_time += timedelta(hours=hours, minutes=minutes)

    def jump_to_time_of_day(self, hour, minute=0):
        """跳转到当天指定时刻（保留当前日期）"""
        with self._time_lock:
            self.simulation_time = self.simulation_time.replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )

    def get_simulation_info(self):
        """获取模拟时间与电价时段信息"""
        from billing import get_current_price_period
        current = self.get_current_time()
        period_key, price = get_current_price_period(current)
        periods = settings.get_price_periods()
        period_name = periods.get(period_key, {}).get('name', period_key)
        speed = settings.get('SIMULATION_SPEED')
        return {
            'simulation_time': current.strftime('%Y-%m-%d %H:%M:%S'),
            'simulation_time_iso': current.isoformat(),
            'real_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'period_key': period_key,
            'period_name': period_name,
            'current_price': price,
            'simulation_speed': speed,
            'speed_hint': f'真实 1 秒 ≈ 模拟 {max(1, speed // 60)} 分钟' if speed >= 60 else f'真实 1 秒 ≈ 模拟 {speed} 秒',
        }

    # ==================== 排队号生成 ====================

    def generate_queue_number(self, mode):
        """生成排队号码（FIFO顺序）"""
        with self._counter_lock:
            if mode == 'fast':
                self._f_counter += 1
                return f'F{self._f_counter}'
            else:
                self._t_counter += 1
                return f'T{self._t_counter}'

    def reset_queue_number(self, mode):
        """
        修改模式时重新生成排队号
        新号码排在修改后对应模式类型队列的最后一位
        """
        with self._counter_lock:
            if mode == 'fast':
                self._f_counter += 1
                return f'F{self._f_counter}'
            else:
                self._t_counter += 1
                return f'T{self._t_counter}'

    # ==================== 核心调度 ====================

    def get_charger_power(self, charger_type):
        """获取充电桩功率"""
        if charger_type == 'fast':
            return settings.get('FAST_CHARGING_POWER')
        return settings.get('TRICKLE_CHARGING_POWER')

    def calculate_wait_time_for_charger(self, charger_id):
        """
        计算某个充电桩队列中所有车辆的完成充电时间之和（等待时间）
        """
        queue = get_charger_queue_requests(charger_id)
        charger = get_charger(charger_id)
        if not charger:
            return 0

        power = charger['power']
        total_wait = 0

        for req in queue:
            if req['status'] == 'charging':
                if req['start_time']:
                    start = datetime.fromisoformat(req['start_time'])
                    current = self.get_current_time()
                    elapsed_hours = (current - start).total_seconds() / 3600
                    total_needed = calculate_charging_duration(
                        req['request_amount'], power
                    )
                    remaining = max(0, total_needed - elapsed_hours)
                    total_wait += remaining
                else:
                    total_wait += calculate_charging_duration(
                        req['request_amount'], power
                    )
            else:
                total_wait += calculate_charging_duration(
                    req['request_amount'], power
                )

        return total_wait

    def find_best_charger_for_vehicle(self, request_amount, mode):
        """
        【调度策略】为指定车辆找到最佳充电桩

        策略：对应匹配充电模式下，被调度车辆完成充电所需时长
        （等待时间 + 自己充电时间）最短。

        返回: (charger_id, total_time) 或 (None, None)
        """
        charger_type_map = {'fast': 'fast', 'slow': 'slow'}
        charger_type = charger_type_map.get(mode, 'slow')
        power = self.get_charger_power(charger_type)
        own_charging_time = calculate_charging_duration(request_amount, power)

        all_chargers = get_all_chargers()
        best_charger_id = None
        best_total_time = float('inf')

        for charger in all_chargers:
            if charger['type'] != charger_type:
                continue
            if charger['status'] != 'working':
                continue

            queue = get_charger_queue_requests(charger['id'])
            if len(queue) >= settings.get('CHARGING_QUEUE_LEN'):
                continue

            wait_time = self.calculate_wait_time_for_charger(charger['id'])
            total_time = wait_time + own_charging_time

            if total_time < best_total_time:
                best_total_time = total_time
                best_charger_id = charger['id']

        return best_charger_id, best_total_time if best_charger_id else None

    def trigger_scheduling(self):
        """
        【核心叫号调度】触发调度检查。

        需求对齐：
        - 当任意充电桩队列存在空位时，开始叫号
        - 从等候区选取「排队号码和该充电桩模式匹配的第一辆车」（FIFO）
        - 按照调度策略（最短完成时间）选择最佳充电桩分配
        - 先到先服务，不跳过排队号靠前的车
        """
        if not self.waiting_area_service:
            return

        if self.fault_handling:
            return

        # 扩展调度模式
        if self.extended_schedule_mode == 'single':
            self.extended_schedule_single()
            return
        elif self.extended_schedule_mode == 'batch':
            self.extended_schedule_batch()
            return

        # 标准调度：对每种充电桩类型，按FIFO叫号
        for ctype in ('fast', 'slow'):
            mode = 'fast' if ctype == 'fast' else 'slow'

            # 获取有空位的工作充电桩
            available_chargers = []
            for charger in get_all_chargers():
                if charger['type'] != ctype:
                    continue
                if charger['status'] != 'working':
                    continue
                queue = get_charger_queue_requests(charger['id'])
                if len(queue) < settings.get('CHARGING_QUEUE_LEN'):
                    available_chargers.append(charger)

            if not available_chargers:
                continue

            # 获取等候区匹配模式的车辆（按排队号FIFO顺序）
            waiting = get_waiting_requests(mode)
            if not waiting:
                continue

            # 对等候区中每辆车（FIFO），逐个调度
            for w_req in waiting:
                if w_req['status'] != 'waiting':
                    continue

                # 检查是否还有可用的充电桩
                still_available = []
                for charger in available_chargers:
                    queue = get_charger_queue_requests(charger['id'])
                    if len(queue) < settings.get('CHARGING_QUEUE_LEN'):
                        still_available.append(charger)

                if not still_available:
                    break

                # 用调度策略选择最佳充电桩
                charger_id, total_time = self.find_best_charger_for_vehicle(
                    w_req['request_amount'], w_req['mode']
                )

                if charger_id:
                    self._assign_to_charger(w_req['id'], charger_id)
                    add_log('schedule',
                            f'叫号: {w_req["queue_number"]} → '
                            f'充电桩{get_charger(charger_id)["charger_no"]}, '
                            f'预计完成{total_time:.2f}小时')

    def _assign_to_charger(self, request_id, charger_id):
        """将请求分配到充电桩队列末尾"""
        req = get_request(request_id)
        charger = get_charger(charger_id)
        if not req or not charger:
            return False

        queue = get_charger_queue_requests(charger_id)
        position = len(queue)  # 排在队列末尾

        update_request(request_id,
                       charger_id=charger_id,
                       charger_queue_position=position,
                       status='queued')

        add_log('assign',
                f'{req["queue_number"]} → 充电桩{charger["charger_no"]}, 位置{position}')

        if position == 0:
            self._start_charging(request_id, charger_id)

        return True

    # ==================== 充电管理 ====================

    def _start_charging(self, request_id, charger_id):
        """开始充电"""
        req = get_request(request_id)
        charger = get_charger(charger_id)
        if not req or not charger:
            return

        current_time = self.get_current_time()
        update_request(request_id,
                       status='charging',
                       start_time=current_time.isoformat())

        add_log('charging_start',
                f'{req["queue_number"]} 开始充电, '
                f'充电桩{charger["charger_no"]}, 请求{req["request_amount"]}度')

        charging_hours = calculate_charging_duration(
            req['request_amount'], charger['power']
        )

        # 安排充电完成
        real_seconds = charging_hours * 3600 / settings.get('SIMULATION_SPEED')

        def complete_task():
            time_module.sleep(real_seconds)
            self._complete_charging(request_id, charger_id)

        thread = threading.Thread(target=complete_task, daemon=True)
        thread.start()
        self._charging_threads[request_id] = thread

    def _complete_charging(self, request_id, charger_id):
        """完成充电"""
        req = get_request(request_id)
        charger = get_charger(charger_id)

        if not req or req['status'] != 'charging':
            return

        start_time = datetime.fromisoformat(req['start_time'])
        charging_hours = calculate_charging_duration(
            req['request_amount'], charger['power']
        )
        self.advance_time(minutes=charging_hours * 60)
        actual_end_time = self.get_current_time()
        actual_amount = req['request_amount']

        # 精确分段计费
        charge_fee, service_fee, total_fee = calculate_total_fee(
            actual_amount, start_time, actual_end_time, power_kw=charger['power']
        )

        update_request(request_id,
                       status='completed',
                       end_time=actual_end_time.isoformat(),
                       actual_amount=actual_amount,
                       charge_fee=charge_fee,
                       service_fee=service_fee,
                       total_fee=total_fee)

        create_bill(request_id, req['user_id'], charger_id,
                     charger['charger_no'], actual_amount, charging_hours,
                     start_time.isoformat(), actual_end_time.isoformat(),
                     charge_fee, service_fee, total_fee, req['mode'])

        update_charger_stats(charger_id, charging_hours, actual_amount,
                            charge_fee, service_fee)

        add_log('charging_complete',
                f'{req["queue_number"]} 完成, {actual_amount}度, '
                f'{charging_hours:.2f}时, 总费用{total_fee}元')

        self._charging_threads.pop(request_id, None)
        self._advance_queue(charger_id)
        self._after_queue_slot_changed(charger_id)

    def _advance_queue(self, charger_id):
        """推进充电桩队列"""
        queue = get_charger_queue_requests(charger_id)
        remaining = [r for r in queue if r['status'] in ('queued', 'charging')]

        for i, req in enumerate(remaining):
            update_request(req['id'], charger_queue_position=i)
            if i == 0 and req['status'] == 'queued':
                self._start_charging(req['id'], charger_id)

    # ==================== 用户操作 ====================

    def submit_request(self, user_id, mode, request_amount):
        """提交充电请求（含电池容量校验）"""
        waiting = get_waiting_requests()
        if len(waiting) >= settings.get('WAITING_AREA_SIZE'):
            return None, "等候区已满，无法提交请求"

        # 电池容量校验：请求充电量不能超过电池总容量
        from database import create_request as db_create_request, get_user_by_id
        user = get_user_by_id(user_id)
        if user and user.get('battery_capacity', 0) > 0:
            if request_amount > user['battery_capacity']:
                return None, f"请求充电量({request_amount}度)超过电池总容量({user['battery_capacity']}度)"

        request_id = db_create_request(user_id, mode, request_amount)

        queue_number = self.generate_queue_number(mode)
        update_request(request_id, queue_number=queue_number)

        add_log('request_submit',
                f'用户{user_id} 提交{mode}充电, '
                f'排队号{queue_number}, {request_amount}度')

        self.trigger_scheduling()
        return request_id, queue_number

    def modify_mode(self, request_id, new_mode):
        """
        修改充电模式（仅等候区允许）
        修改后重新生成排队号，排到新模式队列最后一位
        """
        req = get_request(request_id)
        if not req:
            return False, "请求不存在"

        if req['status'] != 'waiting':
            return False, "仅等候区允许修改充电模式，请先取消当前充电后重新排队"

        # 重新生成排队号（自动排到新模式队列最后）
        new_queue_number = self.reset_queue_number(new_mode)
        # 更新等待起始时间，确保在等候区按新时间排序
        update_request(request_id,
                       mode=new_mode,
                       queue_number=new_queue_number,
                       wait_start_time=self.get_current_time().isoformat())

        add_log('mode_change',
                f'{req["queue_number"]} → {new_mode}模式, 新号{new_queue_number}')

        self.trigger_scheduling()
        return True, f"模式已修改为{new_mode}，新排队号: {new_queue_number}（已排到新模式队列末尾）"

    def modify_amount(self, request_id, new_amount):
        """
        修改请求充电量（严格仅限等候区）
        需求：允许在等候区修改，排队号不变；不允许在充电区修改
        """
        req = get_request(request_id)
        if not req:
            return False, "请求不存在"

        # 【修复4】严格限制：仅 waiting 状态可修改
        if req['status'] != 'waiting':
            return False, "仅等候区允许修改充电量。当前在充电区，请取消后重新排队。"

        update_request(request_id, request_amount=new_amount)

        add_log('amount_change',
                f'{req["queue_number"]} 修改充电量为{new_amount}度')

        self.trigger_scheduling()
        return True, f"充电量已修改为{new_amount}度，排队号不变"

    def cancel_request(self, request_id):
        """取消充电请求（等候区、充电区均允许）"""
        req = get_request(request_id)
        if not req:
            return False, "请求不存在"

        if req['status'] not in ('waiting', 'queued', 'charging'):
            return False, "当前状态不允许取消"

        old_status = req['status']
        charger_id = req['charger_id']

        if old_status == 'charging':
            self._cancel_charging_with_billing(request_id)
        else:
            update_request(request_id, status='cancelled',
                          end_time=self.get_current_time().isoformat())

        add_log('request_cancel',
                f'{req["queue_number"]} 取消, 原状态: {old_status}')

        if charger_id and old_status in ('queued', 'charging'):
            self._advance_queue(charger_id)
            self._after_queue_slot_changed(charger_id)
        else:
            self.trigger_scheduling()
        return True, "已取消充电请求"

    def _cancel_charging_with_billing(self, request_id):
        """取消正在充电的请求并精确计费生成详单"""
        req = get_request(request_id)
        if not req:
            return

        end_time = self.get_current_time()
        start_time = datetime.fromisoformat(req['start_time'])
        charger = get_charger(req['charger_id'])

        elapsed_hours = (end_time - start_time).total_seconds() / 3600
        actual_amount = round(elapsed_hours * charger['power'], 2) if charger else 0

        # 精确分段计费
        charge_fee, service_fee, total_fee = calculate_total_fee(
            actual_amount, start_time, end_time,
            power_kw=charger['power'] if charger else None
        )

        update_request(request_id,
                       status='cancelled',
                       end_time=end_time.isoformat(),
                       actual_amount=actual_amount,
                       charge_fee=charge_fee,
                       service_fee=service_fee,
                       total_fee=total_fee)

        create_bill(request_id, req['user_id'], req['charger_id'],
                     charger['charger_no'] if charger else '',
                     actual_amount, elapsed_hours,
                     start_time.isoformat(), end_time.isoformat(),
                     charge_fee, service_fee, total_fee, req['mode'])

        if charger:
            update_charger_stats(req['charger_id'], elapsed_hours, actual_amount,
                                charge_fee, service_fee)

    def end_charging(self, request_id):
        """用户主动结束充电"""
        req = get_request(request_id)
        if not req or req['status'] != 'charging':
            return False, "当前不在充电状态"

        self._cancel_charging_with_billing(request_id)
        self._advance_queue(req['charger_id'])
        self._after_queue_slot_changed(req['charger_id'])
        return True, "已结束充电"

    # ==================== 故障处理 ====================

    def set_charger_fault(self, charger_id, strategy='priority'):
        """
        设置充电桩故障
        strategy: 'priority' (优先级调度) 或 'time_order' (时间顺序调度)
        """
        charger = get_charger(charger_id)
        if not charger:
            return False, "充电桩不存在"
        if charger['status'] == 'fault':
            return False, "充电桩已处于故障状态"

        update_charger_status(charger_id, 'fault')
        add_log('charger_fault',
                f'充电桩{charger["charger_no"]}故障, 策略: {strategy}')

        # 停止正在充电的车辆并生成详单
        fault_queue = get_charger_queue_requests(charger_id)
        for req in fault_queue:
            if req['status'] == 'charging':
                self._cancel_charging_with_billing(req['id'])
                add_log('fault_charging_stop',
                        f'故障导致{req["queue_number"]}停止充电, 已生成详单')
                fault_queue = get_charger_queue_requests(charger_id)
                break

        self.waiting_area_service = False
        self.fault_handling = True
        self.fault_charger_id = charger_id
        self.fault_strategy = strategy
        self.fault_pending_request_ids = []
        self.fault_charger_type = charger['type']

        if strategy == 'priority':
            self._handle_fault_priority(charger, fault_queue)
        elif strategy == 'time_order':
            self._handle_fault_time_order(charger, fault_queue)

        return True, f"充电桩{charger['charger_no']}已设为故障"

    def _handle_fault_priority(self, fault_charger, fault_queue):
        """优先级调度"""
        charger_type = fault_charger['type']
        self.fault_pending_request_ids = [
            req['id'] for req in fault_queue
            if req['status'] not in ('completed', 'cancelled')
        ]
        self.fault_charger_type = charger_type

        for request_id in self.fault_pending_request_ids:
            update_request(request_id,
                           charger_id=None,
                           charger_queue_position=-1,
                           status='waiting')

        self._try_schedule_fault_pending()

    def _try_schedule_fault_pending(self):
        """尝试把故障待调度车辆优先分配到其它同类型充电桩空位（最短完成时间选桩）。"""
        if not self.fault_handling or self.fault_strategy != 'priority':
            return False
        if not self.fault_pending_request_ids:
            self._complete_fault_handling()
            return True

        fault_charger = get_charger(self.fault_charger_id) if self.fault_charger_id else None
        charger_type = self.fault_charger_type or (fault_charger['type'] if fault_charger else None)
        if not charger_type:
            self._complete_fault_handling()
            return False

        scheduled_any = False
        remaining_ids = []

        for request_id in list(self.fault_pending_request_ids):
            req = get_request(request_id)
            if not req or req['status'] in ('completed', 'cancelled'):
                continue

            # 用"最短完成时间"策略为故障车辆选择最优充电桩（排除故障桩自身）
            power = self.get_charger_power(charger_type)
            own_time = calculate_charging_duration(req['request_amount'], power)
            best_charger_id = None
            best_total_time = float('inf')

            for charger in get_all_chargers():
                if charger['type'] != charger_type:
                    continue
                if charger['status'] != 'working':
                    continue
                if charger['id'] == self.fault_charger_id:
                    continue
                queue = get_charger_queue_requests(charger['id'])
                if len(queue) >= settings.get('CHARGING_QUEUE_LEN'):
                    continue
                wait_time = self.calculate_wait_time_for_charger(charger['id'])
                total_time = wait_time + own_time
                if total_time < best_total_time:
                    best_total_time = total_time
                    best_charger_id = charger['id']

            if best_charger_id:
                charger = get_charger(best_charger_id)
                queue = get_charger_queue_requests(best_charger_id)
                position = len(queue)
                update_request(request_id,
                               charger_id=best_charger_id,
                               charger_queue_position=position,
                               status='queued')
                add_log('fault_reschedule',
                        f'{req["queue_number"]} 故障优先调度至{charger["charger_no"]}, '
                        f'预计{best_total_time:.2f}小时完成')
                if position == 0:
                    self._start_charging(request_id, best_charger_id)
                scheduled_any = True
            else:
                update_request(request_id,
                               charger_id=None,
                               charger_queue_position=-1,
                               status='waiting')
                remaining_ids.append(request_id)

        self.fault_pending_request_ids = remaining_ids
        self._check_fault_handling_complete(charger_type)
        return scheduled_any

    def _after_queue_slot_changed(self, charger_id):
        """充电桩队列出现变化后，优先处理故障队列，再恢复普通叫号。"""
        charger = get_charger(charger_id) if charger_id else None
        if (self.fault_handling and self.fault_strategy == 'priority'
                and (not charger or charger['type'] == self.fault_charger_type)):
            self._try_schedule_fault_pending()
            return
        self.trigger_scheduling()

    def _handle_fault_time_order(self, fault_charger, fault_queue):
        """时间顺序调度"""
        charger_type = fault_charger['type']
        all_uncharged = []
        seen_ids = set()
        for c in get_all_chargers():
            if c['type'] != charger_type or c['id'] == fault_charger['id']:
                continue
            for req in get_charger_queue_requests(c['id']):
                if req['status'] == 'queued' and req['id'] not in seen_ids:
                    all_uncharged.append(req)
                    seen_ids.add(req['id'])
        for req in fault_queue:
            if req['status'] not in ('completed', 'cancelled') and req['id'] not in seen_ids:
                all_uncharged.append(req)
                seen_ids.add(req['id'])

        def sort_key(req):
            qn = req['queue_number']
            return (qn[0], int(qn[1:]))

        all_uncharged.sort(key=sort_key)

        for req in all_uncharged:
            update_request(req['id'], charger_id=None,
                          charger_queue_position=-1, status='waiting')

        available = [
            c for c in get_all_chargers()
            if c['type'] == charger_type and c['status'] == 'working'
        ]
        overflow_ids = []
        for req in all_uncharged:
            for charger in available:
                queue = get_charger_queue_requests(charger['id'])
                if len(queue) < settings.get('CHARGING_QUEUE_LEN'):
                    position = len(queue)
                    update_request(req['id'], charger_id=charger['id'],
                                   charger_queue_position=position, status='queued')
                    if position == 0:
                        self._start_charging(req['id'], charger['id'])
                    break
            else:
                update_request(req['id'], status='waiting')
                overflow_ids.append(req['id'])

        # 溢出到等待区的车辆记入故障候队列，保持 fault_handling=True 等待管理员手动恢复。
        # 恢复时 recover_charger() 会将其与排队车辆合并重调度。
        self.fault_pending_request_ids = overflow_ids
        add_log('fault_time_order',
                f'时间顺序重调度完成，{len(all_uncharged)}辆参与，'
                f'{len(overflow_ids)}辆暂入故障候队列等待恢复')
        # 在时间顺序调度后，检查是否可以结束故障处理并恢复叫号服务
        # （若故障桩队列已为空或无需管理员手动恢复，则自动恢复）
        self._check_fault_handling_complete(charger_type)

    def _check_fault_handling_complete(self, charger_type):
        """
        检查故障处理是否完成：
        - 优先级调度：故障待调度队列清空后才恢复叫号服务。
        - 时间顺序调度：同步完成重调度，立即检查故障桩队列为空则完成。
        """
        if self.fault_strategy == 'priority':
            # 清理已取消/完成的请求
            self.fault_pending_request_ids = [
                rid for rid in self.fault_pending_request_ids
                if (get_request(rid)
                    and get_request(rid)['status'] not in ('completed', 'cancelled'))
            ]
            if not self.fault_pending_request_ids:
                add_log('fault_check', '优先级调度：故障队列已全部重新调度，恢复叫号')
                self._complete_fault_handling()
                return

            available_slots = sum(
                max(0, settings.get('CHARGING_QUEUE_LEN') - len(get_charger_queue_requests(c['id'])))
                for c in get_all_chargers()
                if c['type'] == charger_type and c['status'] == 'working'
            )
            add_log('fault_check',
                    f'优先级调度：故障队列仍有{len(self.fault_pending_request_ids)}辆待分配, '
                    f'当前同类型空位{available_slots}个, 继续暂停叫号')
            return

        # 时间顺序调度：重调度是同步的，故障桩队列已清空即可完成
        if not self.fault_charger_id:
            self._complete_fault_handling()
            return

        # 故障桩的队列（其车辆已被移走，应为空）
        pending_in_fault_charger = [
            r for r in get_charger_queue_requests(self.fault_charger_id)
            if r['status'] not in ('completed', 'cancelled')
        ]
        if not pending_in_fault_charger:
            add_log('fault_check', '时间顺序调度：故障队列重调度完毕，恢复叫号')
            self._complete_fault_handling()
        else:
            add_log('fault_check',
                    f'时间顺序调度：故障桩队列仍有{len(pending_in_fault_charger)}辆, 继续等待')

    def recover_charger(self, charger_id):
        """
        充电桩故障恢复。
        仅在其他同类型充电桩中尚有车辆排队时，
        才暂停叫号并将未充电车辆合并按排队号重新调度。
        否则直接恢复叫号即可。
        """
        charger = get_charger(charger_id)
        if not charger or charger['status'] != 'fault':
            return False, "充电桩未处于故障状态"

        update_charger_status(charger_id, 'working')
        add_log('charger_recover', f'充电桩{charger["charger_no"]}故障恢复')

        charger_type = charger['type']

        # ① 收集同类型其它充电桩中尚未开始充电的排队车辆
        all_uncharged = []
        seen_ids = set()
        for c in get_all_chargers():
            if c['type'] != charger_type:
                continue
            for req in get_charger_queue_requests(c['id']):
                if req['status'] == 'queued' and req['id'] not in seen_ids:
                    all_uncharged.append(req)
                    seen_ids.add(req['id'])

        # ② 将故障候队列中仍在等待区的车辆也纳入合并
        #    （优先级调度时桩满无法立即分配的车辆，或时间顺序调度溢出到等待区的车辆）
        for request_id in list(self.fault_pending_request_ids):
            if request_id in seen_ids:
                continue
            req = get_request(request_id)
            if req and req['status'] == 'waiting':
                all_uncharged.append(req)
                seen_ids.add(request_id)

        if not all_uncharged:
            # 无需重调度，直接恢复叫号
            add_log('charger_recover',
                    f'充电桩{charger["charger_no"]}恢复, 无排队/候队车辆需重调度, 直接恢复叫号')
            self._complete_fault_handling()
            self.trigger_scheduling()
            return True, f"充电桩{charger['charger_no']}已恢复（无需重调度）"

        # 暂停叫号，合并重调度
        self.fault_handling = True
        self.waiting_area_service = False

        def sort_key(req):
            qn = req['queue_number']
            return (qn[0], int(qn[1:]))

        all_uncharged.sort(key=sort_key)

        # 先统一清除原有充电桩分配
        for req in all_uncharged:
            update_request(req['id'], charger_id=None,
                          charger_queue_position=-1, status='waiting')

        # 按队列号顺序重新分配到所有可用同类型充电桩（含刚恢复的故障桩）
        available = [
            c for c in get_all_chargers()
            if c['type'] == charger_type and c['status'] == 'working'
        ]
        for req in all_uncharged:
            for charger_c in available:
                queue = get_charger_queue_requests(charger_c['id'])
                if len(queue) < settings.get('CHARGING_QUEUE_LEN'):
                    position = len(queue)
                    update_request(req['id'], charger_id=charger_c['id'],
                                   charger_queue_position=position, status='queued')
                    if position == 0:
                        self._start_charging(req['id'], charger_c['id'])
                    break
            else:
                update_request(req['id'], status='waiting')

        add_log('charger_recover',
                f'充电桩{charger["charger_no"]}恢复, 合并重调度{len(all_uncharged)}辆')
        self._complete_fault_handling()
        return True, f"充电桩{charger['charger_no']}已恢复，{len(all_uncharged)}辆车已按队列号重新调度"

    def _complete_fault_handling(self):
        """完成故障处理，恢复等候区叫号服务"""
        self.fault_handling = False
        self.fault_charger_id = None
        self.fault_pending_request_ids = []
        self.fault_charger_type = None
        self.waiting_area_service = True
        add_log('fault_handling_complete', '故障处理完成，恢复叫号')
        self.trigger_scheduling()

    # ==================== 扩展调度（选做） ====================

    def set_extended_mode(self, mode):
        """
        设置扩展调度模式
        mode: None(标准) / 'single'(单次调度) / 'batch'(批量调度)
        """
        self.extended_schedule_mode = mode
        if mode:
            add_log('extended_mode', f'启用扩展调度模式: {mode}')
            self.trigger_scheduling()
        else:
            add_log('extended_mode', '恢复标准调度模式')

    def extended_schedule_single(self):
        """
        【扩展调度a】单次调度总充电时长最短
        当充电区出现多个车辆空位时，一次同时叫多个号，
        不考虑排队先后顺序，满足进入充电区的多辆车完成充电总时长最短。
        调度策略：(1)按充电模式分配对应充电桩 (2)总时长最短
        """
        # 统计各类型充电桩的空位数
        fast_free_slots = 0
        slow_free_slots = 0
        fast_chargers_list = []
        slow_chargers_list = []

        for charger in get_all_chargers():
            if charger['status'] != 'working':
                continue
            queue = get_charger_queue_requests(charger['id'])
            free = settings.get('CHARGING_QUEUE_LEN') - len(queue)
            if charger['type'] == 'fast':
                fast_free_slots += free
                fast_chargers_list.append((charger, free))
            else:
                slow_free_slots += free
                slow_chargers_list.append((charger, free))

        if fast_free_slots + slow_free_slots <= 1:
            return

        waiting_fast = get_waiting_requests('fast')
        waiting_slow = get_waiting_requests('slow')

        # 联合分配：在小规模下对两种模式的等候车辆一并求解全局最优分配，
        # 车辆只能分配到与其模式匹配的充电桩上；若规模过大则回退到按模式分配。
        self._extended_assign_combined(waiting_fast, waiting_slow, fast_chargers_list, slow_chargers_list)

    def _extended_assign_combined(self, waiting_fast, waiting_slow, fast_chargers, slow_chargers):
        """联合扩展调度：同时考虑快/慢等候车辆与所有可用空位，求全局最优分配。

        - 在小规模下枚举穷举以求全局最优（车辆数或空位数超过阈值则回退到分模式分配）。
        - 车辆仅能被分配到同模式的充电桩。
        - 每桩内被分配车辆采用短作业优先(SPT)排序以最小化桩内完成时间之和。
        """
        import itertools

        waiting = [v for v in (waiting_fast or []) + (waiting_slow or []) if v.get('status') == 'waiting']
        if not waiting:
            return

        # 构造充电桩列表
        chargers = []  # list of (charger_dict, free_slots)
        chargers.extend([c for c in fast_chargers if c[1] > 0])
        chargers.extend([c for c in slow_chargers if c[1] > 0])
        if not chargers:
            return

        total_free = sum(free for _, free in chargers)
        k = min(total_free, len(waiting))

        # 当规模较大时回退到分别按模式的贪心分配，以保持性能和现有行为
        MAX_ENUM_VEHICLES = 8
        MAX_ENUM_SLOTS = 8
        if len(waiting) > MAX_ENUM_VEHICLES or total_free > MAX_ENUM_SLOTS:
            self._extended_assign(waiting_fast, fast_chargers, 'fast')
            self._extended_assign(waiting_slow, slow_chargers, 'slow')
            return

        # 准备结构
        charger_objs = [c for c, _ in chargers]
        charger_caps = [free for _, free in chargers]
        charger_powers = {c['id']: c['power'] for c in charger_objs}
        charger_load = {c['id']: self.calculate_wait_time_for_charger(c['id']) for c in charger_objs}

        best_cost = float('inf')
        best_plan = None

        # 枚举选择 k 辆车辆
        for combo in itertools.combinations(waiting, k):
            # 为每辆车列出可选的充电桩索引（仅同模式）
            allowed_indices = [ [i for i, c in enumerate(charger_objs) if c['type'] == veh['mode']] for veh in combo ]
            # 若任意车辆无可选桩，则跳过
            if any(len(opts) == 0 for opts in allowed_indices):
                continue

            # 枚举每辆车分配到哪台桩（索引），并检查容量约束
            for assign_indices in itertools.product(*allowed_indices):
                counts = [0] * len(charger_objs)
                valid = True
                for idx in assign_indices:
                    counts[idx] += 1
                    if counts[idx] > charger_caps[idx]:
                        valid = False
                        break
                if not valid:
                    continue

                per_charger = {charger_objs[i]['id']: [] for i in range(len(charger_objs))}
                for veh, idx in zip(combo, assign_indices):
                    per_charger[charger_objs[idx]['id']].append(veh)

                # 计算总成本
                total_cost = 0.0
                for cid, assigned in per_charger.items():
                    t = len(assigned)
                    if t == 0:
                        continue
                    power = charger_powers[cid]
                    proc_times = [calculate_charging_duration(v['request_amount'], power) for v in assigned]
                    proc_times.sort()
                    L = charger_load[cid]
                    total_cost += t * L + sum((t - i) * proc_times[i] for i in range(t))

                if total_cost < best_cost:
                    best_cost = total_cost
                    best_plan = per_charger

        if not best_plan:
            return

        # 执行分配：按每桩的 SPT 顺序分配
        assigned_count = 0
        for cid, vehs in best_plan.items():
            if not vehs:
                continue
            power = charger_powers[cid]
            vehs_sorted = sorted(vehs, key=lambda v: calculate_charging_duration(v['request_amount'], power))
            for v in vehs_sorted:
                queue = get_charger_queue_requests(cid)
                if len(queue) >= settings.get('CHARGING_QUEUE_LEN'):
                    continue
                self._assign_to_charger(v['id'], cid)
                assigned_count += 1

        if assigned_count:
            add_log('extended_single', f'单次扩展联合调度(最优): {assigned_count}辆')

    def _extended_assign(self, waiting_vehicles, chargers_with_free, charger_type):
        """扩展调度分配：枚举搜索在小规模下求全局最优分配

        算法说明：
        - 将每个充电桩的可用空位视为可被填充的容量（按桩分组），
          选择若干等候车辆（数量等于可用空位或等候车辆数的最小值）并分配到各桩。
        - 对于每个桩内被分配的车辆，采用短作业优先(SPT)排序以最小化该桩上被分配车辆的完成时间之和。
        - 在所有可行的车辆-桩分配中穷举（通常规模很小），选取使
          被分配车辆的(等待时间+充电时间)总和最小的方案并按该方案执行分配。
        - 为避免组合爆炸，当等待车辆或空位过多时，会退回到原先的贪心策略。
        """

        import itertools

        # 过滤并准备数据
        waiting = [v for v in waiting_vehicles if v.get('status') == 'waiting']
        if not waiting:
            return

        # 准备可用充电桩列表 (charger_dict, free_slots)
        chargers = [(c, free) for (c, free) in chargers_with_free if free > 0]
        if not chargers:
            return

        total_free = sum(free for _, free in chargers)
        # 要填充的车辆数（尽量填满所有空位）
        k = min(total_free, len(waiting))

        # 当规模较大时回退到贪心以避免性能问题（阈值可调整）
        MAX_ENUM_VEHICLES = 8
        MAX_ENUM_SLOTS = 8
        if len(waiting) > MAX_ENUM_VEHICLES or total_free > MAX_ENUM_SLOTS:
            # 回退到原贪心策略
            power = self.get_charger_power(charger_type)
            assignments = []
            for vehicle in waiting:
                own_time = calculate_charging_duration(vehicle['request_amount'], power)
                for charger, free_slots in chargers:
                    wait_time = self.calculate_wait_time_for_charger(charger['id'])
                    assignments.append((wait_time + own_time, vehicle['id'], charger['id']))
            assignments.sort(key=lambda x: x[0])
            assigned_vehicles = set()
            for _, vehicle_id, charger_id in assignments:
                if vehicle_id in assigned_vehicles:
                    continue
                queue = get_charger_queue_requests(charger_id)
                if len(queue) >= settings.get('CHARGING_QUEUE_LEN'):
                    continue
                self._assign_to_charger(vehicle_id, charger_id)
                assigned_vehicles.add(vehicle_id)
            if assigned_vehicles:
                add_log('extended_single', f'单次扩展调度(贪心回退): {len(assigned_vehicles)}辆{charger_type}车')
            return

        # 枚举选择 k 辆车辆并分配到各桩
        vehicle_list = waiting
        charger_objs = [c for c, _ in chargers]
        charger_caps = [free for _, free in chargers]
        charger_powers = {c['id']: c['power'] for c in charger_objs}

        # 预计算每桩当前负载（现有队列的总剩余时长）
        charger_load = {c['id']: self.calculate_wait_time_for_charger(c['id']) for c in charger_objs}

        best_cost = float('inf')
        best_plan = None  # mapping: charger_id -> list of vehicle dicts

        # 枚举车辆组合（选择 k 辆）
        for combo in itertools.combinations(vehicle_list, k):
            # 为这 k 辆车枚举分配到 m 桩的映射（带容量约束）
            m = len(charger_objs)
            # 使用笛卡尔积遍历每辆车分配到哪个桩（值为 0..m-1）
            for assign_indices in itertools.product(range(m), repeat=k):
                # 容量检查
                counts = [0] * m
                valid = True
                for idx in assign_indices:
                    counts[idx] += 1
                    if counts[idx] > charger_caps[idx]:
                        valid = False
                        break
                if not valid:
                    continue

                # 构建每桩的被分配车辆列表
                per_charger = {charger_objs[i]['id']: [] for i in range(m)}
                for veh, idx in zip(combo, assign_indices):
                    per_charger[charger_objs[idx]['id']].append(veh)

                # 计算该分配的总成本（基于 SPT 在每桩内排序）
                total_cost = 0.0
                for cid, assigned in per_charger.items():
                    t = len(assigned)
                    if t == 0:
                        continue
                    power = charger_powers[cid]
                    # 计算每辆车在该桩上的处理时间
                    proc_times = [calculate_charging_duration(v['request_amount'], power) for v in assigned]
                    proc_times.sort()
                    L = charger_load[cid]
                    # 成本 = t * L + sum_{i=0..t-1} (t-i) * proc_times[i]
                    total_cost += t * L + sum((t - i) * proc_times[i] for i in range(t))

                if total_cost < best_cost:
                    best_cost = total_cost
                    best_plan = per_charger

        # 未找到可行方案则返回
        if not best_plan:
            return

        # 执行分配：对每个桩按 SPT 顺序分配车辆（确保排队顺序最优）
        assigned_count = 0
        for cid, vehs in best_plan.items():
            if not vehs:
                continue
            power = charger_powers[cid]
            vehs_sorted = sorted(vehs, key=lambda v: calculate_charging_duration(v['request_amount'], power))
            for v in vehs_sorted:
                # double check slot still available
                queue = get_charger_queue_requests(cid)
                if len(queue) >= settings.get('CHARGING_QUEUE_LEN'):
                    continue
                self._assign_to_charger(v['id'], cid)
                assigned_count += 1

        if assigned_count:
            add_log('extended_single', f'单次扩展调度(最优): {assigned_count}辆{charger_type}车')

    def extended_schedule_batch(self):
        """
        【扩展调度b】批量调度总充电时长最短
        当到达充电站的车辆等于全部车位数量时，进行一次批量调度。
        不区分快充和慢充模式，所有车辆可分配任意类型充电桩。
        调度策略：(1)所有车辆均可分配任意类型充电桩 (2)总时长最短
        """
        total_capacity = (
            (settings.get('FAST_CHARGING_PILE_NUM') + settings.get('TRICKLE_CHARGING_PILE_NUM'))
            * settings.get('CHARGING_QUEUE_LEN')
            + settings.get('WAITING_AREA_SIZE')
        )
        active = get_active_requests()

        if len(active) < total_capacity:
            return

        all_vehicles = [r for r in active if r['status'] == 'waiting']
        if not all_vehicles:
            return

        all_chargers = [c for c in get_all_chargers() if c['status'] == 'working']

        # 清空队列中未充电车辆
        for charger in all_chargers:
            queue = get_charger_queue_requests(charger['id'])
            for req in queue:
                if req['status'] == 'queued':
                    update_request(req['id'], charger_id=None,
                                  charger_queue_position=-1, status='waiting')
                    all_vehicles.append(req)

        # 贪心分配：大电量优先分配到快充桩
        all_vehicles.sort(key=lambda v: v['request_amount'], reverse=True)
        charger_loads = {c['id']: 0.0 for c in all_chargers}

        for vehicle in all_vehicles:
            best_charger_id = None
            best_completion = float('inf')

            for charger in all_chargers:
                queue = get_charger_queue_requests(charger['id'])
                if len(queue) >= settings.get('CHARGING_QUEUE_LEN'):
                    continue
                own_time = calculate_charging_duration(
                    vehicle['request_amount'], charger['power']
                )
                completion = charger_loads[charger['id']] + own_time
                if completion < best_completion:
                    best_completion = completion
                    best_charger_id = charger['id']

            if best_charger_id:
                charger_loads[best_charger_id] += calculate_charging_duration(
                    vehicle['request_amount'],
                    get_charger(best_charger_id)['power']
                )
                position = len(get_charger_queue_requests(best_charger_id))
                update_request(vehicle['id'], charger_id=best_charger_id,
                              charger_queue_position=position, status='queued')
                if position == 0:
                    self._start_charging(vehicle['id'], best_charger_id)
            else:
                update_request(vehicle['id'], status='waiting')

        add_log('batch_schedule', f'批量调度完成, {len(all_vehicles)}辆车')

    # ==================== 后台调度线程 ====================

    def start_scheduler_thread(self):
        """启动后台调度线程"""
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            return
        self._stop_scheduler = False
        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop, daemon=True
        )
        self._scheduler_thread.start()

    def stop_scheduler_thread(self):
        """停止后台调度线程"""
        self._stop_scheduler = True
        if self._scheduler_thread:
            self._scheduler_thread.join(timeout=5)

    def _scheduler_loop(self):
        """调度循环"""
        while not self._stop_scheduler:
            try:
                self.trigger_scheduling()
            except Exception as e:
                add_log('scheduler_error', str(e))
            time_module.sleep(settings.get('SCHEDULING_INTERVAL'))

    # ==================== 状态查询 ====================

    def get_waiting_count_by_mode(self, mode):
        """获取指定模式的等候车辆数"""
        return len(get_waiting_requests(mode))

    def get_waiting_position(self, request_id):
        """获取车辆在等候区中的位置（同模式下的前车数量）"""
        req = get_request(request_id)
        if not req:
            return -1
        waiting = get_waiting_requests(req['mode'])
        for i, w in enumerate(waiting):
            if w['id'] == request_id:
                return i
        return -1

    def get_charger_queue_info(self, charger_id):
        """获取充电桩队列详细信息（含电池总容量）"""
        charger = get_charger(charger_id)
        if not charger:
            return None

        queue = get_charger_queue_requests(charger_id)
        queue_info = []
        for req in queue:
            from database import get_user_by_id
            user = get_user_by_id(req['user_id'])
            wait_start = datetime.fromisoformat(req['wait_start_time']) if req.get('wait_start_time') else None
            current = self.get_current_time()
            wait_duration = (current - wait_start).total_seconds() / 3600 if wait_start else 0

            queue_info.append({
                'queue_number': req['queue_number'],
                'user_id': req['user_id'],
                'username': user['username'] if user else 'Unknown',
                'battery_capacity': user['battery_capacity'] if user else 60.0,
                'request_amount': req['request_amount'],
                'status': req['status'],
                'position': req['charger_queue_position'],
                'wait_hours': round(wait_duration, 2)
            })

        return {
            'id': charger['id'],
            'charger_no': charger['charger_no'],
            'type': charger['type'],
            'power': charger['power'],
            'status': charger['status'],
            'queue_length': len(queue),
            'max_queue': settings.get('CHARGING_QUEUE_LEN'),
            'vehicles': queue_info
        }

    def get_all_charger_queues(self):
        """获取所有充电桩队列信息"""
        result = []
        for charger in get_all_chargers():
            result.append(self.get_charger_queue_info(charger['id']))
        return result

    def get_system_snapshot(self):
        """获取系统完整快照（供故障演示/测试用）"""
        chargers = get_all_chargers()
        snapshot = {
            'chargers': [],
            'waiting_area': [],
            'fault_state': {
                'fault_handling': self.fault_handling,
                'waiting_area_service': self.waiting_area_service,
                'fault_charger_id': self.fault_charger_id,
                'fault_strategy': self.fault_strategy,
                'fault_pending_count': len(self.fault_pending_request_ids),
            },
            'simulation_time': self.get_current_time().strftime('%Y-%m-%d %H:%M:%S'),
        }
        for c in chargers:
            queue = get_charger_queue_requests(c['id'])
            snapshot['chargers'].append({
                'charger_no': c['charger_no'],
                'type': c['type'],
                'power': c['power'],
                'status': c['status'],
                'queue': [
                    {
                        'queue_number': r['queue_number'],
                        'status': r['status'],
                        'request_amount': r['request_amount'],
                        'position': r['charger_queue_position'],
                    }
                    for r in queue
                ],
            })
        from database import get_waiting_requests as _gwr
        for r in _gwr():
            snapshot['waiting_area'].append({
                'queue_number': r['queue_number'],
                'mode': r['mode'],
                'request_amount': r['request_amount'],
            })
        return snapshot


# 全局调度器实例
scheduler = Scheduler()
