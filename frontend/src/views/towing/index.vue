<template>
  <section class="page" data-module="towing">
    <header class="page-head">
      <div>
        <h2>航空器牵引管理</h2>
        <p class="page-desc">按牵引车号生效时段与起终点机位把关：时段冲突、起终点相同、起点维护封闭都不允许安排；可按时段批量顺延并单列冲突。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记牵引任务</button>
        <button class="btn" type="button" @click="exportRows">导出航空器牵引清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <label class="filter-item">
        <span>牵引状态</span>
        <select v-model="filters.status">
          <option value="">全部</option>
          <option v-for="status in statuses" :key="status" :value="status">{{ status }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <section class="postpone-bar">
      <div class="postpone-title">批量顺延</div>
      <form class="postpone-form" @submit.prevent="submitPostpone">
        <label class="filter-item">
          <span>生效时段起</span>
          <input v-model="postpone.start" type="datetime-local" />
        </label>
        <label class="filter-item">
          <span>生效时段止</span>
          <input v-model="postpone.end" type="datetime-local" />
        </label>
        <label class="filter-item">
          <span>顺延分钟（可负）</span>
          <input v-model.number="postpone.deltaMinutes" type="number" step="10" style="width: 120px" />
        </label>
        <button class="btn primary" type="submit" :disabled="postponeLoading">
          {{ postponeLoading ? '顺延处理中…' : '按时段批量顺延' }}
        </button>
      </form>
      <p class="postpone-hint">仅顺延时段完整落在区间内的「待牵引、牵引中」任务；冲突任务原样保留并在下方单独列出。</p>
    </section>

    <div v-if="postponeResult" class="postpone-result">
      <p :class="postponeResult.conflicts.length ? 'result-warn' : 'result-ok'">{{ postponeResult.message }}</p>
      <ul v-if="postponeResult.postponed.length" class="result-list">
        <li v-for="item in postponeResult.postponed" :key="`ok-${item.id}`">
          ✅ {{ item.牵引编号 }}（{{ item.牵引车号 }}）{{ item.起点机位 }} → {{ item.终点机位 }}，
          新时段 {{ item.生效开始 }} ~ {{ item.生效结束 }}
        </li>
      </ul>
      <ul v-if="postponeResult.conflicts.length" class="result-list result-conflict">
        <li v-for="item in postponeResult.conflicts" :key="`bad-${item.id}`">
          ⚠️ {{ item.牵引编号 }}（{{ item.牵引车号 }}）{{ item.起点机位 }} → {{ item.终点机位 }}，
          拟顺延至 {{ item.顺延开始 }} ~ {{ item.顺延结束 }}，原时段保持 {{ item.生效开始 }} ~ {{ item.生效结束 }}：{{ item.原因 }}
        </li>
      </ul>
    </div>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ displayValue(row, column) }}</td>
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
          <td :colspan="columns.length + 1" class="empty-state">暂无航空器牵引数据，可先登记牵引任务</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条航空器牵引记录<template v-if="statsRange"> · 生效时段覆盖 {{ statsRange.start }} ~ {{ statsRange.end }}</template></span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

type PostponeItem = {
  id: number
  牵引编号: string
  牵引车号: string
  起点机位: string
  终点机位: string
  生效开始: string
  生效结束: string
}

type ConflictItem = PostponeItem & {
  顺延开始: string
  顺延结束: string
  原因: string
}

type PostponeResult = {
  message: string
  postponed: PostponeItem[]
  conflicts: ConflictItem[]
}

const ENDPOINT = '/api/towing'
const columns = ['牵引编号', '关联航班', '牵引车号', '起点机位', '终点机位', '牵引人员', '生效开始', '生效结束', '完成时刻', '牵引状态']
const actions = ['安排牵引', '确认完成', '取消任务']
const statuses = ['待牵引', '牵引中', '已完成', '已取消']
const defaultStats = [
  { label: '待牵引任务', value: 0 },
  { label: '牵引中任务', value: 0 },
  { label: '本月牵引次数', value: 0 },
  { label: '取消任务数', value: 0 },
]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const stats = ref(defaultStats.map((item) => ({ ...item })))
const statsRange = ref<{ start: string; end: string } | null>(null)
const postponeLoading = ref(false)
const postponeResult = ref<PostponeResult | null>(null)
const postpone = reactive({
  start: '2026-09-28T07:30',
  end: '2026-09-28T12:00',
  deltaMinutes: 60,
})

function displayValue(row: Row, column: string): string | number | null {
  if (column === '牵引状态') {
    return String(row.status ?? '—')
  }
  const value = row[column]
  return value === null || value === undefined || value === '' ? '—' : value
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '牵引任务登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  postponeResult.value = null
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json().catch(() => null)
    if (!response.ok || !payload?.ok) {
      throw new Error(payload?.message || payload?.detail || '航空器牵引动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '航空器牵引操作失败'
  }
}

function toDateTimeValue(value: string): string {
  // 后端返回 'YYYY-MM-DD HH:MM'，datetime-local 需要 'YYYY-MM-DDTHH:MM'
  return value.replace(' ', 'T')
}

async function submitPostpone() {
  errorMessage.value = ''
  postponeResult.value = null
  if (!postpone.start || !postpone.end || !Number.isInteger(postpone.deltaMinutes)) {
    errorMessage.value = '请完整填写生效时段起止与整数顺延分钟数'
    return
  }
  postponeLoading.value = true
  try {
    const response = await request(`${ENDPOINT}/postpone`, {
      method: 'POST',
      body: JSON.stringify({
        start: toDateTimeValue(postpone.start),
        end: toDateTimeValue(postpone.end),
        delta_minutes: postpone.deltaMinutes,
      }),
    })
    const payload = await response.json().catch(() => null)
    if (!response.ok) {
      throw new Error(payload?.detail || '批量顺延未生效，请稍后重试')
    }
    postponeResult.value = {
      message: payload.message,
      postponed: payload.postponed ?? [],
      conflicts: payload.conflicts ?? [],
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批量顺延失败'
  } finally {
    postponeLoading.value = false
  }
}

async function loadStats() {
  try {
    const response = await request(`${ENDPOINT}/statistics`)
    if (!response.ok) {
      throw new Error('统计口径读取失败')
    }
    const payload = await response.json()
    stats.value = defaultStats.map((item) => ({ ...item, value: Number(payload[item.label] ?? 0) }))
    statsRange.value = payload.时段最早开始
      ? { start: payload.时段最早开始, end: payload.时段最晚结束 }
      : null
  } catch {
    // 统计读取失败时保留上一次的卡片，不打断列表操作
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('牵引任务列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    await loadStats()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '航空器牵引列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.page-actions { display: flex; gap: 8px; }
.postpone-bar {
  background: #fff;
  border: 1px solid var(--border);
  border-left: 3px solid var(--brand);
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 12px;
}
.postpone-title { font-size: 13px; font-weight: 600; margin-bottom: 8px; }
.postpone-form { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; }
.postpone-hint { margin: 8px 0 0; font-size: 12px; color: var(--muted); }
.postpone-result {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 12px;
  margin-bottom: 12px;
}
.result-ok { color: #027a48; margin: 4px 0; font-size: 13px; }
.result-warn { color: #b54708; margin: 4px 0; font-size: 13px; }
.result-list { margin: 4px 0 0; padding-left: 18px; font-size: 12px; color: #334155; }
.result-list li { margin: 2px 0; }
.result-conflict { color: #b42318; }
</style>
