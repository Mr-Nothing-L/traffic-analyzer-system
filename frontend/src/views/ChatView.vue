<script setup lang="ts">
/** 快速对话整卡:来源条(上传/工作区选择/清空记忆)+ 消息列表(SSE 流式气泡)
 * + 输入区(Enter 发送 / Shift+Enter 换行 / 发送中可停止)。
 * 与数据看板同模式:TreeView 子路由,整卡替换主区;状态在 stores/chat.ts,组件只接线。 */
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import {
  NButton,
  NCollapse,
  NCollapseItem,
  NInput,
  NModal,
  NPopconfirm,
  NScrollbar,
  useMessage,
} from 'naive-ui'
import { apiFetch } from '../api/client'
import { useChatStore } from '../stores/chat'
import type { VideoInfo } from '../stores/workspace'
import UiIcon from '../components/UiIcon.vue'

const chat = useChatStore()
const message = useMessage()

const question = ref('')
const uploading = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)

/* ---- 工作区视频选择弹窗 ---- */
const pickerOpen = ref(false)
const pickerLoading = ref(false)
const pickerFilter = ref('')
const videos = ref<VideoInfo[]>([])
const filteredVideos = computed(() => {
  const q = pickerFilter.value.trim().toLowerCase()
  if (!q) return videos.value
  return videos.value.filter(
    (v) => v.name.toLowerCase().includes(q) || v.rel.toLowerCase().includes(q),
  )
})

/* ---- 图片:加载失败隐藏(onerror);点击放大(NModal) ---- */
const broken = reactive(new Set<string>())
const previewUrl = ref<string | null>(null)

/* ---- 消息列表:历史 + 流式中气泡;新增/增长时自动滚底 ---- */
const scrollbar = ref<InstanceType<typeof NScrollbar> | null>(null)
const displayMessages = computed(() => {
  const arr = [...chat.messages]
  if (chat.current) arr.push(chat.current)
  return arr
})

watch(
  () => [
    chat.messages.length,
    chat.current?.content.length,
    chat.current?.think.length,
    chat.current?.images.length,
  ],
  async () => {
    await nextTick()
    scrollbar.value?.scrollTo({ top: Number.MAX_SAFE_INTEGER })
  },
)

onMounted(async () => {
  try {
    await chat.fetchState()
  } catch (e) {
    message.error(`加载对话状态失败:${(e as Error).message}`)
  }
})

onUnmounted(() => chat.stop()) // 离开页面中断在途流

/* ---- 来源条动作 ---- */
async function onFiles(ev: Event) {
  const input = ev.target as HTMLInputElement
  if (!input.files?.length) return
  uploading.value = true
  try {
    await chat.upload(input.files)
    message.success('已加载来源,可以开始提问')
  } catch (e) {
    message.error(`上传失败:${(e as Error).message}`)
  } finally {
    uploading.value = false
    input.value = '' // 允许重复选择同一文件再次触发 change
  }
}

async function openPicker() {
  pickerOpen.value = true
  pickerFilter.value = ''
  pickerLoading.value = true
  try {
    videos.value = (await apiFetch<VideoInfo[]>('/workspace/videos')) || []
  } catch (e) {
    message.error(`读取工作区视频失败:${(e as Error).message}`)
    videos.value = []
  } finally {
    pickerLoading.value = false
  }
}

async function chooseVideo(rel: string) {
  try {
    await chat.setSource(rel)
    pickerOpen.value = false
    message.success('已切换来源,可以开始提问')
  } catch (e) {
    message.error(`设置来源失败:${(e as Error).message}`)
  }
}

async function onClear() {
  try {
    await chat.clear()
    message.success('已清空对话记忆')
  } catch (e) {
    message.error(`清空失败:${(e as Error).message}`)
  }
}

/* ---- 提问 ---- */
async function onSend() {
  const q = question.value.trim()
  if (!q || chat.sending) return
  question.value = ''
  try {
    await chat.ask(q)
  } catch (e) {
    message.error((e as Error).message)
  }
}
</script>

<template>
  <div class="chat-page">
    <section class="chat-card">
      <!-- 来源条 -->
      <div class="chat-source">
        <UiIcon name="chat" :size="14" />
        <span class="chat-source-name" :title="chat.source?.display_name || ''">
          {{ chat.source ? chat.source.display_name : '未选择视频/图片' }}
        </span>
        <span class="chat-spacer" />
        <n-button size="small" :loading="uploading" @click="fileInput?.click()">
          上传视频/图片
        </n-button>
        <n-button size="small" @click="openPicker">从工作区选择</n-button>
        <n-popconfirm @positive-click="onClear">
          <template #trigger>
            <n-button size="small">清空记忆</n-button>
          </template>
          清空全部对话记忆?
        </n-popconfirm>
      </div>
      <input
        ref="fileInput"
        type="file"
        hidden
        multiple
        accept=".mp4,.avi,.mov,.mkv,.ts,.jpg,.jpeg,.png,.webp"
        @change="onFiles"
      />

      <!-- 消息列表 -->
      <n-scrollbar ref="scrollbar" class="chat-scroll">
        <div v-if="!displayMessages.length" class="chat-empty">
          上传视频/图片,或从工作区选择视频,然后开始提问
        </div>
        <template v-for="(m, i) in displayMessages" :key="i">
          <div v-if="m.role === 'divider'" class="chat-divider">{{ m.content }}</div>
          <div v-else class="chat-row" :class="m.role">
            <div class="bubble">
              <n-collapse v-if="m.role === 'assistant' && m.think" class="think">
                <n-collapse-item title="思考过程" name="think">
                  <div class="think-text">{{ m.think }}</div>
                </n-collapse-item>
              </n-collapse>
              <div v-if="m.content" class="bubble-text">{{ m.content }}</div>
              <div v-if="m.images.length" class="img-group">
                <img
                  v-for="u in m.images"
                  v-show="!broken.has(u)"
                  :key="u"
                  :src="u"
                  alt=""
                  loading="lazy"
                  @error="broken.add(u)"
                  @click="previewUrl = u"
                />
              </div>
            </div>
          </div>
        </template>
      </n-scrollbar>

      <!-- 输入区 -->
      <div class="chat-input">
        <n-input
          v-model:value="question"
          type="textarea"
          :autosize="{ minRows: 1, maxRows: 4 }"
          placeholder="输入问题,Enter 发送,Shift+Enter 换行"
          @keydown.enter.exact.prevent="onSend"
        />
        <n-button v-if="chat.sending" size="small" @click="chat.stop()">停止</n-button>
        <n-button
          v-else
          type="primary"
          size="small"
          :disabled="!question.trim()"
          @click="onSend"
        >
          <template #icon><UiIcon name="send" :size="12" /></template>
          发送
        </n-button>
      </div>
    </section>

    <!-- 工作区视频选择弹窗 -->
    <n-modal v-model:show="pickerOpen">
      <div class="picker-dialog" role="dialog" aria-modal="true" aria-label="选择工作区视频">
        <div class="picker-head">
          <span class="picker-title">选择工作区视频</span>
          <button class="picker-close" title="关闭" @click="pickerOpen = false">
            <UiIcon name="close" :size="13" />
          </button>
        </div>
        <n-input
          v-model:value="pickerFilter"
          size="small"
          spellcheck="false"
          placeholder="搜索名称/路径…"
        />
        <div class="picker-list">
          <div v-if="pickerLoading" class="picker-state">加载中…</div>
          <template v-else>
            <div
              v-for="v in filteredVideos"
              :key="v.rel"
              class="picker-row"
              @click="chooseVideo(v.rel)"
            >
              <span class="picker-ico"><UiIcon name="video" :size="13" /></span>
              <div class="picker-info">
                <div class="picker-name">
                  {{ v.name }}
                  <span v-if="v.has_results" class="picker-badge">已有结果</span>
                </div>
                <div class="picker-rel">{{ v.rel }}</div>
              </div>
            </div>
            <div v-if="!filteredVideos.length" class="picker-state">无匹配视频</div>
          </template>
        </div>
      </div>
    </n-modal>

    <!-- 图片放大预览 -->
    <n-modal :show="!!previewUrl" @update:show="previewUrl = null">
      <img v-if="previewUrl" class="preview-img" :src="previewUrl" alt="" @click="previewUrl = null" />
    </n-modal>
  </div>
</template>

<style scoped>
/* 与看板不同:对话需要内部滚动,卡片占满主区可视高度(.app-main 高度确定,height:100% 有效) */
.chat-page {
  height: 100%;
  display: flex;
  min-height: 0;
}

.chat-card {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--color-card);
  border: 1px solid var(--color-border);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  overflow: hidden;
}

/* ---- 来源条 ---- */
.chat-source {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  padding: var(--space-sm) var(--space-md);
  border-bottom: 1px solid var(--color-border);
  color: var(--color-text2);
}

.chat-source-name {
  font-size: var(--text-md);
  font-weight: 600;
  color: var(--color-text);
  max-width: 40%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chat-spacer {
  flex: 1;
}

/* ---- 消息列表 ---- */
.chat-scroll {
  flex: 1;
  min-height: 0;
  padding: 0 var(--space-md);
}

.chat-empty {
  padding: var(--space-2xl) var(--space-md);
  text-align: center;
  color: var(--color-text2);
  font-size: var(--text-md);
}

.chat-divider {
  text-align: center;
  color: var(--color-text2);
  font-size: var(--text-xs);
  margin: var(--space-md) 0 var(--space-sm);
}

.chat-row {
  display: flex;
  margin: var(--space-sm) 0;
}

.chat-row.user {
  justify-content: flex-end;
}

.chat-row.assistant {
  justify-content: flex-start;
}

.bubble {
  max-width: 72%;
  padding: var(--space-sm) var(--space-md);
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-border);
  background: var(--color-surface-2);
}

.chat-row.user .bubble {
  background: var(--color-accent-soft);
  border-color: var(--color-accent-deep);
}

.bubble-text {
  white-space: pre-wrap;
  word-break: break-word;
  font-size: var(--text-md);
  line-height: 1.6;
}

.think {
  margin-bottom: var(--space-xs);
}

.think-text {
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--color-text2);
  font-size: var(--text-sm);
  line-height: 1.6;
}

.img-group {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-sm);
  margin-top: var(--space-sm);
}

.img-group img {
  width: 120px;
  height: 80px;
  object-fit: cover;
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-border);
  cursor: zoom-in;
}

/* ---- 输入区 ---- */
.chat-input {
  display: flex;
  align-items: flex-end;
  gap: var(--space-sm);
  padding: var(--space-sm) var(--space-md);
  border-top: 1px solid var(--color-border);
}

/* ---- 工作区视频选择弹窗 ---- */
.picker-dialog {
  width: 520px;
  max-width: calc(100vw - 48px);
  background: var(--color-card);
  border-radius: var(--radius);
  box-shadow: var(--shadow-hover);
  padding: var(--space-md);
  display: flex;
  flex-direction: column;
  gap: var(--space-sm);
}

.picker-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.picker-title {
  font-family: var(--font-pixel);
  font-size: var(--text-lg);
  font-weight: 650;
}

.picker-close {
  border: none;
  background: transparent;
  color: var(--color-text2);
  cursor: pointer;
  padding: 4px;
}

.picker-close:hover {
  color: var(--color-text);
}

.picker-list {
  max-height: 50vh;
  overflow-y: auto;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
}

.picker-state {
  padding: var(--space-lg);
  text-align: center;
  color: var(--color-text2);
  font-size: var(--text-sm);
}

.picker-row {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  padding: var(--space-sm) var(--space-md);
  cursor: pointer;
  border-bottom: 1px solid var(--color-border);
}

.picker-row:last-child {
  border-bottom: none;
}

.picker-row:hover {
  background: var(--color-hover-bg);
}

.picker-ico {
  color: var(--color-text2);
  display: inline-flex;
}

.picker-info {
  min-width: 0;
}

.picker-name {
  font-size: var(--text-md);
  color: var(--color-text);
}

.picker-badge {
  margin-left: var(--space-xs);
  padding: 0 6px;
  border-radius: 999px;
  background: var(--color-sage-soft);
  color: var(--color-sage);
  font-size: var(--text-xs);
}

.picker-rel {
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  color: var(--color-text2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ---- 图片放大 ---- */
.preview-img {
  max-width: calc(100vw - 96px);
  max-height: calc(100vh - 96px);
  border-radius: var(--radius-sm);
  cursor: zoom-out;
  display: block;
}
</style>
