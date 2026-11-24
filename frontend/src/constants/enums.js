export const ROLE = {admin:'复核主管', analyst:'分析员', viewer:'只读查看'};
export const STATUS = {queued:'排队中', running:'正在计算', complete:'计算完成', failed:'运行失败'};
export const REVIEW = {pending:'待复核', accepted:'已通过', returned:'已退回'};
export const GROUP_STATE = {
  active:'可换算', no_baseline:'无基准表', conflict:'比测冲突',
};
export const GROUP_PILL = {active:'active', no_baseline:'blocked', conflict:'failed'};
