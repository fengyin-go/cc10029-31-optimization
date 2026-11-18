<template>
  <section class="page" data-module="storage">
    <header class="page-head">
      <div>
        <h2>存储落地管理</h2>
        <p class="page-desc">管理采集结果持久化的存储类型、分区和生命周期。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记存储配置</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出存储落地清单</button>
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
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>分区快照</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="partition-cell" :data-testid="`partition-cell-${row.id}`">
            <template v-if="row['分区快照编号']">
              <span class="partition-snap">{{ row['分区快照编号'] }}</span>
              <span class="partition-line">{{ row['分区口径'] }}</span>
            </template>
            <span v-else class="partition-line muted">未提交快照</span>
          </td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" :data-testid="`partition-${row.id}`" @click="openPartition(row)">分区快照</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无存储落地数据，可先登记存储配置</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记存储配置</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="partitionRow" class="modal-backdrop" data-testid="partition-modal">
      <div class="modal partition-panel">
        <h3>分区计算快照 · {{ partitionRow['配置编号'] }}</h3>
        <p class="page-desc">
          当前配置：{{ partitionRow['分区策略'] || '按天' }} · 保存{{ partitionRow['保存天数'] || '30' }}天 ·
          配置版本 v{{ partitionRow['配置版本'] ?? 1 }}；列表、导出、归档读取同一份已提交快照。
        </p>

        <form class="partition-form" @submit.prevent="submitSnapshot">
          <label>
            <span>批次开始</span>
            <input v-model="snapshotForm.start" type="date" data-testid="snapshot-start" required />
          </label>
          <label>
            <span>批次结束</span>
            <input v-model="snapshotForm.end" type="date" data-testid="snapshot-end" required />
          </label>
          <label>
            <span>版本模式</span>
            <select v-model="snapshotForm.mode" data-testid="snapshot-mode">
              <option>冻结版</option>
              <option>最新版</option>
            </select>
          </label>
          <button class="btn primary" data-testid="snapshot-submit" type="submit" :disabled="submittingSnapshot">
            {{ submittingSnapshot ? '提交中…' : '提交分区快照' }}
          </button>
          <button class="btn" data-testid="snapshot-compare" type="button" @click="loadCompare">对比三入口口径</button>
        </form>
        <p v-if="partitionMessage" class="partition-message" data-testid="partition-message">{{ partitionMessage }}</p>

        <h4>已提交快照</h4>
        <table class="data-table">
          <thead>
            <tr><th>快照编号</th><th>版本模式</th><th>配置来源</th><th>批次边界</th><th>区段数</th><th>校验结论</th></tr>
          </thead>
          <tbody>
            <tr v-for="snap in snapshots" :key="String(snap['快照编号'])" data-testid="snapshot-row">
              <td>{{ snap['快照编号'] }}</td>
              <td>{{ snap['版本模式'] }}</td>
              <td>{{ snap['配置来源'] }}</td>
              <td>{{ snap['批次开始'] }} ~ {{ snap['批次结束'] }}</td>
              <td>{{ snap['区段']?.length ?? 0 }}</td>
              <td>{{ snap['校验结论']?.['结论'] }}</td>
            </tr>
            <tr v-if="!snapshots.length">
              <td colspan="6" class="empty-state">尚未提交分区计算快照</td>
            </tr>
          </tbody>
        </table>

        <div v-if="compareResult" data-testid="partition-compare">
          <h4>三入口口径对比（{{ compareResult['批次开始'] }} ~ {{ compareResult['批次结束'] }}）</h4>
          <p>
            列表 / 导出 / 归档读取同一快照：
            <strong :class="compareResult['口径一致'] ? 'ok-text' : 'warn-text'">
              {{ compareResult['口径一致'] ? '一致' : '不一致' }}
            </strong>
          </p>
          <table class="data-table">
            <thead>
              <tr><th>入口</th><th>分区快照编号</th><th>分区口径</th></tr>
            </thead>
            <tbody>
              <tr v-for="entry in compareEntries" :key="entry.name">
                <td>{{ entry.name }}</td>
                <td>{{ entry['分区快照编号'] || '—' }}</td>
                <td>{{ entry['分区口径'] || '—' }}<template v-if="entry.name === '归档'">（清单 {{ entry['清单行数'] }} 行）</template></td>
              </tr>
            </tbody>
          </table>

          <h4>血缘对比（冻结版 vs 最新版）</h4>
          <table class="data-table">
            <thead>
              <tr><th>版本模式</th><th>配置来源</th><th>分区策略</th><th>保存天数</th><th>本批次区段数</th></tr>
            </thead>
            <tbody>
              <tr v-for="lineage in compareLineages" :key="lineage.mode">
                <td>{{ lineage.mode }}</td>
                <td>{{ lineage['配置来源'] }}</td>
                <td>{{ lineage['分区策略'] }}</td>
                <td>{{ lineage['保存天数'] }}</td>
                <td>{{ lineage['区段数'] }}</td>
              </tr>
            </tbody>
          </table>

          <h4>历史区段保存天数核对</h4>
          <p>
            已落地区段保存天数保持不变：
            <strong :class="compareResult['历史区段保存天数核对']['未变'] ? 'ok-text' : 'warn-text'">
              {{ compareResult['历史区段保存天数核对']['未变'] ? '是' : '否' }}
            </strong>
            （共 {{ compareResult['历史区段保存天数核对']['区段'].length }} 个历史区段）
          </p>
          <p v-if="compareResult['配置冲突']['存在']" class="warn-text">{{ compareResult['配置冲突']['说明'] }}</p>
          <p v-else class="muted">{{ compareResult['配置冲突']['说明'] }}</p>
        </div>

        <div class="page-actions">
          <button class="btn" type="button" data-testid="partition-close" @click="partitionRow = null">关闭</button>
        </div>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条存储落地记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type SnapshotRow = Record<string, any>
type CompareResult = Record<string, any>

const ENDPOINT = '/api/storage'
const columns = ["配置编号", "存储类型", "连接信息", "分区策略", "压缩格式", "保存天数", "备份副本", "清理策略"]
const actions = ["提交校验", "启用配置", "停用配置"]
const statuses = ["草稿", "待校验", "已启用", "已停用"]
const metricNames = ["存储配置总数", "待校验存储配置", "已启用存储配置"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const partitionRow = ref<Row | null>(null)
const snapshots = ref<SnapshotRow[]>([])
const snapshotForm = ref({ start: '', end: '', mode: '冻结版' })
const submittingSnapshot = ref(false)
const partitionMessage = ref('')
const compareResult = ref<CompareResult | null>(null)
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])
const compareEntries = computed<Record<string, any>[]>(() => {
  const entries = compareResult.value?.['入口口径'] ?? {}
  return Object.keys(entries).map((name) => ({ name, ...(entries[name] ?? {}) }))
})
const compareLineages = computed<Record<string, any>[]>(() => {
  const lineage = compareResult.value?.['血缘对比'] ?? {}
  return Object.keys(lineage).map((mode) => ({ mode, ...(lineage[mode] ?? {}) }))
})

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'storage.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('存储落地动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '存储落地操作失败'
  }
}

function openPartition(row: Row) {
  partitionRow.value = row
  partitionMessage.value = ''
  compareResult.value = null
  snapshotForm.value = { start: '', end: '', mode: '冻结版' }
  void loadSnapshots()
}

async function loadSnapshots() {
  const id = partitionRow.value?.id
  if (!id) return
  try {
    const response = await request(`${ENDPOINT}/${id}/partition-snapshots`)
    if (!response.ok) throw new Error('分区快照读取失败')
    const payload = await response.json()
    snapshots.value = payload.items ?? []
  } catch (error) {
    partitionMessage.value = error instanceof Error ? error.message : '分区快照读取失败'
  }
}

async function submitSnapshot() {
  const id = partitionRow.value?.id
  if (!id) return
  submittingSnapshot.value = true
  partitionMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${id}/partition-snapshots`, {
      method: 'POST',
      body: JSON.stringify({
        批次开始: snapshotForm.value.start,
        批次结束: snapshotForm.value.end,
        版本模式: snapshotForm.value.mode,
      }),
    })
    const payload = await response.json()
    partitionMessage.value = payload.message || (payload.ok ? '分区计算快照已写入' : '提交失败')
    if (payload.ok) {
      await loadSnapshots()
      await reload()
    }
  } catch (error) {
    partitionMessage.value = error instanceof Error ? error.message : '分区快照提交失败'
  } finally {
    submittingSnapshot.value = false
  }
}

async function loadCompare() {
  const code = partitionRow.value?.['配置编号']
  if (!code) return
  partitionMessage.value = ''
  try {
    const params = new URLSearchParams({ 配置编号: String(code) })
    if (snapshotForm.value.start) params.set('批次开始', snapshotForm.value.start)
    if (snapshotForm.value.end) params.set('批次结束', snapshotForm.value.end)
    const response = await request(`${ENDPOINT}/partition-compare?${params.toString()}`)
    if (!response.ok) throw new Error('分区口径对比读取失败')
    compareResult.value = await response.json()
  } catch (error) {
    partitionMessage.value = error instanceof Error ? error.message : '分区口径对比读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('存储配置列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '存储落地列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.partition-panel { width: min(880px, 100%); }
.partition-panel h4 { margin: 14px 0 6px; font-size: 13px; }
.partition-form { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; margin: 10px 0; }
.partition-form label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.partition-form input, .partition-form select { padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; }
.partition-cell { max-width: 180px; }
.partition-snap { display: block; font-weight: 600; }
.partition-line { display: block; font-size: 12px; color: var(--muted); }
.partition-message { font-size: 13px; color: var(--brand); }
.muted { color: var(--muted); }
.ok-text { color: #15803d; }
.warn-text { color: #b45309; }
</style>
