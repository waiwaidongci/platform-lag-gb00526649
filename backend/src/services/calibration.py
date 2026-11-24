"""声级计基准换算的业务服务：录入、撤回、基准指定、待更新标记与原子发布。"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from src.domain import calibration as domain
from src.models.calibration import (
    Comparison,
    ComparisonBatch,
    ConversionCursor,
    ConversionRelease,
    Meter,
)
from src.services.audit import record
from src.utils.time import now

LOCK_ORDER = None  # 仅用于文档：所有写事务都按 meter.id 升序加行锁，避免死锁。


# ---------------------------------------------------------------- 基础读取

def _lock_meters(db):
    """锁定全部表计行，把上传/撤回/基准变更/发布串行化。"""
    return list(db.scalars(select(Meter).order_by(Meter.id).with_for_update()))


def _load_edges(db, meters):
    meter_ids = {meter.id for meter in meters}
    rows = db.scalars(select(Comparison).where(Comparison.status == domain.EDGE_ACTIVE))
    edges = []
    code_by_id = {meter.id: meter.meter_code for meter in meters}
    platform_by_code = {meter.meter_code: meter.platform for meter in meters}
    for row in rows:
        if row.meter_a_id not in meter_ids or row.meter_b_id not in meter_ids:
            continue
        edges.append(domain.Edge(
            comparison_id=row.id,
            batch_key=row.batch_id and str(row.batch_id),
            a=code_by_id[row.meter_a_id],
            b=code_by_id[row.meter_b_id],
            low=float(row.deviation_low),
            high=float(row.deviation_high),
        ))
    return edges, platform_by_code


def _baselines_by_platform(meters):
    return {meter.platform: meter.meter_code for meter in meters if meter.is_baseline}


def _solve(db, meters):
    edges, platform_by_code = _load_edges(db, meters)
    codes = [meter.meter_code for meter in meters]
    baselines = _baselines_by_platform(meters)
    results = domain.solve_groups(codes, platform_by_code, baselines, edges)
    return results, edges


def _mark_cursors_stale(db, meter_codes, reason):
    """受影响组的旧结果一律标待更新，不立即删除，等算齐后整体切换。"""
    cursors = db.scalars(select(ConversionCursor).where(ConversionCursor.stale.is_(False))
                         .with_for_update())
    touched = []
    affected = set(meter_codes)
    for cursor in cursors:
        members = set((cursor.current_result or {}).get('meters', []))
        if members & affected:
            cursor.stale = True
            cursor.stale_reason = reason
            cursor.updated_at = now()
            touched.append(cursor.group_key)
    return touched


# ---------------------------------------------------------------- 表计与基准

def create_meter(db, meter_code, label, platform, is_baseline, actor):
    existing = db.scalar(select(Meter).where(Meter.meter_code == meter_code))
    if existing:
        raise ValueError('该声级计代码已存在，请直接选用或修改台账')
    _lock_meters(db)
    if is_baseline:
        _ensure_no_other_baseline(db, platform, exclude_id=None)
    meter = Meter(meter_code=meter_code, label=label, platform=platform,
                  is_baseline=is_baseline, author=actor)
    db.add(meter)
    db.flush()
    if is_baseline:
        _mark_cursors_stale(db, [meter_code], '站台基准表发生变化，旧换算结果待更新')
    record(db, actor, 'calibration.meter_created', meter.id, meter_code=meter_code,
           platform=platform, is_baseline=is_baseline)
    return meter


def _ensure_no_other_baseline(db, platform, exclude_id):
    others = db.scalars(select(Meter).where(Meter.platform == platform, Meter.is_baseline.is_(True))
                        .with_for_update()).all()
    for other in others:
        if other.id != exclude_id:
            raise ValueError(f'站台 {platform} 已指定基准表 {other.meter_code}，请先取消原基准')


def set_baseline(db, meter_id, is_baseline, actor):
    _lock_meters(db)
    meter = db.get(Meter, meter_id, with_for_update=True)
    if not meter:
        raise ValueError('找不到该声级计')
    if is_baseline:
        _ensure_no_other_baseline(db, meter.platform, exclude_id=meter.id)
    if meter.is_baseline != is_baseline:
        meter.is_baseline = is_baseline
        _mark_cursors_stale(db, [meter.meter_code],
                            '站台基准表发生变化，旧换算结果待更新')
        record(db, actor, 'calibration.baseline_changed', meter.id,
               meter_code=meter.meter_code, is_baseline=is_baseline)
    return meter


# ---------------------------------------------------------------- 比测录入

def submit_comparisons(db, batch_key, note, measured_at, items, actor):
    """录入一批同点比测。同 batch_key 重试返回原批次，不重复落库、不重复发布。"""
    if not items:
        raise ValueError('批次至少包含一条同点比测记录')
    existing = db.scalar(select(ComparisonBatch).where(ComparisonBatch.batch_key == batch_key))
    if existing:
        return existing, False, []
    _lock_meters(db)
    # 锁内复查，挡住并发的同版提交
    existing = db.scalar(select(ComparisonBatch).where(ComparisonBatch.batch_key == batch_key)
                         .with_for_update())
    if existing:
        return existing, False, []

    # 先做全部只读校验，任何一条不通过都不写库
    meters_by_code = {meter.meter_code: meter for meter in
                      db.scalars(select(Meter).with_for_update())}
    prepared = []
    seen_pairs = set()
    affected = set()
    for index, item in enumerate(items, 1):
        a = meters_by_code.get(item['meter_a'])
        b = meters_by_code.get(item['meter_b'])
        if not a or not b:
            raise ValueError(f'第 {index} 条比测引用的声级计不存在，请先建台账')
        if a.id == b.id:
            raise ValueError(f'第 {index} 条比测的两台表不能相同')
        if a.platform != b.platform:
            raise ValueError(f'第 {index} 条比测跨站台，同点比测只能在同一站台内进行')
        low, high = float(item['deviation_low']), float(item['deviation_high'])
        if low > high:
            raise ValueError(f'第 {index} 条比测的偏差下界不能大于上界')
        pair = (a.meter_code, b.meter_code)
        if pair in seen_pairs:
            raise ValueError(f'第 {index} 条比测与批次内其他记录重复（相同的有序表对）')
        seen_pairs.add(pair)
        prepared.append((a, b, low, high))
        affected.update((a.meter_code, b.meter_code))

    # 全部写操作放进同一个保存点：后续异常会整体回滚，批次头不会残留
    try:
        with db.begin_nested():
            batch = ComparisonBatch(batch_key=batch_key, note=note, author=actor)
            db.add(batch)
            db.flush()
            for a, b, low, high in prepared:
                db.add(Comparison(batch_id=batch.id, meter_a_id=a.id, meter_b_id=b.id,
                                  deviation_low=low, deviation_high=high,
                                  measured_at=measured_at, status=domain.EDGE_ACTIVE))
            touched = _mark_cursors_stale(db, sorted(affected),
                                          '有新的同点比测录入，旧换算结果待更新')
            record(db, actor, 'calibration.comparisons_submitted', batch.id,
                   batch_key=batch_key, items=len(prepared), stale_groups=touched)
    except IntegrityError:
        prior = db.scalar(select(ComparisonBatch).where(ComparisonBatch.batch_key == batch_key))
        return prior, False, []
    return batch, True, sorted(affected)


def withdraw_comparison(db, comparison_id, reason, actor):
    _lock_meters(db)
    comparison = db.get(Comparison, comparison_id, with_for_update=True)
    if not comparison:
        raise ValueError('找不到该比测记录')
    if comparison.status == domain.EDGE_WITHDRAWN:
        raise ValueError('该比测已撤回，请勿重复操作')
    meters = {m.id: m for m in db.scalars(select(Meter).with_for_update())}
    affected = [meters[comparison.meter_a_id].meter_code, meters[comparison.meter_b_id].meter_code]
    comparison.status = domain.EDGE_WITHDRAWN
    comparison.withdrawn_at = now()
    comparison.withdraw_reason = reason
    # 撤回只标记待更新；若边被拆成多个组，等所有受影响组算齐，在发布事务里一起切换。
    touched = _mark_cursors_stale(db, affected, f'比测 {comparison_id} 已撤回，旧换算结果待更新')
    record(db, actor, 'calibration.comparison_withdrawn', comparison.id,
           reason=reason, stale_groups=touched)
    return comparison, touched


# ---------------------------------------------------------------- 发布切换

def publish(db, actor, expected_fingerprint=None):
    """对全部待更新组重新求解，并在单事务内整体切换为新发布版本。

    并发安全：先锁全部 meter 行，使上传/撤回/基准变更与发布串行；
    切换前再次读取当前边集，任何在本事务提交前补传的比测都已在同一份边集内，
    不会漏算。expected_fingerprint 用于前端确认页面未过期。
    """
    meters = _lock_meters(db)
    results, edges = _solve(db, meters)
    fingerprint = domain.edge_fingerprint(edges)
    if expected_fingerprint and expected_fingerprint != fingerprint:
        raise ValueError('比测数据在发布前发生变化，请刷新页面确认后再切换')

    stale_cursors = list(db.scalars(select(ConversionCursor)
                                    .where(ConversionCursor.stale.is_(True)).with_for_update()))
    all_cursors = list(db.scalars(select(ConversionCursor).with_for_update()))
    payload = [domain.group_payload(result) for result in results]
    existing_keys = {cursor.group_key for cursor in all_cursors}
    missing_groups = [item['group_key'] for item in payload if item['group_key'] not in existing_keys]
    # 首次发布（组还没有游标）、有待更新组、或出现新拆分出的组，都需要整体切换。
    if not stale_cursors and not missing_groups:
        return {'changed': False, 'reason': '没有待更新的换算组', 'release_id': None,
                'fingerprint': fingerprint}

    active_count = sum(1 for item in payload if item['status'] == domain.ACTIVE)
    blocked = [item for item in payload if item['status'] != domain.ACTIVE]
    release = ConversionRelease(
        fingerprint=fingerprint,
        group_count=len(payload),
        active_count=active_count,
        blocked_count=len(blocked),
        summary={'groups': payload},
        author=actor,
    )
    db.add(release)
    db.flush()

    existing = {cursor.group_key: cursor for cursor in all_cursors}
    replaced = []
    for item in payload:
        cursor = existing.get(item['group_key'])
        if cursor is None:
            cursor = ConversionCursor(group_key=item['group_key'], platform=item['platform'])
            db.add(cursor)
        cursor.current_release_id = release.id
        cursor.current_result = item
        cursor.stale = False
        cursor.stale_reason = None
        cursor.updated_at = now()
        replaced.append(item['group_key'])
    # 本次没有重新出现的旧组（撤回边后被拆开/合并），其成员已由本次发布的新组覆盖，
    # 在同一事务内标记为已取代，不再挂在待更新列表里，实现「拆开的组算齐再一起切换」。
    replaced_set = set(replaced)
    superseded = []
    for cursor in stale_cursors:
        if cursor.group_key not in replaced_set:
            cursor.stale = False
            cursor.stale_reason = '该组已被撤回后的新换算组取代，结果见同次发布的拆分组合'
            cursor.updated_at = now()
            superseded.append(cursor.group_key)
    record(db, actor, 'calibration.published', release.id, fingerprint=fingerprint,
           groups=replaced, blocked=[item['group_key'] for item in blocked], superseded=superseded)
    db.flush()
    return {'changed': True, 'release_id': release.id, 'fingerprint': fingerprint,
            'group_count': len(payload), 'active_count': active_count,
            'blocked_count': len(blocked), 'groups': payload}


# ---------------------------------------------------------------- 查询视图

def overview(db):
    meters = list(db.scalars(select(Meter).order_by(Meter.platform, Meter.id)))
    edges, _ = _load_edges(db, meters)
    baselines = _baselines_by_platform(meters)
    results = domain.solve_groups([m.meter_code for m in meters],
                                  {m.meter_code: m.platform for m in meters},
                                  baselines, edges)
    pending = {item.group_key: domain.group_payload(item) for item in results}
    cursors = list(db.scalars(select(ConversionCursor).order_by(ConversionCursor.id)))
    current_views = []
    for cursor in cursors:
        view = {'group_key': cursor.group_key, 'stale': cursor.stale,
                'stale_reason': cursor.stale_reason,
                'release_id': cursor.current_release_id,
                'updated_at': cursor.updated_at.isoformat(sep=' ') if cursor.updated_at else None,
                'result': cursor.current_result,
                'pending': pending.get(cursor.group_key)}
        current_views.append(view)
    return {
        'meters': [
            {'id': m.id, 'meter_code': m.meter_code, 'label': m.label, 'platform': m.platform,
             'is_baseline': m.is_baseline}
            for m in meters
        ],
        'baselines': baselines,
        'fingerprint': domain.edge_fingerprint(edges),
        'stale_group_keys': [c.group_key for c in cursors if c.stale],
        'cursors': current_views,
        'pending_groups': [domain.group_payload(item) for item in results],
    }


def list_comparisons(db):
    rows = db.scalars(select(Comparison).order_by(Comparison.id.desc()).limit(200))
    meter_by_id = {m.id: m for m in db.scalars(select(Meter))}
    batch_by_id = {b.id: b for b in db.scalars(select(ComparisonBatch))}
    out = []
    for row in rows:
        a, b = meter_by_id[row.meter_a_id], meter_by_id[row.meter_b_id]
        batch = batch_by_id.get(row.batch_id)
        out.append({
            'id': row.id,
            'batch_key': batch.batch_key if batch else None,
            'meter_a': a.meter_code,
            'meter_b': b.meter_code,
            'platform': a.platform,
            'deviation_low': row.deviation_low,
            'deviation_high': row.deviation_high,
            'measured_at': row.measured_at,
            'status': row.status,
            'withdraw_reason': row.withdraw_reason,
            'created_at': row.created_at.isoformat(sep=' ') if row.created_at else None,
        })
    return out
