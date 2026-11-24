"""声级计基准换算接口：台账、基准、比测录入/撤回、待更新总览与原子发布。"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from src.config.database import connection
from src.constants.enums import Role
from src.controllers.auth import allow, current
from src.models.identity import User
from src.schemas.requests import (
    BaselineRequest,
    ComparisonBatchRequest,
    MeterRequest,
    PublishRequest,
    WithdrawRequest,
)
from src.services import calibration as service
from src.services.audit import record

router = APIRouter(prefix='/calibration')


@router.get('/overview')
def overview(db: Session = Depends(connection), actor: User = Depends(current)):
    return service.overview(db)


@router.get('/comparisons')
def comparisons(db: Session = Depends(connection), actor: User = Depends(current)):
    return service.list_comparisons(db)


@router.post('/meters', status_code=201)
def create_meter(body: MeterRequest, db: Session = Depends(connection),
                 actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    meter = service.create_meter(db, body.meter_code, body.label, body.platform,
                                 body.is_baseline, actor.username)
    db.commit()
    return {'id': meter.id, 'meter_code': meter.meter_code, 'platform': meter.platform,
            'is_baseline': meter.is_baseline}


@router.post('/meters/{meter_id}/baseline')
def set_baseline(meter_id: int, body: BaselineRequest, db: Session = Depends(connection),
                 actor: User = Depends(allow(Role.ADMIN))):
    meter = service.set_baseline(db, meter_id, body.is_baseline, actor.username)
    db.commit()
    return {'id': meter.id, 'is_baseline': meter.is_baseline}


@router.post('/comparisons', status_code=201)
def submit(body: ComparisonBatchRequest, db: Session = Depends(connection),
           actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    batch, created, affected = service.submit_comparisons(
        db, body.batch_key, body.note or None, body.measured_at or None,
        [item.model_dump() for item in body.items], actor.username)
    db.commit()
    return {'batch_id': batch.id, 'batch_key': batch.batch_key, 'created': created,
            'affected_meters': affected,
            'message': '同版批次已存在，未重复录入' if not created else '比测已录入，受影响换算组待更新'}


@router.post('/comparisons/{comparison_id}/withdraw')
def withdraw(comparison_id: int, body: WithdrawRequest, db: Session = Depends(connection),
             actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    _comparison, touched = service.withdraw_comparison(db, comparison_id, body.reason, actor.username)
    db.commit()
    return {'comparison_id': comparison_id, 'stale_groups': touched,
            'message': '已撤回，受影响换算组标记为待更新，发布后整体切换'}


@router.post('/publish')
def publish(body: PublishRequest, db: Session = Depends(connection),
            actor: User = Depends(allow(Role.ADMIN))):
    result = service.publish(db, actor.username, body.expected_fingerprint or None)
    db.commit()
    return result
