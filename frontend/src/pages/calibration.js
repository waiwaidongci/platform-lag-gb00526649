import {feedback, field} from '../components/common/fields.js';
import {empty, panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {GROUP_PILL, GROUP_STATE} from '../constants/enums.js';
import {handler} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, h, p} from '../utils/dom.js';
import {date} from '../utils/format.js';

const rangeText = item =>
  item.is_baseline ? '基准表（换算量 0）'
    : `${item.offset_low} ~ ${item.offset_high} dB(A)`;

function groupBadge(group) {
  return pill(GROUP_STATE[group.status] || group.status, GROUP_PILL[group.status] || group.status);
}

function pendingGroupPanel(group) {
  const head = el('div', {class:'group-head'},
    h(3, `换算组 · ${group.platform || '跨站台'} · ${group.meters.join(' / ')}`),
    groupBadge(group));
  const children = [head];
  if (group.status === 'no_baseline') {
    children.push(p(group.reason, 'feedback'));
  } else if (group.status === 'conflict') {
    children.push(p(group.reason, 'feedback'));
    children.push(table([
      {key:'comparison_id', label:'比测编号'},
      {key:'meter_a', label:'表 A'}, {key:'meter_b', label:'表 B'},
      {label:'偏差区间（A−B，dB）', render:row=>`${row.low} ~ ${row.high}`},
    ], group.conflict_edges, {caption:'参与冲突的比测', pageSize:8}));
  } else {
    children.push(p(`基准表：${group.baseline}；换算范围同时满足组内全部同点比测。`, 'subtle'));
    children.push(table([
      {key:'meter', label:'声级计'},
      {label:'是否基准', render:row=>row.is_baseline ? '是' : '否'},
      {label:'换算范围（折到基准的 offset，dB(A)）', render:rangeText},
    ], group.ranges, {caption:'各表换算范围', pageSize:10}));
  }
  return el('article', {class:'group-card'}, ...children);
}

function parseIntervalLines(text) {
  const items = [];
  const lines = text.split('\n').map(line => line.trim()).filter(Boolean);
  for (const [index, line] of lines.entries()) {
    const parts = line.split(/[,\t，;；]/).map(part => part.trim());
    if (parts.length !== 4) {
      throw new Error(`第 ${index + 1} 行应为「表A,表B,偏差下界,偏差上界」，实际为：${line}`);
    }
    const [meter_a, meter_b, lowRaw, highRaw] = parts;
    const low = Number(lowRaw);
    const high = Number(highRaw);
    if (!meter_a || !meter_b || !Number.isFinite(low) || !Number.isFinite(high)) {
      throw new Error(`第 ${index + 1} 行的表号或偏差数值无法识别：${line}`);
    }
    items.push({meter_a, meter_b, deviation_low:low, deviation_high:high});
  }
  if (!items.length) throw new Error('请至少填写一行同点比测');
  return items;
}

export async function calibrationPage(route, refresh) {
  const data = await workspace.calibration();
  const comparisons = await workspace.calibrationComparisons();
  const canWrite = session.canWrite;
  const canReview = session.canReview;

  // ------------------------------------------------ 待更新 / 待发布的换算组
  const pending = data.pending_groups || [];
  const staleKeys = new Set(data.stale_group_keys || []);
  const pendingPanel = panel('待发布的换算结果（当前比测口径）',
    p('每次录入或撤回比测后，受影响组的旧结果标记为待更新；点「整体切换」在一个事务内算齐并一起发布。', 'subtle'),
    pending.length
      ? el('div', {class:'group-grid'}, pending.map(group =>
          el('div', {}, pendingGroupPanel(group),
            staleKeys.has(group.group_key) && pill('旧结果待更新', 'blocked'))))
      : empty('还没有可求解的换算组，请先建台账并录入同点比测。'));

  // ------------------------------------------------ 已发布的换算版本
  const cursors = data.cursors || [];
  const releasePanel = panel('当前生效的换算版本',
    cursors.length ? table([
      {label:'换算组', render:row=>row.result?.meters?.join(' / ') || row.group_key},
      {label:'站台', render:row=>row.result?.platform || row.platform || '—'},
      {label:'状态', render:row=>row.stale
        ? el('span', {}, pill('待更新', 'blocked'), p(row.stale_reason || '', 'feedback'))
        : groupBadge(row.result || {status:'active'})},
      {label:'发布编号', render:row=>row.release_id ?? '—'},
      {label:'更新时间', render:row=>date(row.updated_at)},
      {label:'换算范围 / 原因', render:row=>{
        const result = row.result;
        if (!result) return '—';
        if (result.status !== 'active') return p(result.reason, 'feedback');
        return el('ul', {class:'range-list'}, result.ranges.map(item =>
          el('li', {}, `${item.meter}：${rangeText(item)}`)));
      }},
    ], cursors, {caption:'当前生效的换算版本', empty:'尚未发布过换算版本。', pageSize:10})
      : empty('尚未发布过换算版本。'));

  // ------------------------------------------------ 发布按钮
  const publishMessage = feedback();
  const publishBtn = button('整体切换并发布', {class:'primary', 'data-testid':'publish-calibration'});
  publishBtn.addEventListener('click', handler(publishBtn, publishMessage, async () => {
    const result = await workspace.publishCalibration(data.fingerprint);
    publishMessage.textContent = result.changed
      ? `已发布版本 #${result.release_id}：可换算 ${result.active_count} 组，暂停 ${result.blocked_count} 组。`
      : (result.reason || '没有需要切换的换算组');
    await refresh();
  }));
  const publishBar = panel('发布',
    p(`当前比测边集指纹：${data.fingerprint.slice(0, 12)}…（发布前若有补传会自动并入，无需手动刷新）`, 'subtle'),
    canReview ? el('div', {class:'actions'}, publishBtn, publishMessage)
      : p('仅复核主管可以执行整体切换。', 'empty'));

  // ------------------------------------------------ 声级计台账
  const meterMessage = feedback();
  const meterColumns = [
    {key:'meter_code', label:'表号'}, {key:'label', label:'名称'},
    {key:'platform', label:'站台'},
    {label:'基准表', render:row=>row.is_baseline ? pill('站台基准', 'active') : '—'},
  ];
  if (canReview) {
    meterColumns.push({label:'基准操作', render:row=>{
      const btn = button(row.is_baseline ? '取消基准' : '设为基准', {class:'quiet'});
      btn.addEventListener('click', handler(btn, meterMessage, async () => {
        await workspace.setBaseline(row.id, !row.is_baseline);
        await refresh();
      }));
      return btn;
    }});
  }
  const meterForm = canWrite && el('form', {class:'fields'},
    field({key:'meter_code', label:'声级计表号', type:'string', max_length:40}),
    field({key:'label', label:'名称/型号', type:'string', max_length:120}),
    field({key:'platform', label:'所属站台', type:'string', max_length:80}),
    el('label', {class:'check'}, el('input', {name:'is_baseline', type:'checkbox'}), '设为该站台基准表'),
    button('登记声级计', {type:'submit', class:'primary'}), meterMessage);
  if (meterForm) {
    meterForm.addEventListener('submit', handler(meterForm.querySelector('button'), meterMessage, async event => {
      const form = new FormData(event.target);
      await workspace.createMeter({
        meter_code:form.get('meter_code'), label:form.get('label'),
        platform:form.get('platform'), is_baseline:form.has('is_baseline'),
      });
      await refresh();
    }));
  }
  const ledgerPanel = panel('声级计台账与基准指定',
    p('每个站台至多一台基准表；换算组没有基准时暂停换算并在上方标明原因。', 'subtle'),
    table(meterColumns, data.meters, {caption:'声级计台账', empty:'还没有登记声级计。', pageSize:10}),
    meterForm);

  // ------------------------------------------------ 比测录入
  const compareMessage = feedback();
  const compareForm = canWrite && el('form', {class:'fields'},
    field({key:'batch_key', label:'比测批次号（同版重试不重复发布）', type:'string', max_length:80}),
    field({key:'measured_at', label:'比测时间（可选）', type:'string', required:false, max_length:30}),
    el('label', {class:'field', for:'interval-lines'}, el('span',{},'同点比测（每行：表A,表B,偏差下界,偏差上界）'),
      el('textarea', {id:'interval-lines', name:'intervals', rows:5, required:true,
        placeholder:'M1,M0,0.4,0.8\nM2,M1,-0.2,0.2'})),
    button('录入比测批次', {type:'submit', class:'primary'}), compareMessage);
  if (compareForm) {
    compareForm.addEventListener('submit', handler(compareForm.querySelector('button'), compareMessage, async event => {
      const form = new FormData(event.target);
      const items = parseIntervalLines(String(form.get('intervals') || ''));
      const result = await workspace.submitComparisons({
        batch_key:form.get('batch_key'), measured_at:form.get('measured_at') || '',
        note:'', items,
      });
      compareMessage.textContent = result.message;
      await refresh();
    }));
  }

  const comparisonColumns = [
    {key:'id', label:'编号'}, {key:'batch_key', label:'批次号'},
    {key:'meter_a', label:'表 A'}, {key:'meter_b', label:'表 B'},
    {key:'platform', label:'站台'},
    {label:'偏差区间（A−B）', render:row=>`${row.deviation_low} ~ ${row.deviation_high}`},
    {label:'状态', render:row=>row.status === 'withdrawn'
      ? pill('已撤回', 'blocked') : pill('生效中', 'active')},
    {key:'withdraw_reason', label:'撤回原因'},
  ];
  const withdrawMessage = feedback();
  if (canWrite) {
    comparisonColumns.push({label:'撤回', render:row=>{
      if (row.status === 'withdrawn') return '—';
      const btn = button('撤回', {class:'quiet'});
      btn.addEventListener('click', handler(btn, withdrawMessage, async () => {
        const reason = window.prompt('请填写撤回原因（撤回后旧结果标待更新，发布时整体切换）');
        if (!reason || !reason.trim()) throw new Error('撤回需要填写原因');
        const result = await workspace.withdrawComparison(row.id, reason.trim());
        withdrawMessage.textContent = result.message;
        await refresh();
      }));
      return btn;
    }});
  }
  const comparePanel = panel('同点比测记录与撤回',
    p('偏差 = 表 A 读数 − 表 B 读数（dB(A)）。撤回不会删数据，只让受影响组待更新。', 'subtle'),
    table(comparisonColumns, comparisons, {caption:'同点比测记录', empty:'还没有比测记录。', pageSize:10}),
    withdrawMessage, compareForm);

  return el('div', {class:'calibration-page'},
    el('div', {class:'overview-main'}, pendingPanel, releasePanel, comparePanel),
    el('aside', {class:'sidebar-panel'}, publishBar, ledgerPanel,
      panel('口径说明', el('ul', {class:'method-list'}, [
        '同点比测的偏差区间按差分约束联合求解，每台表的换算范围必须同时满足组内全部比测。',
        '无基准表或比测冲突的组暂停换算，其他组照常出结果，原因在卡片内标明。',
        '撤回、补传或更换基准后旧结果标待更新，整体切换时拆开的组算齐再一起发布。',
        '同一批次号重试直接复用原批次，不会重复落库或重复发布。',
      ].map(text => el('li', {}, text))))));
}
