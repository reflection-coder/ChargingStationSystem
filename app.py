"""
智能充电桩调度计费系统 - 主应用程序
Flask Web应用，包含用户端和管理员端所有功能
"""

import hashlib
import json
from datetime import datetime
from functools import wraps

import os
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify, make_response
)

# 初始化Flask应用
app = Flask(__name__)
app.secret_key = 'charging_station_secret_key_2024'

# 导入模块
from settings import settings
from database import (
    init_db, get_db, create_user, get_user_by_username, get_user_by_id,
    get_all_users, update_user, delete_user,
    get_all_chargers, get_charger, get_charger_by_no,
    update_charger_status, update_charger_stats, update_request,
    get_request, get_user_requests, get_user_bills,
    get_all_bills, get_waiting_requests,
    get_charger_queue_requests, get_active_requests,
    get_charger_stats_by_period, add_log, get_logs
)
from scheduler import scheduler
from billing import (
    get_current_price_period, get_price_at_time,
    calculate_total_fee, calculate_charging_duration,
    calculate_charging_fee, calculate_service_fee,
    format_price_info
)


# ==================== 模板上下文注入 ====================

@app.context_processor
def inject_config():
    """向所有模板注入配置变量"""
    s = settings.get_all()
    return {
        'config': {
            'FAST_CHARGING_PILE_NUM': s['FAST_CHARGING_PILE_NUM'],
            'TRICKLE_CHARGING_PILE_NUM': s['TRICKLE_CHARGING_PILE_NUM'],
            'FAST_CHARGING_POWER': s['FAST_CHARGING_POWER'],
            'TRICKLE_CHARGING_POWER': s['TRICKLE_CHARGING_POWER'],
            'WAITING_AREA_SIZE': s['WAITING_AREA_SIZE'],
            'CHARGING_QUEUE_LEN': s['CHARGING_QUEUE_LEN'],
            'SERVICE_FEE_RATE': s['SERVICE_FEE_RATE'],
            'SIMULATION_SPEED': s['SIMULATION_SPEED'],
        },
        'get_all_chargers': get_all_chargers,
        'get_charger': get_charger,
    }


# ==================== 辅助函数 ====================

def hash_password(password):
    """密码哈希"""
    return hashlib.sha256(password.encode()).hexdigest()


def login_required(f):
    """登录验证装饰器"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('请先登录', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """管理员验证装饰器"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('请先登录', 'warning')
            return redirect(url_for('login'))
        if session.get('role') != 'admin':
            flash('需要管理员权限', 'danger')
            return redirect(url_for('user_dashboard'))
        return f(*args, **kwargs)
    return decorated


# ==================== 首页 ====================

@app.route('/')
def index():
    """首页"""
    if 'user_id' in session:
        if session.get('role') == 'admin':
            return redirect(url_for('admin_dashboard'))
        return redirect(url_for('user_dashboard'))
    return redirect(url_for('login'))


# ==================== 用户认证 ====================

@app.route('/login', methods=['GET', 'POST'])
def login():
    """登录页面"""
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        if not username or not password:
            flash('请输入用户名和密码', 'danger')
            return render_template('login.html')

        user = get_user_by_username(username)
        if user and user['password_hash'] == hash_password(password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']
            flash(f'欢迎回来，{username}！', 'success')

            if user['role'] == 'admin':
                return redirect(url_for('admin_dashboard'))
            return redirect(url_for('user_dashboard'))
        else:
            flash('用户名或密码错误', 'danger')

    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    """注册页面（含车辆信息：电池总容量）"""
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
        try:
            battery_capacity = float(request.form.get('battery_capacity', 60.0))
        except ValueError:
            battery_capacity = 60.0
        vehicle_model = request.form.get('vehicle_model', '').strip()

        if not username or not password:
            flash('请填写所有必填字段', 'danger')
            return render_template('register.html')

        if len(username) < 3:
            flash('用户名至少3个字符', 'danger')
            return render_template('register.html')

        if len(password) < 6:
            flash('密码至少6个字符', 'danger')
            return render_template('register.html')

        if password != confirm:
            flash('两次密码输入不一致', 'danger')
            return render_template('register.html')

        if battery_capacity <= 0:
            flash('电池总容量必须大于0', 'danger')
            return render_template('register.html')

        success, msg = create_user(username, hash_password(password),
                                   battery_capacity, vehicle_model)
        if success:
            flash('注册成功，请登录', 'success')
            return redirect(url_for('login'))
        else:
            flash(msg, 'danger')

    return render_template('register.html')


@app.route('/logout')
def logout():
    """退出登录"""
    session.clear()
    flash('已退出登录', 'info')
    return redirect(url_for('login'))


# ==================== 用户端 ====================

@app.route('/user/dashboard')
@login_required
def user_dashboard():
    """用户仪表盘"""
    user = get_user_by_id(session['user_id'])
    active_reqs = [
        r for r in get_user_requests(session['user_id'])
        if r['status'] in ('waiting', 'queued', 'charging')
    ]
    bills = get_user_bills(session['user_id'])[:5]

    return render_template('user/dashboard.html',
                          user=user,
                          active_requests=active_reqs,
                          recent_bills=bills,
                          price_info=format_price_info())


@app.route('/user/submit_request', methods=['GET', 'POST'])
@login_required
def submit_request():
    """提交充电请求"""
    if request.method == 'POST':
        mode = request.form.get('mode', 'fast')
        try:
            request_amount = float(request.form.get('request_amount', 0))
        except ValueError:
            flash('请输入有效的充电量', 'danger')
            return redirect(url_for('submit_request'))

        if request_amount <= 0:
            flash('充电量必须大于0', 'danger')
            return redirect(url_for('submit_request'))

        if mode not in ('fast', 'slow'):
            mode = 'fast'

        # 检查等候区容量
        waiting = get_waiting_requests()
        waiting_capacity = settings.get('WAITING_AREA_SIZE')
        if len(waiting) >= waiting_capacity:
            flash(f'等候区已满（容量{waiting_capacity}），请稍后再试', 'danger')
            return redirect(url_for('submit_request'))

        request_id, queue_number = scheduler.submit_request(
            session['user_id'], mode, request_amount
        )

        if request_id:
            flash(f'充电请求已提交！排队号: {queue_number}', 'success')
            return redirect(url_for('user_dashboard'))
        else:
            flash(queue_number or '提交失败', 'danger')

    price_info = format_price_info()
    return render_template('user/submit_request.html',
                          price_info=price_info,
                          fast_power=settings.get('FAST_CHARGING_POWER'),
                          slow_power=settings.get('TRICKLE_CHARGING_POWER'))


@app.route('/user/queue_status')
@login_required
def queue_status():
    """查看排队状态"""
    active_reqs = [
        r for r in get_user_requests(session['user_id'])
        if r['status'] in ('waiting', 'queued', 'charging')
    ]

    queue_details = []
    for req in active_reqs:
        detail = {**req}
        if req['status'] == 'waiting':
            position = scheduler.get_waiting_position(req['id'])
            detail['waiting_position'] = position
            detail['ahead_count'] = position
        elif req['status'] in ('queued', 'charging'):
            charger = get_charger(req['charger_id'])
            detail['charger_no'] = charger['charger_no'] if charger else 'N/A'
            detail['charger_power'] = charger['power'] if charger else 0
            if req['status'] == 'queued':
                charger_queue = get_charger_queue_requests(req['charger_id'])
                wait_hours = 0
                power = charger['power'] if charger else 1
                for qr in charger_queue:
                    if qr['charger_queue_position'] < req['charger_queue_position']:
                        if qr['status'] == 'charging':
                            if qr['start_time']:
                                start = datetime.fromisoformat(qr['start_time'])
                                current = scheduler.get_current_time()
                                elapsed = (current - start).total_seconds() / 3600
                                needed = calculate_charging_duration(
                                    qr['request_amount'], power
                                )
                                wait_hours += max(0, needed - elapsed)
                            else:
                                wait_hours += calculate_charging_duration(
                                    qr['request_amount'], power
                                )
                        else:
                            wait_hours += calculate_charging_duration(
                                qr['request_amount'], power
                            )
                detail['estimated_wait_hours'] = round(wait_hours, 2)
        queue_details.append(detail)

    return render_template('user/queue_status.html',
                          queue_details=queue_details)


@app.route('/user/bills')
@login_required
def user_bills():
    """查看充电详单"""
    bills = get_user_bills(session['user_id'])
    return render_template('user/bills.html', bills=bills)


@app.route('/user/modify_request/<int:request_id>', methods=['GET', 'POST'])
@login_required
def modify_request(request_id):
    """修改充电请求"""
    req = get_request(request_id)
    if not req or req['user_id'] != session['user_id']:
        flash('请求不存在', 'danger')
        return redirect(url_for('user_dashboard'))

    if request.method == 'POST':
        action = request.form.get('action', '')

        if action == 'modify_mode':
            new_mode = request.form.get('mode', 'fast')
            success, msg = scheduler.modify_mode(request_id, new_mode)
            flash(msg, 'success' if success else 'danger')
            return redirect(url_for('user_dashboard'))

        elif action == 'modify_amount':
            try:
                new_amount = float(request.form.get('request_amount', 0))
            except ValueError:
                flash('请输入有效的充电量', 'danger')
                return redirect(url_for('modify_request', request_id=request_id))

            if new_amount <= 0:
                flash('充电量必须大于0', 'danger')
                return redirect(url_for('modify_request', request_id=request_id))

            success, msg = scheduler.modify_amount(request_id, new_amount)
            flash(msg, 'success' if success else 'danger')
            return redirect(url_for('user_dashboard'))

        elif action == 'cancel':
            success, msg = scheduler.cancel_request(request_id)
            flash(msg, 'success' if success else 'danger')
            return redirect(url_for('user_dashboard'))

    return render_template('user/modify_request.html', req=req)


@app.route('/user/cancel_request/<int:request_id>')
@login_required
def cancel_request(request_id):
    """取消充电请求"""
    req = get_request(request_id)
    if not req or req['user_id'] != session['user_id']:
        flash('请求不存在', 'danger')
        return redirect(url_for('user_dashboard'))

    success, msg = scheduler.cancel_request(request_id)
    flash(msg, 'success' if success else 'danger')
    return redirect(url_for('user_dashboard'))


@app.route('/user/end_charging/<int:request_id>')
@login_required
def end_charging(request_id):
    """结束充电"""
    req = get_request(request_id)
    if not req or req['user_id'] != session['user_id']:
        flash('请求不存在', 'danger')
        return redirect(url_for('user_dashboard'))

    success, msg = scheduler.end_charging(request_id)
    flash(msg, 'success' if success else 'danger')
    return redirect(url_for('user_dashboard'))


# ==================== 管理员端 ====================

@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    """管理员仪表盘"""
    chargers = get_all_chargers()
    active_requests = get_active_requests()
    waiting_area = get_waiting_requests()
    waiting_fast = [w for w in waiting_area if w['mode'] == 'fast']
    waiting_slow = [w for w in waiting_area if w['mode'] == 'slow']

    stats = {
        'total_chargers': len(chargers),
        'working_chargers': len([c for c in chargers if c['status'] == 'working']),
        'fault_chargers': len([c for c in chargers if c['status'] == 'fault']),
        'stopped_chargers': len([c for c in chargers if c['status'] == 'stopped']),
        'waiting_total': len(waiting_area),
        'waiting_fast': len(waiting_fast),
        'waiting_slow': len(waiting_slow),
        'active_total': len(active_requests),
        'waiting_area_capacity': settings.get('WAITING_AREA_SIZE'),
        'charging_queue_len': settings.get('CHARGING_QUEUE_LEN'),
    }

    return render_template('admin/dashboard.html',
                          stats=stats,
                          chargers=chargers,
                          time_info=scheduler.get_simulation_info(),
                          scheduler_status={
                              'waiting_area_service': scheduler.waiting_area_service,
                              'fault_handling': scheduler.fault_handling,
                              'fault_charger_id': scheduler.fault_charger_id,
                              'extended_mode': scheduler.extended_schedule_mode,
                          })


@app.route('/admin/chargers')
@admin_required
def admin_chargers():
    """查看所有充电桩状态"""
    chargers = get_all_chargers()
    charger_details = []
    for charger in chargers:
        queue_info = scheduler.get_charger_queue_info(charger['id'])
        if queue_info:
            queue_info['id'] = charger['id']
            queue_info['stats'] = charger
        charger_details.append(queue_info)

    return render_template('admin/chargers.html',
                          charger_details=charger_details,
                          fault_strategy=scheduler.fault_strategy)


@app.route('/admin/charger/<int:charger_id>/start')
@admin_required
def admin_start_charger(charger_id):
    """启动/恢复充电桩"""
    charger = get_charger(charger_id)
    if not charger:
        flash('充电桩不存在', 'danger')
        return redirect(url_for('admin_chargers'))

    if charger['status'] == 'fault':
        success, msg = scheduler.recover_charger(charger_id)
        flash(msg, 'success' if success else 'danger')
    else:
        update_charger_status(charger_id, 'working')
        add_log('charger_start', f'管理员启动充电桩{charger["charger_no"]}')
        flash(f'充电桩{charger["charger_no"]}已启动', 'success')
        scheduler.trigger_scheduling()

    return redirect(url_for('admin_chargers'))


@app.route('/admin/charger/<int:charger_id>/stop')
@admin_required
def admin_stop_charger(charger_id):
    """关闭充电桩"""
    charger = get_charger(charger_id)
    if not charger:
        flash('充电桩不存在', 'danger')
        return redirect(url_for('admin_chargers'))

    # 关闭前处理队列中的车辆
    queue = get_charger_queue_requests(charger_id)
    for req in queue:
        if req['status'] == 'charging':
            scheduler._cancel_charging_with_billing(req['id'])
        elif req['status'] == 'queued':
            update_request(req['id'], charger_id=None,
                          charger_queue_position=-1, status='waiting')

    update_charger_status(charger_id, 'stopped')
    add_log('charger_stop', f'管理员关闭充电桩{charger["charger_no"]}')
    flash(f'充电桩{charger["charger_no"]}已关闭', 'info')
    scheduler.trigger_scheduling()

    return redirect(url_for('admin_chargers'))


@app.route('/admin/charger/<int:charger_id>/fault', methods=['POST'])
@admin_required
def admin_set_fault(charger_id):
    """设置充电桩故障（调度策略以7c演示页全局设置为准）"""
    strategy = scheduler.fault_strategy
    success, msg = scheduler.set_charger_fault(charger_id, strategy)
    flash(msg, 'warning' if success else 'danger')
    return redirect(url_for('admin_chargers'))


@app.route('/admin/queue')
@admin_required
def admin_queue():
    """查看各充电桩等候服务的车辆信息（含真实电池总容量）"""
    all_chargers = get_all_chargers()
    charger_queues = []

    for charger in all_chargers:
        queue_info = scheduler.get_charger_queue_info(charger['id'])
        if queue_info:
            # 车辆信息已在 get_charger_queue_info 中包含 battery_capacity
            for v in queue_info.get('vehicles', []):
                user = get_user_by_id(v['user_id'])
                v['username'] = user['username'] if user else 'Unknown'
                # 使用真实的电池总容量
                v['battery_capacity'] = user['battery_capacity'] if user else 60.0
            charger_queues.append(queue_info)

    # 等候区车辆（含真实电池总容量）
    waiting_area = get_waiting_requests()
    waiting_details = []
    for req in waiting_area:
        user = get_user_by_id(req['user_id'])
        wait_start = datetime.fromisoformat(req['wait_start_time']) if req.get('wait_start_time') else datetime.now()
        current = scheduler.get_current_time()
        wait_hours = (current - wait_start).total_seconds() / 3600
        waiting_details.append({
            'queue_number': req['queue_number'],
            'user_id': req['user_id'],
            'username': user['username'] if user else 'Unknown',
            'mode': req['mode'],
            'request_amount': req['request_amount'],
            'battery_capacity': user['battery_capacity'] if user else 60.0,
            'wait_hours': round(max(0, wait_hours), 2)
        })

    return render_template('admin/queue.html',
                          charger_queues=charger_queues,
                          waiting_details=waiting_details)


@app.route('/admin/reports')
@admin_required
def admin_reports():
    """报表展示"""
    period = request.args.get('period', 'day')

    if period == 'custom':
        from_date = request.args.get('from_date', '')
        to_date = request.args.get('to_date', '')
        conn = get_db()
        stats = conn.execute('''
            SELECT
                date(generated_time) as time_period,
                charger_no,
                COUNT(*) as total_charges,
                ROUND(SUM(charge_duration), 2) as total_duration,
                ROUND(SUM(charge_amount), 2) as total_energy,
                ROUND(SUM(charge_fee), 2) as total_charge_fee,
                ROUND(SUM(service_fee), 2) as total_service_fee,
                ROUND(SUM(total_fee), 2) as total_fee
            FROM bills
            WHERE date(generated_time) BETWEEN ? AND ?
            GROUP BY time_period, charger_no
            ORDER BY time_period DESC, charger_no
        ''', (from_date, to_date)).fetchall()
        conn.close()
        stats = [dict(s) for s in stats]
    else:
        stats = get_charger_stats_by_period(period)

    summary = {
        'total_charges': sum(s['total_charges'] for s in stats),
        'total_duration': round(sum(s['total_duration'] or 0 for s in stats), 2),
        'total_energy': round(sum(s['total_energy'] or 0 for s in stats), 2),
        'total_charge_fee': round(sum(s['total_charge_fee'] or 0 for s in stats), 2),
        'total_service_fee': round(sum(s['total_service_fee'] or 0 for s in stats), 2),
        'total_fee': round(sum(s['total_fee'] or 0 for s in stats), 2),
    }

    return render_template('admin/reports.html',
                          stats=stats,
                          summary=summary,
                          period=period)


@app.route('/admin/bills')
@admin_required
def admin_bills():
    """管理员：全站充电详单查询"""
    bills = get_all_bills()
    return render_template('admin/bills.html', bills=bills)


@app.route('/admin/logs')
@admin_required
def admin_logs():
    """查看系统日志"""
    logs = get_logs(50)
    return render_template('admin/logs.html', logs=logs)


@app.route('/admin/toggle_waiting_service')
@admin_required
def toggle_waiting_service():
    """切换等候区叫号服务"""
    scheduler.waiting_area_service = not scheduler.waiting_area_service
    status = '开启' if scheduler.waiting_area_service else '暂停'
    add_log('toggle_service', f'管理员{status}等候区叫号服务')
    flash(f'等候区叫号服务已{status}', 'info')
    if scheduler.waiting_area_service:
        scheduler.trigger_scheduling()
    return redirect(url_for('admin_dashboard'))


# ==================== 用户（车辆）信息维护（管理员端） ====================

@app.route('/admin/users')
@admin_required
def admin_users():
    """管理员：用户（车辆）信息维护"""
    users = get_all_users()
    return render_template('admin/users.html', users=users)


@app.route('/admin/user/<int:user_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_edit_user(user_id):
    """管理员：编辑用户车辆信息"""
    user = get_user_by_id(user_id)
    if not user:
        flash('用户不存在', 'danger')
        return redirect(url_for('admin_users'))

    if request.method == 'POST':
        battery_capacity = float(request.form.get('battery_capacity', 60.0))
        vehicle_model = request.form.get('vehicle_model', '').strip()
        new_password = request.form.get('password', '').strip()

        update_data = {
            'battery_capacity': battery_capacity,
            'vehicle_model': vehicle_model,
        }
        if new_password:
            update_data['password_hash'] = hash_password(new_password)

        update_user(user_id, **update_data)
        add_log('user_edit', f'管理员更新用户{user["username"]}的车辆信息')
        flash(f'用户{user["username"]}信息已更新', 'success')
        return redirect(url_for('admin_users'))

    return render_template('admin/edit_user.html', user=user)


@app.route('/admin/user/<int:user_id>/delete')
@admin_required
def admin_delete_user(user_id):
    """管理员：删除用户"""
    user = get_user_by_id(user_id)
    if not user:
        flash('用户不存在', 'danger')
        return redirect(url_for('admin_users'))
    if user['role'] == 'admin':
        flash('不能删除管理员账户', 'danger')
        return redirect(url_for('admin_users'))

    delete_user(user_id)
    add_log('user_delete', f'管理员删除用户{user["username"]}')
    flash(f'用户{user["username"]}已删除', 'info')
    return redirect(url_for('admin_users'))


# ==================== 系统参数设置（管理员） ====================

def _parse_settings_form(form):
    """从表单解析系统设置"""
    return {
        'FAST_CHARGING_PILE_NUM': form.get('fast_charging_pile_num'),
        'TRICKLE_CHARGING_PILE_NUM': form.get('trickle_charging_pile_num'),
        'FAST_CHARGING_POWER': form.get('fast_charging_power'),
        'TRICKLE_CHARGING_POWER': form.get('trickle_charging_power'),
        'WAITING_AREA_SIZE': form.get('waiting_area_size'),
        'CHARGING_QUEUE_LEN': form.get('charging_queue_len'),
        'SERVICE_FEE_RATE': form.get('service_fee_rate'),
        'SIMULATION_SPEED': form.get('simulation_speed'),
        'SCHEDULING_INTERVAL': form.get('scheduling_interval'),
        'peak_price': form.get('peak_price'),
        'normal_price': form.get('normal_price'),
        'valley_price': form.get('valley_price'),
    }


@app.route('/admin/settings', methods=['GET', 'POST'])
@admin_required
def admin_settings():
    """管理员：在线修改系统参数"""
    if request.method == 'POST':
        action = request.form.get('action', 'save')
        if action == 'reset':
            success, msg = settings.reset_to_defaults()
            flash(msg, 'success' if success else 'danger')
            if success:
                add_log('settings_reset', '管理员恢复系统默认参数')
                scheduler.trigger_scheduling()
        else:
            success, msg = settings.update(_parse_settings_form(request.form))
            flash(msg, 'success' if success else 'danger')
            if success:
                add_log('settings_update', '管理员更新系统参数')
                scheduler.trigger_scheduling()
        return redirect(url_for('admin_settings'))

    current = settings.get_all()
    return render_template('admin/settings.html', s=current)


@app.route('/api/admin/settings', methods=['GET'])
@admin_required
def api_get_settings():
    """API：获取系统参数"""
    return jsonify(settings.get_all())


@app.route('/api/admin/settings', methods=['PUT', 'POST'])
@admin_required
def api_update_settings():
    """API：更新系统参数"""
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({'success': False, 'message': '请提供 JSON 参数'}), 400

    success, msg = settings.update(data)
    if success:
        add_log('settings_update', '管理员通过 API 更新系统参数')
        scheduler.trigger_scheduling()
        return jsonify({'success': True, 'message': msg, 'settings': settings.get_all()})
    return jsonify({'success': False, 'message': msg}), 400


# ==================== 模拟时间控制（管理员） ====================

TIME_PRESETS = [
    {'label': '峰时 10:30', 'hour': 10, 'minute': 30},
    {'label': '峰时 19:00', 'hour': 19, 'minute': 0},
    {'label': '平时 08:00', 'hour': 8, 'minute': 0},
    {'label': '平时 16:00', 'hour': 16, 'minute': 0},
    {'label': '谷时 02:00', 'hour': 2, 'minute': 0},
    {'label': '谷时 23:30', 'hour': 23, 'minute': 30},
]


def _handle_simulation_time_action(action, form):
    """处理模拟时间相关操作，返回 (success, message)"""
    if action == 'set_time':
        raw = form.get('simulation_datetime', '').strip()
        if not raw:
            return False, '请指定模拟时间'
        try:
            target = datetime.fromisoformat(raw.replace('Z', ''))
        except ValueError:
            try:
                target = datetime.strptime(raw, '%Y-%m-%dT%H:%M')
            except ValueError:
                return False, '时间格式无效'
        scheduler.set_simulation_time(target)
        add_log('sim_time_set', f'管理员设置模拟时间为 {target.strftime("%Y-%m-%d %H:%M:%S")}')
        return True, f'模拟时间已设为 {target.strftime("%Y-%m-%d %H:%M:%S")}'

    if action == 'preset':
        try:
            hour = int(form.get('preset_hour', 0))
            minute = int(form.get('preset_minute', 0))
        except ValueError:
            return False, '预设时间无效'
        scheduler.jump_to_time_of_day(hour, minute)
        info = scheduler.get_simulation_info()
        add_log('sim_time_preset', f'管理员跳转模拟时间到 {info["simulation_time"]}')
        return True, f'已跳转到 {info["simulation_time"]}（{info["period_name"]}）'

    if action == 'advance':
        try:
            hours = int(form.get('advance_hours', 0) or 0)
            minutes = int(form.get('advance_minutes', 0) or 0)
        except ValueError:
            return False, '推进时长无效'
        if hours == 0 and minutes == 0:
            return False, '请指定推进的小时或分钟数'
        scheduler.advance_simulation_time(hours=hours, minutes=minutes)
        info = scheduler.get_simulation_info()
        add_log('sim_time_advance', f'管理员推进模拟时间 {hours}小时{minutes}分钟')
        return True, f'模拟时间已推进至 {info["simulation_time"]}'

    if action == 'reset_time':
        scheduler.reset_simulation_time()
        info = scheduler.get_simulation_info()
        add_log('sim_time_reset', '管理员将模拟时间重置为真实时间')
        return True, f'模拟时间已同步为真实时间：{info["simulation_time"]}'

    if action == 'set_speed':
        try:
            speed = int(form.get('simulation_speed', 0))
        except ValueError:
            return False, '加速倍率必须是整数'
        success, msg = settings.update({'SIMULATION_SPEED': speed})
        if success:
            add_log('sim_speed_update', f'管理员设置模拟加速倍率为 {speed}x')
        return success, msg

    return False, '未知操作'


@app.route('/admin/simulation_time', methods=['GET', 'POST'])
@admin_required
def admin_simulation_time():
    """管理员：模拟时间与加速倍率控制"""
    if request.method == 'POST':
        action = request.form.get('action', '')
        success, msg = _handle_simulation_time_action(action, request.form)
        flash(msg, 'success' if success else 'danger')
        return redirect(url_for('admin_simulation_time'))

    return render_template('admin/simulation_time.html',
                          time_info=scheduler.get_simulation_info(),
                          presets=TIME_PRESETS)


@app.route('/api/admin/simulation_time', methods=['GET'])
@admin_required
def api_get_simulation_time():
    """API：获取模拟时间状态"""
    return jsonify(scheduler.get_simulation_info())


@app.route('/api/admin/simulation_time', methods=['PUT', 'POST'])
@admin_required
def api_update_simulation_time():
    """API：调节模拟时间或加速倍率"""
    data = request.get_json(silent=True) or {}
    action = data.get('action', 'set_time')

    if action == 'set_time':
        raw = data.get('simulation_time') or data.get('simulation_datetime')
        if not raw:
            return jsonify({'success': False, 'message': '请提供 simulation_time'}), 400
        try:
            target = datetime.fromisoformat(str(raw).replace('Z', ''))
        except ValueError:
            return jsonify({'success': False, 'message': '时间格式无效'}), 400
        scheduler.set_simulation_time(target)
        add_log('sim_time_set', f'API 设置模拟时间为 {target}')
    elif action == 'preset':
        scheduler.jump_to_time_of_day(int(data.get('hour', 0)), int(data.get('minute', 0)))
    elif action == 'advance':
        scheduler.advance_simulation_time(
            hours=int(data.get('hours', 0) or 0),
            minutes=int(data.get('minutes', 0) or 0),
        )
    elif action == 'reset_time':
        scheduler.reset_simulation_time()
    elif action == 'set_speed':
        success, msg = settings.update({'SIMULATION_SPEED': int(data.get('simulation_speed', 60))})
        if not success:
            return jsonify({'success': False, 'message': msg}), 400
    else:
        return jsonify({'success': False, 'message': '未知 action'}), 400

    return jsonify({'success': True, 'message': '模拟时间已更新', **scheduler.get_simulation_info()})


# ==================== 扩展调度控制 ====================

@app.route('/admin/extended_schedule/<mode>')
@admin_required
def admin_extended_schedule(mode):
    """设置扩展调度模式"""
    if mode == 'off':
        scheduler.set_extended_mode(None)
        flash('已恢复标准调度模式（FIFO叫号 + 最短完成时间选桩）', 'info')
    elif mode == 'single':
        scheduler.set_extended_mode('single')
        flash('已启用扩展调度a：单次调度总充电时长最短（多空位时同时叫多个号）', 'info')
    elif mode == 'batch':
        scheduler.set_extended_mode('batch')
        flash('已启用扩展调度b：批量调度总充电时长最短（满容量触发，不区分快慢充）', 'info')
    else:
        flash('未知扩展调度模式', 'danger')
    return redirect(url_for('admin_dashboard'))


# ==================== API接口 ====================

@app.route('/api/status')
def api_status():
    """获取系统状态"""
    chargers = get_all_chargers()
    waiting = get_waiting_requests()
    waiting_fast = len([w for w in waiting if w['mode'] == 'fast'])
    waiting_slow = len([w for w in waiting if w['mode'] == 'slow'])

    charger_status = []
    for c in chargers:
        queue = get_charger_queue_requests(c['id'])
        charger_status.append({
            'charger_no': c['charger_no'],
            'type': c['type'],
            'status': c['status'],
            'power': c['power'],
            'queue_count': len(queue),
            'max_queue': settings.get('CHARGING_QUEUE_LEN'),
            'total_charges': c['total_charges'],
            'total_energy': round(c['total_energy'], 2),
        })

    return jsonify({
        'waiting_area': {
            'total': len(waiting),
            'fast': waiting_fast,
            'slow': waiting_slow,
            'capacity': settings.get('WAITING_AREA_SIZE')
        },
        'chargers': charger_status,
        'waiting_area_service': scheduler.waiting_area_service,
        'fault_handling': scheduler.fault_handling,
        'extended_mode': scheduler.extended_schedule_mode,
        'current_time': scheduler.get_current_time().isoformat(),
    })


@app.route('/api/user/status')
@login_required
def api_user_status():
    """获取当前用户状态（AJAX）"""
    active_reqs = [
        r for r in get_user_requests(session['user_id'])
        if r['status'] in ('waiting', 'queued', 'charging')
    ]

    result = []
    for req in active_reqs:
        info = {
            'id': req['id'],
            'queue_number': req['queue_number'],
            'mode': req['mode'],
            'request_amount': req['request_amount'],
            'status': req['status'],
        }

        if req['status'] == 'waiting':
            position = scheduler.get_waiting_position(req['id'])
            info['ahead_count'] = position
        elif req['status'] in ('queued', 'charging'):
            charger = get_charger(req['charger_id'])
            info['charger_no'] = charger['charger_no'] if charger else 'N/A'
            info['queue_position'] = req['charger_queue_position']

            if req['status'] == 'charging' and req['start_time']:
                start = datetime.fromisoformat(req['start_time'])
                charger_power = charger['power'] if charger else 1
                needed_hours = calculate_charging_duration(req['request_amount'], charger_power)
                elapsed = (scheduler.get_current_time() - start).total_seconds() / 3600
                info['progress'] = round(min(100, (elapsed / needed_hours) * 100), 1)
                info['remaining_hours'] = round(max(0, needed_hours - elapsed), 2)

        result.append(info)

    return jsonify(result)


@app.route('/api/price')
def api_price():
    """获取当前电价"""
    period, price = get_current_price_period(scheduler.get_current_time())
    return jsonify({
        'period': period,
        'price': price,
        'service_fee': settings.get('SERVICE_FEE_RATE'),
        'current_time': scheduler.get_current_time().isoformat()
    })


# ==================== 7c 故障处理演示 ====================

def _demo_snapshot():
    """获取当前系统状态快照（供演示页使用）"""
    chargers = get_all_chargers()
    result = []
    for c in chargers:
        queue = get_charger_queue_requests(c['id'])
        result.append({
            'id': c['id'],
            'charger_no': c['charger_no'],
            'type': c['type'],
            'power': c['power'],
            'status': c['status'],
            'queue': [
                {'queue_number': r['queue_number'],
                 'status': r['status'],
                 'request_amount': r['request_amount']}
                for r in queue
            ],
        })
    waiting = get_waiting_requests()
    fault_charger_count = sum(1 for c in chargers if c['status'] == 'fault')
    fault_pending_ids = set(scheduler.fault_pending_request_ids)
    fault_pending = []
    true_waiting = []
    for r in waiting:
        entry = {'queue_number': r['queue_number'],
                 'mode': r['mode'],
                 'request_amount': r['request_amount']}
        if r['id'] in fault_pending_ids:
            fault_pending.append(entry)
        else:
            true_waiting.append(entry)
    return {
        'chargers': result,
        'waiting': true_waiting,
        'fault_pending': fault_pending,
        'fault_handling': scheduler.fault_handling,
        'waiting_area_service': scheduler.waiting_area_service,
        'fault_strategy': scheduler.fault_strategy,
        'fault_charger_id': scheduler.fault_charger_id,
        'fault_pending_count': len(scheduler.fault_pending_request_ids),
        'fault_charger_count': fault_charger_count,
    }


def _demo_setup_scenario(charge_amount=1.0):
    """
    初始化7c演示场景：
      F1: F1(充电中), F4(排队), F7(排队)
      F2: F2(充电中), F5(排队), F8(排队)
      F3: F3(充电中), F6(排队), F9(排队)
      T1: T1(充电中)
    """
    import hashlib as _hl
    charge_amount = max(0.1, float(charge_amount))
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM bills")
        cur.execute("DELETE FROM system_logs")
        cur.execute("DELETE FROM requests")
        cur.execute("DELETE FROM users WHERE role != 'admin'")
        cur.execute("UPDATE chargers SET status='working'")
        cur.execute("UPDATE chargers SET total_charges=0, total_duration=0, "
                    "total_energy=0, total_charge_fee=0, total_service_fee=0, total_fee=0")
        conn.commit()

        pw = _hl.sha256(b'demo123').hexdigest()
        user_ids = []
        for i in range(10):
            cur.execute(
                "INSERT INTO users (username,password_hash,role,battery_capacity) "
                "VALUES (?,?,'user',100)",
                (f'demo_user_{i+1}', pw)
            )
            user_ids.append(cur.lastrowid)
        conn.commit()

        chargers = {c['charger_no']: c for c in get_all_chargers()}
        base_dt = datetime(2024, 6, 1, 10, 0, 0)
        base = base_dt.isoformat()

        def ins(uid, qno, mode, cno, pos, status):
            cur.execute(
                """INSERT INTO requests
                   (user_id,queue_number,mode,request_amount,status,
                    charger_id,charger_queue_position,created_at,wait_start_time,start_time)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (uid, qno, mode, charge_amount, status,
                 chargers[cno]['id'], pos, base, base,
                 base if status == 'charging' else None)
            )

        ins(user_ids[0], 'F1', 'fast', 'F1', 0, 'charging')
        ins(user_ids[1], 'F4', 'fast', 'F1', 1, 'queued')
        ins(user_ids[2], 'F7', 'fast', 'F1', 2, 'queued')
        ins(user_ids[3], 'F2', 'fast', 'F2', 0, 'charging')
        ins(user_ids[4], 'F5', 'fast', 'F2', 1, 'queued')
        ins(user_ids[5], 'F8', 'fast', 'F2', 2, 'queued')
        ins(user_ids[6], 'F3', 'fast', 'F3', 0, 'charging')
        ins(user_ids[7], 'F6', 'fast', 'F3', 1, 'queued')
        ins(user_ids[8], 'F9', 'fast', 'F3', 2, 'queued')
        ins(user_ids[9], 'T1', 'slow', 'T1', 0, 'charging')
        conn.commit()

        # 重置调度器内存状态，并将模拟时间对齐到演示基准时间
        scheduler.simulation_time = base_dt   # 关键：避免 elapsed_hours 计算出天文数字
        scheduler.waiting_area_service = True
        scheduler.fault_handling = False
        scheduler.fault_charger_id = None
        scheduler.fault_pending_request_ids = []
        scheduler.fault_charger_type = None

        add_log('demo_setup', '管理员初始化7c演示场景')
        return True, '演示场景初始化成功'
    except Exception as e:
        conn.rollback()
        return False, f'初始化失败: {e}'
    finally:
        conn.close()


@app.route('/admin/fault_demo', methods=['GET'])
@admin_required
def admin_fault_demo():
    """7c 故障处理演示页"""
    return render_template('admin/fault_demo.html',
                           snapshot=_demo_snapshot(),
                           chargers=get_all_chargers())


@app.route('/api/admin/fault_demo', methods=['POST'])
@admin_required
def api_fault_demo():
    """7c 演示操作 API"""
    data = request.get_json(silent=True) or {}
    action = data.get('action', '')

    if action == 'setup':
        charge_amount = data.get('charge_amount', 1.0)
        ok, msg = _demo_setup_scenario(charge_amount)
        return jsonify({'success': ok, 'message': msg,
                        'snapshot': _demo_snapshot()})

    if action == 'get_state':
        return jsonify({'success': True, 'snapshot': _demo_snapshot()})

    if action == 'trigger_fault':
        charger_id = data.get('charger_id')
        strategy = data.get('strategy', 'time_order')
        if not charger_id:
            return jsonify({'success': False, 'message': '请指定充电桩'}), 400
        ok, msg = scheduler.set_charger_fault(int(charger_id), strategy)
        add_log('demo_fault', f'演示触发故障: charger_id={charger_id}, 策略={strategy}')
        return jsonify({'success': ok, 'message': msg,
                        'snapshot': _demo_snapshot()})

    if action == 'recover':
        charger_id = data.get('charger_id')
        if not charger_id:
            return jsonify({'success': False, 'message': '请指定充电桩'}), 400
        ok, msg = scheduler.recover_charger(int(charger_id))
        add_log('demo_recover', f'演示故障恢复: charger_id={charger_id}')
        return jsonify({'success': ok, 'message': msg,
                        'snapshot': _demo_snapshot()})

    if action == 'run_tests':
        import subprocess, sys as _sys
        test_file = os.path.join(os.path.dirname(__file__), 'test_fault_handling_7c.py')
        try:
            result = subprocess.run(
                [_sys.executable, '-X', 'utf8', test_file],
                capture_output=True, text=True, encoding='utf-8',
                timeout=180,
                cwd=os.path.dirname(__file__)
            )
            output = result.stdout + (result.stderr if result.stderr else '')
            passed = result.returncode == 0
        except subprocess.TimeoutExpired:
            output = '测试超时（120秒）'
            passed = False
        except Exception as e:
            output = f'运行测试出错: {e}'
            passed = False
        return jsonify({'success': True, 'test_passed': passed, 'output': output})

    return jsonify({'success': False, 'message': f'未知操作: {action}'}), 400


# ==================== 启动应用 ====================

def create_app():
    """创建并初始化应用"""
    init_db()
    scheduler.start_scheduler_thread()
    return app


if __name__ == '__main__':
    app = create_app()
    print("=" * 60)
    print("智能充电桩调度计费系统")
    print("=" * 60)
    s = settings.get_all()
    print(f"快充电桩: {s['FAST_CHARGING_PILE_NUM']}个, 功率: {s['FAST_CHARGING_POWER']}度/小时")
    print(f"慢充电桩: {s['TRICKLE_CHARGING_PILE_NUM']}个, 功率: {s['TRICKLE_CHARGING_POWER']}度/小时")
    print(f"等候区容量: {s['WAITING_AREA_SIZE']}")
    print(f"充电桩队列长度: {s['CHARGING_QUEUE_LEN']}")
    print(f"服务费单价: {s['SERVICE_FEE_RATE']}元/度")
    print(f"模拟速度: {s['SIMULATION_SPEED']}x")
    print("=" * 60)
    print("管理员账户: admin / admin123")
    print("=" * 60)
    app.run(host='0.0.0.0', port=5000, debug=True)
