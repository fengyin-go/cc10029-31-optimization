<template>
  <section class="page" data-module="archive">
    <header class="page-head">
      <div>
        <h2>历史归档管理</h2>
        <p class="page-desc">管理采集历史数据的归档周期、格式和检索策略。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记归档策略</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出历史归档清单</button>
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
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
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
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无历史归档数据，可先登记归档策略</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记归档策略</h3>
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

    <section class="manifest-panel" data-testid="manifest-panel">
      <header class="page-head">
        <div>
          <h3>归档批次清单</h3>
          <p class="page-desc">读取分区计算快照生成的归档批次清单，清单区段与原落地记录一一对应。</p>
        </div>
      </header>
      <form class="filter-bar" @submit.prevent="loadManifest">
        <label class="filter-item">
          <span>配置编号</span>
          <input v-model="manifestConfig" data-testid="manifest-filter" placeholder="按配置编号检索，留空查全部" />
        </label>
        <button class="btn" data-testid="manifest-query" type="submit">查询清单</button>
      </form>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in manifestColumns" :key="column">{{ column }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in manifestRows" :key="String(row['清单编号'])" data-testid="manifest-row">
            <td v-for="column in manifestColumns" :key="column">{{ row[column] ?? '—' }}</td>
          </tr>
          <tr v-if="!manifestRows.length">
            <td :colspan="manifestColumns.length" class="empty-state">暂无归档批次清单，可先在存储落地提交分区计算快照</td>
          </tr>
        </tbody>
      </table>
      <footer class="page-foot">
        <span>共 {{ manifestTotal }} 行归档批次清单</span>
        <span v-if="manifestError" class="error-text">{{ manifestError }}</span>
      </footer>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条历史归档记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/archive'
const columns = ["策略编号", "数据来源", "归档周期", "归档格式", "保留年限", "压缩级别", "索引规则", "清理策略"]
const actions = ["提交校验", "启用配置", "停用配置"]
const statuses = ["草稿", "待校验", "已启用", "已停用"]
const metricNames = ["归档策略总数", "待校验归档策略", "已启用归档策略"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const manifestColumns = ["清单编号", "配置编号", "快照编号", "批次开始", "批次结束", "区段", "保存天数", "配置来源", "校验结论", "配置冲突"]
const manifestRows = ref<Row[]>([])
const manifestTotal = ref(0)
const manifestConfig = ref('')
const manifestError = ref('')
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

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
    link.download = 'archive.csv'
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
      throw new Error('历史归档动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '历史归档操作失败'
  }
}

async function loadManifest() {
  manifestError.value = ''
  const params = new URLSearchParams()
  if (manifestConfig.value.trim()) params.set('配置编号', manifestConfig.value.trim())
  try {
    const response = await request(`${ENDPOINT}/manifest?${params.toString()}`)
    if (!response.ok) throw new Error('归档批次清单读取失败')
    const payload = await response.json()
    manifestRows.value = payload.items ?? []
    manifestTotal.value = payload.total ?? manifestRows.value.length
  } catch (error) {
    manifestError.value = error instanceof Error ? error.message : '归档批次清单读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('归档策略列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '历史归档列表读取失败'
  }
}

onMounted(() => {
  void reload()
  void loadManifest()
})
</script>

<style scoped>
.manifest-panel { margin-top: 20px; border-top: 1px solid var(--border); padding-top: 4px; }
.manifest-panel h3 { margin: 0; }
</style>

