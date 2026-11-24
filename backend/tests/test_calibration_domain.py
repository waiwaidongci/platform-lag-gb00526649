"""基准换算领域算法的标准库单元测试：python -m unittest tests.test_calibration_domain -v"""

import unittest
from src.domain import calibration as cal


def edge(cid, a, b, low, high, batch='B1'):
    return cal.Edge(comparison_id=cid, batch_key=batch, a=a, b=b, low=low, high=high)


class SolverTests(unittest.TestCase):
    def setUp(self):
        self.platform = {'M0': '澄湾站', 'M1': '澄湾站', 'M2': '澄湾站'}
        self.baselines = {'澄湾站': 'M0'}

    def test_single_interval_conversion_range(self):
        # M1 - M0 ∈ [0.4, 0.8]，即 x1 ∈ [0.4, 0.8]，offset1 ∈ [-0.8, -0.4]
        edges = [edge(1, 'M1', 'M0', 0.4, 0.8)]
        results = cal.solve_groups(['M0', 'M1'], self.platform, self.baselines, edges)
        self.assertEqual(len(results), 1)
        group = results[0]
        self.assertEqual(group.status, cal.ACTIVE)
        ranges = {item.meter: item for item in group.ranges}
        self.assertEqual(ranges['M0'].offset_low, 0.0)
        self.assertEqual(ranges['M0'].offset_high, 0.0)
        self.assertEqual(ranges['M1'].offset_low, -0.8)
        self.assertEqual(ranges['M1'].offset_high, -0.4)

    def test_chained_intervals_intersect(self):
        # M1-M0 ∈ [0.4,0.8]; M2-M1 ∈ [-0.2,0.2] ⇒ x2 ∈ [0.2,1.0]，offset2 ∈ [-1.0,-0.2]
        edges = [edge(1, 'M1', 'M0', 0.4, 0.8), edge(2, 'M2', 'M1', -0.2, 0.2)]
        group = cal.solve_groups(['M0', 'M1', 'M2'], self.platform, self.baselines, edges)[0]
        ranges = {item.meter: item for item in group.ranges}
        self.assertEqual((ranges['M2'].offset_low, ranges['M2'].offset_high), (-1.0, -0.2))
        self.assertEqual((ranges['M1'].offset_low, ranges['M1'].offset_high), (-0.8, -0.4))

    def test_cycle_tightens_range(self):
        # 两条链式约束外再加 M2-M0 ∈ [0.3,0.7]
        edges = [
            edge(1, 'M1', 'M0', 0.4, 0.8),
            edge(2, 'M2', 'M1', -0.2, 0.2),
            edge(3, 'M2', 'M0', 0.3, 0.7),
        ]
        group = cal.solve_groups(['M0', 'M1', 'M2'], self.platform, self.baselines, edges)[0]
        ranges = {item.meter: item for item in group.ranges}
        # x2 上界 = min(0.8+0.2, 0.7)=0.7；下界 = max(0.4-0.2, 0.3)=0.3
        # offset2 ∈ [-x2_high, -x2_low] = [-0.7, -0.3]
        self.assertEqual((ranges['M2'].offset_low, ranges['M2'].offset_high), (-0.7, -0.3))

    def test_conflict_negative_cycle_blocks_group(self):
        # M1-M0 ∈ [0.4,0.8]，M0-M1 ∈ [0.5,0.9]（即 M1-M0 ∈ [-0.9,-0.5]），无交集
        edges = [edge(1, 'M1', 'M0', 0.4, 0.8), edge(2, 'M0', 'M1', 0.5, 0.9)]
        group = cal.solve_groups(['M0', 'M1'], self.platform, self.baselines, edges)[0]
        self.assertEqual(group.status, cal.CONFLICT)
        self.assertTrue(group.reason)
        self.assertEqual({item['comparison_id'] for item in group.conflict_edges}, {1, 2})
        self.assertEqual(group.ranges, [])

    def test_missing_baseline_blocks_only_that_group(self):
        platform = {'M0': '澄湾站', 'M1': '澄湾站', 'X0': '林埭站', 'X1': '林埭站'}
        edges = [edge(1, 'M1', 'M0', 0.1, 0.3), edge(2, 'X1', 'X0', -0.2, 0.0)]
        results = cal.solve_groups(['M0', 'M1', 'X0', 'X1'], platform,
                                   {'澄湾站': 'M0'}, edges)
        by_members = {tuple(group.meters): group.status for group in results}
        self.assertEqual(by_members[('M0', 'M1')], cal.ACTIVE)
        self.assertEqual(by_members[('X0', 'X1')], cal.NO_BASELINE)
        blocked = next(group for group in results if group.status == cal.NO_BASELINE)
        self.assertIn('基准表', blocked.reason)

    def test_withdraw_splits_group_and_both_solve(self):
        # M0-M1-M2 链，撤掉中间边后拆成两个组，各自仍可求解
        edges_before = [edge(1, 'M1', 'M0', 0.4, 0.8), edge(2, 'M2', 'M1', -0.2, 0.2)]
        before = cal.solve_groups(['M0', 'M1', 'M2'], self.platform, self.baselines, edges_before)
        self.assertEqual(len(before), 1)
        edges_after = [edge(1, 'M1', 'M0', 0.4, 0.8)]
        after = cal.solve_groups(['M0', 'M1', 'M2'], self.platform, self.baselines, edges_after)
        self.assertEqual(len(after), 2)
        members = {tuple(group.meters) for group in after}
        self.assertEqual(members, {('M0', 'M1'), ('M2',)})
        # 孤立组没有任何边也没有基准成员 → no_baseline
        orphan = next(group for group in after if group.meters == ['M2'])
        self.assertEqual(orphan.status, cal.NO_BASELINE)

    def test_other_group_unaffected_by_conflict(self):
        platform = {'M0': '澄湾站', 'M1': '澄湾站', 'X0': '林埭站', 'X1': '林埭站'}
        baselines = {'澄湾站': 'M0', '林埭站': 'X0'}
        edges = [
            edge(1, 'M1', 'M0', 0.4, 0.8),
            edge(2, 'M0', 'M1', 0.5, 0.9),  # 澄湾站冲突
            edge(3, 'X1', 'X0', -0.1, 0.1),  # 林埭站正常
        ]
        results = cal.solve_groups(list(platform), platform, baselines, edges)
        by_members = {tuple(group.meters): group.status for group in results}
        self.assertEqual(by_members[('M0', 'M1')], cal.CONFLICT)
        self.assertEqual(by_members[('X0', 'X1')], cal.ACTIVE)
        # 冲突不影响正常组照常出换算范围
        healthy = next(group for group in results if tuple(group.meters) == ('X0', 'X1'))
        x1 = next(item for item in healthy.ranges if item.meter == 'X1')
        self.assertEqual((x1.offset_low, x1.offset_high), (-0.1, 0.1))

    def test_fingerprint_stable_and_order_independent(self):
        e1 = [edge(1, 'M1', 'M0', 0.4, 0.8), edge(2, 'M2', 'M1', -0.2, 0.2)]
        e2 = list(reversed(e1))
        self.assertEqual(cal.edge_fingerprint(e1), cal.edge_fingerprint(e2))
        e3 = [edge(1, 'M1', 'M0', 0.4, 0.8), edge(2, 'M2', 'M1', -0.2, 0.25)]
        self.assertNotEqual(cal.edge_fingerprint(e1), cal.edge_fingerprint(e3))

    def test_point_interval_feasible(self):
        # 零宽区间（点估计）也应可解
        edges = [edge(1, 'M1', 'M0', 0.5, 0.5)]
        group = cal.solve_groups(['M0', 'M1'], self.platform, self.baselines, edges)[0]
        ranges = {item.meter: item for item in group.ranges}
        self.assertEqual(ranges['M1'].offset_low, -0.5)
        self.assertEqual(ranges['M1'].offset_high, -0.5)


if __name__ == '__main__':
    unittest.main()
