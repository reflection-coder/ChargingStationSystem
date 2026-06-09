"""
智能充电桩调度计费系统 - 数据库模块
使用SQLite数据库，管理所有数据表及基本CRUD操作。
"""

import sqlite3
import os
from datetime import datetime
from config import DATABASE_PATH


def get_db():
    """获取数据库连接"""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """初始化数据库，创建所有表"""
    conn = get_db()
    cursor = conn.cursor()

    # 用户表（含车辆信息：电池总容量）
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            battery_capacity REAL DEFAULT 60.0,
            vehicle_model TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 充电桩表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS chargers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            charger_no TEXT UNIQUE NOT NULL,
            type TEXT NOT NULL,
            power REAL NOT NULL,
            status TEXT DEFAULT 'working',
            total_charges INTEGER DEFAULT 0,
            total_duration REAL DEFAULT 0,
            total_energy REAL DEFAULT 0,
            total_charge_fee REAL DEFAULT 0,
            total_service_fee REAL DEFAULT 0,
            total_fee REAL DEFAULT 0
        )
    ''')

    # 充电请求表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            queue_number TEXT,
            mode TEXT NOT NULL,
            request_amount REAL NOT NULL,
            actual_amount REAL DEFAULT 0,
            status TEXT DEFAULT 'waiting',
            charger_id INTEGER,
            charger_queue_position INTEGER DEFAULT -1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            start_time TIMESTAMP,
            end_time TIMESTAMP,
            charge_fee REAL DEFAULT 0,
            service_fee REAL DEFAULT 0,
            total_fee REAL DEFAULT 0,
            wait_start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')

    # 详单表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bill_no TEXT UNIQUE NOT NULL,
            request_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            charger_id INTEGER,
            charger_no TEXT,
            generated_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            charge_amount REAL,
            charge_duration REAL,
            start_time TIMESTAMP,
            end_time TIMESTAMP,
            charge_fee REAL,
            service_fee REAL,
            total_fee REAL,
            mode TEXT,
            FOREIGN KEY (request_id) REFERENCES requests(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')

    # 系统日志表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS system_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 系统运行时设置表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    ''')

    # 尝试为旧数据库添加新列（兼容升级）
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN battery_capacity REAL DEFAULT 60.0")
    except sqlite3.OperationalError:
        pass  # 列已存在
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN vehicle_model TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass

    conn.commit()

    # 初始化充电桩数据
    _init_chargers(cursor, conn)

    # 初始化管理员账户
    _init_admin(cursor, conn)

    conn.close()

    from settings import settings
    settings.load()


def _init_chargers(cursor, conn):
    """初始化充电桩"""
    from config import FAST_CHARGING_PILE_NUM, TRICKLE_CHARGING_PILE_NUM, \
        FAST_CHARGING_POWER, TRICKLE_CHARGING_POWER

    cursor.execute("SELECT COUNT(*) FROM chargers")
    if cursor.fetchone()[0] == 0:
        for i in range(1, FAST_CHARGING_PILE_NUM + 1):
            cursor.execute(
                "INSERT INTO chargers (charger_no, type, power) VALUES (?, ?, ?)",
                (f'F{i}', 'fast', FAST_CHARGING_POWER)
            )
        for i in range(1, TRICKLE_CHARGING_PILE_NUM + 1):
            cursor.execute(
                "INSERT INTO chargers (charger_no, type, power) VALUES (?, ?, ?)",
                (f'T{i}', 'slow', TRICKLE_CHARGING_POWER)
            )
        conn.commit()


def _init_admin(cursor, conn):
    """初始化管理员账户"""
    import hashlib
    cursor.execute("SELECT COUNT(*) FROM users WHERE role='admin'")
    if cursor.fetchone()[0] == 0:
        pw_hash = hashlib.sha256("admin123".encode()).hexdigest()
        cursor.execute(
            "INSERT INTO users (username, password_hash, role, battery_capacity) VALUES (?, ?, ?, 0)",
            ('admin', pw_hash, 'admin')
        )
        conn.commit()


# ==================== 用户相关操作 ====================

def create_user(username, password_hash, battery_capacity=60.0, vehicle_model=''):
    """创建用户（含车辆信息）"""
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, role, battery_capacity, vehicle_model) VALUES (?, ?, 'user', ?, ?)",
            (username, password_hash, battery_capacity, vehicle_model)
        )
        conn.commit()
        return True, "注册成功"
    except sqlite3.IntegrityError:
        return False, "用户名已存在"
    finally:
        conn.close()


def get_user_by_username(username):
    """根据用户名获取用户"""
    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()
    conn.close()
    return dict(user) if user else None


def get_user_by_id(user_id):
    """根据ID获取用户"""
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(user) if user else None


def get_all_users():
    """获取所有用户（管理员用）"""
    conn = get_db()
    users = conn.execute(
        "SELECT * FROM users ORDER BY role DESC, id ASC"
    ).fetchall()
    conn.close()
    return [dict(u) for u in users]


def update_user(user_id, **kwargs):
    """更新用户信息（车辆信息维护）"""
    if not kwargs:
        return
    conn = get_db()
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [user_id]
    conn.execute(f"UPDATE users SET {set_clause} WHERE id = ?", values)
    conn.commit()
    conn.close()


def delete_user(user_id):
    """删除用户"""
    conn = get_db()
    conn.execute("DELETE FROM users WHERE id = ? AND role != 'admin'", (user_id,))
    conn.commit()
    conn.close()


# ==================== 充电桩相关操作 ====================

def get_all_chargers():
    """获取所有充电桩"""
    conn = get_db()
    chargers = conn.execute(
        "SELECT * FROM chargers ORDER BY charger_no"
    ).fetchall()
    conn.close()
    return [dict(c) for c in chargers]


def get_charger(charger_id):
    """获取单个充电桩"""
    conn = get_db()
    charger = conn.execute(
        "SELECT * FROM chargers WHERE id = ?", (charger_id,)
    ).fetchone()
    conn.close()
    return dict(charger) if charger else None


def get_charger_by_no(charger_no):
    """根据编号获取充电桩"""
    conn = get_db()
    charger = conn.execute(
        "SELECT * FROM chargers WHERE charger_no = ?", (charger_no,)
    ).fetchone()
    conn.close()
    return dict(charger) if charger else None


def update_charger_status(charger_id, status):
    """更新充电桩状态"""
    conn = get_db()
    conn.execute(
        "UPDATE chargers SET status = ? WHERE id = ?",
        (status, charger_id)
    )
    conn.commit()
    conn.close()


def update_charger_stats(charger_id, duration, energy, charge_fee, service_fee):
    """更新充电桩统计数据"""
    conn = get_db()
    conn.execute('''
        UPDATE chargers SET
            total_charges = total_charges + 1,
            total_duration = total_duration + ?,
            total_energy = total_energy + ?,
            total_charge_fee = total_charge_fee + ?,
            total_service_fee = total_service_fee + ?,
            total_fee = total_fee + ? + ?
        WHERE id = ?
    ''', (duration, energy, charge_fee, service_fee, charge_fee, service_fee, charger_id))
    conn.commit()
    conn.close()


# ==================== 请求相关操作 ====================

def create_request(user_id, mode, request_amount):
    """创建充电请求"""
    conn = get_db()
    cursor = conn.execute(
        """INSERT INTO requests (user_id, mode, request_amount, status, wait_start_time)
           VALUES (?, ?, ?, 'waiting', ?)""",
        (user_id, mode, request_amount, datetime.now().isoformat())
    )
    request_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return request_id


def update_request(request_id, **kwargs):
    """更新请求"""
    if not kwargs:
        return
    conn = get_db()
    try:
        set_clause = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [request_id]
        conn.execute(f"UPDATE requests SET {set_clause} WHERE id = ?", values)
        conn.commit()
    finally:
        conn.close()


def get_request(request_id):
    """获取单个请求"""
    conn = get_db()
    req = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
    conn.close()
    return dict(req) if req else None


def get_user_requests(user_id, status_filter=None):
    """获取用户的请求列表"""
    conn = get_db()
    if status_filter:
        reqs = conn.execute(
            "SELECT * FROM requests WHERE user_id = ? AND status = ? ORDER BY created_at DESC",
            (user_id, status_filter)
        ).fetchall()
    else:
        reqs = conn.execute(
            "SELECT * FROM requests WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in reqs]


def get_waiting_requests(mode=None):
    """获取等候区的请求（按排队号顺序 = FIFO）"""
    conn = get_db()
    if mode:
        reqs = conn.execute(
            "SELECT * FROM requests WHERE status = 'waiting' AND mode = ? ORDER BY queue_number",
            (mode,)
        ).fetchall()
    else:
        reqs = conn.execute(
            "SELECT * FROM requests WHERE status = 'waiting' ORDER BY queue_number"
        ).fetchall()
    conn.close()
    return [dict(r) for r in reqs]


def get_charger_queue_requests(charger_id):
    """获取充电桩队列中的请求（包括正在充电的）"""
    conn = get_db()
    reqs = conn.execute(
        """SELECT * FROM requests
           WHERE charger_id = ? AND status IN ('queued', 'charging')
           ORDER BY charger_queue_position""",
        (charger_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in reqs]


def get_active_requests():
    """获取所有活跃请求（等候区+充电区）"""
    conn = get_db()
    reqs = conn.execute(
        "SELECT * FROM requests WHERE status IN ('waiting', 'queued', 'charging') ORDER BY created_at"
    ).fetchall()
    conn.close()
    return [dict(r) for r in reqs]


# ==================== 详单相关操作 ====================

def create_bill(request_id, user_id, charger_id, charger_no, charge_amount,
                charge_duration, start_time, end_time, charge_fee, service_fee, total_fee, mode):
    """创建详单"""
    conn = get_db()
    try:
        bill_no = f"BILL{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        cursor = conn.execute('''
            INSERT INTO bills (bill_no, request_id, user_id, charger_id, charger_no,
                              charge_amount, charge_duration, start_time, end_time,
                              charge_fee, service_fee, total_fee, mode, generated_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (bill_no, request_id, user_id, charger_id, charger_no,
              charge_amount, charge_duration, start_time, end_time,
              charge_fee, service_fee, total_fee, mode, datetime.now().isoformat()))
        bill_id = cursor.lastrowid
        conn.commit()
        return bill_id
    finally:
        conn.close()


def get_user_bills(user_id):
    """获取用户详单"""
    conn = get_db()
    bills = conn.execute(
        "SELECT * FROM bills WHERE user_id = ? ORDER BY generated_time DESC",
        (user_id,)
    ).fetchall()
    conn.close()
    return [dict(b) for b in bills]


def get_all_bills():
    """获取所有详单"""
    conn = get_db()
    bills = conn.execute(
        "SELECT * FROM bills ORDER BY generated_time DESC"
    ).fetchall()
    conn.close()
    return [dict(b) for b in bills]


# ==================== 统计报表相关操作 ====================

def get_charger_stats_by_period(period='day'):
    """按时间周期获取充电桩统计数据"""
    conn = get_db()
    if period == 'day':
        date_format = '%Y-%m-%d'
    elif period == 'week':
        date_format = '%Y-%W'
    elif period == 'month':
        date_format = '%Y-%m'
    else:
        date_format = '%Y-%m-%d'

    stats = conn.execute(f'''
        SELECT
            strftime('{date_format}', generated_time) as time_period,
            charger_no,
            COUNT(*) as total_charges,
            SUM(charge_duration) as total_duration,
            SUM(charge_amount) as total_energy,
            SUM(charge_fee) as total_charge_fee,
            SUM(service_fee) as total_service_fee,
            SUM(total_fee) as total_fee
        FROM bills
        GROUP BY time_period, charger_no
        ORDER BY time_period DESC, charger_no
    ''').fetchall()
    conn.close()
    return [dict(s) for s in stats]


# ==================== 日志相关操作 ====================

def add_log(event_type, description):
    """添加系统日志"""
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO system_logs (event_type, description) VALUES (?, ?)",
            (event_type, description)
        )
        conn.commit()
    finally:
        conn.close()


def get_logs(limit=100):
    """获取系统日志"""
    conn = get_db()
    logs = conn.execute(
        "SELECT * FROM system_logs ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(l) for l in logs]


# ==================== 系统设置相关操作 ====================

def get_all_settings_from_db():
    """获取所有系统设置"""
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM system_settings").fetchall()
    conn.close()
    return {r['key']: r['value'] for r in rows}


def save_settings_to_db(settings_dict):
    """保存系统设置"""
    conn = get_db()
    for key, value in settings_dict.items():
        conn.execute(
            "INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)",
            (key, value)
        )
    conn.commit()
    conn.close()


def _charger_has_active_queue(conn, charger_id):
    count = conn.execute(
        """SELECT COUNT(*) FROM requests
           WHERE charger_id = ? AND status IN ('queued', 'charging')""",
        (charger_id,)
    ).fetchone()[0]
    return count > 0


def sync_chargers_with_settings(fast_num, slow_num, fast_power, slow_power):
    """根据设置同步充电桩数量与功率"""
    conn = get_db()

    conn.execute("UPDATE chargers SET power = ? WHERE type = 'fast'", (fast_power,))
    conn.execute("UPDATE chargers SET power = ? WHERE type = 'slow'", (slow_power,))

    for ctype, target_num, prefix in (
        ('fast', fast_num, 'F'),
        ('slow', slow_num, 'T'),
    ):
        rows = conn.execute(
            "SELECT * FROM chargers WHERE type = ? ORDER BY charger_no",
            (ctype,)
        ).fetchall()
        current = len(rows)

        for i in range(current + 1, target_num + 1):
            conn.execute(
                "INSERT INTO chargers (charger_no, type, power) VALUES (?, ?, ?)",
                (f'{prefix}{i}', ctype, fast_power if ctype == 'fast' else slow_power)
            )

        if target_num < current:
            excess = sorted(
                [dict(r) for r in rows],
                key=lambda c: int(c['charger_no'][1:]),
                reverse=True
            )[:current - target_num]
            for charger in excess:
                if _charger_has_active_queue(conn, charger['id']):
                    conn.close()
                    return False, (
                        f"无法减少{'快' if ctype == 'fast' else '慢'}充电桩数量："
                        f"充电桩{charger['charger_no']}仍有车辆在队列中"
                    )
                conn.execute("DELETE FROM chargers WHERE id = ?", (charger['id'],))

    conn.commit()
    conn.close()
    return True, "充电桩已同步"
