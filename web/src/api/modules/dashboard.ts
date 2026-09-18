import dayjs from "dayjs";

import type { Dashboard } from "@/api/interface";

/**
 * @description 单店财务看板数据源
 *
 * ⚠️ 当前为【本地 mock 数据源】，后端接口尚未就绪。
 * 数据真实来源约定（后续替换时保持字段名不变即可，页面无需改动）：
 *   GET /api/dashboard/overview  ->  Dashboard.ResDashboard
 *
 * 替换步骤：
 *   1) 把 USE_LOCAL_MOCK 置为 false；
 *   2) 取消 mockDashboardApi 中 `http.get(...)` 的注释并删除下面的 getLocalMockDashboard()。
 */

/** 是否使用本地 mock 数据（后端接通后改为 false） */
export const USE_LOCAL_MOCK = true;

/** 店铺名（mock，未来由接口返回） */
export const MOCK_SHOP_NAME = "OZON 俄罗斯站 · 主力店";

/** 订单核算状态枚举 */
export const ORDER_STATUS_MAP: Record<Dashboard.OrderStatus, { label: string; tagType: "success" | "warning" }> = {
  complete: { label: "完整", tagType: "success" },
  incomplete: { label: "不完整", tagType: "warning" }
};

/** mock 汇率兜底值：1 CNY ≈ 11.05 ₽ */
const MOCK_EXCHANGE_RATE = 11.05;

/** 今日日期，mock 订单与趋势都以此为基准向前推算，保证演示数据永远"新鲜" */
const today = () => dayjs().format("YYYY-MM-DD");

/** 相对今天的偏移日期 */
const offsetDate = (daysAgo: number) => dayjs().subtract(daysAgo, "day").format("YYYY-MM-DD");

/**
 * @description 订单明细 mock 数据（9 行）
 * 费用口径：actual_profit_cny = direct_net_rub / exchange_rate - purchase_cost_cny - (平台费用 + 物流费用)
 *          其中平台费用与物流费用按固定比例分摊，仅用于演示。
 */
const MOCK_ORDER_SEED: Array<
  Pick<Dashboard.ResOrderRow, "posting_number" | "direct_net_rub" | "purchase_cost_cny" | "status" | "unknown_reason"> & {
    daysAgo: number;
    exchange_rate: number;
    fee_cny: number;
  }
> = [
  {
    posting_number: "0571-0234-8901",
    daysAgo: 0,
    direct_net_rub: 8460,
    exchange_rate: 11.05,
    purchase_cost_cny: 268.5,
    status: "complete",
    fee_cny: 0
  },
  {
    posting_number: "0571-0234-8902",
    daysAgo: 1,
    direct_net_rub: 6240,
    exchange_rate: 11.08,
    purchase_cost_cny: 192.8,
    status: "complete",
    fee_cny: 0
  },
  {
    posting_number: "0571-0234-8903",
    daysAgo: 2,
    direct_net_rub: 11890,
    exchange_rate: 11.1,
    purchase_cost_cny: 425.6,
    status: "incomplete",
    unknown_reason: "缺少采购发票，采购成本按上次报价暂估",
    fee_cny: 0
  },
  {
    posting_number: "0571-0234-8904",
    daysAgo: 3,
    direct_net_rub: 4120,
    exchange_rate: 11.02,
    purchase_cost_cny: 118.4,
    status: "complete",
    fee_cny: 0
  },
  {
    posting_number: "0571-0234-8905",
    daysAgo: 4,
    direct_net_rub: 9750,
    exchange_rate: 10.98,
    purchase_cost_cny: 336.2,
    status: "complete",
    fee_cny: 0
  },
  {
    posting_number: "0571-0234-8906",
    daysAgo: 5,
    direct_net_rub: 7330,
    exchange_rate: 11.05,
    purchase_cost_cny: 246.9,
    status: "incomplete",
    unknown_reason: "平台佣金账单未同步，平台费用待补",
    fee_cny: 0
  },
  {
    posting_number: "0571-0234-8907",
    daysAgo: 6,
    direct_net_rub: 5890,
    exchange_rate: 11.12,
    purchase_cost_cny: 174.6,
    status: "incomplete",
    unknown_reason: "物流费用缺失，跨境头程单号未匹配到账单",
    fee_cny: 0
  },
  {
    posting_number: "0571-0234-8908",
    daysAgo: 7,
    direct_net_rub: 10460,
    exchange_rate: 11.0,
    purchase_cost_cny: 372.5,
    status: "complete",
    fee_cny: 0
  },
  {
    posting_number: "0571-0234-8909",
    daysAgo: 8,
    direct_net_rub: 2614,
    exchange_rate: 11.06,
    purchase_cost_cny: 96.3,
    status: "complete",
    fee_cny: 0
  }
];

/** 由订单种子生成完整订单明细（含派生金额） */
const buildMockOrders = (): Dashboard.ResOrderRow[] =>
  MOCK_ORDER_SEED.map(seed => {
    // 平台费用 + 物流费用：按直接净额的固定比例分摊（mock 口径）
    const grossCny = seed.direct_net_rub / seed.exchange_rate;
    const platformFeeCny = grossCny * 0.15;
    const logisticsFeeCny = grossCny * 0.08;
    const actualProfitCny = grossCny - seed.purchase_cost_cny - platformFeeCny - logisticsFeeCny;

    return {
      posting_number: seed.posting_number,
      settlement_date: offsetDate(seed.daysAgo),
      direct_net_rub: seed.direct_net_rub,
      exchange_rate: seed.exchange_rate,
      purchase_cost_cny: seed.purchase_cost_cny,
      actual_profit_cny: Number(actualProfitCny.toFixed(2)),
      status: seed.status,
      unknown_reason: seed.unknown_reason
    };
  });

/** 由订单明细汇总顶部指标（保证指标卡与明细表口径一致） */
const buildOverview = (orders: Dashboard.ResOrderRow[]): Dashboard.ResOverview => {
  const actualProfit = orders.reduce((sum, row) => sum + row.actual_profit_cny, 0);
  // 预估利润 = 实际利润 + 未完整核算订单的待补费用（mock 估算口径）
  const estimatedGap = orders.filter(row => row.status === "incomplete").length * 42.5;
  const completedCount = orders.filter(row => row.status === "complete").length;
  // 待发货逾期单：mock 口径 = 不完整核算订单数 + 1
  const overdueCount = orders.filter(row => row.status === "incomplete").length + 1;

  return {
    shop_name: MOCK_SHOP_NAME,
    data_updated_at: dayjs().format("YYYY-MM-DD HH:mm"),
    actual_profit_cny: Number(actualProfit.toFixed(2)),
    estimated_profit_cny: Number((actualProfit + estimatedGap).toFixed(2)),
    completion_rate: orders.length ? completedCount / orders.length : 0,
    overdue_count: overdueCount,
    total_order_count: orders.length,
    completed_order_count: completedCount
  };
};

/**
 * @description 按日期区间生成每日"实际利润 / 预估利润"mock 曲线
 * 使用正弦波 + 线性趋势，保证同一区间每次渲染结果稳定（便于核对），不做随机抖动。
 */
const buildTrend = (startDate: string, endDate: string): Dashboard.ResTrendPoint[] => {
  const start = dayjs(startDate);
  const days = Math.max(dayjs(endDate).diff(start, "day") + 1, 1);

  return Array.from({ length: days }, (_, index) => {
    const wave = Math.sin(index / 1.6) * 26 + Math.cos(index / 3.1) * 14;
    const actual = 312 + index * 9.4 + wave;
    // 预估永远略高于实际：未完整核算订单的待补费用尚未冲减
    const estimated = actual + 38 + Math.sin(index / 2.2) * 6;

    return {
      date: start.add(index, "day").format("YYYY-MM-DD"),
      actual_profit_cny: Number(actual.toFixed(2)),
      estimated_profit_cny: Number(estimated.toFixed(2))
    };
  });
};

/**
 * @description 利润构成（统一折算为 CNY，便于占比可解释）
 * ⚠️ 直接净额原始单位为 ₽，此处按区间平均汇率折算为 ¥；
 *    真实接口应直接下发统一币种口径，届时删除折算逻辑即可。
 */
const buildProfitBreakdown = (orders: Dashboard.ResOrderRow[]): Dashboard.ResProfitBreakdownItem[] => {
  const directNetRub = orders.reduce((sum, row) => sum + row.direct_net_rub, 0);
  const purchaseCostCny = orders.reduce((sum, row) => sum + row.purchase_cost_cny, 0);
  // 平台费用 15% + 物流费用 8%，与订单明细中的分摊口径保持一致
  const directNetCny = directNetRub / MOCK_EXCHANGE_RATE;

  return [
    { name: "直接净额", value_cny: Number(directNetCny.toFixed(2)) },
    { name: "采购成本", value_cny: Number(purchaseCostCny.toFixed(2)) },
    { name: "平台费用", value_cny: Number((directNetCny * 0.15).toFixed(2)) },
    { name: "物流费用", value_cny: Number((directNetCny * 0.08).toFixed(2)) }
  ];
};

/** 生成本地 mock 看板聚合数据 */
export const getLocalMockDashboard = (params: Dashboard.ReqOverview = {}): Dashboard.ResDashboard => {
  const orders = buildMockOrders();
  const endDate = params.endDate ?? today();
  const startDate = params.startDate ?? dayjs(endDate).subtract(13, "day").format("YYYY-MM-DD");

  return {
    overview: buildOverview(orders),
    trend: buildTrend(startDate, endDate),
    profit_breakdown: buildProfitBreakdown(orders),
    orders
  };
};

/** 模拟网络往返，让加载态可见（真实接口接入后删除） */
const mockDelay = (ms = 260) => new Promise(resolve => setTimeout(resolve, ms));

/**
 * @description 单店财务看板聚合数据
 * @param params Dashboard.ReqOverview 统计区间
 * @returns Promise<Dashboard.ResDashboard>
 */
export const mockDashboardApi = async (params: Dashboard.ReqOverview = {}): Promise<Dashboard.ResDashboard> => {
  if (USE_LOCAL_MOCK) {
    await mockDelay();
    return getLocalMockDashboard(params);
  }
  // 后端就绪后启用下面这行，并删除上方 mock 分支
  // return http.get<Dashboard.ResDashboard>("/dashboard/overview", params, { loading: false });
  throw new Error("看板接口尚未接入：请将 USE_LOCAL_MOCK 置为 true，或补全 dashboard.ts 中的真实请求");
};
