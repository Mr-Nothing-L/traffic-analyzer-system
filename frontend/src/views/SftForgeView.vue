<script setup lang="ts">
/** SFT 生成面板(右上角独立入口,工作台主区视图):
 * 创建任务卡(勾选视频/指定目录 + max_rounds + 思考档位)→ 连接逐任务 SSE
 * 展示进度:左侧视频状态索引,右侧每视频一张可折叠卡片(ForgeVideoCard,
 * 全部轮次 + 流式思考 + 最终结论都在卡内;进行中展开、完成自动折叠);
 * 终态展示结果面板(统计 + JSONL)。
 * 状态在 stores/sftforge.ts,组件只接线。 */
import { computed, onMounted } from 'vue'
import { NButton } from 'naive-ui'
import { useRouter } from 'vue-router'
import ForgeCreateForm from '../components/sftforge/ForgeCreateForm.vue'
import ForgeResultPanel from '../components/sftforge/ForgeResultPanel.vue'
import ForgeVideoCard from '../components/sftforge/ForgeVideoCard.vue'
import UiIcon from '../components/UiIcon.vue'
import { useSftForgeStore } from '../stores/sftforge'
import type { ForgeVideoState } from '../stores/sftforge'
import { forgeOutcomeBadge, videoBaseName } from '../utils/sftforge'

const forge = useSftForgeStore()
const router = useRouter()

function videoBadge(v: ForgeVideoState): { text: string; cls: string } {
  if (v.summary) {
    return forgeOutcomeBadge(
      v.summary,
      v.rounds.map((r) => ({ round_no: r.round_no, patched: !!r.trace?.patched })),
    )
  }
  if (v.rounds.length) return { text: '进行中', cls: 'ff-badge-run' }
  return { text: '等待', cls: 'ff-badge-wait' }
}

const statusText = computed(() => {
  if (forge.status === 'running') {
    return `进行中 · 已完成 ${forge.completedCount} / ${forge.videos.length} 个视频`
  }
  if (forge.status === 'done') return '已完成'
  if (forge.status === 'failed') return '失败'
  return ''
})

// store 跨路由存活:离开面板不重置(结果保留,SSE 保持连接);
// 回来时若连接已断则重连(服务端回放历史,无感续上);
// 服务重启后内存为空(idle 无视频)则从工作区持久化缓存恢复上次结果
onMounted(() => {
  forge.ensureConnected()
  void forge.restore()
})
</script>

<template>
  <div class="ff-page">
    <div class="ff-head">
      <h1 class="ff-title">SFT 生成</h1>
      <span v-if="statusText" class="ff-card-sub">{{ statusText }}</span>
      <span class="ff-spacer" />
      <n-button size="small" @click="router.push('/')">
        <template #icon><UiIcon name="up" :size="12" style="transform: rotate(-90deg)" /></template>
        返回树视图
      </n-button>
    </div>

    <ForgeCreateForm />

    <div v-if="forge.videos.length" class="ff-progress">
      <div class="ff-videos" role="listbox" aria-label="任务视频列表">
        <span
          v-for="v in forge.videos"
          :key="v.video"
          class="ff-video-row"
          role="option"
          :title="v.video"
        >
          <span class="ff-video-name">{{ videoBaseName(v.video) }}</span>
          <span v-if="v.rounds.length" class="ff-video-meta">{{ v.rounds.length }} 轮</span>
          <span class="ff-badge" :class="videoBadge(v).cls">{{ videoBadge(v).text }}</span>
        </span>
      </div>
      <div class="ff-timeline">
        <ForgeVideoCard
          v-for="v in forge.videos"
          :key="v.video"
          :video-state="v"
          :event-names="forge.eventNames"
          :target-names-of="forge.targetNamesOf"
        />
      </div>
    </div>

    <ForgeResultPanel v-if="forge.result" />
  </div>
</template>

<style scoped>
.ff-head {
  max-width: 1280px;
  display: flex;
  align-items: baseline;
  gap: var(--space-md);
  margin-bottom: var(--space-md);
}

.ff-title {
  font-family: var(--font-pixel);
  font-size: var(--text-2xl);
  font-weight: 650;
  font-style: normal; /* 标题一律 roman(design.md §2) */
  margin: 0;
}

.ff-head .ff-spacer {
  flex: 1;
}
</style>
