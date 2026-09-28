<template>
  <section class="page" data-module="towing">
    <header class="page-head">
      <div>
        <h2>航空器牵引管理</h2>
        <p class="page-desc">维护牵引任务，按牵引车号、生效时段与起终点机位把关安排，并支持按生效时段批量顺延。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记牵引任务</button>
        <button class="btn" type="button" @click="openPostpone">批量顺延</button>
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
          <td v-for="column in columns" :key="column">{{ row[column] || '—' }}</td>
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
      <span>共 {{ total }} 条航空器牵引记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <div v-if="schedule.open" class="modal-mask" @click.self="schedule.open = false">
      <div class="modal">
        <h3>安排牵引 · {{ schedule.form['牵引编号'] }}</h3>
        <p class="modal-tip">牵引车号相同且生效时段重叠、起点机位维护封闭、起终点机位相同的安排会被拦下。</p>
        <label class="modal-field">
          <span>牵引车号</span>
          <input v-model="schedule.form['牵引车号']" placeholder="如 TC-01" />
        </label>
        <label class="modal-field">
          <span>起点机位</span>
          <input v-model="schedule.form['起点机位']" placeholder="如 STAN-0001" />
        </label>
        <label class="modal-field">
          <span>终点机位</span>
          <input v-model="schedule.form['终点机位']" placeholder="如 STAN-0002" />
        </label>
        <label class="modal-field">
          <span>生效开始</span>
          <input v-model="schedule.form['生效开始']" type="datetime-local" />
        </label>
        <label class="modal-field">
          <span>生效结束</span>
          <input v-model="schedule.form['生效结束']" type="datetime-local" />
        </label>
        <p v-if="schedule.error" class="error-text">{{ schedule.error }}</p>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="schedule.open = false">取消</button>
          <button class="btn primary" type="button" :disabled="schedule.saving" @click="submitSchedule">
            {{ schedule.saving ? '提交中…' : '确认安排' }}
          </button>
        </div>
      </div>
    </div>

    <div v-if="postpone.open" class="modal-mask" @click.self="postpone.open = false">
      <div class="modal">
        <h3>按生效时段批量顺延</h3>
        <p class="modal-tip">生效时段范围内的待牵引、牵引中任务统一后移；冲突的任务会单列原因，其余照常顺延。</p>
        <label class="modal-field">
          <span>范围开始</span>
          <input v-model="postpone.form.range_start" type="datetime-local" />
        </label>
        <label class="modal-field">
          <span>范围结束</span>
          <input v-model="postpone.form.range_end" type="datetime-local" />
        </label>
        <label class="modal-field">
          <span>顺延分钟数</span>
          <input v-model.number="postpone.form.delta_minutes" type="number" min="1" step="1" placeholder="如 60" />
        </label>

        <div v-if="postpone.result" class="postpone-result">
          <p>{{ postpone.result.message }}</p>
          <p v-if="postpone.result.postponed.length" class="result-ok">
            已顺延：<span v-for="item in postpone.result.postponed" :key="Number(item.id)">{{ item['牵引编号'] }}（{{ item['生效时段'] }}） </span>
          </p>
          <div v-if="postpone.result.conflicts.length" class="result-conflict">
            <p>以下 {{ postpone.result.conflicts.length }} 条冲突，未顺延：</p>
            <ul>
              <li v-for="item in postpone.result.conflicts" :key="item.id">
                <strong>{{ item['牵引编号'] }}</strong>：{{ item.reason }}
              </li>
            </ul>
          </div>
        </div>
        <p v-else-if="postpone.error" class="error-text">{{ postpone.error }}</p>

        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="postpone.open = false">{{ postpone.result ? '关闭' : '取消' }}</button>
          <button v-if="!postpone.result" class="btn primary" type="button" :disabled="postpone.saving" @click="submitPostpone">
            {{ postpone.saving ? '顺延中…' : '执行顺延' }}
          </button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type Stat = { label: string; value: number }

const ENDPOINT = '/api/towing'
const columns = ["牵引编号", "关联航班", "牵引车号", "起点机位", "终点机位", "生效时段", "牵引人员", "完成时刻", "牵引状态"]
const actions = ["安排牵引", "确认完成", "取消任务"]
const statuses = ["待牵引", "牵引中", "已完成", "已取消"]

const rows = ref<Row[]>([])
const total = ref(0)
const stats = ref<Stat[]>([
  { label: "待牵引任务", value: 0 },
  { label: "本月牵引次数", value: 0 },
  { label: "取消任务数", value: 0 },
])
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const schedule = reactive({
  open: false,
  saving: false,
  error: '',
  id: 0,
  form: {
    '牵引编号': '',
    '牵引车号': '',
    '起点机位': '',
    '终点机位': '',
    '生效开始': '',
    '生效结束': '',
  },
})

const postpone = reactive<{
  open: boolean
  saving: boolean
  error: string
  result: null | { message: string; postponed: Row[]; conflicts: Array<{ id: number; '牵引编号': string; reason: string }> }
  form: { range_start: string; range_end: string; delta_minutes: number | null }
}>({
  open: false,
  saving: false,
  error: '',
  result: null,
  form: { range_start: '', range_end: '', delta_minutes: 60 },
})

function toDatetimeLocal(value: unknown): string {
  const text = String(value ?? '').trim()
  // 后端展示形如 "2026-09-28 08:00"，datetime-local 需要 "2026-09-28T08:00"
  return text ? text.replace(' ', 'T').slice(0, 16) : ''
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

function openSchedule(row: Row) {
  schedule.id = Number(row.id)
  schedule.error = ''
  schedule.form = {
    '牵引编号': String(row['牵引编号'] ?? ''),
    '牵引车号': String(row['牵引车号'] ?? ''),
    '起点机位': String(row['起点机位'] ?? ''),
    '终点机位': String(row['终点机位'] ?? ''),
    '生效开始': toDatetimeLocal(row['生效开始']),
    '生效结束': toDatetimeLocal(row['生效结束']),
  }
  schedule.open = true
}

async function submitSchedule() {
  schedule.saving = true
  schedule.error = ''
  try {
    const response = await request(`${ENDPOINT}/${schedule.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({
        values: {
          action: '安排牵引',
          '牵引车号': schedule.form['牵引车号'],
          '起点机位': schedule.form['起点机位'],
          '终点机位': schedule.form['终点机位'],
          '生效开始': schedule.form['生效开始'],
          '生效结束': schedule.form['生效结束'],
        },
      }),
    })
    const payload = await response.json().catch(() => null)
    if (!response.ok || !payload?.ok) {
      schedule.error = payload?.message || '航空器牵引安排未生效，请稍后重试'
      return
    }
    schedule.open = false
    await reload()
  } catch (error) {
    schedule.error = error instanceof Error ? error.message : '航空器牵引操作失败'
  } finally {
    schedule.saving = false
  }
}

function openPostpone() {
  postpone.open = true
  postpone.error = ''
  postpone.result = null
  postpone.form = { range_start: '', range_end: '', delta_minutes: 60 }
}

async function submitPostpone() {
  postpone.saving = true
  postpone.error = ''
  try {
    const response = await request(`${ENDPOINT}/postpone`, {
      method: 'POST',
      body: JSON.stringify(postpone.form),
    })
    const payload = await response.json().catch(() => null)
    if (!response.ok) {
      postpone.error = payload?.detail || '批量顺延未生效，请稍后重试'
      return
    }
    postpone.result = {
      message: payload.message,
      postponed: payload.postponed ?? [],
      conflicts: payload.conflicts ?? [],
    }
    await reload()
  } catch (error) {
    postpone.error = error instanceof Error ? error.message : '批量顺延失败'
  } finally {
    postpone.saving = false
  }
}

async function runAction(action: string, row: Row) {
  if (action === '安排牵引') {
    openSchedule(row)
    return
  }
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const payload = await response.json().catch(() => null)
    if (!response.ok || !payload?.ok) {
      throw new Error(payload?.message || '航空器牵引动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '航空器牵引操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const [listResponse, statsResponse] = await Promise.all([
      request(`${ENDPOINT}?${query}`),
      request(`${ENDPOINT}/stats`),
    ])
    if (!listResponse.ok) {
      throw new Error('牵引任务列表读取失败')
    }
    const payload = await listResponse.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    if (statsResponse.ok) {
      const statsPayload = await statsResponse.json()
      stats.value = statsPayload.items ?? stats.value
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '航空器牵引列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 20;
}
.modal {
  width: 460px;
  max-height: 82vh;
  overflow-y: auto;
  background: #fff;
  border-radius: 10px;
  padding: 18px 20px;
}
.modal h3 { margin: 0 0 6px; font-size: 16px; }
.modal-tip { color: var(--muted); font-size: 12px; margin: 0 0 12px; }
.modal-field { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.modal-field span { width: 72px; font-size: 13px; color: var(--muted); flex-shrink: 0; }
.modal-field input { flex: 1; border: 1px solid var(--border); border-radius: 6px; padding: 6px 8px; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 14px; }
.postpone-result { border-top: 1px dashed var(--border); padding-top: 10px; font-size: 13px; }
.postpone-result p { margin: 6px 0; }
.result-ok { color: #157347; }
.result-conflict { color: #b42318; }
.result-conflict ul { margin: 4px 0 0; padding-left: 18px; }
.btn:disabled { opacity: 0.6; cursor: not-allowed; }
</style>
