"""端到端接口冒烟测试：FastAPI TestClient + sqlite，覆盖鉴权/权限/幂等/发布全链路。

    PYTHONPATH=/tmp/pylibs python3 -m unittest tests.test_calibration_api -v
"""

import os
import tempfile
import unittest

_DB = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB.close()
os.environ['DATABASE_URL'] = f'sqlite:///{_DB.name}'
os.environ['JWT_SECRET'] = 'test-secret'
os.environ['SEED_DEMO'] = 'false'

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from src.config.database import Base, Session, engine  # noqa: E402
from src.main import app  # noqa: E402
from src.models.calibration import Comparison, ComparisonBatch  # noqa: E402
from src.models.identity import User  # noqa: E402
from src.utils.security import hash_password  # noqa: E402


class CalibrationApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(engine)
        with Session.begin() as db:
            for name, role in (('admin', 'admin'), ('analyst', 'analyst'), ('viewer', 'viewer')):
                if not db.get(User, name):
                    db.add(User(username=name, role=role,
                                password_hash=hash_password(f'{name}-pw')))
        cls.client = TestClient(app)
        cls.tokens = {}
        for name in ('admin', 'analyst', 'viewer'):
            resp = cls.client.post('/auth/login', json={'username': name, 'password': f'{name}-pw'})
            cls.tokens[name] = resp.json()['token']

    def auth(self, name):
        return {'Authorization': f"Bearer {self.tokens[name]}"}

    def test_end_to_end_calibration_flow(self):
        c = self.client
        # viewer 不能登记表计
        denied = c.post('/calibration/meters',
                        json={'meter_code': 'M0', 'label': '基准', 'platform': '澄湾站',
                              'is_baseline': True}, headers=self.auth('viewer'))
        self.assertEqual(denied.status_code, 403)

        # analyst 登记三台表，M0 为基准
        for code, label, base in (('M0', '基准表', True), ('M1', '表1', False), ('M2', '表2', False)):
            resp = c.post('/calibration/meters',
                          json={'meter_code': code, 'label': label, 'platform': '澄湾站',
                                'is_baseline': base}, headers=self.auth('analyst'))
            self.assertEqual(resp.status_code, 201, resp.text)

        # 同站台第二个基准被拒
        dup = c.post('/calibration/meters',
                     json={'meter_code': 'M9', 'label': '重复基准', 'platform': '澄湾站',
                           'is_baseline': True}, headers=self.auth('admin'))
        self.assertEqual(dup.status_code, 422)

        batch = {'batch_key': 'RC-1', 'note': '', 'measured_at': '', 'items': [
            {'meter_a': 'M1', 'meter_b': 'M0', 'deviation_low': 0.4, 'deviation_high': 0.8},
            {'meter_a': 'M2', 'meter_b': 'M1', 'deviation_low': -0.2, 'deviation_high': 0.2},
        ]}
        first = c.post('/calibration/comparisons', json=batch, headers=self.auth('analyst'))
        self.assertEqual(first.status_code, 201, first.text)
        self.assertTrue(first.json()['created'])

        # 同版重试：不重复落库
        retry = c.post('/calibration/comparisons', json=batch, headers=self.auth('analyst'))
        self.assertFalse(retry.json()['created'])
        with Session() as db:
            self.assertEqual(len(db.scalars(select(ComparisonBatch)
                                            .where(ComparisonBatch.batch_key == 'RC-1')).all()), 1)
            self.assertEqual(len(db.scalars(select(Comparison)).all()), 2)

        # analyst 无权发布
        forbidden = c.post('/calibration/publish', json={'expected_fingerprint': ''},
                           headers=self.auth('analyst'))
        self.assertEqual(forbidden.status_code, 403)

        # admin 发布
        published = c.post('/calibration/publish', json={'expected_fingerprint': ''},
                           headers=self.auth('admin'))
        self.assertEqual(published.status_code, 200, published.text)
        payload = published.json()
        self.assertTrue(payload['changed'])
        self.assertEqual(payload['active_count'], 1)
        group = next(g for g in payload['groups'] if g['status'] == 'active')
        ranges = {r['meter']: r for r in group['ranges']}
        self.assertEqual((ranges['M1']['offset_low'], ranges['M1']['offset_high']), (-0.8, -0.4))

        # 无待更新时再发布为空操作
        noop = c.post('/calibration/publish', json={'expected_fingerprint': ''},
                      headers=self.auth('admin'))
        self.assertFalse(noop.json()['changed'])

        # 录入冲突比测，再发布：组被暂停，原因可查
        conflict_batch = {'batch_key': 'RC-2', 'note': '', 'measured_at': '', 'items': [
            {'meter_a': 'M0', 'meter_b': 'M1', 'deviation_low': 0.5, 'deviation_high': 0.9},
        ]}
        c.post('/calibration/comparisons', json=conflict_batch, headers=self.auth('analyst'))
        blocked = c.post('/calibration/publish', json={'expected_fingerprint': ''},
                         headers=self.auth('admin')).json()
        self.assertEqual(blocked['blocked_count'], 1)
        bad = next(g for g in blocked['groups'] if g['status'] == 'conflict')
        self.assertIn('冲突', bad['reason'])
        self.assertTrue(bad['conflict_edges'])

        # overview 暴露待更新/原因，viewer 可只读查看
        overview = c.get('/calibration/overview', headers=self.auth('viewer'))
        self.assertEqual(overview.status_code, 200)
        body = overview.json()
        self.assertIn('pending_groups', body)
        self.assertTrue(any(g['status'] == 'conflict' for g in body['pending_groups']))

    def test_withdraw_and_split_publish(self):
        c = self.client
        c.post('/calibration/meters',
               json={'meter_code': 'X0', 'label': '林埭基准', 'platform': '林埭站',
                     'is_baseline': True}, headers=self.auth('admin'))
        c.post('/calibration/meters',
               json={'meter_code': 'X1', 'label': '林埭表1', 'platform': '林埭站',
                     'is_baseline': False}, headers=self.auth('admin'))
        c.post('/calibration/comparisons',
               json={'batch_key': 'LX-1', 'note': '', 'measured_at': '', 'items': [
                   {'meter_a': 'X1', 'meter_b': 'X0', 'deviation_low': -0.1,
                    'deviation_high': 0.1}]},
               headers=self.auth('analyst'))
        c.post('/calibration/publish', json={'expected_fingerprint': ''}, headers=self.auth('admin'))
        # 用列表接口拿到林埭站比测 id
        listing = c.get('/calibration/comparisons', headers=self.auth('viewer')).json()
        target = next(item for item in listing if item['meter_a'] == 'X1')
        withdrawn = c.post(f"/calibration/comparisons/{target['id']}/withdraw",
                           json={'reason': '比测表填写有误'}, headers=self.auth('analyst'))
        self.assertEqual(withdrawn.status_code, 200, withdrawn.text)
        result = c.post('/calibration/publish', json={'expected_fingerprint': ''},
                        headers=self.auth('admin')).json()
        statuses = {tuple(g['meters']): g['status'] for g in result['groups']}
        self.assertEqual(statuses[('X0',)], 'active')
        self.assertEqual(statuses[('X1',)], 'no_baseline')
        # 撤回原因在页面数据中可见
        listing = c.get('/calibration/comparisons', headers=self.auth('viewer')).json()
        target = next(item for item in listing if item['id'] == target['id'])
        self.assertEqual(target['status'], 'withdrawn')
        self.assertEqual(target['withdraw_reason'], '比测表填写有误')

    @classmethod
    def tearDownClass(cls):
        engine.dispose()
        os.unlink(_DB.name)


if __name__ == '__main__':
    unittest.main()
