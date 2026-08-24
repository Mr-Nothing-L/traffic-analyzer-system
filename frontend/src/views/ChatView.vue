<script setup lang="ts">
/** 快速对话整卡:来源条(上传/预览/清空记忆)+ 消息列表(SSE 流式气泡)
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
import { useChatStore } from '../stores/chat'
import UiIcon from '../components/UiIcon.vue'

const chat = useChatStore()
const message = useMessage()

const question = ref('')
const uploading = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)

/* ---- 来源预览面板(上传的视频/图片) ---- */
const previewOpen = ref(false)

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
    previewOpen.value = true // 上传成功后直接展示预览
    message.success('已加载来源,可以开始提问')
  } catch (e) {
    message.error(`上传失败:${(e as Error).message}`)
  } finally {
    uploading.value = false
    input.value = '' // 允许重复选择同一文件再次触发 change
  }
}

async function onClear() {
  try {
    await chat.clear()
    previewOpen.value = false
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
        <n-button
          size="small"
          :disabled="!chat.source?.files?.length"
          @click="previewOpen = !previewOpen"
        >
          预览
        </n-button>
        <span class="chat-spacer" />
        <n-button size="small" :loading="uploading" @click="fileInput?.click()">
          上传视频/图片
        </n-button>
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

      <!-- 来源预览:视频内嵌播放 / 图片缩略图(点击放大) -->
      <div v-if="previewOpen && chat.source?.files?.length" class="source-preview">
        <video
          v-if="chat.source.kind === 'upload_video'"
          class="source-video"
          :src="chat.source.files[0]"
          controls
          preload="metadata"
        />
        <template v-else>
          <img
            v-for="u in chat.source.files"
            v-show="!broken.has(u)"
            :key="u"
            class="source-thumb"
            :src="u"
            alt=""
            loading="lazy"
            @error="broken.add(u)"
            @click="previewUrl = u"
          />
        </template>
      </div>

      <!-- 消息列表 -->
      <n-scrollbar ref="scrollbar" class="chat-scroll">
        <div v-if="!displayMessages.length" class="chat-empty">
          上传视频/图片,然后开始提问
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

/* ---- 来源预览 ---- */
.source-preview {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-sm);
  padding: var(--space-sm) var(--space-md);
  border-bottom: 1px solid var(--color-border);
  background: var(--color-surface-2);
}

.source-video {
  max-width: 360px;
  max-height: 220px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-border);
  background: #000;
}

.source-thumb {
  width: 120px;
  height: 80px;
  object-fit: cover;
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-border);
  cursor: zoom-in;
}

/* ---- 消息列表 ---- */
.chat-scroll {
  flex: 1;
  min-height: 0;
  padding: var(--space-md) var(--space-lg);
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

/* ---- 图片放大 ---- */
.preview-img {
  max-width: calc(100vw - 96px);
  max-height: calc(100vh - 96px);
  border-radius: var(--radius-sm);
  cursor: zoom-out;
  display: block;
}
</style>
