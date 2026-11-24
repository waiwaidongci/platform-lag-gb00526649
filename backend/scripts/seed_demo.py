import json
from sqlalchemy import func, select
from src.config.database import Session
from src.constants.enums import Role
from src.domain import calibration_demo as cal_demo
from src.domain.demo import demo_rows
from src.domain.spec import SPEC
from src.models.calibration import Comparison, ComparisonBatch, Meter
from src.models.identity import User
from src.models.source import Dataset
from src.services import calibration as cal_service
from src.services.datasets import save
from src.utils.security import hash_password


def _seed_calibration(db):
    """幂等写入声级计台账与演示比测，并发布一次换算版本。"""
    if db.scalar(select(func.count()).select_from(Meter)):
        return {'calibration_meters': db.scalar(select(func.count()).select_from(Meter))}
    meter_id = {}
    for code, label, platform, is_baseline in cal_demo.DEMO_METERS:
        meter = cal_service.create_meter(db, code, label, platform, is_baseline, 'seed')
        meter_id[code] = meter.id
    items = [
        {'meter_a': a, 'meter_b': b, 'deviation_low': low, 'deviation_high': high}
        for a, b, low, high in cal_demo.DEMO_COMPARISONS
    ]
    cal_service.submit_comparisons(db, cal_demo.DEMO_BATCH_KEY, '演示同点比测',
                                   '2026-11-24 09:00:00', items, 'seed')
    published = cal_service.publish(db, 'seed')
    return {'calibration_meters': len(meter_id),
            'calibration_release': published.get('release_id'),
            'calibration_blocked': published.get('blocked_count')}


def seed():
    with Session.begin() as db:
        for role in Role:
            if not db.get(User, role.value):
                db.add(User(username=role.value, role=role, password_hash=hash_password(f'{role.value}-demo-2611')))
        item, created = save(db, SPEC['default_dataset'], SPEC['sample_name'], demo_rows(), 'seed')
        calibration = _seed_calibration(db)
        return {'users': db.scalar(select(func.count()).select_from(User)),
                'datasets': db.scalar(select(func.count()).select_from(Dataset)),
                'seed_dataset_id': item.id, 'row_count': item.row_count, 'checksum': item.checksum,
                'created_dataset': created, **calibration}


if __name__ == '__main__':
    print(json.dumps(seed(), ensure_ascii=False))
