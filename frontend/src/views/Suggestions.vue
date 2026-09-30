<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../api'
const tips = ref<any[]>([])
const reportId = ref<number | null>(null)
const loaded = ref(false)
onMounted(async () => {
  // 建议只取自最近一条【已落库】报告；试算结果不会出现在这里
  const res = await api('/reports/suggestions?line_id=1')
  tips.value = res.suggestions || []
  reportId.value = res.report_id ?? null
  loaded.value = true
})
</script>
<template>
  <h1>建议</h1>
  <p class="sub">
    <template v-if="reportId != null">来自最近一次已落库检测（报告 #{{ reportId }}），与报告、时间轴基于同一套事件</template>
    <template v-else-if="loaded">暂无已落库报告，请先在「串车报告」页执行「重新检测」；试算结果不会在此显示</template>
  </p>
  <div class="card" v-for="(t,i) in tips" :key="i">
    <div><strong>{{ t.stop_name }}</strong> · {{ t.earlier_trip }} → {{ t.later_trip }} · 间隔 {{ t.gap_min }} 分</div>
    <p class="muted">{{ t.suggestion }}</p>
  </div>
  <p v-if="loaded && !tips.length && reportId != null" class="muted">最近一条已落库报告中暂无异常建议</p>
</template>
