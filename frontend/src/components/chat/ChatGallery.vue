<script setup lang="ts">
/** 图片画廊:user 气泡附件图 / 工具结果图 / 检测卡逐事件标注图按时间序进画廊;
 * 单图放大 + 左右切换(按钮/键盘,边界不循环)。自 ChatView 拆出:画廊清单
 * 从 agent 时间线推导(条目数据直连 store),父组件经 ref 调 openPreview(url)。 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { NModal } from 'naive-ui'
import { useAgentChatStore } from '../../stores/agentchat'
import type { DetectionPayload } from '../../stores/agentchat'
import UiIcon from '../UiIcon.vue'

const agent = useAgentChatStore()

function asPayload(data: unknown): DetectionPayload | null {
  return data && typeof data === 'object' ? (data as DetectionPayload) : null
}

const previewIndex = ref<number | null>(null)

const galleryImages = computed(() => {
  const urls: string[] = []
  for (const e of agent.entries) {
    if (e.kind === 'user' && e.images?.length) urls.push(...e.images)
    else if (e.kind === 'tool' && e.images.length) urls.push(...e.images)
    else if (e.kind === 'detection') {
      const p = asPayload(e.data)
      if (p?.events) {
        for (const ev of p.events) if (ev.annotated_image) urls.push(ev.annotated_image)
      }
    }
  }
  return urls
})
const previewUrl = computed(() =>
  previewIndex.value != null ? (galleryImages.value[previewIndex.value] ?? null) : null,
)

/* 画廊打开期间列表收缩(切换会话):收敛或关闭,防越界空 modal */
watch(galleryImages, (imgs) => {
  if (previewIndex.value == null) return
  if (!imgs.length) previewIndex.value = null
  else if (previewIndex.value >= imgs.length) previewIndex.value = imgs.length - 1
})

function openPreview(u: string) {
  const i = galleryImages.value.indexOf(u)
  if (i >= 0) previewIndex.value = i
}

function previewNav(delta: number) {
  if (previewIndex.value == null) return
  const next = previewIndex.value + delta
  if (next < 0 || next >= galleryImages.value.length) return // 边界不循环
  previewIndex.value = next
}

/** 仅画廊打开时响应 ←/→(挂在 window,组件卸载移除;关闭时不拦截任何按键)。 */
function onGalleryKey(ev: KeyboardEvent) {
  if (previewIndex.value == null) return
  if (ev.key === 'ArrowLeft') previewNav(-1)
  else if (ev.key === 'ArrowRight') previewNav(1)
}

onMounted(() => window.addEventListener('keydown', onGalleryKey))
onUnmounted(() => window.removeEventListener('keydown', onGalleryKey))

defineExpose({ openPreview })
</script>

<template>
    <n-modal :show="previewIndex != null" @update:show="previewIndex = null">
      <div v-if="previewUrl" class="gallery-wrap">
        <img class="preview-img" :src="previewUrl" alt="" @click="previewIndex = null" />
        <button
          type="button"
          class="gallery-nav gallery-prev"
          title="上一张 (←)"
          :disabled="previewIndex === 0"
          @click.stop="previewNav(-1)"
        >
          <UiIcon name="left" :size="18" />
        </button>
        <button
          type="button"
          class="gallery-nav gallery-next"
          title="下一张 (→)"
          :disabled="previewIndex === galleryImages.length - 1"
          @click.stop="previewNav(1)"
        >
          <UiIcon name="right" :size="18" />
        </button>
      </div>
    </n-modal>
</template>

<style scoped>
/* ---- 图片放大(画廊);深色遮罩用 stage-bg token 的 color-mix,不写 inline hex ---- */
.gallery-wrap {
  position: relative;
  display: flex;
  align-items: center;
}

.preview-img {
  max-width: calc(100vw - 96px);
  max-height: calc(100vh - 96px);
  border-radius: var(--radius-sm);
  cursor: zoom-out;
  display: block;
}

.gallery-nav {
  position: absolute;
  top: 50%;
  transform: translateY(-50%);
  width: 36px;
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  border-radius: 50%;
  background: color-mix(in srgb, var(--color-stage-bg) 45%, transparent);
  color: var(--color-on-accent);
  cursor: pointer;
}

.gallery-nav:hover:not(:disabled) {
  background: color-mix(in srgb, var(--color-stage-bg) 65%, transparent);
}

.gallery-nav:focus-visible {
  outline: 2px solid var(--color-accent);
  outline-offset: 1px;
}

.gallery-nav:disabled {
  opacity: 0.3;
  cursor: default;
}

.gallery-prev {
  left: -48px; /* 按钮放图外两侧,不遮图 */
}

.gallery-next {
  right: -48px;
}
</style>
