<template>
  <div class="cost-page">
    <!-- 台账读不出来（401/403/503/网络）：不白屏，给出原因 + 下一步 -->
    <div v-if="loadError" class="card error-panel">
      <el-icon class="error-icon"><Warning /></el-icon>
      <h3 class="error-title">{{ errorTitle }}</h3>
      <p class="error-desc">{{ errorDesc }}</p>
      <div class="error-actions">
        <el-button v-if="loadError.status !== 403" type="primary" :icon="Refresh" @click="bootstrap">重试</el-button>
        <el-button @click="goLogin">去登录</el-button>
      </div>
      <p class="error-tip">
        接口：<code>GET /api/costs?keyword=&amp;limit=&amp;offset=</code> → 127.0.0.1:8849（经 vite dev 代理）
      </p>
    </div>

    <template v-else>
      <header class="page-head card">
        <div class="head-main">
          <div class="head-title">
            <el-icon class="head-icon"><Wallet /></el-icon>
            <span>成本管理</span>
            <el-tag class="count-tag" size="small" effect="plain">
              {{ ledgerLoading && !ledgerLoaded ? "读取中…" : `${formatInt(ledgerTotal)} 条成本` }}
            </el-tag>
            <el-tag
              class="count-tag clickable-tag"
              size="small"
              type="info"
              effect="plain"
              title="查看成本变更留痕（谁、何时、把哪个货号从多少改成多少）"
              @click="openEvents"
            >
              变更留痕 {{ formatInt(changeEventTotal) }} 条 · 查看
            </el-tag>
          </div>
          <div class="head-meta">
            <span class="meta-item">
              <el-icon><InfoFilled /></el-icon>
              成本库：<code>{{ bookPath || "读取中…" }}</code>
            </span>
            <el-divider direction="vertical" />
            <span class="meta-item">
              <el-icon><Clock /></el-icon>
              最后一次变更：{{ formatTime(lastChange?.occurred_at) }}
              <template v-if="lastChange">（{{ lastChange.seller_sku }} · {{ lastChange.action }}）</template>
            </span>
          </div>
        </div>
        <div class="head-actions">
          <el-button :icon="Refresh" :loading="ledgerLoading" @click="reloadAll">刷新</el-button>
          <!-- 写按钮只给 root / finance：前端隐藏只是省掉一次必然 403 的点击，
               真正的隔离点在服务端（operator01 直接调接口会拿到 403） -->
          <el-button v-if="canWrite" :icon="FolderOpened" :loading="migrating" @click="onMigrateLegacy">
            从原产品成本库迁移
          </el-button>
          <el-button v-if="canWrite" type="primary" :icon="UploadFilled" @click="openImportTab">导入成本表</el-button>
        </div>
      </header>

      <!-- 只读边界：把「谁能写、谁在拦」说清楚，而不是假装前端就是隔离点 -->
      <el-alert v-if="roleLoaded && !canWrite" class="mb16" type="info" :closable="false" show-icon>
        <template #title> 当前账号角色为「{{ role || "未知" }}」：只能查看成本台账，看不到迁移 / 导入入口 </template>
        <template #default>
          <p class="readonly-note">
            写操作（迁移、导入成本表）只对 <strong>root / finance</strong> 开放。 这里不显示按钮只是少给一个点了必然失败的入口 ——
            <strong>真正的隔离在服务端</strong>：受限角色直接调用写接口会拿到 <code>403</code>「无权修改采购成本（只有 root /
            finance 角色可以）」。
          </p>
        </template>
      </el-alert>

      <el-tabs v-model="activeTab" class="cost-tabs" @tab-change="onTabChange">
        <!-- ───────────────────────── 1. 采购成本台账 ───────────────────────── -->
        <el-tab-pane label="采购成本台账" name="ledger">
          <div class="card">
            <div class="card-head">
              <h3 class="card-title">采购成本台账</h3>
              <span class="card-tip"> 同一货号可以有多条（按生效日期形成历史）；金额是字符串，界面只做格式化，不做任何加减 </span>
            </div>

            <div class="toolbar">
              <el-input
                v-model="keyword"
                class="search-input"
                clearable
                :prefix-icon="Search"
                placeholder="按货号 / OZON 数字 SKU 搜索（输入后 400ms 自动查）"
              />
              <span class="toolbar-tip">
                命中的是「{{ appliedKeyword || "全部货号" }}」： <b>{{ formatInt(ledgerTotal) }}</b> 条
              </span>
            </div>

            <!-- 表格上方也放一个紧凑翻页条：一页 100 条时不用滚到底才能翻页 -->
            <TablePager
              compact
              :page="page"
              :page-count="pageCount"
              :total="ledgerTotal"
              :page-size="pageSize"
              :page-sizes="pageSizeOptions"
              :loading="ledgerLoading"
              unit="条"
              @update:page="page = $event"
              @change="loadLedger"
            />

            <el-table
              v-loading="ledgerLoading"
              element-loading-text="正在读取成本台账…"
              :data="ledgerRows"
              stripe
              show-overflow-tooltip
              class="cost-table"
            >
              <el-table-column prop="seller_sku" label="货号" min-width="190" align="left" fixed />
              <el-table-column label="OZON 数字 SKU" min-width="130" align="left">
                <template #default="{ row }">{{ row.platform_sku || "--" }}</template>
              </el-table-column>
              <el-table-column label="单价 (¥)" min-width="110" align="right">
                <template #default="{ row }">{{ formatMoney(row.unit_cost_cny) }}</template>
              </el-table-column>
              <el-table-column prop="effective_from" label="生效日期" min-width="115" align="center" />
              <el-table-column label="状态" min-width="95" align="center">
                <template #default="{ row }">
                  <el-tag :type="COST_STATUS_MAP[row.status]?.tagType ?? 'info'" size="small" effect="light">
                    {{ COST_STATUS_MAP[row.status]?.label ?? row.status }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="来源" min-width="180" align="left">
                <template #default="{ row }">{{ row.source || "--" }}</template>
              </el-table-column>
              <el-table-column label="备注" min-width="140" align="left">
                <template #default="{ row }">{{ row.note || "--" }}</template>
              </el-table-column>
              <el-table-column label="更新时间" min-width="140" align="center">
                <template #default="{ row }">{{ formatTime(row.updated_at) }}</template>
              </el-table-column>
              <template #empty>
                <el-empty :image-size="96">
                  <template #description>
                    <p v-if="appliedKeyword">没有匹配「{{ appliedKeyword }}」的成本记录。</p>
                    <p v-else>成本库（<code>{{ bookPath }}</code>）里一条记录都没有。</p>
                    <p v-if="!appliedKeyword" class="empty-hint">
                      可以通过右上角的「从原产品成本库迁移」或「导入成本表」写入。
                    </p>
                  </template>
                </el-empty>
              </template>
            </el-table>

            <TablePager
              :page="page"
              :page-count="pageCount"
              :total="ledgerTotal"
              :page-size="pageSize"
              :page-sizes="pageSizeOptions"
              :loading="ledgerLoading"
              unit="条"
              @update:page="page = $event"
              @update:page-size="pageSize = $event"
              @change="loadLedger"
              @size-change="onPageSizeChange"
            />
          </div>
        </el-tab-pane>

        <!-- ───────────────────────── 2. 缺成本清单 ───────────────────────── -->
        <el-tab-pane label="缺成本清单" name="missing">
          <div class="card">
            <div class="card-head">
              <h3 class="card-title">缺成本清单</h3>
              <span class="card-tip">
                口径：窗口内<strong>有订单</strong>、但成本库里没有这个货号单价的货号（与看板「缺成本 SKU 数」同口径）
              </span>
            </div>

            <div class="toolbar">
              <el-select v-model="missingAlias" class="store-select" placeholder="选择店铺" @change="loadMissing">
                <el-option
                  v-for="item in storeOptions"
                  :key="item.alias"
                  :label="item.display_name"
                  :value="item.alias"
                  :disabled="!item.available"
                />
              </el-select>
              <el-select v-model="missingDays" class="days-select" @change="loadMissing">
                <el-option v-for="item in DASHBOARD_DAY_OPTIONS" :key="item" :label="`近 ${item} 天`" :value="item" />
              </el-select>
              <el-button :icon="Refresh" :loading="missingLoading" @click="loadMissing">刷新</el-button>
              <span v-if="missingData" class="toolbar-tip">
                窗口 {{ missingData.period.start }} ~ {{ missingData.period.end }}（{{ missingData.period.days }} 天） · 单价来源
                <code>{{ missingData.price_source }}</code>
              </span>
            </div>

            <!-- 三个数都是**货号级**（去重）：窗口内货号数 / 有价货号数 / 缺价货号数 -->
            <el-row :gutter="16" class="mb16 metric-row">
              <el-col :xs="24" :sm="8">
                <div class="card metric-card">
                  <div class="metric-label">窗口内货号数</div>
                  <div class="metric-value">{{ missingData ? formatInt(missingData.offer_in_window) : "--" }}</div>
                  <div class="metric-foot">窗口内有订单的货号（去重），不是订单数</div>
                </div>
              </el-col>
              <el-col :xs="24" :sm="8">
                <div class="card metric-card">
                  <div class="metric-label">有单价的货号数</div>
                  <div class="metric-value metric-ok">{{ missingData ? formatInt(missingData.offer_with_price) : "--" }}</div>
                  <div class="metric-foot">
                    成本库共
                    {{ missingData ? formatInt(missingData.book_offer_count) : "--" }} 个货号有单价（按结算日回溯取最近一条）
                  </div>
                </div>
              </el-col>
              <el-col :xs="24" :sm="8">
                <div class="card metric-card">
                  <div class="metric-label">缺单价的货号数</div>
                  <div class="metric-value" :class="missingData?.missing_count ? 'metric-bad' : 'metric-ok'">
                    {{ missingData ? formatInt(missingData.missing_count) : "--" }}
                  </div>
                  <div class="metric-foot">需要补价的就是这些货号；为 0 表示这个窗口不需要补价</div>
                </div>
              </el-col>
            </el-row>

            <el-table
              v-loading="missingLoading"
              element-loading-text="正在比对窗口内货号与成本库…"
              :data="missingData?.missing ?? []"
              stripe
              show-overflow-tooltip
              class="cost-table"
            >
              <el-table-column prop="seller_sku" label="货号" min-width="200" align="left" fixed />
              <el-table-column label="SKU" min-width="140" align="left">
                <template #default="{ row }">{{ row.sku || "--" }}</template>
              </el-table-column>
              <el-table-column label="商品名" min-width="240" align="left">
                <template #default="{ row }">{{ row.product_name || "--" }}</template>
              </el-table-column>
              <el-table-column label="数量" min-width="90" align="right">
                <template #default="{ row }">{{ formatInt(row.quantity) }}</template>
              </el-table-column>
              <el-table-column label="订单数" min-width="90" align="right">
                <template #default="{ row }">{{ formatInt(row.order_count) }}</template>
              </el-table-column>
              <el-table-column label="归属成本 (¥)" min-width="130" align="right">
                <template #default="{ row }">{{ formatMoney(row.attributed_cost_cny) }}</template>
              </el-table-column>
              <el-table-column prop="reason" label="原因" min-width="220" align="left" />
              <template #empty>
                <!-- ⚠️ 空清单是**正确结果**（窗口内每个货号都有单价），
                     不能写成「暂无数据 / 加载失败」—— 那会让人去补一个并不缺的价 -->
                <div class="missing-empty">
                  <el-icon class="missing-empty-icon"><CircleCheck /></el-icon>
                  <p class="missing-empty-title">
                    <template v-if="missingData">
                      窗口内 {{ formatInt(missingData.offer_in_window) }} 个货号都有单价 （{{
                        formatInt(missingData.offer_with_price)
                      }}/{{ formatInt(missingData.offer_in_window) }} 覆盖），
                      缺成本清单为空说明<strong>这个窗口不需要补价</strong>。
                    </template>
                    <template v-else>还没有取到缺成本清单</template>
                  </p>
                  <p class="missing-empty-hint">
                    ⚠️ 这与看板上的「缺成本 SKU」不是一件事：看板那个数来自
                    <code>cost_not_attributable</code>（订单级成本**归不到货号**，例如多货号订单不摊分），
                    本清单只回答「成本库里有没有这个货号的价」。两件事分开看，否则会去补一个并不缺的价。
                  </p>
                  <p v-if="missingData?.truncated" class="missing-empty-hint">
                    另有
                    {{ formatInt(Math.max(0, missingData.missing_count - (missingData.missing?.length ?? 0))) }}
                    个缺价货号没显示（单次最多 200 行）。
                  </p>
                </div>
              </template>
            </el-table>
            <p v-if="missingData && missingData.missing.length && missingData.truncated" class="truncated-tip">
              清单已截断：共 {{ formatInt(missingData.missing_count) }} 个缺价货号，本次显示前
              {{ formatInt(missingData.missing.length) }} 个。
            </p>
          </div>
        </el-tab-pane>

        <!-- ───────────────────────── 3. 导入成本表 ───────────────────────── -->
        <el-tab-pane label="导入成本表" name="import">
          <div class="card">
            <div class="card-head">
              <h3 class="card-title">导入成本表</h3>
              <span class="card-tip">
                模板列名：<code>货号</code> + <code>单价</code>（可选 <code>OZON 数字 SKU</code>、<code>备注</code>、
                <code>生效日期</code>）；支持 .xlsx / .xlsm / .csv，单文件 ≤ 20 MB
              </span>
            </div>

            <el-alert v-if="roleLoaded && !canWrite" class="mb16" type="warning" :closable="false" show-icon>
              <template #title>当前账号（{{ role || "未知" }}）没有写权限，「预览」与「确认导入」都会得到 403</template>
              <template #default>
                <p class="readonly-note">
                  下面保留完整表单是为了说明「谁在拦」：提交后端会返回
                  <code>403</code>「无权修改采购成本（只有 root / finance 角色可以）」。
                </p>
              </template>
            </el-alert>

            <div class="toolbar">
              <span class="toolbar-label">影响面按哪个店铺、哪个窗口算：</span>
              <el-select v-model="importAlias" class="store-select" placeholder="选择店铺" @change="onImportInputChange">
                <el-option
                  v-for="item in storeOptions"
                  :key="item.alias"
                  :label="item.display_name"
                  :value="item.alias"
                  :disabled="!item.available"
                />
              </el-select>
              <el-select v-model="importDays" class="days-select" @change="onImportInputChange">
                <el-option v-for="item in DASHBOARD_DAY_OPTIONS" :key="item" :label="`近 ${item} 天`" :value="item" />
              </el-select>
            </div>

            <el-upload
              v-model:file-list="fileList"
              class="cost-upload"
              drag
              :auto-upload="false"
              :limit="1"
              :disabled="!canWrite"
              accept=".xlsx,.xlsm,.csv"
              :on-change="onFileChange"
              :on-remove="onFileRemove"
              :on-exceed="onFileExceed"
            >
              <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
              <div class="el-upload__text">把成本表拖到这里，或<em>点击选择文件</em></div>
              <template #tip>
                <div class="el-upload__tip">只接受 .xlsx / .xlsm / .csv；一次一个文件；**预览不落库**，确认导入才写成本库</div>
              </template>
            </el-upload>

            <div class="toolbar import-actions">
              <el-button
                type="primary"
                :icon="View"
                :loading="previewing"
                :disabled="!selectedFile || !canWrite"
                @click="onPreview"
              >
                预览（不落库）
              </el-button>
              <el-button type="success" :icon="CircleCheck" :loading="applying" :disabled="!canConfirmImport" @click="onApply">
                确认导入
              </el-button>
              <span class="toolbar-tip">
                「确认导入」只在预览 <code>ok=true</code>（没有任何非法行）、文件里有成本行、 且参数没变时才可点 ——
                后端同样整批校验：有非法行就整批 422，什么都不写。
              </span>
            </div>

            <!-- 预览结果 -->
            <div v-if="preview" class="preview-panel">
              <div class="card-head">
                <h3 class="card-title">预览结果（未落库）</h3>
                <span class="card-tip">
                  文件 {{ preview.file.name || "--" }} · sha256 {{ shortSha(preview.file.sha256) }} · 解析出
                  {{ formatInt(preview.parsed_rows) }} 行
                </span>
              </div>

              <el-row :gutter="16" class="mb16 metric-row">
                <el-col :xs="12" :sm="6">
                  <div class="card metric-card is-compact">
                    <div class="metric-label">新增</div>
                    <div class="metric-value metric-ok">{{ formatInt(preview.preview.created_count) }}</div>
                  </div>
                </el-col>
                <el-col :xs="12" :sm="6">
                  <div class="card metric-card is-compact">
                    <div class="metric-label">更新</div>
                    <div class="metric-value metric-warn">{{ formatInt(preview.preview.updated_count) }}</div>
                  </div>
                </el-col>
                <el-col :xs="12" :sm="6">
                  <div class="card metric-card is-compact">
                    <div class="metric-label">未变</div>
                    <div class="metric-value">{{ formatInt(preview.preview.unchanged_count) }}</div>
                  </div>
                </el-col>
                <el-col :xs="12" :sm="6">
                  <div class="card metric-card is-compact">
                    <div class="metric-label">非法</div>
                    <div class="metric-value" :class="preview.preview.invalid_count ? 'metric-bad' : 'metric-ok'">
                      {{ formatInt(preview.preview.invalid_count) }}
                    </div>
                  </div>
                </el-col>
              </el-row>

              <!-- 有非法行：红色横幅 + 逐行原因（原样展示，用户照着改文件） -->
              <el-alert v-if="preview.preview.invalid.length" class="mb16" type="error" :closable="false" show-icon>
                <template #title> 文件里有 {{ formatInt(preview.preview.invalid_count) }} 行非法数据，整批不能导入 </template>
                <template #default>
                  <ul class="invalid-list">
                    <li v-for="item in preview.preview.invalid" :key="`${item.line}-${item.reason}`">
                      第 <b>{{ item.line }}</b> 行：{{ item.reason }}
                      <div v-if="item.value" class="inline-extra">原始值：<code>{{ item.value }}</code></div>
                    </li>
                  </ul>
                  <p class="readonly-note">
                    后端的行为是「有非法行就整批 422、什么都不写」（不做部分导入）。 改好文件后重新预览即可。
                  </p>
                </template>
              </el-alert>

              <el-alert v-else-if="!preview.preview.total" class="mb16" type="warning" :closable="false" show-icon>
                <template #title>这份文件里没有成本行（只有表头？），没什么可导入的</template>
                <template #default>
                  <p class="readonly-note">
                    后端解析出 0 行数据，因此「确认导入」不可点 —— 直接提交只会拿到
                    <code>422</code>「文件里没有成本行」。请检查表格是不是空表。
                  </p>
                </template>
              </el-alert>

              <el-alert v-else class="mb16" type="success" :closable="false" show-icon>
                <template #title>预览通过：没有非法行，可以点「确认导入」</template>
                <template #default>
                  <p class="readonly-note">确认后写入的是成本库 <code>{{ preview.book_path }}</code>。</p>
                  <p class="readonly-note">
                    原产品目录（<code><DATA_ROOT>\</code>）不会被改。
                  </p>
                </template>
              </el-alert>

              <div class="card-head">
                <h3 class="card-title">明细（新增 / 更新）</h3>
                <span class="card-tip"> 「更新」给出旧单价 → 新单价；累计超过 50 行时表格只显示前 50 行，计数仍是全量 </span>
              </div>
              <el-table :data="previewRows" stripe show-overflow-tooltip class="cost-table">
                <el-table-column label="变更" min-width="90" align="center">
                  <template #default="{ row }">
                    <el-tag :type="row.kind === 'created' ? 'success' : 'warning'" size="small" effect="light">
                      {{ row.kind === "created" ? "新增" : "更新" }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="seller_sku" label="货号" min-width="200" align="left" />
                <el-table-column label="OZON 数字 SKU" min-width="140" align="left">
                  <template #default="{ row }">{{ row.platform_sku || "--" }}</template>
                </el-table-column>
                <el-table-column label="单价 (¥)" min-width="150" align="right">
                  <template #default="{ row }">
                    <template v-if="row.kind === 'updated'">
                      <span class="old-value">{{ formatMoney(row.old_unit_cost_cny) }}</span>
                      <el-icon class="arrow-icon"><Right /></el-icon>
                      <span class="new-value">{{ formatMoney(row.unit_cost_cny) }}</span>
                    </template>
                    <template v-else>{{ formatMoney(row.unit_cost_cny) }}</template>
                  </template>
                </el-table-column>
                <el-table-column prop="effective_from" label="生效日期" min-width="120" align="center" />
                <el-table-column prop="status" label="状态" min-width="100" align="center" />
                <el-table-column label="备注" min-width="140" align="left">
                  <template #default="{ row }">{{ row.note || "--" }}</template>
                </el-table-column>
                <template #empty>
                  <el-empty description="这份文件没有新增或更新的行（全部未变）" :image-size="96" />
                </template>
              </el-table>

              <!-- 两种策略的影响面：切「成本库权威」前的依据，note 原样展示 -->
              <div class="card-head impact-head">
                <h3 class="card-title">影响面（窗口 {{ preview.impact.window.start }} ~ {{ preview.impact.window.end }}）</h3>
                <span class="card-tip">
                  命中 {{ formatInt(preview.impact.matched_skus) }} 个货号；当前策略：
                  {{ COST_POLICY_LABELS[preview.impact.current_policy] ?? preview.impact.current_policy }}
                </span>
              </div>
              <div class="impact-grid">
                <div
                  v-for="item in impactItems"
                  :key="item.policy"
                  class="impact-item"
                  :class="{ 'is-current': item.policy === preview.impact.current_policy }"
                >
                  <div class="impact-title">
                    {{ item.title }}
                    <el-tag v-if="item.policy === preview.impact.current_policy" size="small" type="primary" effect="plain">
                      当前策略
                    </el-tag>
                  </div>
                  <div class="impact-numbers">
                    <div>受影响订单：<b>{{ formatInt(item.affected_orders) }}</b> 单</div>
                    <div>
                      采购成本合计变化：
                      <b :class="isNegative(item.delta) ? 'metric-bad' : ''">{{ formatMoney(item.delta) }}</b>
                    </div>
                  </div>
                  <p class="impact-note">{{ item.note }}</p>
                </div>
              </div>
            </div>

            <!-- 落库结果 -->
            <el-alert v-if="applyResult" class="mb16 apply-result" type="success" :closable="false" show-icon>
              <template #title>
                导入完成：新增 {{ formatInt(applyResult.applied.created) }} / 更新
                {{ formatInt(applyResult.applied.updated) }} （未变 {{ formatInt(applyResult.applied.unchanged) }}）
              </template>
              <template #default>
                <p class="readonly-note">
                  操作人 {{ applyResult.actor }} · 成本库现有 <strong>{{ formatInt(applyResult.total_after) }}</strong> 条 · 文件
                  {{ applyResult.file.name || "--" }}（sha256 {{ shortSha(applyResult.file.sha256) }}）
                </p>
                <p class="readonly-note">台账已自动刷新；变更留痕也写了一条（同一文件重复导入只会是「未变」）。</p>
              </template>
            </el-alert>
          </div>
        </el-tab-pane>
      </el-tabs>
    </template>

    <!-- 迁移结果：计数 + 跳过原因明细（71 行货号为空也要能给用户看） -->
    <el-dialog v-model="migrateVisible" title="从原产品成本库迁移 —— 结果" width="880px">
      <template v-if="migrateResult">
        <el-descriptions :column="3" border size="small">
          <el-descriptions-item label="新增">{{ formatInt(migrateResult.created) }}</el-descriptions-item>
          <el-descriptions-item label="更新">{{ formatInt(migrateResult.updated) }}</el-descriptions-item>
          <el-descriptions-item label="未变">{{ formatInt(migrateResult.unchanged) }}</el-descriptions-item>
          <el-descriptions-item label="扫描旧库">{{ formatInt(migrateResult.scanned) }}</el-descriptions-item>
          <el-descriptions-item label="跳过">{{ formatInt(migrateResult.skipped) }}</el-descriptions-item>
          <el-descriptions-item label="成本库现有">{{ formatInt(migrateResult.total_after) }} 条</el-descriptions-item>
        </el-descriptions>

        <el-alert class="mt16" type="info" :closable="false" show-icon>
          <template #title>这次迁移对旧库做了什么：什么都没做</template>
          <template #default>
            <p class="readonly-note">
              迁移源（只读）：<code>{{ migrateResult.legacy_path }}</code>
            </p>
            <p class="readonly-note">
              旧库文件 mtime 未变化：<strong>{{ migrateResult.legacy_mtime_unchanged ? "是" : "否" }}</strong>
              （迁移是复制不是改）。
            </p>
            <p class="readonly-note">
              旧库以只读方式打开：<strong>{{ migrateResult.legacy_readonly ? "是" : "否" }}</strong>。
            </p>
            <p class="readonly-note">迁移是幂等的：再点一次的结果会全是「未变」，不会重复写入。</p>
          </template>
        </el-alert>

        <template v-if="migrateResult.skipped_detail.length">
          <div class="card-head mt16">
            <h3 class="card-title">跳过的行（{{ formatInt(migrateResult.skipped) }} 行，逐条给原因）</h3>
            <span class="card-tip">
              旧库里 {{ formatInt(migrateResult.skipped) }} 行没有货号（2026-09-07 的一次脏导入）—— 无法归属到任何
              SKU，既不静默丢弃也不拿数字 SKU 瞎猜
            </span>
          </div>
          <el-table :data="migrateResult.skipped_detail" stripe show-overflow-tooltip max-height="320">
            <el-table-column label="货号" min-width="140" align="left">
              <template #default="{ row }">{{ row.seller_sku || "--" }}</template>
            </el-table-column>
            <el-table-column label="OZON 数字 SKU" min-width="140" align="left">
              <template #default="{ row }">{{ row.platform_sku || "--" }}</template>
            </el-table-column>
            <el-table-column label="旧单价 (¥)" min-width="110" align="right">
              <template #default="{ row }">{{ row.unit_cost_cny || "--" }}</template>
            </el-table-column>
            <el-table-column label="旧记录时间" min-width="150" align="center">
              <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
            </el-table-column>
            <el-table-column prop="reason" label="跳过原因" min-width="240" align="left" />
          </el-table>
          <p v-if="migrateResult.skipped_truncated" class="truncated-tip">
            跳过明细只显示前 50 行（共 {{ formatInt(migrateResult.skipped) }} 行）。
          </p>
        </template>
      </template>

      <!--
        ⚠️ `#footer` 必须是 `el-dialog` 的**直接子节点**。
        它原先被写在 `<template v-if="migrateResult">` 里面 —— 具名插槽套在条件分支里
        会让 @vue/compiler-core 的 codegen 直接抛
        「Codegen node is missing for element/if/for node」，
        整个 `vite build` 挂在这一个文件上（dev 下也会白屏）。
      -->
      <template #footer>
        <el-button type="primary" @click="migrateVisible = false">知道了</el-button>
      </template>
    </el-dialog>

    <!--
      变更留痕（`GET /api/costs/events`）：ADR-0009 规则 5 要的审计视角 ——
      「谁、何时、把哪个货号从 A 改成 B、来源文件是谁」。表只追加、不修改、不删除，
      所以迁移/导入/清理都能在这里追到。
    -->
    <el-drawer v-model="eventsOpen" title="成本变更留痕" size="960px" @open="loadEvents">
      <div class="toolbar">
        <el-input
          v-model="eventsSku"
          class="search-input"
          clearable
          placeholder="按货号精确筛选（留空看全部）"
          @keyup.enter="onEventsSearch"
        />
        <el-button :icon="Search" @click="onEventsSearch">查询</el-button>
        <span class="toolbar-tip"> 共 {{ formatInt(eventsTotal) }} 条 · 只追加不改，改价与清理都会留痕 </span>
      </div>

      <TablePager
        compact
        :page="eventsPage"
        :page-count="eventsPageCount"
        :total="eventsTotal"
        :page-size="eventsPageSize"
        :page-sizes="pageSizeOptions"
        :loading="eventsLoading"
        unit="条"
        @update:page="eventsPage = $event"
        @change="loadEvents"
      />

      <el-table
        v-loading="eventsLoading"
        element-loading-text="正在读取变更留痕…"
        :data="eventsRows"
        stripe
        show-overflow-tooltip
        class="cost-table"
      >
        <el-table-column label="时间" min-width="150" align="center">
          <template #default="{ row }">{{ formatTime(row.occurred_at) }}</template>
        </el-table-column>
        <el-table-column prop="seller_sku" label="货号" min-width="190" align="left" />
        <el-table-column prop="effective_from" label="生效日期" min-width="115" align="center" />
        <el-table-column label="动作" min-width="160" align="left">
          <template #default="{ row }">
            <el-tag :type="COST_ACTION_MAP[row.action]?.tagType ?? 'info'" size="small" effect="light">
              {{ COST_ACTION_MAP[row.action]?.label ?? row.action }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="单价变化 (¥)" min-width="170" align="right">
          <template #default="{ row }">{{ formatCostChange(row) }}</template>
        </el-table-column>
        <el-table-column label="操作人" min-width="110" align="left">
          <template #default="{ row }">{{ row.actor_id || "--" }}</template>
        </el-table-column>
        <el-table-column label="来源" min-width="200" align="left">
          <template #default="{ row }">{{ row.source || "--" }}</template>
        </el-table-column>
        <template #empty>
          <el-empty description="没有匹配的变更记录" :image-size="96" />
        </template>
      </el-table>

      <TablePager
        :page="eventsPage"
        :page-count="eventsPageCount"
        :total="eventsTotal"
        :page-size="eventsPageSize"
        :page-sizes="pageSizeOptions"
        :loading="eventsLoading"
        unit="条"
        @update:page="eventsPage = $event"
        @update:page-size="eventsPageSize = $event"
        @change="loadEvents"
        @size-change="onEventsPageSizeChange"
      />
    </el-drawer>
  </div>
</template>

<script setup lang="ts" name="costManage">
import {
  CircleCheck,
  Clock,
  FolderOpened,
  InfoFilled,
  Refresh,
  Right,
  Search,
  UploadFilled,
  View,
  Wallet,
  Warning
} from "@element-plus/icons-vue";
import dayjs from "dayjs";
import { ElMessage, ElMessageBox } from "element-plus";
import type { UploadFile, UploadUserFile } from "element-plus";
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRouter } from "vue-router";

import { handleUnauthorized, type BackendError } from "@/api/backendRequest";
import type {
  ResCostChangeEvent,
  ResCostImportApply,
  ResCostImportPreview,
  ResCostPreviewRow,
  ResCostRow,
  ResCostMigrate,
  ResMissingCosts
} from "@/api/interfaces/backend";
import { getMeApi } from "@/api/modules/login";
import {
  applyCostImportApi,
  COST_POLICY_LABELS,
  COST_WRITE_ROLES,
  getCostEventsApi,
  getCostListApi,
  getMissingCostsApi,
  migrateLegacyCostsApi,
  previewCostImportApi
} from "@/api/modules/costs";
import { LOGIN_URL } from "@/config";
import { DASHBOARD_DAY_OPTIONS, DEFAULT_DASHBOARD_DAYS, DEFAULT_PAGE_SIZE, PAGE_SIZE_OPTIONS } from "@/config/store";
import { useStoreStore } from "@/stores/modules/store";
import { useUserStore } from "@/stores/modules/user";
import TablePager from "@/views/dashboard/components/TablePager.vue";

/**
 * 成本管理（ADR-0009 的前端部分）
 *
 * 三块内容对应三条不同的数据路径：
 *   1. **台账** —— 我们自己的成本库（`<DATA_ROOT>Platform\cost_book.db`），
 *      服务端分页 + 按货号/数字 SKU 搜索；
 *   2. **缺成本清单** —— 店铺库的订单 × 成本库的单价，回答「窗口内哪些货号没价」。
 *      实测当前 14 天窗口 115/115 全覆盖、缺 0：**空清单是正确结果**，
 *      空态必须这么说，否则用户会去补一个并不缺的价（ADR-0009 §三之三）；
 *   3. **导入 / 迁移** —— 本项目第一批**写操作**。写只落我们的成本库，
 *      生产店铺库与旧产品目录一概不动。
 *
 * ⚠️ 三条纪律（照抄 ADR）：
 *  - 金额一律是字符串，界面只做格式化（千分位 + 2 位小数），**不做任何加减**；
 *  - `null` 是「缺失」不是 0，显示成 `--`；
 *  - 写按钮按角色隐藏，但**隔离点在后端**（operator01 直接调写接口得到 403）。
 */
const router = useRouter();
const storeStore = useStoreStore();
const userStore = useUserStore();

/* ────────────────────────── 通用格式化 ────────────────────────── */
// 金额只格式化不计算：后端下发字符串就是为了避开 JS 浮点误差，前端不许把它变成数再相加
const moneyFormatter = new Intl.NumberFormat("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** 后端金额字符串 → 显示用数字；缺失返回 null（`null` 表示缺失，不是 0） */
const toNumber = (value: unknown): number | null => {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
};

/** ¥ 金额：千分位 + 2 位小数；缺失显示 `--` */
const formatMoney = (value: unknown) => {
  const num = toNumber(value);
  return num === null ? "--" : `¥${moneyFormatter.format(num)}`;
};

/** 计数：千分位（纯展示，不参与计算） */
const formatInt = (value: unknown) => {
  const num = toNumber(value);
  return num === null ? "--" : String(Math.trunc(num)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
};

/** 是否负数：只用于着色 */
const isNegative = (value: unknown) => {
  const num = toNumber(value);
  return num !== null && num < 0;
};

/** 北京时间 ISO 串 → 分钟级（成本表里的时间只用于展示） */
const formatTime = (value: string | null | undefined) => (value ? dayjs(value).format("YYYY-MM-DD HH:mm") : "--");

/** sha256 很长，只显示前 12 位（完整值没必要占屏幕；它是「这批是哪次导入」的指纹） */
const shortSha = (value: string | null | undefined) => (value ? `${value.slice(0, 12)}…` : "--");

/** 成本状态：只有 confirmed 参与核算 */
const COST_STATUS_MAP: Record<string, { label: string; tagType: "success" | "warning" | "info" }> = {
  confirmed: { label: "已确认", tagType: "success" },
  pending: { label: "待确认", tagType: "warning" }
};

/* ────────────────────────── 角色与写权限 ────────────────────────── */
/**
 * 角色**不在** pinia 的 `userInfo` 里（登录页只存了 token），
 * 所以「能不能写」必须现查 `GET /api/auth/me` 的 `user.role`。
 *
 * 拿不到角色时**不允许写**：前端少给一个入口的代价只是少点一下，
 * 而给错了入口会让人以为自己有权限（隔离本来也不靠这里）。
 */
const role = ref("");
const roleLoaded = ref(false);
const canWrite = computed(() => (COST_WRITE_ROLES as readonly string[]).includes(role.value));

const loadRole = async () => {
  const cached = (userStore.userInfo as unknown as { role?: string } | undefined)?.role;
  if (cached) {
    role.value = cached;
    roleLoaded.value = true;
    return;
  }
  try {
    const me = await getMeApi();
    role.value = me.user?.role ?? "";
    roleLoaded.value = true;
  } catch (error) {
    const e = error as BackendError;
    if (e?.status === 401) {
      handleUnauthorized();
      return;
    }
    // 读不到角色不算页面故障：只影响写入口的显隐，读路径照常
    role.value = "";
    roleLoaded.value = true;
    ElMessage.warning(`读不到当前账号角色（${e?.message ?? "未知错误"}），已隐藏写入口`);
  }
};

/* ────────────────────────── 顶部：台账与错误态 ────────────────────────── */
const activeTab = ref("ledger");

const bookPath = ref("");
const ledgerTotal = ref(0);
const changeEventTotal = ref(0);
const lastChange = ref<{ seller_sku: string; action: string; occurred_at: string } | null>(null);
const ledgerRows = ref<ResCostRow[]>([]);
const ledgerLoading = ref(false);
/** 首次成功拿到台账（用于区分「正在读」与「真的 0 条」） */
const ledgerLoaded = ref(false);
const loadError = ref<BackendError | null>(null);

/** 台账服务端分页状态 */
const page = ref(1);
const pageSize = ref(DEFAULT_PAGE_SIZE);
const pageSizeOptions = computed(() => (storeStore.pageSizeOptions.length ? storeStore.pageSizeOptions : [...PAGE_SIZE_OPTIONS]));
const pageCount = computed(() => Math.max(1, Math.ceil(ledgerTotal.value / pageSize.value)));

/** 搜索框（400ms 防抖）与「实际生效的关键词」分开存：后者用于文案与空态，避免输入中就闪 */
const keyword = ref("");
const appliedKeyword = ref("");
let searchTimer: number | undefined;

const loadLedger = async () => {
  ledgerLoading.value = true;
  try {
    const data = await getCostListApi({
      keyword: keyword.value,
      limit: pageSize.value,
      offset: (page.value - 1) * pageSize.value
    });
    bookPath.value = data.book_path;
    ledgerTotal.value = data.total;
    changeEventTotal.value = data.change_event_total;
    lastChange.value = data.last_change;
    ledgerRows.value = data.rows;
    appliedKeyword.value = (data.keyword ?? "").trim();
    ledgerLoaded.value = true;
    loadError.value = null;
    if (data.note) {
      // 成本库还不存在时后端下发 total=0 + note，这不是错误但必须说出来
      ElMessage.warning(data.note);
    }
  } catch (error) {
    const e = error as BackendError;
    ledgerRows.value = [];
    loadError.value = e;
    if (e?.status === 401) {
      handleUnauthorized();
      ElMessage.error("登录已失效，请重新登录");
      return;
    }
    ElMessage.error(e?.message ?? "成本台账加载失败");
  } finally {
    ledgerLoading.value = false;
  }
};

const onPageSizeChange = () => {
  page.value = 1;
  loadLedger();
};

/**
 * 输入关键词：**400ms 防抖**后才发请求。
 * 每敲一个字就查会打满后端，而且页码会在用户还没输完时乱跳。
 * 同时把页码重置到第 1 页 —— 换了关键字还停在第 7 页会看到一片空白。
 */
watch(keyword, () => {
  window.clearTimeout(searchTimer);
  searchTimer = window.setTimeout(() => {
    page.value = 1;
    loadLedger();
  }, 400);
});

const reloadAll = async () => {
  await loadLedger();
  // 缺成本清单是按店铺算的，只有用户已经打开过那个页签才跟着刷新（避免无谓的重算）
  if (missingLoaded.value) await loadMissing();
};

const goLogin = () => router.replace(LOGIN_URL);

const errorTitle = computed(() => {
  const status = loadError.value?.status;
  if (status === 401) return "登录已失效";
  if (status === 403) return "无权查看成本台账";
  if (status === 503) return "成本库不存在";
  if (status !== undefined) return `成本台账加载失败（HTTP ${status}）`;
  return "无法连接后端服务";
});
const errorDesc = computed(() => {
  const message = loadError.value?.message ?? "未知错误";
  const status = loadError.value?.status;
  if (status === 503) {
    return `${message}。成本库路径由服务端 OZON_COST_BOOK 配置决定（当前后端用的是 <DATA_ROOT>Platform\\cost_book.db），前端不能指定路径。`;
  }
  if (status === 403) {
    return `${message}。成本台账对所有已登录账号可读 —— 这里出现 403 说明服务端授权配置有问题。`;
  }
  return message;
});

/* ────────────────────────── 缺成本清单 ────────────────────────── */
const missingAlias = ref("");
const missingDays = ref<number>(DEFAULT_DASHBOARD_DAYS);
const missingData = ref<ResMissingCosts | null>(null);
const missingLoading = ref(false);
const missingLoaded = ref(false);

/** 下拉里的店铺：直接复用 pinia 里的可见店铺（后端已按授权过滤） */
const storeOptions = computed(() => storeStore.stores);

const loadMissing = async () => {
  if (!missingAlias.value) {
    // 一个可看的店都没有：这是授权结果，不是加载失败
    missingData.value = null;
    return;
  }
  missingLoading.value = true;
  try {
    missingData.value = await getMissingCostsApi(missingAlias.value, missingDays.value, 200);
    missingLoaded.value = true;
  } catch (error) {
    const e = error as BackendError;
    missingData.value = null;
    missingLoaded.value = false;
    if (e?.status === 401) {
      handleUnauthorized();
      return;
    }
    ElMessage.error(e?.message ?? "缺成本清单加载失败");
  } finally {
    missingLoading.value = false;
  }
};

/* ────────────────────────── 导入：文件与预览 ────────────────────────── */
const importAlias = ref("");
const importDays = ref<number>(DEFAULT_DASHBOARD_DAYS);
const fileList = ref<UploadUserFile[]>([]);
const selectedFile = ref<File | null>(null);
const preview = ref<ResCostImportPreview | null>(null);
const previewing = ref(false);
const applying = ref(false);
const applyResult = ref<ResCostImportApply | null>(null);

/**
 * 预览时的参数指纹（别名|天数|文件名|大小|最后修改时间）。
 * 为什么需要它：「确认导入」用的必须是**刚预览过的那份文件 + 那套参数**，
 * 否则用户改了天数或换了文件再点确认，落库结果会和屏幕上的影响面对不上。
 */
const previewKey = ref("");
const currentKey = computed(() =>
  [
    importAlias.value,
    importDays.value,
    selectedFile.value?.name ?? "",
    selectedFile.value?.size ?? 0,
    selectedFile.value?.lastModified ?? 0
  ].join("|")
);
/**
 * 能点「确认导入」的三个条件：
 *  1. 预览过、且预览没有任何非法行（`ok`）；
 *  2. 参数与文件没变（指纹一致）；
 *  3. **文件里真的有成本行**（`total > 0`）。
 *
 * 第 3 条是前端自己加的护栏：只有表头的文件后端预览会返回 `ok=true`（0 行也算
 * 「没有非法行」），要等 apply 才 422「文件里没有成本行」——
 * 那等于让用户白点一次。这里依据预览响应里的真实行数提前挡住，并说明原因。
 */
const canConfirmImport = computed(
  () =>
    !!preview.value &&
    preview.value.preview.ok &&
    preview.value.preview.total > 0 &&
    previewKey.value === currentKey.value &&
    canWrite.value
);

const invalidatePreview = (reason: string) => {
  if (!preview.value && !applyResult.value) return;
  preview.value = null;
  previewKey.value = "";
  applyResult.value = null;
  ElMessage.info(`${reason}，请重新预览`);
};

const onFileChange = (file: UploadFile) => {
  selectedFile.value = (file.raw as File) ?? null;
  invalidatePreview("已更换文件");
};

const onFileRemove = () => {
  selectedFile.value = null;
  invalidatePreview("已移除文件");
};

/** limit=1：再选第二个文件时 Element Plus 不会加进来，这里只负责说明为什么没反应 */
const onFileExceed = () => {
  ElMessage.warning("一次只能导入一个文件，请先移除已选文件");
};

const onImportInputChange = () => invalidatePreview("已改导入参数（店铺 / 天数）");

const onPreview = async () => {
  if (!selectedFile.value) return;
  if (!importAlias.value) {
    ElMessage.warning("请先选择店铺（影响面要按店铺的订单算）");
    return;
  }
  previewing.value = true;
  applyResult.value = null;
  try {
    const data = await previewCostImportApi(importAlias.value, importDays.value, selectedFile.value);
    preview.value = data;
    previewKey.value = currentKey.value;
    if (data.preview.ok) {
      ElMessage.success(
        `预览完成：新增 ${data.preview.created_count} / 更新 ${data.preview.updated_count} / 未变 ${data.preview.unchanged_count}`
      );
    } else {
      ElMessage.warning(`预览发现 ${data.preview.invalid_count} 行非法数据，整批不能导入（未落库）`);
    }
  } catch (error) {
    const e = error as BackendError;
    preview.value = null;
    previewKey.value = "";
    if (e?.status === 401) {
      handleUnauthorized();
      return;
    }
    // 422 的 detail（第几行、什么原因）是给用户改文件的，原样展示
    ElMessage.error(e?.message ?? "预览失败");
  } finally {
    previewing.value = false;
  }
};

const onApply = async () => {
  if (!selectedFile.value || !canConfirmImport.value) return;
  applying.value = true;
  try {
    const data = await applyCostImportApi(importAlias.value, importDays.value, selectedFile.value);
    applyResult.value = data;
    preview.value = null;
    previewKey.value = "";
    ElMessage.success(`导入完成：新增 ${data.applied.created} / 更新 ${data.applied.updated}`);
    // 台账数字变了：回到第 1 页重新取，保证顶部总数与表格是同一时刻的数
    page.value = 1;
    await loadLedger();
    if (missingLoaded.value) await loadMissing();
    if (eventsOpen.value) await loadEvents();
  } catch (error) {
    const e = error as BackendError;
    if (e?.status === 401) {
      handleUnauthorized();
      return;
    }
    // 有非法行时后端整批 422 且什么都不写，detail 里写明第几行什么原因
    ElMessage.error(e?.message ?? "导入失败");
  } finally {
    applying.value = false;
  }
};

/** 预览明细表：新增在前、更新在后（更新行带旧单价，用于「旧值 → 新值」） */
const previewRows = computed(() => {
  if (!preview.value) return [] as (ResCostPreviewRow & { kind: "created" | "updated" })[];
  return [
    ...preview.value.preview.created.map(row => ({ ...row, kind: "created" as const })),
    ...preview.value.preview.updated.map(row => ({ ...row, kind: "updated" as const }))
  ];
});

/** 影响面两块：两种策略都展示（ADR-0009 §四，切权威前要有据可依） */
const impactItems = computed(() => {
  const impact = preview.value?.impact;
  if (!impact) return [];
  return [
    {
      policy: "book_first",
      title: "库优先（book_first）",
      affected_orders: impact.book_first.affected_orders,
      delta: impact.book_first.delta_purchase_cost_cny,
      note: impact.book_first.note
    },
    {
      policy: "book_authoritative",
      title: "成本库为权威（book_authoritative）",
      affected_orders: impact.book_authoritative.affected_orders,
      delta: impact.book_authoritative.delta_purchase_cost_cny,
      note: impact.book_authoritative.note
    }
  ];
});

/* ────────────────────────── 迁移（幂等，只读旧库） ────────────────────────── */
const migrating = ref(false);
const migrateVisible = ref(false);
const migrateResult = ref<ResCostMigrate | null>(null);

const onMigrateLegacy = async () => {
  try {
    await ElMessageBox.confirm(
      [
        "把原产品成本库（<DATA_ROOT>\\data\\desktop\\purchase_costs.db）里的成本搬到我们的成本库（<DATA_ROOT>Platform\\cost_book.db）。",
        "· 幂等：可以重复点，第二次的结果全是「未变」；",
        "· 旧库只读打开、迁移是复制 —— 不会修改原产品的任何文件；",
        "· 货号为空的旧记录无法归属，会被跳过并逐条给出原因（实测 71 行）。"
      ].join("\n"),
      "从原产品成本库迁移",
      { type: "info", confirmButtonText: "开始迁移", cancelButtonText: "取消" }
    );
  } catch {
    return; // 用户取消：什么都不做
  }

  migrating.value = true;
  try {
    const data = await migrateLegacyCostsApi();
    migrateResult.value = data;
    migrateVisible.value = true;
    ElMessage.success(`迁移完成：新增 ${data.created} / 更新 ${data.updated} / 未变 ${data.unchanged} / 跳过 ${data.skipped}`);
    page.value = 1;
    await loadLedger();
    if (missingLoaded.value) await loadMissing();
    if (eventsOpen.value) await loadEvents();
  } catch (error) {
    const e = error as BackendError;
    if (e?.status === 401) {
      handleUnauthorized();
      return;
    }
    ElMessage.error(e?.message ?? "迁移失败");
  } finally {
    migrating.value = false;
  }
};

/* ────────────────────────── 变更留痕（抽屉） ────────────────────────── */
const eventsOpen = ref(false);
const eventsRows = ref<ResCostChangeEvent[]>([]);
const eventsTotal = ref(0);
const eventsLoading = ref(false);
const eventsSku = ref("");
const eventsPage = ref(1);
const eventsPageSize = ref(DEFAULT_PAGE_SIZE);
const eventsPageCount = computed(() => Math.max(1, Math.ceil(eventsTotal.value / eventsPageSize.value)));

/** 变更动作的中文标签：枚举值来自 `core/repository/cost_book.py` 的写入路径 */
const COST_ACTION_MAP: Record<string, { label: string; tagType: "success" | "warning" | "danger" | "info" }> = {
  insert: { label: "新增", tagType: "success" },
  update: { label: "改价", tagType: "warning" },
  migrated_insert: { label: "迁移新增", tagType: "success" },
  migrated_update: { label: "迁移改价", tagType: "warning" },
  purge: { label: "清理", tagType: "danger" }
};

/** 从留痕的 JSON 里取单价：解析不了就返回 null（**不猜**，也不当 0） */
const costOfJson = (raw: string | null): string | null => {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as { unit_cost_cny?: unknown };
    return typeof parsed?.unit_cost_cny === "string" ? parsed.unit_cost_cny : null;
  } catch {
    return null;
  }
};

/** 单价变化：新增只给新值，改价给「旧 → 新」，删掉给旧值 */
const formatCostChange = (row: ResCostChangeEvent) => {
  const before = costOfJson(row.old_json);
  const after = costOfJson(row.new_json);
  if (before && after && before !== after) return `${formatMoney(before)} → ${formatMoney(after)}`;
  if (after) return formatMoney(after);
  if (before) return `${formatMoney(before)}（已删除）`;
  return "--";
};

const loadEvents = async () => {
  eventsLoading.value = true;
  try {
    const data = await getCostEventsApi({
      seller_sku: eventsSku.value.trim() || undefined,
      limit: eventsPageSize.value,
      offset: (eventsPage.value - 1) * eventsPageSize.value
    });
    eventsRows.value = data.rows;
    eventsTotal.value = data.total;
  } catch (error) {
    const e = error as BackendError;
    eventsRows.value = [];
    eventsTotal.value = 0;
    if (e?.status === 401) {
      handleUnauthorized();
      return;
    }
    ElMessage.error(e?.message ?? "变更留痕加载失败");
  } finally {
    eventsLoading.value = false;
  }
};

const openEvents = () => {
  // 台账里正在搜某个货号时，把关键字带进留痕（多数场景就是「这个货号怎么变的」）
  eventsSku.value = appliedKeyword.value && appliedKeyword.value === keyword.value.trim() ? appliedKeyword.value : "";
  eventsPage.value = 1;
  eventsOpen.value = true;
};

const onEventsSearch = () => {
  eventsPage.value = 1;
  loadEvents();
};

const onEventsPageSizeChange = () => {
  eventsPage.value = 1;
  loadEvents();
};

/* ────────────────────────── 生命周期 ────────────────────────── */
const onTabChange = (name: string | number) => {
  // 页签第一次被打开时才去取数：缺成本清单要跑一遍订单窗口比对，不该由首屏买单
  if (name === "missing" && !missingLoaded.value && !missingLoading.value) loadMissing();
};

const openImportTab = () => {
  activeTab.value = "import";
};

/** 首屏：店铺列表（alias 选择用）→ 台账 → 角色。顺序与页面上的依赖一致 */
const bootstrap = async () => {
  loadError.value = null;
  await storeStore.load();
  // 默认选中第一个可用店铺（别名由后端下发，前端不写死）
  const fallback = storeStore.defaultAliases()[0] ?? "";
  if (!missingAlias.value) missingAlias.value = storeStore.resolveAliases([fallback])[0] ?? fallback;
  if (!importAlias.value) importAlias.value = missingAlias.value;
  await loadLedger();
  await loadRole();
  /**
   * 缺成本清单在首屏也取一次（不等到点开页签才取）。
   *
   * 为什么：`el-tabs` 默认把所有页签内容都渲染出来，页签还没点开时
   * 那三个数字会显示成「--」、表格显示空态 —— 看起来像「查不到」，
   * 而这份清单恰恰是**最容易被误读成故障**的那块（当前 14 天窗口缺 0）。
   * 代价是多一次窗口比对查询，比让用户先看到「--」再自己猜要值。
   */
  if (!missingLoaded.value) await loadMissing();
};

onMounted(async () => {
  await bootstrap();
});

onBeforeUnmount(() => {
  window.clearTimeout(searchTimer);
});
</script>

<style scoped lang="scss">
.cost-page {
  --board-gap: 16px;
  --board-radius: 8px;

  .card {
    padding: 16px;
    background-color: var(--el-bg-color-overlay);
    border: 1px solid var(--el-border-color-lighter);
    border-radius: var(--board-radius);
  }
}

.mb16 {
  margin-bottom: 16px;
}

.mt16 {
  margin-top: 16px;
}

/* 顶部信息条 */
.page-head {
  display: flex;
  flex-wrap: wrap;
  gap: var(--board-gap);
  align-items: center;
  justify-content: space-between;

  .head-title {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    align-items: center;
    font-size: 18px;
    font-weight: 700;
    color: var(--el-text-color-primary);
  }

  .head-icon {
    margin-right: 8px;
    font-size: 20px;
    color: var(--el-color-primary);
  }

  .count-tag {
    margin-left: 8px;
    font-weight: 400;
  }

  .head-meta {
    margin-top: 8px;
    font-size: 13px;
    color: var(--el-text-color-secondary);
  }

  .meta-item {
    display: inline-flex;
    align-items: center;
    gap: 4px;

    code {
      padding: 2px 6px;
      background-color: var(--el-fill-color-light);
      border-radius: 4px;
    }
  }

  .head-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }
}

.cost-tabs {
  :deep(.el-tabs__header) {
    margin-bottom: 16px;
  }
}

/* 顶部那个「变更留痕 · 查看」是可以点的，鼠标要给出提示 */
.clickable-tag {
  cursor: pointer;
}

.readonly-note {
  margin: 0 0 4px;
  line-height: 1.7;

  &:last-child {
    margin-bottom: 0;
  }
}

.card-head {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: 12px;

  .card-title {
    margin: 0;
    font-size: 15px;
    font-weight: 600;
  }

  .card-tip {
    font-size: 12px;
    color: var(--el-text-color-secondary);
  }
}

/* 工具条：搜索 / 店铺 / 天数 / 刷新都在这里，保持一条横向节奏 */
.toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 12px;

  .search-input {
    width: 360px;
  }

  .store-select {
    width: 240px;
  }

  .days-select {
    width: 120px;
  }

  .toolbar-label {
    font-size: 13px;
    color: var(--el-text-color-secondary);
  }

  .toolbar-tip {
    font-size: 12px;
    color: var(--el-text-color-secondary);

    code {
      padding: 2px 6px;
      background-color: var(--el-fill-color-light);
      border-radius: 4px;
    }
  }
}

.import-actions {
  margin-top: 12px;
}

/* 明细表：数字用等宽数字，金额右对齐时不会跳来跳去 */
.cost-table {
  font-variant-numeric: tabular-nums;
}

/* 缺成本清单的指标卡 */
.metric-row {
  :deep(.el-col) {
    margin-bottom: 16px;
  }
}

.metric-card {
  height: 100%;

  &.is-compact {
    padding: 12px;
  }

  .metric-label {
    font-size: 13px;
    color: var(--el-text-color-secondary);
  }

  .metric-value {
    margin: 6px 0;
    font-size: 26px;
    font-weight: 700;
    line-height: 1.2;
    color: var(--el-text-color-primary);
    font-variant-numeric: tabular-nums;
  }

  .metric-foot {
    font-size: 12px;
    color: var(--el-text-color-placeholder);
  }
}

.metric-ok {
  color: var(--el-color-success);
}

.metric-warn {
  color: var(--el-color-warning);
}

.metric-bad {
  color: var(--el-color-danger);
}

/* 缺成本清单为空：这是**正确结果**，所以用成功的语气 + 明确口径，而不是「暂无数据」 */
.missing-empty {
  max-width: 720px;
  padding: 24px 8px;
  margin: 0 auto;
  text-align: left;

  .missing-empty-icon {
    font-size: 36px;
    color: var(--el-color-success);
  }

  .missing-empty-title {
    margin: 8px 0;
    font-size: 15px;
    line-height: 1.7;
    color: var(--el-text-color-primary);
  }

  .missing-empty-hint {
    margin: 0;
    font-size: 12px;
    line-height: 1.8;
    color: var(--el-text-color-secondary);

    code {
      padding: 2px 6px;
      background-color: var(--el-fill-color-light);
      border-radius: 4px;
    }
  }
}

.truncated-tip {
  margin: 8px 0 0;
  font-size: 12px;
  color: var(--el-color-warning);
}

/* 上传区：把 Element Plus 默认的窄框放宽一点，跟卡片同宽 */
.cost-upload {
  :deep(.el-upload),
  :deep(.el-upload-dragger) {
    width: 100%;
  }
}

/* 预览结果面板：与上方表单之间加一条分隔，视觉上属于「结果」 */
.preview-panel {
  padding-top: 16px;
  margin-top: 16px;
  border-top: 1px solid var(--el-border-color-lighter);
}

.invalid-list {
  padding-left: 18px;
  margin: 0 0 8px;

  li {
    line-height: 1.8;
  }

  code {
    padding: 2px 6px;
    background-color: var(--el-fill-color-light);
    border-radius: 4px;
  }

  /* 「原始值：xxx」跟在行号原因后面，用 inline 保持一行读完 */
  .inline-extra {
    display: inline;
    margin-left: 8px;
    color: var(--el-text-color-secondary);
  }
}

/* 旧值 → 新值：旧值弱化，新值加重，箭头居中 */
.old-value {
  color: var(--el-text-color-secondary);
  text-decoration: line-through;
}

.new-value {
  font-weight: 600;
  color: var(--el-color-primary);
}

.arrow-icon {
  margin: 0 4px;
  color: var(--el-text-color-placeholder);
}

/* 影响面：两种策略并排，当前生效的那个加边框提示 */
.impact-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 12px;
}

.impact-head {
  margin-top: 20px;
}

.impact-item {
  padding: 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: var(--board-radius);

  &.is-current {
    border-color: var(--el-color-primary-light-5);
    background-color: var(--el-color-primary-light-9);
  }

  .impact-title {
    display: flex;
    gap: 8px;
    align-items: center;
    font-size: 14px;
    font-weight: 600;
  }

  .impact-numbers {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 16px;
    margin: 8px 0;
    font-size: 13px;
    font-variant-numeric: tabular-nums;
  }

  .impact-note {
    margin: 0;
    font-size: 12px;
    line-height: 1.7;
    color: var(--el-text-color-secondary);
  }
}

.apply-result {
  margin-top: 16px;
}

/* 错误面板：与看板/店铺页保持一致（不白屏、给出下一步） */
.error-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
  align-items: center;
  padding: 48px 24px;
  text-align: center;

  .error-icon {
    font-size: 40px;
    color: var(--el-color-danger);
  }

  .error-title {
    margin: 0;
    font-size: 18px;
  }

  .error-desc {
    max-width: 720px;
    margin: 0;
    line-height: 1.6;
    color: var(--el-text-color-secondary);
  }

  .error-actions {
    display: flex;
    gap: 8px;
  }

  .error-tip {
    margin: 0;
    font-size: 12px;
    color: var(--el-text-color-placeholder);

    code {
      padding: 2px 6px;
      background-color: var(--el-fill-color-light);
      border-radius: 4px;
    }
  }
}

@media (max-width: 768px) {
  .toolbar {
    .search-input,
    .store-select {
      width: 100%;
    }
  }
}
</style>
