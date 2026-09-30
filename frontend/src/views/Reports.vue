<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import { unifyStatusLabel } from '../viewHints'

const trips = ref<any[]>([])
const events = ref<any[]>([])
const reports = ref<any[]>([])
const stops = ref<string[]>([])
const stopName = ref('')
const loading = ref(false)
const previewing = ref(false)
const errorMsg = ref('')
// 当前结果区展示的是「试算结果（未落库）」还是「已保存报告」
const viewing = ref<{ kind: 'preview' | 'saved'; id?: number } | null>(null)

const savedCount = computed(() => reports.value.length)

function scopeQuery() {
  return stopName.value ? `&stop_name=${encodeURIComponent(stopName.value)}` : ''
}

async function loadReports() {
  reports.value = await api('/reports')
}

// 试算：只返回事件，不写入报告、不刷新历史条数
async function preview() {
  previewing.value = true
  errorMsg.value = ''
  try {
    const res = await api(`/reports/preview?line_id=1${scopeQuery()}`, { method: 'POST' })
    events.value = res.events || []
    viewing.value = { kind: 'preview' }
  } catch (e: any) {
    errorMsg.value = `试算失败：${e?.message || e}`
  } finally { previewing.value = false }
}

// 真检：成功后恰好新增一条已保存报告，再刷新历史；失败如实提示，不伪装成试算
async function run() {
  loading.value = true
  errorMsg.value = ''
  try {
    const res = await api(`/reports/run?line_id=1${scopeQuery()}`, { method: 'POST' })
    if (!res?.saved || res.id == null) {
      // 后端没落成行却回了成功，视为失败而非空转
      throw new Error('服务端未生成报告行')
    }
    events.value = res.events || []
    viewing.value = { kind: 'saved', id: res.id }
    await loadReports()
  } catch (e: any) {
    errorMsg.value = `重新检测失败：${e?.message || e}（已有报告未受影响）`
  } finally { loading.value = false }
}

function viewSaved(r: any) {
  events.value = r.events || []
  viewing.value = { kind: 'saved', id: r.id }
  errorMsg.value = ''
}

onMounted(async () => {
  trips.value = await api('/trips')
  const arrivals = await api('/arrivals?line_id=1')
  const seen = new Map<number, string>()
  for (const a of arrivals) if (!seen.has(a.stop_seq)) seen.set(a.stop_seq, a.stop_name)
  stops.value = [...seen.entries()].sort((x, y) => x[0] - y[0]).map(([, n]) => n)
  await loadReports()
  await preview()
})

function stripClass(s: string) {
  return s === 'bunching' ? 'bg-bunch' : s === 'large_gap' ? 'bg-large' : ''
}
function label(s: string) {
  return unifyStatusLabel(s)
}
function fmtTime(iso: string) {
  return (iso || '').replace('T', ' ').slice(0, 19)
}
</script>
<template>
  <h1>串车报告</h1>
  <p class="sub">按实际到站间隔对照计划发车间隔 · 试算只看事件不落库，「重新检测」才生成报告行</p>
  <div class="bg-report-bar">
    <label class="bg-scope">
      范围
      <select v-model="stopName" class="bg-select">
        <option value="">全线</option>
        <option v-for="s in stops" :key="s" :value="s">{{ s }}</option>
      </select>
    </label>
    <button class="btn" :disabled="previewing" @click="preview">试算</button>
    <button class="btn" :disabled="loading" @click="run">重新检测</button>
    <span class="muted">已保存报告 {{ savedCount }} 条（试算不改变此数）</span>
  </div>
  <p v-if="errorMsg" class="bg-view-tag bg-view-error">{{ errorMsg }}</p>
  <p v-if="viewing?.kind === 'preview'" class="bg-view-tag bg-view-preview">
    当前结果：试算块（未落库 · 不占报告行 · 不产生时间轴点）
  </p>
  <p v-else-if="viewing?.kind === 'saved'" class="bg-view-tag bg-view-saved">
    当前结果：已保存报告 #{{ viewing.id }}
  </p>
  <div class="bg-split" style="margin-top:1rem">
    <aside class="bg-trip-col">
      <h2>关联班次</h2>
      <div v-for="r in trips" :key="r.id ?? r.trip_no" class="bg-trip-row">
        <div>
          <div>{{ r.trip_no }}</div>
          <div class="bg-trip-meta">{{ r.vehicle_no }}</div>
        </div>
        <div class="bg-trip-meta">{{ r.planned_depart }}</div>
      </div>
    </aside>
    <div class="bg-strip-col" :class="{ 'bg-strip-preview': viewing?.kind === 'preview' }">
      <article
        v-for="(e, i) in events"
        :key="i"
        class="bg-gap-strip"
        :class="stripClass(e.status)"
      >
        <header>{{ e.stop_name }}</header>
        <div class="bg-gap-body">
          <div class="bg-gap-val">{{ e.gap_min }}′</div>
          <div>计划 {{ e.planned_headway_min }}′</div>
          <div>{{ e.earlier_trip }} → {{ e.later_trip }}</div>
          <span class="badge" :class="e.status === 'bunching' ? 'badge-bad' : e.status === 'large_gap' ? 'badge-warn' : 'badge-ok'">
            {{ label(e.status) }}
          </span>
        </div>
      </article>
    </div>
  </div>
  <section class="card" style="margin-top:1rem">
    <h2 class="bg-history-title">历史报告（{{ savedCount }} 条，仅真检写入）</h2>
    <table v-if="reports.length">
      <thead>
        <tr><th>ID</th><th>范围</th><th>生成时间</th><th>事件数</th><th></th></tr>
      </thead>
      <tbody>
        <tr v-for="r in reports" :key="r.id">
          <td>#{{ r.id }}</td>
          <td>{{ r.stop_name === '*' ? '全线' : r.stop_name }}</td>
          <td>{{ fmtTime(r.created_at) }}</td>
          <td>{{ r.events.length }}</td>
          <td><button class="btn btn-ghost" @click="viewSaved(r)">查看</button></td>
        </tr>
      </tbody>
    </table>
    <p v-else class="muted">暂无已保存报告，点击「重新检测」生成第一条。</p>
  </section>
</template>
