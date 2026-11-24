"""声级计台账、同点比测与基准换算发布的表结构。"""

from datetime import datetime
from sqlalchemy import JSON, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base
from src.utils.time import now


class Meter(Base):
    """声级计台账。每个站台至多一台基准表（is_baseline=1）。"""

    __tablename__ = 'meters'
    __table_args__ = (
        UniqueConstraint('meter_code', name='uk_meters_code'),
        Index('ix_meters_platform', 'platform'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    meter_code: Mapped[str] = mapped_column(String(40))
    label: Mapped[str] = mapped_column(String(120))
    platform: Mapped[str] = mapped_column(String(80))
    is_baseline: Mapped[bool] = mapped_column(default=False, index=True)
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)


class ComparisonBatch(Base):
    """一次比测录入的批次。batch_key 唯一，同版重试直接复用、不重复发布。"""

    __tablename__ = 'comparison_batches'
    __table_args__ = (UniqueConstraint('batch_key', name='uk_comparison_batches_key'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_key: Mapped[str] = mapped_column(String(80))
    note: Mapped[str | None] = mapped_column(String(300))
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)


class Comparison(Base):
    """同点比测一条记录：meter_a 读数 − meter_b 读数 ∈ [deviation_low, deviation_high]。"""

    __tablename__ = 'comparisons'
    __table_args__ = (
        Index('ix_comparison_batch_pair', 'batch_id', 'meter_a_id', 'meter_b_id'),
        Index('ix_comparison_status', 'status'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey('comparison_batches.id'))
    meter_a_id: Mapped[int] = mapped_column(ForeignKey('meters.id'))
    meter_b_id: Mapped[int] = mapped_column(ForeignKey('meters.id'))
    deviation_low: Mapped[float]
    deviation_high: Mapped[float]
    # 同一批次内允许同一对表的正反两条比测（A−B 与 B−A），仅禁止完全相同的有序对重复
    status: Mapped[str] = mapped_column(String(20), default='active', index=True)
    measured_at: Mapped[str | None] = mapped_column(String(30))
    withdrawn_at: Mapped[datetime | None]
    withdraw_reason: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(default=now)


class ConversionRelease(Base):
    """一次换算发布：在单个事务内为当时全部待更新组算出结果并整体切换。"""

    __tablename__ = 'conversion_releases'

    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    group_count: Mapped[int]
    active_count: Mapped[int]
    blocked_count: Mapped[int]
    summary: Mapped[dict] = mapped_column(JSON)
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)


class ConversionCursor(Base):
    """每个换算组当前生效的发布版本；待更新时 current_release_id 保留旧版本并置 stale。"""

    __tablename__ = 'conversion_cursors'
    __table_args__ = (UniqueConstraint('group_key', name='uk_conversion_cursors_group'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    group_key: Mapped[str] = mapped_column(String(200))
    platform: Mapped[str | None] = mapped_column(String(80))
    current_release_id: Mapped[int | None] = mapped_column(ForeignKey('conversion_releases.id'))
    current_result: Mapped[dict | None] = mapped_column(JSON)
    stale: Mapped[bool] = mapped_column(default=False, index=True)
    stale_reason: Mapped[str | None] = mapped_column(String(300))
    updated_at: Mapped[datetime] = mapped_column(default=now)
