<template>
  <section class="page" data-module="partitioncheck">
    <header class="page-head">
      <div>
        <h2>分区检测管理</h2>
        <p class="page-desc">管理数据表分区的完整性检测规则，自动发现和补齐缺失分区。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记检测规则</button>
        <button class="btn" type="button" @click="exportRows">导出分区检测清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <div class="notice-row">
      <span class="pill">日期键 {{ compatibility.date_key || 'dt' }}</span>
      <span class="pill">分区字段 {{ compatibility.partition_field || 'dt' }}</span>
      <span v-for="(count, mode) in compatibility.compat_counts" :key="mode" class="pill">
        {{ mode }} {{ count }}
      </span>
      <span v-if="compatibility.pending_rules.length" class="pill warn">
        待确认规则 {{ compatibility.pending_rules.length }} 条
      </span>
      <span v-if="compatibility.conflict_rules.length" class="pill warn">
        口径冲突 {{ compatibility.conflict_rules.length }} 条
      </span>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ formatCell(row, column) }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无分区检测数据，可先登记检测规则</td>
        </tr>
      </tbody>
    </table>

    <section class="detail-block">
      <h3>缺失分区与完整性结论</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>规则编号</th>
            <th>来源规则编号</th>
            <th>规则来源</th>
            <th>兼容标记</th>
            <th>缺失分区数</th>
            <th>缺失分区清单</th>
            <th>分区完整性结论</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in conclusions" :key="item['规则编号']">
            <td>{{ item['规则编号'] }}</td>
            <td>{{ item['来源规则编号'] }}</td>
            <td>{{ item['规则来源'] }}</td>
            <td>{{ item['兼容标记'] }}</td>
            <td>{{ item['缺失分区数'] }}</td>
            <td>{{ (item['缺失分区清单'] || []).map((entry) => entry['分区值']).join('、') || '—' }}</td>
            <td>{{ item['分区完整性结论'] }}</td>
          </tr>
          <tr v-if="!conclusions.length">
            <td colspan="7" class="empty-state">暂无检测结论</td>
          </tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条分区检测记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { fetchJson, request } from '@/api/client'

type Row = Record<string, string | number | boolean | string[] | null>

type Compatibility = {
  date_key: string
  partition_field: string
  compat_counts: Record<string, number>
  pending_rules: string[]
  conflict_rules: string[]
  source: string
  total: number
}

type MissingPartition = Record<string, string>

type Conclusion = {
  规则编号: string
  来源规则编号: string
  规则来源: string
  兼容标记: string
  缺失分区数: number
  缺失分区清单: MissingPartition[]
  分区完整性结论: string
}

const ENDPOINT = '/api/partitioncheck'
const columns = [
  '规则编号',
  '数据表名',
  '期望分区数',
  '日期键',
  '分区字段',
  '兼容标记',
  '缺失分区数',
  '分区完整性结论',
]
const actions = ['提交规则评审', '启用规则', '暂停规则']
const statuses = ['草稿', '规则评审中', '已启用', '已暂停']

const rows = ref<Row[]>([])
const conclusions = ref<Conclusion[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 2)
const compatibility = ref<Compatibility>({
  date_key: 'dt',
  partition_field: 'dt',
  compat_counts: {},
  pending_rules: [],
  conflict_rules: [],
  source: '',
  total: 0,
})
const stats = ref([
  { label: '检测规则总数', value: 0 },
  { label: '已启用规则', value: 0 },
  { label: '待确认规则', value: 0 },
])

function formatCell(row: Row, column: string): string | number {
  if (column === '缺失分区数') {
    return Number(row['缺失分区数'] ?? 0)
  }
  const value = row[column]
  if (value === undefined || value === null || value === '') {
    return '—'
  }
  return Array.isArray(value) ? value.join('、') : String(value)
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '检测规则登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('分区检测动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分区检测操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('检测规则列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value = [
      { label: '检测规则总数', value: total.value },
      { label: '已启用规则', value: rows.value.filter((row) => row.status === '已启用').length },
      { label: '待确认规则', value: rows.value.filter((row) => row['兼容标记'] === '待确认').length },
    ]
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分区检测列表读取失败'
  }
  await Promise.all([reloadCompatibility(), reloadConclusions()])
}

async function reloadCompatibility() {
  try {
    compatibility.value = await fetchJson<Compatibility>(`${ENDPOINT}/compatibility`)
  } catch {
    // 兼容视图失败不影响列表展示。
  }
}

async function reloadConclusions() {
  try {
    const payload = await fetchJson<{ items: Conclusion[] }>(`${ENDPOINT}/conclusions`)
    conclusions.value = payload.items ?? []
  } catch {
    conclusions.value = []
  }
}

onMounted(reload)
</script>
