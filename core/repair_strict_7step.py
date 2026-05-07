#!/usr/bin/env python3
"""
智能告警修复 - v5.2 最终修复版
修复问题:
1. 步骤2执行完成的准确判断
2. 步骤4状态检查不显示详细状态的问题
3. API查询失败时的错误处理

作者: OpenClaw
日期: 2026-03-27
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import auto_load_env
from config.config import DS_CONFIG, FUYAN_WORKFLOWS, REPAIR_CONFIG, TABLE_CONFIG, WORKSPACE_CONFIG

import json
import os
import urllib.request
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode

# 配置
WORKSPACE = WORKSPACE_CONFIG['root']
AUTO_REPAIR_RECORDS_DIR = WORKSPACE_CONFIG['auto_repair_records_dir']
DS_BASE = DS_CONFIG['base_url']
PROJECT_CODE = DS_CONFIG['project_code']
FUYAN_PROJECT_CODE = DS_CONFIG['fuyan_project_code']
DS_TOKEN = DS_CONFIG['token']
DS_ENVIRONMENT_CODE = DS_CONFIG['environment_code']
DS_TENANT_CODE = DS_CONFIG['tenant_code']
QUALITY_RESULT_TABLE = TABLE_CONFIG['quality_result_table']
MANUAL_REVIEW_STATE_FILE = WORKSPACE_CONFIG['manual_review_state_file']
SCAN_LOOKBACK_DAYS = REPAIR_CONFIG['scan_lookback_days']

# 维护任务关键词（排除）
MAINTENANCE_KEYWORDS = ['补充', '删除', '清理', '修复', '历史', '冗余', '临时', 'test', 'copy', '手插入']


def get_workflow_definition_detail(workflow_code):
    """兼容 DS 3.3 workflow-definition 与 DS 3.2 process-definition 详情接口"""
    endpoints = [
        f"/projects/{PROJECT_CODE}/workflow-definition/{workflow_code}",
        f"/projects/{PROJECT_CODE}/process-definition/{workflow_code}",
    ]
    last_msg = ""
    for endpoint in endpoints:
        success, detail, msg = ds_api_get(endpoint)
        if success:
            return True, detail, msg
        last_msg = msg
    return False, {}, last_msg


def get_workflow_definition_list():
    """兼容 DS 3.3 workflow-definition 与 DS 3.2 process-definition 列表接口"""
    endpoints = [
        f"/projects/{PROJECT_CODE}/workflow-definition?pageNo=1&pageSize=100",
        f"/projects/{PROJECT_CODE}/process-definition?pageNo=1&pageSize=100",
    ]
    last_msg = ""
    for endpoint in endpoints:
        success, data, msg = ds_api_get(endpoint)
        if success:
            return True, data, msg
        last_msg = msg
    return False, {}, last_msg


def get_instance_detail(project_code, instance_id):
    """兼容 DS 3.3 workflow-instances 与 DS 3.2 process-instances 详情接口"""
    endpoints = [
        f"/projects/{project_code}/workflow-instances/{instance_id}",
        f"/projects/{project_code}/process-instances/{instance_id}",
    ]
    last_msg = ""
    for endpoint in endpoints:
        success, data, msg = ds_api_get(endpoint)
        if success:
            return True, data, msg
        last_msg = msg
    return False, {}, last_msg


def get_fuyan_name(workflow):
    return workflow.get('name') or workflow.get('workflow_name') or '未命名复验工作流'


def get_fuyan_code(workflow):
    return workflow.get('code') or workflow.get('workflow_code') or ''


def normalize_fuyan_level(workflow):
    level = (workflow.get('level') or '').strip().lower()
    if level in {'all', '全级别'}:
        return 'all'
    if '1' in level:
        return '1'
    if '2' in level:
        return '2'
    if '3' in level:
        return '3'
    return level


def select_fuyan_workflows(alerts):
    """按旧策略智能选择复验工作流：每日全级别必跑，dwb 走1级，其余走3级"""
    selected_levels = set()
    for alert in alerts or []:
        table = (alert.get('table') or '').lower()
        if table.startswith('dwb_'):
            selected_levels.add('1')
        else:
            selected_levels.add('3')

    selected = []
    seen_codes = set()
    for workflow in FUYAN_WORKFLOWS:
        workflow_code = get_fuyan_code(workflow)
        workflow_level = normalize_fuyan_level(workflow)
        workflow_name = get_fuyan_name(workflow)
        include = False
        if workflow_level == 'all':
            include = workflow_name.startswith('每日复验全级别数据')
        elif workflow_level in selected_levels:
            include = True

        if include and workflow_code not in seen_codes:
            selected.append(workflow)
            seen_codes.add(workflow_code)
    return selected


def log(msg):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"[{ts}] {msg}", flush=True)


def ds_api_get(endpoint):
    """DS API GET请求"""
    url = f"{DS_BASE}{endpoint}"
    req = urllib.request.Request(url)
    req.add_header('token', DS_TOKEN)
    req.add_header('Accept', 'application/json, text/plain, */*')
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            result = json.loads(response.read().decode('utf-8'))
            return result.get('code') == 0, result.get('data', {}), result.get('msg', '')
    except Exception as e:
        return False, {}, str(e)


def ds_api_post(endpoint, data):
    """DS API POST请求"""
    url = f"{DS_BASE}{endpoint}"
    encoded_data = urlencode(data).encode('utf-8')
    req = urllib.request.Request(
        url, data=encoded_data,
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        method='POST'
    )
    req.add_header('token', DS_TOKEN)
    req.add_header('Accept', 'application/json, text/plain, */*')
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode('utf-8'))
            return result.get('code') == 0, result, result.get('msg', '')
    except Exception as e:
        return False, {}, str(e)


def normalize_to_datetime(value):
    """将数据库中的时间字段尽量标准化为 datetime"""
    if not value:
        return None

    if hasattr(value, 'strftime'):
        return value

    text = str(value).strip()
    for pattern in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d', '%Y-%m-%dT%H:%M:%S.%fZ'):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


def resolve_alert_dt(row, now=None):
    """解析告警对应的修复 dt，优先 begin，其次 end-1 天，最后兜底当天"""
    if now is None:
        now = datetime.now()

    begin_time = normalize_to_datetime(row.get('begin'))
    if begin_time:
        return begin_time.strftime('%Y-%m-%d')

    end_time = normalize_to_datetime(row.get('end'))
    if end_time:
        return (end_time - timedelta(days=1)).strftime('%Y-%m-%d')

    return now.strftime('%Y-%m-%d')


def get_table_layer_priority(db_name):
    """为不同数仓层级打分，分值越高越优先作为修复目标"""
    normalized = (db_name or '').strip().lower()
    priorities = {
        'ads': 4,
        'dm': 3,
        'dwd': 2,
        'dwb': 2,
        'ods': 1,
    }
    return priorities.get(normalized, 0)


def resolve_repair_table(row):
    """统一决定当前告警应该修复哪张表，尽量优先目标层和下游层"""
    src_db = row.get('src_db') or ''
    src_tbl = row.get('src_tbl') or ''
    dest_db = row.get('dest_db') or ''
    dest_tbl = row.get('dest_tbl') or ''

    candidates = []
    if dest_tbl:
        candidates.append((get_table_layer_priority(dest_db), 1, dest_tbl))
    if src_tbl:
        candidates.append((get_table_layer_priority(src_db), 0, src_tbl))

    if not candidates:
        return ''

    best_priority = max(priority for priority, _, _ in candidates)
    if best_priority > 0:
        prioritized = [item for item in candidates if item[0] == best_priority]
        prioritized.sort(key=lambda item: item[1], reverse=True)
        return prioritized[0][2]

    return dest_tbl or src_tbl


def count_remaining_alert_tables():
    """统计当前剩余未处理告警的去重表数，口径与扫描阶段保持一致"""
    return len(get_remaining_alert_tables())


def get_remaining_alert_tables():
    """查询当前数据库中仍未处理的去重告警表集合"""
    from alert.db_config import get_db_connection

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            sql = """
                SELECT src_db, src_tbl, dest_db, dest_tbl
                FROM {quality_result_table}
                WHERE result = 1 AND is_repaired = 0
                  AND created_at >= DATE_SUB(NOW(), INTERVAL {scan_lookback_days} DAY)
                ORDER BY created_at DESC
            """.format(
                quality_result_table=QUALITY_RESULT_TABLE,
                scan_lookback_days=SCAN_LOOKBACK_DAYS,
            )
            cursor.execute(sql)
            rows = cursor.fetchall()
    finally:
        conn.close()

    unique_tables = set()
    for row in rows:
        table_name = resolve_repair_table(row)
        if table_name:
            unique_tables.add(table_name)

    return unique_tables


def step1_scan_alerts():
    """步骤1: 扫描告警"""
    log("="*70)
    log("【步骤1】扫描告警")
    log("="*70)
    
    alerts = []
    try:
        from alert.db_config import get_db_connection
        conn = get_db_connection()
        with conn.cursor() as cursor:
            sql = """
                SELECT id, name, src_db, src_tbl, dest_db, dest_tbl, `begin`, `end`, diff
                FROM {quality_result_table}
                WHERE result = 1 AND is_repaired = 0
                  AND created_at >= DATE_SUB(NOW(), INTERVAL {scan_lookback_days} DAY)
                ORDER BY created_at DESC
            """.format(
                quality_result_table=QUALITY_RESULT_TABLE,
                scan_lookback_days=SCAN_LOOKBACK_DAYS,
            )
            cursor.execute(sql)
            rows = cursor.fetchall()
            
            for row in rows:
                table_name = resolve_repair_table(row)
                
                dt = resolve_alert_dt(row)
                
                alerts.append({
                    'id': row['id'],
                    'table': table_name,
                    'dt': dt,
                    'name': row.get('name', ''),
                    'diff': row.get('diff', '')
                })
        
        conn.close()
        log(f"✅ 查询到 {len(alerts)} 条异常记录")
        
    except Exception as e:
        log(f"❌ 查询数据库失败: {e}")
        return []
    
    # 去重
    table_alerts = {}
    for alert in alerts:
        table = alert['table']
        if table and table not in table_alerts:
            table_alerts[table] = alert
    
    unique_alerts = list(table_alerts.values())
    log(f"📊 去重后: {len(unique_alerts)} 个")
    for alert in unique_alerts[:5]:  # 只显示前5个
        log(f"  ✅ {alert['table']} (dt={alert['dt']})")
    if len(unique_alerts) > 5:
        log(f"  ... 还有 {len(unique_alerts)-5} 个")
    
    return unique_alerts


def step2_search_in_workflow(workflow_code, table_name):
    """在指定工作流中搜索表"""
    success, detail, msg = get_workflow_definition_detail(workflow_code)
    if not success:
        return None
    
    search_term = table_name.lower().replace('dwd_', '').replace('dwb_', '').replace('ods_', '')
    tasks = detail.get('taskDefinitionList', [])
    
    for task in tasks:
        task_name = task.get('name', '')
        task_name_lower = task_name.lower()
        
        # 排除维护任务
        is_maintenance = any(kw in task_name_lower for kw in MAINTENANCE_KEYWORDS)
        if is_maintenance:
            continue
        
        # 匹配任务名
        if search_term in task_name_lower:
            return {
                'workflow_code': workflow_code,
                'workflow_name': detail.get('processDefinition', {}).get('name', ''),
                'task_code': task.get('code'),
                'task_name': task_name
            }
        
        # 匹配SQL
        task_params = task.get('taskParams', '{}')
        if isinstance(task_params, str):
            try:
                task_params = json.loads(task_params)
            except:
                task_params = {}
        
        sql = task_params.get('sql', '').lower()
        if search_term in sql:
            return {
                'workflow_code': workflow_code,
                'workflow_name': detail.get('processDefinition', {}).get('name', ''),
                'task_code': task.get('code'),
                'task_name': task_name
            }
    
    return None


def step2_find_locations(alerts):
    """步骤2: 查找工作流位置 - 优化版（缓存工作流列表）"""
    log("\n" + "="*70)
    log("【步骤2】查找工作流位置")
    log("="*70)
    
    # 优先搜索这些工作流（提高效率）
    priority_workflows = [
        ('158514956979200', 'DWD'),
        ('158514957656064', 'DWD(D-1)'),
        ('158514958374912', '国内-数仓工作流(H-1)'),
        ('158514957337600', '国内-数仓工作流(D-1)'),
        ('158514957297664', 'DWB'),
        ('158514957701120', 'DWB(D-1)'),
        ('158514957779968', 'DWS'),
        ('158514958004224', 'DWS(D-1)'),
    ]
    
    # 缓存所有工作流列表（只获取一次）
    all_workflows = None
    
    tasks = []
    found_count = 0
    
    for alert in alerts:
        table = alert['table']
        log(f"🔍 {table}")
        
        location = None
        # 先在优先工作流中搜索
        for wf_code, wf_name in priority_workflows:
            result = step2_search_in_workflow(wf_code, table)
            if result:
                location = result
                break
        
        # 如果没找到，再搜索所有工作流（使用缓存）
        if not location:
            if all_workflows is None:
                log(f"  在优先工作流中未找到，获取所有工作流列表...")
                success, data, msg = get_workflow_definition_list()
                if success:
                    all_workflows = data.get('totalList', [])
                    log(f"  获取到 {len(all_workflows)} 个工作流")
                else:
                    log(f"  ❌ 获取工作流列表失败: {msg}")
                    all_workflows = []
            
            # 在缓存的工作流中搜索
            for wf in all_workflows:
                wf_code = wf.get('code')
                # 跳过已在priority中搜索过的工作流
                if wf_code not in [pw[0] for pw in priority_workflows]:
                    result = step2_search_in_workflow(wf_code, table)
                    if result:
                        location = result
                        break
        
        if location:
            task = {
                'alert_id': alert['id'],
                'table': table,
                'dt': alert['dt'],
                'workflow_code': location['workflow_code'],
                'workflow_name': location['workflow_name'],
                'task_code': location['task_code'],
                'task_name': location['task_name']
            }
            log(f"  ✅ {location['workflow_name']} -> {location['task_name']}")
            found_count += 1
        else:
            task = {
                'alert_id': alert['id'],
                'table': table,
                'dt': alert['dt'],
                'workflow_code': '',
                'workflow_name': '未找到',
                'task_code': '',
                'task_name': ''
            }
            log(f"  ❌ 未找到")
        
        tasks.append(task)
    
    log(f"\n📊 找到 {found_count}/{len(alerts)} 个工作流")
    return tasks


def load_manual_review_state():
    """加载人工处理策略状态"""
    if not os.path.exists(MANUAL_REVIEW_STATE_FILE):
        return {}

    try:
        with open(MANUAL_REVIEW_STATE_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        log(f"⚠️ 读取人工处理状态失败: {e}")
        return {}


def save_manual_review_state(state):
    """保存人工处理策略状态"""
    os.makedirs(os.path.dirname(MANUAL_REVIEW_STATE_FILE), exist_ok=True)
    with open(MANUAL_REVIEW_STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def is_suspected_redundant_data(task):
    """根据 diff 判断是否为疑似冗余数据告警"""
    diff = task.get('diff')
    if diff in (None, ''):
        return False

    try:
        return float(diff) < 0
    except (TypeError, ValueError):
        return False


def build_redundant_data_manual_review_reason():
    """构造疑似冗余/底层少数场景的人工处理提示"""
    return '疑似当前层数据多于底层，重跑一次后仍未恢复，建议检查底层是否需要删数，并人工判断修复'


def apply_repair_strategy(tasks, strategy_state):
    """应用修复策略：疑似冗余数据仅允许自动重跑一次"""
    runnable_tasks = []
    manual_review_tasks = []

    for task in tasks:
        if not is_suspected_redundant_data(task):
            runnable_tasks.append(task)
            continue

        table_state = strategy_state.get(task['table'], {}).get(task['dt'], {})
        if table_state.get('redundant_retry_done'):
            manual_task = dict(task)
            manual_task['status'] = 'skipped_manual_review'
            manual_task['error'] = build_redundant_data_manual_review_reason()
            manual_review_tasks.append(manual_task)
        else:
            runnable_tasks.append(task)

    return runnable_tasks, manual_review_tasks


def record_redundant_retry_attempt(strategy_state, completed_tasks):
    """记录疑似冗余数据告警的首次自动重跑尝试"""
    for task in completed_tasks:
        if not is_suspected_redundant_data(task):
            continue

        table_state = strategy_state.setdefault(task['table'], {})
        dt_state = table_state.setdefault(task['dt'], {})
        dt_state['redundant_retry_done'] = True
        dt_state.setdefault('manual_review_required', False)
        dt_state['last_completed_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def record_manual_review_tasks(strategy_state, manual_review_tasks):
    """记录需要人工处理的任务状态"""
    for task in manual_review_tasks:
        table_state = strategy_state.setdefault(task['table'], {})
        dt_state = table_state.setdefault(task['dt'], {})
        dt_state['redundant_retry_done'] = True
        dt_state['manual_review_required'] = True
        dt_state['reason'] = task.get('error', '需人工处理')
        dt_state['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def step3_start_repair(tasks):
    """步骤3: 启动修复任务"""
    log("\n" + "="*70)
    log("【步骤3】启动修复任务")
    log("="*70)
    
    results = []
    running_instances = []
    
    for i, task in enumerate(tasks, 1):
        table = task['table']
        dt = task['dt']
        workflow_code = task.get('workflow_code')
        task_code = task.get('task_code')
        
        if not workflow_code or not task_code:
            log(f"[{i}/{len(tasks)}] ⏭️ {table} - 未找到工作流")
            task['status'] = 'skipped_no_workflow'
            results.append(task)
            continue
        
        log(f"\n[{i}/{len(tasks)}] {table}")
        log(f"  工作流: {task['workflow_name']}")
        log(f"  任务: {task['task_name']}")
        
        # 启动修复
        data = {
            'processDefinitionCode': workflow_code,
            'startNodeList': task_code,
            'taskDependType': 'TASK_ONLY',
            'failureStrategy': 'CONTINUE',
            'warningType': 'NONE',
            'warningGroupId': 0,
            'execType': 'START_PROCESS',
            'startParams': json.dumps({'global': [{'prop': 'dt', 'value': dt}]}),
            'environmentCode': DS_ENVIRONMENT_CODE,
            'tenantCode': DS_TENANT_CODE,
            'dryRun': 0,
            'scheduleTime': ''
        }
        
        success, result, msg = ds_api_post(f"/projects/{PROJECT_CODE}/executors/start-process-instance", data)
        
        if success:
            instance_data = result.get('data')
            # DS 3.3.0 返回的是数组格式 [id]，需要取第一个元素
            if isinstance(instance_data, list) and len(instance_data) > 0:
                instance_id = instance_data[0]
            else:
                instance_id = instance_data
            log(f"  ✅ 启动成功，实例ID: {instance_id}")
            task['status'] = 'success'
            task['instance_id'] = instance_id
            running_instances.append({
                'table': table,
                'instance_id': instance_id,
                'task': task
            })
        else:
            error_msg = result.get('msg', '未知错误')
            log(f"  ❌ 启动失败: {error_msg}")
            task['status'] = 'failed'
            task['error'] = error_msg
        
        results.append(task)
        time.sleep(1)
    
    log(f"\n📊 启动结果: {len(running_instances)} 个成功, {len(tasks) - len(running_instances)} 个跳过/失败")
    return results, running_instances


def step4_wait_and_check(running_instances, poll_interval=30, max_wait=1800):
    """步骤4: 动态监控任务状态（每30秒检查一次）- 修复版（增加失败次数限制）"""
    if not running_instances:
        log("\n  没有需要等待的任务")
        return [], []
    
    log("\n" + "="*70)
    log("【步骤4】动态监控任务状态")
    log("="*70)
    log(f"  共 {len(running_instances)} 个任务")
    log(f"  轮询间隔: {poll_interval}秒")
    log(f"  最大等待: {max_wait}秒 (30分钟)")
    
    start_time = time.time()
    completed_tasks = []
    failed_tasks = []
    pending = running_instances.copy()
    
    # 初始化失败计数
    for item in pending:
        item['fail_count'] = 0
    
    check_count = 0
    
    while pending and (time.time() - start_time) < max_wait:
        elapsed = int(time.time() - start_time)
        check_count += 1
        log(f"\n⏱️  第{check_count}次检查 (已等待 {elapsed}秒)")
        log("-" * 70)
        
        still_pending = []
        status_changed = False
        
        for item in pending:
            table = item['table']
            instance_id = item['instance_id']
            
            # 查询实例状态
            success, data, msg = get_instance_detail(PROJECT_CODE, instance_id)
            
            if success and data:
                state = data.get('state', 'UNKNOWN')
                
                if state in ['SUCCESS', 'FINISHED']:
                    log(f"  ✅ {table}: 完成 ({state})")
                    item['task']['final_status'] = 'success'
                    item['task']['end_time'] = data.get('endTime')
                    completed_tasks.append(item['task'])
                    status_changed = True
                elif state in ['FAILED', 'KILL', 'STOP']:
                    log(f"  ❌ {table}: 失败 ({state})")
                    item['task']['final_status'] = 'failed'
                    item['task']['error'] = f"状态: {state}"
                    failed_tasks.append(item['task'])
                    status_changed = True
                else:
                    # 仍在运行中，重置失败计数
                    item['fail_count'] = 0
                    log(f"  ⏳ {table}: {state}")
                    still_pending.append(item)
            else:
                # 查询失败，增加失败计数
                item['fail_count'] += 1
                if item['fail_count'] >= 3:
                    log(f"  ❌ {table}: 查询失败超过3次，标记为失败 ({msg})")
                    item['task']['final_status'] = 'failed'
                    item['task']['error'] = f"查询失败: {msg}"
                    failed_tasks.append(item['task'])
                    status_changed = True
                else:
                    log(f"  ⚠️  {table}: 查询失败 ({msg})，第{item['fail_count']}次")
                    still_pending.append(item)
        
        pending = still_pending
        
        # 显示汇总
        log("-" * 70)
        log(f"📊 当前状态: 成功={len(completed_tasks)}, 失败={len(failed_tasks)}, 运行中={len(pending)}")
        
        # 如果都完成了，立即退出
        if not pending:
            log(f"\n🎉 所有任务已完成！")
            break
        
        # 等待下一轮
        log(f"  还有 {len(pending)} 个任务运行中，{poll_interval}秒后再次检查...")
        time.sleep(poll_interval)
    
    # 处理超时任务
    if pending:
        log(f"\n⚠️  等待超时，以下任务未完成:")
        for item in pending:
            log(f"    - {item['table']}: {item['instance_id']}")
            item['task']['final_status'] = 'timeout'
            failed_tasks.append(item['task'])
    
    log(f"\n📊 最终结果:")
    log(f"  ✅ 成功: {len(completed_tasks)} 个")
    log(f"  ❌ 失败/超时: {len(failed_tasks)} 个")
    
    return completed_tasks, failed_tasks


def execute_repairs_in_batches(tasks, max_parallel=5):
    """分批执行修复任务，控制同时运行的实例数量"""
    if max_parallel <= 0:
        raise ValueError("max_parallel must be greater than 0")

    all_results = []
    all_completed_tasks = []
    all_failed_tasks = []

    total_batches = (len(tasks) + max_parallel - 1) // max_parallel

    for batch_index, start in enumerate(range(0, len(tasks), max_parallel), 1):
        batch_tasks = tasks[start:start + max_parallel]
        log("\n" + "=" * 70)
        log(f"【批次 {batch_index}/{total_batches}】执行 {len(batch_tasks)} 个修复任务")
        log("=" * 70)

        batch_results, running_instances = step3_start_repair(batch_tasks)
        completed_tasks, failed_tasks = step4_wait_and_check(running_instances)

        all_results.extend(batch_results)
        all_completed_tasks.extend(completed_tasks)
        all_failed_tasks.extend(failed_tasks)

    return all_results, all_completed_tasks, all_failed_tasks


def step5_execute_fuyan(completed_tasks, failed_tasks, alerts):
    """步骤5: 执行复验"""
    log("\n" + "="*70)
    log("【步骤5】执行复验")
    log("="*70)

    if not completed_tasks:
        log("\n5.1 跳过复验：本次没有实际完成的重跑任务")
        return []
    
    # 记录重跑次数
    log("\n5.1 记录重跑次数...")
    record_file = WORKSPACE_CONFIG['repair_counts_file']
    os.makedirs(os.path.dirname(record_file), exist_ok=True)
    counts = {}
    if os.path.exists(record_file):
        with open(record_file, 'r') as f:
            counts = json.load(f)
    
    today = datetime.now().strftime('%Y-%m-%d')
    for task in completed_tasks:
        table = task['table']
        if table not in counts:
            counts[table] = {}
        counts[table][today] = counts[table].get(today, 0) + 1
        log(f"  📝 {table}: 今日第{counts[table][today]}次")
    
    with open(record_file, 'w') as f:
        json.dump(counts, f, indent=2)
    
    # 执行复验
    log(f"\n5.2 执行复验工作流...")
    fuyan_workflows = select_fuyan_workflows(alerts)
    fuyan_results = []

    log(f"  选中复验工作流: {len(fuyan_workflows)} 个")
    
    for i, fuyan in enumerate(fuyan_workflows, 1):
        fuyan_name = get_fuyan_name(fuyan)
        fuyan_code = get_fuyan_code(fuyan)
        log(f"  [{i}] {fuyan_name}")
        
        data = {
            'processDefinitionCode': fuyan_code,
            'failureStrategy': 'CONTINUE',
            'warningType': 'NONE',
            'warningGroupId': 0,
            'processInstancePriority': 'MEDIUM',
            'workerGroup': 'default',
            'execType': 'START_PROCESS',
            'environmentCode': DS_ENVIRONMENT_CODE,
            'tenantCode': DS_TENANT_CODE,
            'taskDependType': 'TASK_POST',
            'runMode': 'RUN_MODE_SERIAL',
            'dryRun': 0,
            'scheduleTime': ''
        }
        
        success, result, msg = ds_api_post(f"/projects/{FUYAN_PROJECT_CODE}/executors/start-process-instance", data)
        if success:
            instance_id = result.get('data')
            if isinstance(instance_id, list) and len(instance_id) > 0:
                instance_id = instance_id[0]
            log(f"    ✅ 启动成功: {instance_id}")
            fuyan_results.append({'name': fuyan_name, 'id': instance_id, 'status': 'success'})
        else:
            error_msg = result.get('msg', '未知错误')
            log(f"    ❌ 启动失败: {error_msg}")
            fuyan_results.append({'name': fuyan_name, 'status': 'failed', 'error': error_msg})
    
    return fuyan_results


def wait_for_fuyan_results(fuyan_results, poll_interval=30, max_wait=1800):
    """等待已启动的复验工作流完成，补充最终状态"""
    running_results = [
        dict(item)
        for item in fuyan_results
        if item.get('status') == 'success' and item.get('id')
    ]
    if not running_results:
        return fuyan_results

    start_time = time.time()
    pending = {str(item['id']): item for item in running_results}

    while pending and (time.time() - start_time) < max_wait:
        still_pending = {}
        for instance_id, item in pending.items():
            success, data, msg = ds_api_get(
                f"/projects/{FUYAN_PROJECT_CODE}/workflow-instances/{instance_id}"
            )
            if not success or not data:
                success, data, msg = ds_api_get(
                    f"/projects/{FUYAN_PROJECT_CODE}/process-instances/{instance_id}"
                )
            if not success or not data:
                item['final_status'] = 'query_failed'
                item['error'] = msg or '查询复验实例状态失败'
                still_pending[instance_id] = item
                continue

            state = data.get('state', 'UNKNOWN')
            item['state'] = state
            item['end_time'] = data.get('endTime')
            if state in ['SUCCESS', 'FINISHED']:
                item['final_status'] = 'success'
            elif state in ['FAILED', 'KILL', 'STOP']:
                item['final_status'] = 'failed'
                item['error'] = f"状态: {state}"
            else:
                still_pending[instance_id] = item

        pending = still_pending
        if pending:
            time.sleep(poll_interval)

    for item in pending.values():
        item['final_status'] = 'timeout'
        item.setdefault('error', '等待复验结果超时')

    final_results = []
    running_by_id = {str(item['id']): item for item in running_results}
    for item in fuyan_results:
        instance_id = item.get('id')
        if instance_id is not None and str(instance_id) in running_by_id:
            final_results.append(running_by_id[str(instance_id)])
        else:
            final_results.append(item)
    return final_results


def summarize_repair_outcome(alerts, completed_tasks, failed_tasks, manual_review_tasks, remaining_tables):
    """基于复验后的数据库状态汇总最终修复结果"""
    initial_alerts = []
    seen_tables = set()
    for alert in alerts:
        table = alert.get('table')
        if table and table not in seen_tables:
            seen_tables.add(table)
            initial_alerts.append(dict(alert))

    initial_by_table = {item['table']: item for item in initial_alerts}
    completed_by_table = {item['table']: item for item in completed_tasks if item.get('table')}
    failed_by_table = {item['table']: item for item in failed_tasks if item.get('table')}
    manual_by_table = {item['table']: item for item in manual_review_tasks if item.get('table')}

    rerun_tasks = []
    resolved_tasks = []
    remaining_tasks = []

    for alert in initial_alerts:
        table = alert['table']
        if table in completed_by_table or table in failed_by_table:
            rerun_task = dict(alert)
            rerun_task.update(completed_by_table.get(table, {}))
            if table in failed_by_table:
                rerun_task.update(failed_by_table[table])
            rerun_tasks.append(rerun_task)

        if table not in remaining_tables:
            resolved_task = dict(alert)
            resolved_task.update(completed_by_table.get(table, {}))
            resolved_task['result'] = 'resolved'
            resolved_tasks.append(resolved_task)
            continue

        remaining_task = dict(alert)
        remaining_task.update(completed_by_table.get(table, {}))
        if table in failed_by_table:
            remaining_task.update(failed_by_table[table])
        if table in manual_by_table:
            remaining_task.update(manual_by_table[table])
        remaining_task['result'] = 'manual_review'
        if is_suspected_redundant_data(remaining_task):
            remaining_task.setdefault('error', build_redundant_data_manual_review_reason())
        else:
            remaining_task.setdefault('error', '复验完成后告警仍存在，需人工处理')
        remaining_tasks.append(remaining_task)

    return {
        'initial_alert_count': len(initial_alerts),
        'resolved_count': len(resolved_tasks),
        'remaining_count': len(remaining_tasks),
        'manual_review_count': len(remaining_tasks),
        'rerun_tasks': rerun_tasks,
        'resolved_tasks': resolved_tasks,
        'remaining_tasks': remaining_tasks,
        'post_fuyan_remaining_tables': set(remaining_tables),
    }


def evaluate_repair_outcome(alerts, completed_tasks, failed_tasks, manual_review_tasks, fuyan_results):
    """等待复验完成后，再根据数据库回查结果判断最终修复成败"""
    log("\n5.3 等待复验完成并回查告警结果...")
    final_fuyan_results = wait_for_fuyan_results(fuyan_results)
    remaining_tables = get_remaining_alert_tables()
    log(f"  📋 复验后数据库仍未处理告警表: {len(remaining_tables)} 个")
    summary = summarize_repair_outcome(
        alerts=alerts,
        completed_tasks=completed_tasks,
        failed_tasks=failed_tasks,
        manual_review_tasks=manual_review_tasks,
        remaining_tables=remaining_tables,
    )
    return summary, final_fuyan_results


def step6_save_report(results, completed_tasks, failed_tasks, final_fuyan_results, summary, manual_review_tasks=None):
    """步骤6: 保存记录并发送TV报告"""
    if manual_review_tasks is None:
        manual_review_tasks = []

    log("\n" + "="*70)
    log("【步骤6】保存记录")
    log("="*70)
    
    record_dir = f"{AUTO_REPAIR_RECORDS_DIR}/{datetime.now().strftime('%Y-%m-%d')}"
    os.makedirs(record_dir, exist_ok=True)
    
    detail_file = f"{record_dir}/detail_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    summary_for_json = dict(summary)
    summary_for_json['post_fuyan_remaining_tables'] = sorted(summary['post_fuyan_remaining_tables'])
    with open(detail_file, 'w') as f:
        json.dump({
            'timestamp': datetime.now().isoformat(),
            'results': results,
            'completed_tasks': completed_tasks,
            'failed_tasks': failed_tasks,
            'manual_review_tasks': manual_review_tasks,
            'fuyan_results': final_fuyan_results,
            'summary': summary_for_json,
        }, f, indent=2, ensure_ascii=False)
    
    log(f"  ✅ 记录已保存: {detail_file}")
    
    # 生成TV报告内容
    tv_report = generate_tv_report(summary, final_fuyan_results)
    
    # 发送TV报告到钉钉
    send_tv_report_to_dingtalk(tv_report)


def generate_tv_report(summary, fuyan_results):
    """生成TV格式报告"""
    report_lines = []
    report_lines.append("📺 【智能告警修复报告】")
    report_lines.append("")
    report_lines.append(f"⏰ 执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    report_lines.append("")
    
    report_lines.append("📊 本次执行统计:")
    report_lines.append(f"  • 初始去重告警: {summary['initial_alert_count']} 个")
    report_lines.append(f"  • 复验后已消失: {summary['resolved_count']} 个")
    report_lines.append(f"  • 复验后仍存在: {summary['remaining_count']} 个")
    report_lines.append(f"  • 需人工处理: {summary['manual_review_count']} 个")
    report_lines.append(f"  • 复验启动: {len(fuyan_results)} 个")
    report_lines.append("")
    
    report_lines.append(
        f"📋 当前未处理告警表: {len(summary['post_fuyan_remaining_tables'])} 个"
    )
    report_lines.append("")

    if summary.get('rerun_tasks'):
        report_lines.append("🔁 【本次已重跑任务】")
        for task in summary['rerun_tasks']:
            report_lines.append(f"  • {task['table']}")
            if task.get('instance_id'):
                report_lines.append(f"    实例ID: {task['instance_id']}")
            if task.get('end_time'):
                report_lines.append(f"    完成时间: {task['end_time']}")
            elif task.get('error'):
                report_lines.append(f"    结果: {task['error']}")
        report_lines.append("")
    
    if summary['resolved_tasks']:
        report_lines.append("✅ 【复验后已消失】")
        for task in summary['resolved_tasks']:
            report_lines.append(f"  • {task['table']}")
            if task.get('end_time'):
                report_lines.append(f"    完成时间: {task['end_time']}")
        report_lines.append("")
    
    if summary['remaining_tasks']:
        report_lines.append("⚠️ 【复验后仍存在，需人工处理】")
        for task in summary['remaining_tasks']:
            report_lines.append(f"  • {task['table']}")
            report_lines.append(f"    原因: {task.get('error', '复验完成后告警仍存在，需人工处理')}")
        report_lines.append("")
    
    report_lines.append("🔄 【复验工作流状态】")
    for fuyan in fuyan_results:
        final_status = fuyan.get('final_status')
        if final_status == 'success':
            report_lines.append(f"  ✅ {fuyan['name']}")
        elif final_status in ['failed', 'timeout', 'query_failed']:
            report_lines.append(f"  ❌ {fuyan['name']}")
            report_lines.append(f"     原因: {fuyan.get('error', final_status)}")
        elif fuyan.get('status') == 'success':
            report_lines.append(f"  ⏳ {fuyan['name']}")
            report_lines.append("     状态: 已启动，等待结果中")
        else:
            report_lines.append(f"  ❌ {fuyan['name']}")
            if fuyan.get('error'):
                report_lines.append(f"     错误: {fuyan['error']}")
        if fuyan.get('id'):
            report_lines.append(f"     实例ID: {fuyan['id']}")
    report_lines.append("")
    
    # 结尾
    report_lines.append("=" * 40)
    report_lines.append("📌 智能告警修复系统自动生成")
    
    return "\n".join(report_lines)


def send_tv_report_to_dingtalk(report_content):
    """发送TV报告到钉钉群"""
    log("\n" + "="*70)
    log("【发送TV报告到钉钉群】")
    log("="*70)
    
    try:
        # 保存报告到文件
        report_file = (
            f"{AUTO_REPAIR_RECORDS_DIR}/{datetime.now().strftime('%Y-%m-%d')}/"
            f"tv_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        )
        with open(report_file, 'w', encoding='utf-8') as f:
            f.write(report_content)
        
        log(f"✅ TV报告已生成: {report_file}")
        
        # 优先直接调用TV API，避免依赖本机 openclaw 命令
        try:
            from core.send_tv_report import send_tv_report

            mentions_env = os.environ.get('TV_MENTIONS', '').strip()
            mentions = [m.strip() for m in mentions_env.split(',') if m.strip()]
            result = send_tv_report(report_content, mentions=mentions)

            if result.get('success'):
                log(f"✅ TV报告已直接发送到TV API (HTTP {result.get('status_code')})")
            else:
                log(
                    "⚠️ 直接发送TV API失败: "
                    f"HTTP {result.get('status_code')}, {result.get('response')}"
                )
        except Exception as e:
            log(f"⚠️ 尝试直接发送TV API失败: {e}")
        
        # 兜底：控制台输出，便于n8n继续采集日志
        print(f"\n{'='*50}")
        print("📺 TV告警修复报告")
        print(f"{'='*50}")
        print(report_content)
        print(f"{'='*50}\n")
        
        log("✅ TV报告已输出到控制台")
        
    except Exception as e:
        log(f"❌ 发送TV报告时出错: {e}")
        import traceback
        traceback.print_exc()
    
    log("\n" + "="*70)


def main():
    """主函数"""
    log("="*70)
    log("🚀 智能告警修复流程（v5.2 最终修复版）")
    log("="*70)
    log(f"⏰ 执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log("")
    
    # 步骤1: 扫描告警
    alerts = step1_scan_alerts()
    if not alerts:
        log("\n✅ 没有需要处理的告警，流程结束")
        return
    
    # 步骤2: 查找工作流
    tasks = step2_find_locations(alerts)

    # 策略判断：疑似冗余数据告警首次允许重跑，后续转人工处理
    strategy_state = load_manual_review_state()
    runnable_tasks, manual_review_tasks = apply_repair_strategy(tasks, strategy_state)
    
    # 步骤3-4: 分批启动修复并动态监控（最多并行5个）
    results, completed_tasks, failed_tasks = execute_repairs_in_batches(runnable_tasks, max_parallel=5)

    record_redundant_retry_attempt(strategy_state, completed_tasks)
    record_manual_review_tasks(strategy_state, manual_review_tasks)
    save_manual_review_state(strategy_state)

    if manual_review_tasks:
        log("\n⚠️ 以下任务疑似冗余数据，已转人工处理，不再自动重跑:")
        for task in manual_review_tasks:
            log(f"  - {task['table']}: {task['error']}")

    results.extend(manual_review_tasks)

    if completed_tasks:
        # 步骤5: 执行复验
        fuyan_results = step5_execute_fuyan(completed_tasks, failed_tasks, alerts)

        summary, final_fuyan_results = evaluate_repair_outcome(
            alerts=alerts,
            completed_tasks=completed_tasks,
            failed_tasks=failed_tasks,
            manual_review_tasks=manual_review_tasks,
            fuyan_results=fuyan_results,
        )
    else:
        log("\n⚠️ 本次没有成功启动并完成的修复任务，跳过复验和复验回查")
        remaining_tables = get_remaining_alert_tables()
        summary = summarize_repair_outcome(
            alerts=alerts,
            completed_tasks=completed_tasks,
            failed_tasks=failed_tasks,
            manual_review_tasks=manual_review_tasks,
            remaining_tables=remaining_tables,
        )
        final_fuyan_results = []
    
    # 步骤6: 保存记录并发送TV报告
    step6_save_report(
        results,
        completed_tasks,
        failed_tasks,
        final_fuyan_results,
        summary,
        manual_review_tasks=manual_review_tasks,
    )
    
    log("\n" + "="*70)
    log("✅ 流程完成")
    log("="*70)


if __name__ == '__main__':
    main()
