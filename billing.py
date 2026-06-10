"""
智能充电桩调度计费系统 - 计费模块
处理电价计算、服务费计算、时段判断、跨时段精确分段计费等计费逻辑。
"""

from datetime import datetime, time, timedelta
from settings import settings


def _price_periods():
    return settings.get_price_periods()


def _service_fee_rate():
    return settings.get('SERVICE_FEE_RATE')


def get_current_price_period(dt=None):
    """
    获取当前时间对应的电价时段
    返回: (period_key, price_per_kwh)
    """
    if dt is None:
        dt = datetime.now()
    t = dt.time()
    hour = t.hour

    price_periods = _price_periods()
    for period_key, period_info in price_periods.items():
        for start_hour, end_hour in period_info['hours']:
            if start_hour <= hour < end_hour:
                return period_key, period_info['price']
            # 处理跨午夜的情况 (如23:00~7:00)
            if start_hour > end_hour:
                if hour >= start_hour or hour < end_hour:
                    return period_key, period_info['price']

    # 默认平时
    return 'normal', price_periods['normal']['price']


def get_price_at_time(dt):
    """获取指定时间的电价（元/度）"""
    _, price = get_current_price_period(dt)
    return price


def get_period_for_hour(hour):
    """根据小时数获取所属电价时段和价格"""
    price_periods = _price_periods()
    for period_key, period_info in price_periods.items():
        for start_hour, end_hour in period_info['hours']:
            if start_hour <= hour < end_hour:
                return period_key, period_info['price']
            if start_hour > end_hour:
                if hour >= start_hour or hour < end_hour:
                    return period_key, period_info['price']
    return 'normal', price_periods['normal']['price']


def _next_period_boundary(dt):
    """返回 dt 之后下一个电价时段切换点的 datetime（按时段边界小时跳转）。"""
    price_periods = _price_periods()
    boundaries = set()
    for period_info in price_periods.values():
        for start_h, end_h in period_info['hours']:
            boundaries.add(start_h)
            boundaries.add(end_h % 24)
    current_hour = dt.hour
    future = sorted(b for b in boundaries if b > current_hour)
    if future:
        return dt.replace(hour=future[0], minute=0, second=0, microsecond=0)
    min_b = min(boundaries)
    return (dt + timedelta(days=1)).replace(hour=min_b, minute=0, second=0, microsecond=0)


def calculate_charging_fee_precise(charge_amount_kwh, power_kw, start_time, end_time):
    """
    【精确计费】按各时段实际充电量分段计费

    以电价时段边界为步长跳转（而非逐分钟），复杂度 O(跨越时段数)，
    对任意充电时长都能瞬时完成。

    参数:
        charge_amount_kwh: 总充电度数
        power_kw: 充电功率（度/小时）
        start_time: 充电开始时间 (datetime)
        end_time: 充电结束时间 (datetime)
    返回:
        total_charge_fee: 总充电费（元）
        segment_details: 各时段明细列表
    """
    if power_kw <= 0:
        return 0.0, []

    total_duration_hours = charge_amount_kwh / power_kw
    period_energy = {}
    current = start_time
    remaining_hours = total_duration_hours

    while remaining_hours > 1e-9:
        period_key, _ = get_current_price_period(current)
        next_boundary = _next_period_boundary(current)
        hours_to_boundary = (next_boundary - current).total_seconds() / 3600
        # 防止浮点误差导致 hours_to_boundary <= 0
        if hours_to_boundary <= 0:
            hours_to_boundary = 24.0
        hours_in_segment = min(hours_to_boundary, remaining_hours)
        kwh_in_segment = hours_in_segment * power_kw
        period_energy[period_key] = period_energy.get(period_key, 0) + kwh_in_segment
        remaining_hours -= hours_in_segment
        if remaining_hours > 1e-9:
            current = next_boundary

    price_periods = _price_periods()
    total_charge_fee = 0.0
    segment_details = []
    for period_key, kwh in period_energy.items():
        price = price_periods[period_key]['price']
        fee = round(kwh * price, 2)
        total_charge_fee += fee
        segment_details.append({
            'period': price_periods[period_key]['name'],
            'kwh': round(kwh, 4),
            'price_per_kwh': price,
            'fee': fee
        })

    return round(total_charge_fee, 2), segment_details


def calculate_charging_fee(charge_amount_kwh, start_time, end_time=None):
    """
    计算充电费用（兼容旧接口，内部调用精确计费）

    充电费 = 各时段单位电价 × 该时段充电度数 之和

    参数:
        charge_amount_kwh: 充电度数
        start_time: 充电开始时间 (datetime)
        end_time: 充电结束时间 (datetime)，None时使用开始时间电价简算
    """
    if end_time is None:
        # 简化：使用开始时间的电价
        price_per_kwh = get_price_at_time(start_time)
        return round(charge_amount_kwh * price_per_kwh, 2)

    # 使用精确分段计费
    # 需要知道功率，从充电量和时长反推
    duration_hours = (end_time - start_time).total_seconds() / 3600
    if duration_hours <= 0:
        price_per_kwh = get_price_at_time(start_time)
        return round(charge_amount_kwh * price_per_kwh, 2)

    power_kw = charge_amount_kwh / duration_hours
    total_fee, _ = calculate_charging_fee_precise(
        charge_amount_kwh, power_kw, start_time, end_time
    )
    return total_fee


def calculate_service_fee(charge_amount_kwh):
    """
    计算服务费
    服务费 = 服务费单价 × 充电度数
    """
    return round(charge_amount_kwh * _service_fee_rate(), 2)


def calculate_total_fee(charge_amount_kwh, start_time, end_time=None, power_kw=None):
    """
    计算总费用（精确分段计费版本）
    总费用 = 充电费（分段计费） + 服务费

    参数:
        charge_amount_kwh: 充电度数
        start_time: 开始时间
        end_time: 结束时间
        power_kw: 充电功率，用于精确分段计费
    返回:
        (charge_fee, service_fee, total_fee)
    """
    if end_time is not None and power_kw is not None:
        charge_fee, _ = calculate_charging_fee_precise(
            charge_amount_kwh, power_kw, start_time, end_time
        )
    else:
        charge_fee = calculate_charging_fee(charge_amount_kwh, start_time, end_time)

    service_fee = calculate_service_fee(charge_amount_kwh)
    total_fee = round(charge_fee + service_fee, 2)
    return charge_fee, service_fee, total_fee


def calculate_charging_duration(charge_amount_kwh, power_kw_per_hour):
    """
    计算充电时长（小时）
    充电时长 = 实际充电度数 / 充电功率
    """
    if power_kw_per_hour <= 0:
        return 0
    return charge_amount_kwh / power_kw_per_hour


def format_price_info():
    """获取电价信息描述"""
    info = []
    price_periods = _price_periods()
    for key, period in price_periods.items():
        hours_str = ', '.join(
            f'{s}:00~{e if e != 24 else 24}:00' for s, e in period['hours']
        )
        info.append(f"{period['name']}({period['price']}元/度): {hours_str}")
    info.append(f"服务费单价: {_service_fee_rate()}元/度")
    info.append("注：跨时段充电按各时段实际充电量分段计费")
    return '\n'.join(info)
