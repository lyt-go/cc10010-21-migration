<template>
  <section class="page" data-module="metastore">
    <header class="page-head">
      <div>
        <h2>元数据存储管理</h2>
        <p class="page-desc">维护ETL涉及的元数据信息，包括表结构、分层归属、分区字段和分区完整性。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记元数据表</button>
        <button class="btn" type="button" @click="exportRows">导出元数据存储清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <div class="notice-row">
      <span class="pill">日期键 {{ dateKey }}</span>
      <span class="pill">分区字段 {{ partitionField }}</span>
      <span class="pill" :class="{ warn: !consistency.ok }">
        {{ consistency.ok ? '与分区检测口径一致' : '口径待核对' }}
      </span>
      <span v-for="table in consistency.missing_in_metastore" :key="table" class="pill warn">
        未登记表 {{ table }}
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
          <td :colspan="columns.length + 1" class="empty-state">暂无元数据存储数据，可先登记元数据表</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条元数据存储记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { fetchJson, request } from '@/api/client'

type Row = Record<string, string | number | boolean | string[] | null>

type Consistency = {
  ok: boolean
  date_key: string
  partition_field: string
  mismatches: Record<string, string>[]
  missing_in_metastore: string[]
}

const ENDPOINT = '/api/metastore'
const columns = [
  '表编号',
  '表名',
  '所属数仓层',
  '日期键',
  '分区字段',
  '缺失分区数',
  '分区完整性结论',
  '最近更新时间',
]
const actions = ['提交元数据评审', '确认元数据', '标记过期']
const statuses = ['草稿', '元数据评审中', '已确认', '已过期']

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 2)
const dateKey = ref('dt')
const partitionField = ref('dt')
const consistency = ref<Consistency>({
  ok: true,
  date_key: 'dt',
  partition_field: 'dt',
  mismatches: [],
  missing_in_metastore: [],
})
const stats = ref([
  { label: '元数据表总数', value: 0 },
  { label: '缺失分区表', value: 0 },
  { label: '评审中表', value: 0 },
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
  errorMessage.value = '元数据表登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('元数据存储动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '元数据存储操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('元数据表列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value = [
      { label: '元数据表总数', value: total.value },
      {
        label: '缺失分区表',
        value: rows.value.filter((row) => row['分区完整性结论'] === '缺失').length,
      },
      { label: '评审中表', value: rows.value.filter((row) => row.status === '元数据评审中').length },
    ]
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '元数据存储列表读取失败'
  }
  await reloadConsistency()
}

async function reloadConsistency() {
  try {
    consistency.value = await fetchJson<Consistency>(`${ENDPOINT}/consistency`)
    dateKey.value = consistency.value.date_key || 'dt'
    partitionField.value = consistency.value.partition_field || 'dt'
  } catch {
    // 自检失败时保留上次口径，不影响列表。
  }
}

onMounted(reload)
</script>
