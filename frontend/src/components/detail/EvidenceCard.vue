<script setup lang="ts">
/** 证据编辑卡:多事件 Tab + 画布 + 表格 + 保存/重置。
 * 乐观锁:base_sig 取自 GET 缓存的 evidence_sig;409 冲突提示并可刷新(同 legacy)。 */
import { computed, ref } from 'vue'
import { NButton, NCard, useDialog, useMessage } from 'naive-ui'
import type { VideoSource } from '../../api/results'
import { useEvidenceStore } from '../../stores/evidence'
import EvidenceCanvas from './EvidenceCanvas.vue'
import EvidenceTable from './EvidenceTable.vue'

const props = defineProps<{ stem: string; source: VideoSource }>()
const store = useEvidenceStore()
const message = useMessage()
const dialog = useDialog()
const savedFlash = ref(false) // 保存成功反馈:按钮短暂显示「已保存」(同 legacy flashSaveBtn)

const events = computed(() => store.draft?.events ?? [])
const current = computed(() => events.value[store.tabIdx] || null)
const videoInfo = computed(() => store.draft?.video || {})

async function onSave() {
  const r = await store.save(props.stem)
  if (r.ok) {
    message.success('证据已保存')
    savedFlash.value = true
    setTimeout(() => {
      savedFlash.value = false
    }, 1000)
    return
  }
  if (r.conflict) {
    // 乐观锁冲突:他人已修改;重载会丢弃当前未保存的修改,先确认(同 legacy confirm)
    dialog.warning({
      title: '保存冲突',
      content: '该视频的证据已被他人修改。可丢弃当前未保存的修改并刷新为最新版本。',
      positiveText: '丢弃并刷新',
      negativeText: '保留我的修改',
      onPositiveClick: async () => {
        await store.load(props.stem)
        message.warning('他人已修改,已为你刷新')
      },
    })
    return
  }
  message.error(`保存失败(${r.status}):${r.message}`)
}

async function onReset() {
  const r = await store.reset(props.stem)
  if (r.ok) message.success('已重置为磁盘版本')
  else message.error(`重置失败:${r.message}`)
}
</script>

<template>
  <n-card class="card-evidence">
    <template #header>
      <span class="card-head">证据编辑</span>
      <span class="card-sub">拖拽多边形端点 / 证据框角点进行调整</span>
    </template>
    <template #header-extra>
      <span v-if="store.dirty" class="dirty-flag">● 未保存</span>
      <n-button size="small" quaternary :disabled="!store.dirty" @click="onReset">重置</n-button>
      <n-button
        size="small"
        type="primary"
        :disabled="!store.dirty"
        :loading="store.saving"
        @click="onSave"
      >
        {{ savedFlash ? '已保存' : '保存' }}
      </n-button>
    </template>
    <div v-if="!store.draft || !events.length" class="empty-note">无证据数据</div>
    <template v-else>
      <div class="ev-tabs">
        <button
          v-for="(ev, i) in events"
          :key="i"
          type="button"
          class="ev-tab"
          :class="{ active: i === store.tabIdx }"
          @click="store.tabIdx = i"
        >
          <span class="dot" :class="{ detected: ev.detected }" />{{ ev.event_id }} {{ ev.name }}
        </button>
      </div>
      <template v-if="current">
        <!-- 切 Tab 整体重挂画布(帧/形状/选中态随事件重置,同 legacy 重渲染) -->
        <EvidenceCanvas
          :key="store.tabIdx"
          :stem="stem"
          :source="source"
          :ev="current"
          :video-info="videoInfo"
        />
        <EvidenceTable :ev="current" />
      </template>
    </template>
  </n-card>
</template>

<style scoped>
/* ---------- 事件 Tab ---------- */
.ev-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 0 0 12px;
}

.ev-tab {
  border: 1px solid var(--color-border);
  background: transparent;
  color: var(--color-text2);
  border-radius: 999px;
  padding: 4px 12px;
  font-size: var(--text-sm);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  transition:
    background var(--dur-fast) var(--ease-out),
    color var(--dur-fast) var(--ease-out),
    border-color var(--dur-fast) var(--ease-out);
}

.ev-tab:hover {
  border-color: var(--color-accent);
  color: var(--color-accent);
}

.ev-tab:focus-visible {
  outline: 2px solid var(--color-accent);
  outline-offset: 1px;
}

.ev-tab.active {
  background: var(--color-accent);
  border-color: var(--color-accent);
  color: var(--color-on-accent);
}

.ev-tab .dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--color-line-strong);
}

.ev-tab .dot.detected {
  background: var(--color-sage);
}

.ev-tab.active .dot.detected {
  background: var(--color-on-accent);
}
</style>
