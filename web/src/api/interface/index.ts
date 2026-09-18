// 请求响应参数（不包含data）
// 注：上游脚手架此处声明为 string，但实际接口与 ResultEnum.SUCCESS 都是数字码，
// 且响应拦截器用 `==` 比较，故修正为 number 以匹配真实返回。
export interface Result {
  code: number;
  msg: string;
}

// 请求响应参数（包含data）
export interface ResultData<T = any> extends Result {
  data: T;
}

// 分页响应参数
export interface ResPage<T> {
  list: T[];
  pageNum: number;
  pageSize: number;
  total: number;
}

// 分页请求参数
export interface ReqPage {
  pageNum: number;
  pageSize: number;
}

// 文件上传模块
export namespace Upload {
  export interface ResFileUrl {
    fileUrl: string;
  }
}

// 登录模块
export namespace Login {
  export interface ReqLoginForm {
    username: string;
    password: string;
  }
  export interface ResLogin {
    access_token: string;
  }
  export interface ResAuthButtons {
    [key: string]: string[];
  }
}

// 单店财务看板模块
export namespace Dashboard {
  /** 看板查询参数 */
  export interface ReqOverview {
    /** 店铺标识（当前仅支持单店） */
    shopId?: string;
    /** 统计起始日期 YYYY-MM-DD */
    startDate?: string;
    /** 统计结束日期 YYYY-MM-DD */
    endDate?: string;
  }

  /** 顶部指标卡数据 */
  export interface ResOverview {
    /** 店铺名 */
    shop_name: string;
    /** 数据截止时间 */
    data_updated_at: string;
    /** 实际利润合计（CNY） */
    actual_profit_cny: number;
    /** 预估利润合计（CNY） */
    estimated_profit_cny: number;
    /** 核算完成率，0~1，= 完整核算订单数 / 总订单数 */
    completion_rate: number;
    /** 待发货逾期单数 */
    overdue_count: number;
    /** 总订单数 */
    total_order_count: number;
    /** 完整核算订单数 */
    completed_order_count: number;
  }

  /** 趋势图单日数据点 */
  export interface ResTrendPoint {
    /** 日期 YYYY-MM-DD */
    date: string;
    /** 当日实际利润（CNY） */
    actual_profit_cny: number;
    /** 当日预估利润（CNY） */
    estimated_profit_cny: number;
  }

  /** 利润构成项（金额单位统一为 CNY） */
  export interface ResProfitBreakdownItem {
    /** 构成项名称 */
    name: string;
    /** 构成项金额（CNY） */
    value_cny: number;
  }

  /** 订单核算状态：完整 / 不完整 */
  export type OrderStatus = "complete" | "incomplete";

  /** 订单明细行 */
  export interface ResOrderRow {
    /** 订单号 */
    posting_number: string;
    /** 结算日期 YYYY-MM-DD */
    settlement_date: string;
    /** 直接净额（₽） */
    direct_net_rub: number;
    /** 卢布对人民币汇率 */
    exchange_rate: number;
    /** 采购成本（¥） */
    purchase_cost_cny: number;
    /** 实际利润（¥） */
    actual_profit_cny: number;
    /** 核算状态 */
    status: OrderStatus;
    /** 核算不完整的原因，status 为 complete 时为空 */
    unknown_reason?: string;
  }

  /** 看板聚合响应 */
  export interface ResDashboard {
    overview: ResOverview;
    trend: ResTrendPoint[];
    profit_breakdown: ResProfitBreakdownItem[];
    orders: ResOrderRow[];
  }
}

// 用户管理模块
export namespace User {
  export interface ReqUserParams extends ReqPage {
    username: string;
    gender: number;
    idCard: string;
    email: string;
    address: string;
    createTime: string[];
    status: number;
  }
  export interface ResUserList {
    id: string;
    username: string;
    gender: number;
    user: { detail: { age: number } };
    idCard: string;
    email: string;
    address: string;
    createTime: string;
    status: number;
    avatar: string;
    photo: any[];
    children?: ResUserList[];
  }
  export interface ResStatus {
    userLabel: string;
    userValue: number;
  }
  export interface ResGender {
    genderLabel: string;
    genderValue: number;
  }
  export interface ResDepartment {
    id: string;
    name: string;
    children?: ResDepartment[];
  }
  export interface ResRole {
    id: string;
    name: string;
    children?: ResDepartment[];
  }
}
