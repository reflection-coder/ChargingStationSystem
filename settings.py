"""
智能充电桩调度计费系统 - 运行时系统设置
支持管理员在线修改并持久化到数据库。
"""

import copy
import json
import threading

from config import (
    FAST_CHARGING_PILE_NUM,
    TRICKLE_CHARGING_PILE_NUM,
    FAST_CHARGING_POWER,
    TRICKLE_CHARGING_POWER,
    WAITING_AREA_SIZE,
    CHARGING_QUEUE_LEN,
    SERVICE_FEE_RATE,
    PRICE_PERIODS,
    SIMULATION_SPEED,
    SCHEDULING_INTERVAL,
)

JSON_KEYS = {'PRICE_PERIODS'}

SCALAR_KEYS = [
    'FAST_CHARGING_PILE_NUM',
    'TRICKLE_CHARGING_PILE_NUM',
    'FAST_CHARGING_POWER',
    'TRICKLE_CHARGING_POWER',
    'WAITING_AREA_SIZE',
    'CHARGING_QUEUE_LEN',
    'SERVICE_FEE_RATE',
    'SIMULATION_SPEED',
    'SCHEDULING_INTERVAL',
]


class SettingsManager:
    """系统设置管理器（单例）"""

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
        self._cache = {}
        self._loaded = False

    def _defaults(self):
        return {
            'FAST_CHARGING_PILE_NUM': FAST_CHARGING_PILE_NUM,
            'TRICKLE_CHARGING_PILE_NUM': TRICKLE_CHARGING_PILE_NUM,
            'FAST_CHARGING_POWER': FAST_CHARGING_POWER,
            'TRICKLE_CHARGING_POWER': TRICKLE_CHARGING_POWER,
            'WAITING_AREA_SIZE': WAITING_AREA_SIZE,
            'CHARGING_QUEUE_LEN': CHARGING_QUEUE_LEN,
            'SERVICE_FEE_RATE': SERVICE_FEE_RATE,
            'SIMULATION_SPEED': SIMULATION_SPEED,
            'SCHEDULING_INTERVAL': SCHEDULING_INTERVAL,
            'PRICE_PERIODS': copy.deepcopy(PRICE_PERIODS),
        }

    def _deserialize(self, key, value):
        if key in JSON_KEYS:
            return json.loads(value)
        if key in ('FAST_CHARGING_PILE_NUM', 'TRICKLE_CHARGING_PILE_NUM',
                   'WAITING_AREA_SIZE', 'CHARGING_QUEUE_LEN', 'SIMULATION_SPEED',
                   'SCHEDULING_INTERVAL'):
            return int(value)
        return float(value)

    def _serialize(self, value):
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    def load(self):
        """从数据库加载设置，若无记录则根据现有数据或 config 初始化"""
        from database import get_all_settings_from_db, get_all_chargers, save_settings_to_db

        db_settings = get_all_settings_from_db()
        if db_settings:
            self._cache = self._defaults()
            for key, value in db_settings.items():
                if key in self._cache:
                    self._cache[key] = self._deserialize(key, value)
        else:
            self._cache = self._defaults()
            chargers = get_all_chargers()
            if chargers:
                fast = [c for c in chargers if c['type'] == 'fast']
                slow = [c for c in chargers if c['type'] == 'slow']
                self._cache['FAST_CHARGING_PILE_NUM'] = len(fast)
                self._cache['TRICKLE_CHARGING_PILE_NUM'] = len(slow)
                if fast:
                    self._cache['FAST_CHARGING_POWER'] = fast[0]['power']
                if slow:
                    self._cache['TRICKLE_CHARGING_POWER'] = slow[0]['power']
            save_settings_to_db({
                k: self._serialize(v) for k, v in self._cache.items()
            })

        self._loaded = True

    def ensure_loaded(self):
        if not self._loaded:
            self.load()

    def get(self, key, default=None):
        self.ensure_loaded()
        return self._cache.get(key, default)

    def get_all(self):
        self.ensure_loaded()
        return copy.deepcopy(self._cache)

    def get_price_periods(self):
        return copy.deepcopy(self.get('PRICE_PERIODS'))

    def _persist(self):
        from database import save_settings_to_db
        save_settings_to_db({
            k: self._serialize(v) for k, v in self._cache.items()
        })

    def _validate(self, updates):
        errors = []

        int_fields = {
            'FAST_CHARGING_PILE_NUM': (1, 20, '快充电桩数量'),
            'TRICKLE_CHARGING_PILE_NUM': (1, 20, '慢充电桩数量'),
            'WAITING_AREA_SIZE': (1, 100, '等候区容量'),
            'CHARGING_QUEUE_LEN': (1, 20, '充电桩队列长度'),
            'SIMULATION_SPEED': (1, 3600, '模拟速度倍率'),
            'SCHEDULING_INTERVAL': (1, 60, '调度检查间隔(秒)'),
        }
        float_fields = {
            'FAST_CHARGING_POWER': (0.1, 500, '快充功率'),
            'TRICKLE_CHARGING_POWER': (0.1, 500, '慢充功率'),
            'SERVICE_FEE_RATE': (0, 100, '服务费单价'),
        }

        for key, (lo, hi, label) in int_fields.items():
            if key not in updates:
                continue
            try:
                val = int(updates[key])
                if not lo <= val <= hi:
                    errors.append(f'{label}须在 {lo}~{hi} 之间')
                else:
                    updates[key] = val
            except (TypeError, ValueError):
                errors.append(f'{label}必须是整数')

        for key, (lo, hi, label) in float_fields.items():
            if key not in updates:
                continue
            try:
                val = float(updates[key])
                if not lo <= val <= hi:
                    errors.append(f'{label}须在 {lo}~{hi} 之间')
                else:
                    updates[key] = val
            except (TypeError, ValueError):
                errors.append(f'{label}必须是数字')

        if 'PRICE_PERIODS' in updates:
            periods = updates['PRICE_PERIODS']
            if not isinstance(periods, dict):
                errors.append('电价时段配置格式无效')
            else:
                for period_key in ('peak', 'normal', 'valley'):
                    if period_key not in periods:
                        errors.append(f'缺少电价时段: {period_key}')
                        continue
                    try:
                        price = float(periods[period_key]['price'])
                        if price <= 0:
                            errors.append(f'{periods[period_key].get("name", period_key)}电价须大于0')
                        periods[period_key]['price'] = price
                    except (KeyError, TypeError, ValueError):
                        errors.append(f'电价时段 {period_key} 配置无效')

        if 'peak_price' in updates or 'normal_price' in updates or 'valley_price' in updates:
            periods = copy.deepcopy(self.get('PRICE_PERIODS'))
            if 'peak_price' in updates:
                periods['peak']['price'] = float(updates.pop('peak_price'))
            if 'normal_price' in updates:
                periods['normal']['price'] = float(updates.pop('normal_price'))
            if 'valley_price' in updates:
                periods['valley']['price'] = float(updates.pop('valley_price'))
            updates['PRICE_PERIODS'] = periods

        return errors

    def update(self, updates):
        """更新设置并同步充电桩"""
        self.ensure_loaded()
        updates = {k: v for k, v in updates.items() if k in self._cache or k in (
            'peak_price', 'normal_price', 'valley_price'
        )}
        if not updates:
            return False, '没有可更新的设置项'

        errors = self._validate(updates)
        if errors:
            return False, '；'.join(errors)

        new_cache = copy.deepcopy(self._cache)
        new_cache.update(updates)

        from database import sync_chargers_with_settings
        ok, msg = sync_chargers_with_settings(
            new_cache['FAST_CHARGING_PILE_NUM'],
            new_cache['TRICKLE_CHARGING_PILE_NUM'],
            new_cache['FAST_CHARGING_POWER'],
            new_cache['TRICKLE_CHARGING_POWER'],
        )
        if not ok:
            return False, msg

        self._cache = new_cache
        self._persist()
        return True, '系统参数已更新并生效'

    def reset_to_defaults(self):
        """恢复 config.py 默认配置"""
        self._cache = self._defaults()
        from database import sync_chargers_with_settings
        ok, msg = sync_chargers_with_settings(
            self._cache['FAST_CHARGING_PILE_NUM'],
            self._cache['TRICKLE_CHARGING_PILE_NUM'],
            self._cache['FAST_CHARGING_POWER'],
            self._cache['TRICKLE_CHARGING_POWER'],
        )
        if not ok:
            return False, msg
        self._persist()
        return True, '已恢复为默认配置'


settings = SettingsManager()
