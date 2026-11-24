"""声级计基准换算：把同点比测给出的表间偏差区间求解为每台表相对基准表的换算范围。

业务口径
--------
同点比测时两台声级计 A、B 在同一测点同时读数，记录偏差区间 [low, high]，
约定偏差 = A 表读数 − B 表读数（单位 dB(A)）。换算是把任意一台表的读数折算到
站台基准表 M0 的读数口径：

    reading_M0 = reading_M + offset_M

同点同时意味着真实声级相同，因此 A、B 两台表的折算读数必须落在比测偏差区间内：

    low <= (reading_A) - (reading_B) <= high
    令 x_M = -offset_M，则 low <= x_A - x_B <= high

这是一组差分约束。以基准表为 x = 0 的锚点，用 Bellman-Ford 求最短路径距离，
得到每个变量 x_M 的最紧可行区间，再翻回 offset 区间即换算范围。

- 同一站台、通过活跃比测边连通的表计构成一个换算组（连通分量）。
- 组内没有基准表：状态 no_baseline，暂停换算。
- 约束不相容（负环）：状态 conflict，暂停换算，并给出参与冲突的比测边。
- 其余组：状态 active，组内全部比测同时满足时，每台表得到一个换算范围。
"""

from collections import defaultdict
from dataclasses import dataclass, field

# 组状态
ACTIVE = 'active'          # 有基准且约束相容，已得到每台表的换算范围
NO_BASELINE = 'no_baseline'  # 连通组内没有指定基准表
CONFLICT = 'conflict'      # 比测区间互相冲突（差分约束无解）

# 边状态（与 models 中字符串保持一致，领域层不依赖 ORM）
EDGE_ACTIVE = 'active'
EDGE_WITHDRAWN = 'withdrawn'


@dataclass(frozen=True)
class Edge:
    """一条同点比测：meter_a 读数 − meter_b 读数 ∈ [low, high]。"""

    comparison_id: int
    batch_key: str
    a: str
    b: str
    low: float
    high: float


@dataclass
class MeterRange:
    meter: str
    is_baseline: bool
    # offset 满足 reading_base = reading_meter + offset，offset ∈ [offset_low, offset_high]
    offset_low: float | None = None
    offset_high: float | None = None
    # 内部变量 x = -offset 的可行界，便于排查
    x_low: float | None = None
    x_high: float | None = None


@dataclass
class GroupResult:
    group_key: str
    platform: str | None
    meters: list[str]
    baseline: str | None
    status: str
    reason: str = ''
    ranges: list[MeterRange] = field(default_factory=list)
    conflict_edges: list[dict] = field(default_factory=list)


def _connected_components(meters, edges):
    """按活跃比测边求表计连通分量；孤立表各自成组。"""
    adjacency = defaultdict(set)
    for edge in edges:
        adjacency[edge.a].add(edge.b)
        adjacency[edge.b].add(edge.a)
    remaining = set(meters)
    components = []
    while remaining:
        start = min(remaining)
        stack = [start]
        component = []
        remaining.discard(start)
        while stack:
            node = stack.pop()
            component.append(node)
            for neighbour in adjacency[node]:
                if neighbour in remaining:
                    remaining.discard(neighbour)
                    stack.append(neighbour)
        components.append(sorted(component))
    return components


def _build_graph(edges):
    """差分约束图。

    low <= x_a - x_b <= high 拆成：
        x_a - x_b <= high   →  边 b → a，权 high
        x_b - x_a <= -low   →  边 a → b，权 -low
    """
    graph = defaultdict(list)
    for edge in edges:
        graph[edge.b].append((edge.a, edge.high, edge))
        graph[edge.a].append((edge.b, -edge.low, edge))
    return graph


def _shortest_paths(nodes, graph, source):
    """以 source 为零锚点的单源 Bellman-Ford，返回 (距离, 前驱, 负环上的边)。

    最短距离 d[v] 即约束 x_v - x_source <= d[v] 的最紧上界。
    第 V 轮仍可松弛说明存在负环；沿前驱链回溯可定位该负环，
    返回环上经过的全部比测边作为冲突证据。
    """
    infinity = float('inf')
    dist = {node: infinity for node in nodes}
    dist[source] = 0.0
    predecessor = {}
    for _ in range(len(nodes) - 1):
        changed = False
        for tail in nodes:
            if dist[tail] == infinity:
                continue
            base = dist[tail]
            for head, weight, _edge in graph.get(tail, ()):
                candidate = base + weight
                if candidate + 1e-12 < dist[head]:
                    dist[head] = candidate
                    predecessor[head] = tail
                    changed = True
        if not changed:
            break
    conflict_edges = set()
    for tail in nodes:
        if dist[tail] == infinity:
            continue
        for head, weight, edge in graph.get(tail, ()):
            if dist[tail] + weight + 1e-12 < dist[head]:
                # 沿前驱链回溯，定位 head 所在负环上的节点
                cycle_nodes = set()
                cursor = head
                while cursor not in cycle_nodes and cursor in predecessor:
                    cycle_nodes.add(cursor)
                    cursor = predecessor[cursor]
                cycle_nodes.add(cursor)
                # 收集端点都在环上的比测边
                for node in cycle_nodes:
                    for _h, _w, candidate_edge in graph.get(node, ()):
                        if _h in cycle_nodes:
                            conflict_edges.add(candidate_edge.comparison_id)
    return dist, predecessor, conflict_edges


def _feasible_bounds(nodes, graph, baseline):
    """以基准表为零锚点，求每个变量 x_m 的最紧可行区间 [x_low, x_high]。

    d[v] = 超级源（距离 0）最短距离给出 x_v 的上界；把所有边反向并再次求最短路，
    得到 -x_v 的上界，即 x_v 的下界。
    """
    upper, _pred, conflict = _shortest_paths(nodes, graph, baseline)
    if conflict:
        return None, conflict
    reversed_graph = defaultdict(list)
    for tail in nodes:
        for head, weight, edge in graph.get(tail, ()):
            reversed_graph[head].append((tail, weight, edge))
    neg_upper, _pred, conflict2 = _shortest_paths(nodes, reversed_graph, baseline)
    if conflict2:
        return None, conflict2
    bounds = {}
    for node in nodes:
        x_high = upper[node]                       # x_node <= d[node]
        x_low = -neg_upper[node]                   # -x_node <= d'[node]
        bounds[node] = (x_low, x_high)
    return bounds, set()


def solve_groups(meters, meter_platform, baselines, edges, precision=3):
    """对全部表计分组求解。

    meters: 参与计算的表计代码列表
    meter_platform: {meter: platform}
    baselines: {platform: baseline_meter}
    edges: Edge 列表（仅活跃比测）
    precision: 换算范围展示与对外使用时的小数位
    返回 GroupResult 列表，按组键排序。
    """
    results = []
    for component in _connected_components(sorted(meters), edges):
        platforms = sorted({meter_platform[m] for m in component if meter_platform.get(m)})
        platform = platforms[0] if len(platforms) == 1 else (platforms[0] if platforms else None)
        group_edges = [e for e in edges if e.a in component and e.b in component]
        group_key = f'{platform or "跨站台"}:' + '|'.join(component)
        baseline = baselines.get(platform) if platform else None
        if baseline not in component:
            baseline = None
        if baseline is None:
            reason = '该换算组没有可用的站台基准表，暂停换算；请先在本站台指定基准表。'
            results.append(GroupResult(group_key, platform, component, None, NO_BASELINE, reason))
            continue
        graph = _build_graph(group_edges)
        nodes = component
        bounds, conflict = _feasible_bounds(nodes, graph, baseline)
        if bounds is None:
            evidence = []
            seen = set()
            for edge in group_edges:
                if edge.comparison_id in conflict and edge.comparison_id not in seen:
                    seen.add(edge.comparison_id)
                    evidence.append({
                        'comparison_id': edge.comparison_id,
                        'batch_key': edge.batch_key,
                        'meter_a': edge.a,
                        'meter_b': edge.b,
                        'low': edge.low,
                        'high': edge.high,
                    })
            reason = '组内比测偏差区间无法同时满足（约束冲突），暂停换算；请核对冲突比测后撤回重录。'
            results.append(GroupResult(group_key, platform, component,
                                       baselines.get(platform), CONFLICT, reason,
                                       conflict_edges=evidence))
            continue
        ranges = []
        for node in nodes:
            x_low, x_high = bounds[node]
            # x = -offset，故 offset ∈ [-x_high, -x_low]
            offset_low = round(-x_high, precision)
            offset_high = round(-x_low, precision)
            ranges.append(MeterRange(
                meter=node,
                is_baseline=(node == baseline),
                offset_low=0.0 if node == baseline else offset_low,
                offset_high=0.0 if node == baseline else offset_high,
                x_low=round(x_low, precision),
                x_high=round(x_high, precision),
            ))
        results.append(GroupResult(group_key, platform, component, baseline, ACTIVE,
                                   ranges=ranges))
    return sorted(results, key=lambda item: item.group_key)


def edge_fingerprint(edges):
    """当前活跃边集的稳定指纹，用于发布去重。"""
    import hashlib
    import json
    payload = [
        {'a': e.a, 'b': e.b, 'low': round(e.low, 6), 'high': round(e.high, 6),
         'batch': e.batch_key, 'id': e.comparison_id}
        for e in sorted(edges, key=lambda e: (e.a, e.b, e.comparison_id))
    ]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def group_payload(result):
    """把求解结果转成可 JSON 序列化、可落库的 dict。"""
    return {
        'group_key': result.group_key,
        'platform': result.platform,
        'meters': result.meters,
        'baseline': result.baseline,
        'status': result.status,
        'reason': result.reason,
        'conflict_edges': result.conflict_edges,
        'ranges': [
            {
                'meter': item.meter,
                'is_baseline': item.is_baseline,
                'offset_low': item.offset_low,
                'offset_high': item.offset_high,
            }
            for item in result.ranges
        ],
    }
