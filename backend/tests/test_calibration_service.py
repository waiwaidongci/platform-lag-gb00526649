"""服务层集成测试：真实 SQLAlchemy ORM + sqlite，覆盖录入/撤回/发布全流程。

运行（仓库外安装隔离的 sqlalchemy 后）：
    PYTHONPATH=/tmp/pylibs python3 -m unittest tests.test_calibration_service -v
"""

import os
import tempfile
import unittest

os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')
os.environ.setdefault('JWT_SECRET', 'test-secret')

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.config.database import Base  # noqa: E402
from src.models import identity  # noqa: E402,F401  确保审计表也注册到 metadata
from src.models.calibration import (  # noqa: E402
    Comparison,
    ComparisonBatch,
    ConversionCursor,
    ConversionRelease,
    Meter,
)
from src.services import calibration as service  # noqa: E402

ACTOR = 'analyst'


def items(a, b, low, high):
    return [{'meter_a': a, 'meter_b': b, 'deviation_low': low, 'deviation_high': high}]


class CalibrationServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        cls.tmp.close()
        cls.engine = create_engine(f'sqlite:///{cls.tmp.name}', future=True)
        Base.metadata.create_all(cls.engine)
        cls.Session = sessionmaker(cls.engine, future=True)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        os.unlink(cls.tmp.name)

    def setUp(self):
        # 每个用例清空业务表，保留结构
        with self.engine.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                conn.execute(table.delete())

    def meter(self, code, platform, baseline=False, label=None):
        with self.Session.begin() as db:
            meter = service.create_meter(db, code, label or f'表{code}', platform, baseline, ACTOR)
            return meter.id

    def test_full_publish_lifecycle_and_idempotent_retry(self):
        self.meter('M0', '澄湾站', baseline=True)
        self.meter('M1', '澄湾站')
        self.meter('M2', '澄湾站')

        with self.Session.begin() as db:
            batch, created, affected = service.submit_comparisons(
                db, 'B1', None, None,
                items('M1', 'M0', 0.4, 0.8) + items('M2', 'M1', -0.2, 0.2), ACTOR)
            self.assertTrue(created)
            self.assertEqual(set(affected), {'M0', 'M1', 'M2'})
            # 同版重试：不重复落库
            again, created2, _ = service.submit_comparisons(
                db, 'B1', None, None,
                items('M1', 'M0', 0.4, 0.8), ACTOR)
            self.assertFalse(created2)
            self.assertEqual(again.id, batch.id)
            self.assertEqual(len(db.scalars(select(Comparison)).all()), 2)

            first = service.publish(db, ACTOR)
            self.assertTrue(first['changed'])
            self.assertEqual(first['active_count'], 1)
            group = next(g for g in first['groups'] if g['status'] == 'active')
            ranges = {r['meter']: r for r in group['ranges']}
            self.assertEqual((ranges['M1']['offset_low'], ranges['M1']['offset_high']), (-0.8, -0.4))
            self.assertEqual((ranges['M2']['offset_low'], ranges['M2']['offset_high']), (-1.0, -0.2))

            # 没有待更新组时再次发布为空操作，不产生新版本
            noop = service.publish(db, ACTOR)
            self.assertFalse(noop['changed'])
            self.assertEqual(len(db.scalars(select(ConversionRelease)).all()), 1)

        # 新比测使旧结果待更新，再发布整体切换
        with self.Session.begin() as db:
            service.submit_comparisons(db, 'B2', None, None,
                                       items('M2', 'M0', 0.3, 0.7), ACTOR)
            cursor = db.scalar(select(ConversionCursor))
            self.assertTrue(cursor.stale)
            self.assertIn('待更新', cursor.stale_reason)
            second = service.publish(db, ACTOR)
            self.assertTrue(second['changed'])
            self.assertEqual(second['release_id'], 2)
            group = next(g for g in second['groups'] if g['status'] == 'active')
            ranges = {r['meter']: r for r in group['ranges']}
            # x2 ∈ [0.3, 0.7] → offset2 ∈ [-0.7, -0.3]
            self.assertEqual((ranges['M2']['offset_low'], ranges['M2']['offset_high']), (-0.7, -0.3))
            cursor = db.scalar(select(ConversionCursor))
            self.assertFalse(cursor.stale)
            self.assertEqual(cursor.current_release_id, 2)

    def test_withdraw_marks_stale_and_split_groups_switch_together(self):
        self.meter('X0', '林埭站', baseline=True)
        self.meter('X1', '林埭站')
        with self.Session.begin() as db:
            service.submit_comparisons(db, 'X-B1', None, None,
                                       items('X1', 'X0', -0.1, 0.1), ACTOR)
            service.publish(db, ACTOR)
            comparison = db.scalar(select(Comparison))
            service.withdraw_comparison(db, comparison.id, '比测表填写有误', ACTOR)
            cursor = db.scalar(select(ConversionCursor))
            self.assertTrue(cursor.stale)
            # 撤回后旧结果仍保留，未被删除
            self.assertIsNotNone(cursor.current_result)
            result = service.publish(db, ACTOR)
            # 边撤掉后拆为两个组：X0 基准单表 active；X1 孤立无基准 no_baseline
            statuses = {tuple(g['meters']): g['status'] for g in result['groups']}
            self.assertEqual(statuses[('X0',)], 'active')
            self.assertEqual(statuses[('X1',)], 'no_baseline')
            # 同一个发布版本内一起切换，游标均不再 stale
            self.assertEqual(len(db.scalars(select(ConversionCursor)
                                            .where(ConversionCursor.stale.is_(True))).all()), 0)
            self.assertEqual(db.scalar(select(Comparison)).status, 'withdrawn')

    def test_conflict_blocks_one_group_but_other_groups_publish(self):
        self.meter('M0', '澄湾站', baseline=True)
        self.meter('M1', '澄湾站')
        self.meter('X0', '林埭站', baseline=True)
        self.meter('X1', '林埭站')
        with self.Session.begin() as db:
            service.submit_comparisons(
                db, 'C1', None, None,
                items('M1', 'M0', 0.4, 0.8) + items('M0', 'M1', 0.5, 0.9)
                + items('X1', 'X0', -0.1, 0.1), ACTOR)
            result = service.publish(db, ACTOR)
            by_members = {tuple(g['meters']): g for g in result['groups']}
            blocked = by_members[('M0', 'M1')]
            healthy = by_members[('X0', 'X1')]
            self.assertEqual(blocked['status'], 'conflict')
            self.assertIn('冲突', blocked['reason'])
            self.assertTrue(blocked['conflict_edges'])
            self.assertEqual(healthy['status'], 'active')
            self.assertEqual(result['blocked_count'], 1)
            self.assertEqual(result['active_count'], 1)

    def test_baseline_uniqueness_and_change_marks_stale(self):
        self.meter('M0', '澄湾站', baseline=True)
        m1 = self.meter('M1', '澄湾站')
        with self.Session.begin() as db:
            service.submit_comparisons(db, 'D1', None, None,
                                       items('M1', 'M0', 0.2, 0.4), ACTOR)
            service.publish(db, ACTOR)
        with self.Session.begin() as db:
            with self.assertRaises(ValueError):
                service.set_baseline(db, m1, True, ACTOR)
            # 取消原基准后可指定新基准，并使旧结果待更新
            m0 = db.scalar(select(Meter).where(Meter.meter_code == 'M0'))
            service.set_baseline(db, m0.id, False, ACTOR)
            service.set_baseline(db, m1, True, ACTOR)
            self.assertTrue(db.scalar(select(ConversionCursor)).stale)

    def test_stale_fingerprint_guard(self):
        self.meter('M0', '澄湾站', baseline=True)
        self.meter('M1', '澄湾站')
        with self.Session.begin() as db:
            service.submit_comparisons(db, 'E1', None, None,
                                       items('M1', 'M0', 0.2, 0.4), ACTOR)
            with self.assertRaises(ValueError):
                service.publish(db, ACTOR, expected_fingerprint='not-the-current-fingerprint')
            # 正确指纹可以发布
            result = service.publish(db, ACTOR)
            self.assertTrue(result['changed'])

    def test_invalid_comparison_rejected(self):
        self.meter('M0', '澄湾站', baseline=True)
        self.meter('M1', '澄湾站')
        for key, payload in (
            ('F1', items('M1', 'M0', 0.9, 0.2)),
            ('F2', items('M0', 'M0', 0.0, 0.1)),
        ):
            db = self.Session()
            try:
                with self.assertRaises(ValueError):
                    service.submit_comparisons(db, key, None, None, payload, ACTOR)
                db.rollback()
            finally:
                db.close()
        with self.Session.begin() as db:
            self.assertEqual(len(db.scalars(select(ComparisonBatch)).all()), 0)
            self.assertEqual(len(db.scalars(select(Comparison)).all()), 0)


if __name__ == '__main__':
    unittest.main()
