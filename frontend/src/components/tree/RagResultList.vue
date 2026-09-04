<script setup lang="ts">
/** 侧栏语义检索结果列表:score 徽标(2 位)+ 事件编号 + 桩位 + 起始时间。
 * 点击跳详情路由,与文件树视频行同口径(params stem + query rel + currentRel 高亮);
 * 行结构复用 tree.css 的 video-item/video-meta/video-name/video-sub。 */
import { useRouter } from 'vue-router'
import type { RagResult } from '../../api/rag'
import { fmtTs, stemOfVideoPath } from '../../api/rag'
import { useWorkspaceStore } from '../../stores/workspace'

defineProps<{ results: RagResult[] }>()

const router = useRouter()
const ws = useWorkspaceStore()

function onOpen(r: RagResult) {
  ws.currentRel = r.video_path // 行高亮 + presence viewing(同 TreeNode onSelect)
  router.push({
    name: 'detail',
    params: { stem: stemOfVideoPath(r.video_path) },
    query: { rel: r.video_path },
  })
}
</script>

<template>
  <!-- key 带 start_ts:同一视频可因不同时段多次命中 -->
  <div
    v-for="r in results"
    :key="r.video_path + ':' + r.start_ts"
    class="video-item rag-item"
    :class="{ active: ws.currentRel === r.video_path }"
    role="button"
    tabindex="0"
    @click="onOpen(r)"
    @keydown.enter.prevent="onOpen(r)"
  >
    <span class="rag-score" :title="'相关度 ' + r.score.toFixed(2)">{{ r.score.toFixed(2) }}</span>
    <div class="video-meta">
      <div class="video-name file-name" :title="r.video_path">
        {{ stemOfVideoPath(r.video_path) }}
      </div>
      <div class="video-sub">事件 {{ r.events.join('、') }} · {{ r.site }}</div>
      <div class="video-sub">{{ fmtTs(r.start_ts) }}</div>
    </div>
  </div>
</template>

<style scoped>
.rag-item {
  align-items: flex-start; /* 两行 sub 信息时徽标与首行对齐 */
  padding-top: 8px;
}

/* score 徽标:造型对齐 tree.css .badge(全局类不便复用——它带状态色语义) */
.rag-score {
  flex: 0 0 auto;
  font-size: 12px;
  font-weight: 600;
  padding: 2px 7px;
  border-radius: 999px;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
  background: var(--color-accent-soft);
  color: var(--color-accent);
}
</style>
