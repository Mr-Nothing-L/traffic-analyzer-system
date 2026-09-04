<script setup lang="ts">
/** 工具调用明细(分析链路节点展开区):原 ChatEntryTool 的结果内容区抽出复用。
 * 首行 tool_call: 参数摘要(mono);子代理迷你时间线(思考折叠块 / text /
 * 子工具行,与主干节点同构:label 行点击展开,思考折叠=一句话摘要);之下是
 * 结果文本 + load_video 静态提示 + 结果图片(点击进画廊)+ track_suspects
 * 取证叠加视频与可复制目录路径。折叠头(工具名/状态)由宿主(链路节点)负责,
 * 这里只渲染参数与结果部分。 */
import { computed, reactive, ref, watch } from 'vue'
import type { AgentToolEntry } from '../../stores/agentchat'
import UiIcon from '../UiIcon.vue'
import ThinkLine from './ThinkLine.vue'
import { toolLabel, trackSuspectsView } from '../../utils/chatDisplay'
import { useCopyFeedback } from '../../composables/useCopyFeedback'

const props = defineProps<{
  entry: AgentToolEntry
}>()

const emit = defineEmits<{
  preview: [url: string]
}>()

/** track_suspects 取证产物(trackSuspectsView 解析;其他工具恒 null):
 * clip 推导出 stream 地址则渲染可播放叠加视频,推不出/渲染报错只留路径文本。 */
const trackView = computed(() =>
  props.entry.name === 'track_suspects' ? trackSuspectsView(props.entry.result) : null,
)

/** 叠加视频加载失败标记:置真后隐藏 video 换路径文本;换新结果(新 src)时复位。 */
const overlayBroken = ref(false)
watch(
  () => trackView.value?.videoSrc,
  () => {
    overlayBroken.value = false
  },
)

/** 取证目录点击复制:成功图标变 ✓ 一秒,失败静默保持原样(低调调试辅助)。 */
const { copiedKey: dirCopiedKey, copyWithFeedback: copyDirWithFeedback } = useCopyFeedback()
const dirCopied = computed(() => dirCopiedKey.value !== null)
async function onCopyArtifactsDir(): Promise<void> {
  const dir = trackView.value?.dir
  if (!dir || dirCopied.value) return
  await copyDirWithFeedback('artifacts-dir', dir)
}

function argsSummary(args: string): string {
  const cut = (s: string) => (s.length > 120 ? `${s.slice(0, 120)}…` : s)
  try {
    const obj = JSON.parse(args) as Record<string, unknown>
    const parts = Object.entries(obj).map(
      ([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`,
    )
    return cut(parts.join(', ') || '(无参数)')
  } catch {
    return cut(args)
  }
}

/** 子代理内层节点折叠态(思考块/子工具行同构,本地记忆;节点收起随明细卸载,
 * 重开复位):key = `${entry.id}:${j}`,与子项在 children 里的位置一一对应。 */
const subOpen = reactive(new Set<string>())
function toggleSub(key: string) {
  if (subOpen.has(key)) subOpen.delete(key)
  else subOpen.add(key)
}

/** 子代理思考全文复制:成功图标变 ✓ 一秒,失败静默(同取证目录复制口径)。 */
const { copiedKey, copyWithFeedback } = useCopyFeedback()
</script>

<template>
  <div class="tool-detail">
    <!-- 首行:tool_call 引导的参数摘要(mono,随 .tool-detail 等宽底) -->
    <div class="tool-call-line">tool_call: {{ argsSummary(entry.args) }}</div>
    <!-- 子代理迷你时间线(spawn_subagent:思考折叠块 / text / 子工具行,与主干
         节点同构:折叠=label 行,展开=tool_call 参数行 / 思考全文) -->
    <template v-for="(c, j) in entry.children" :key="`${entry.id}:${j}`">
      <div v-if="c.kind === 'think'" class="sub-think">
        <div class="sub-row">
          <button
            class="sub-think-head"
            :title="subOpen.has(`${entry.id}:${j}`) ? '收起思考全文' : '展开思考全文'"
            @click="toggleSub(`${entry.id}:${j}`)"
          >
            <span class="row-caret" :class="{ open: subOpen.has(`${entry.id}:${j}`) }">▸</span>
            <span>思考过程:</span>
            <ThinkLine
              v-if="!subOpen.has(`${entry.id}:${j}`)"
              :think="c.text"
              :live="false"
            />
          </button>
          <button
            class="row-copy"
            title="复制思考内容"
            @click="copyWithFeedback(`${entry.id}:${j}`, c.text)"
          >
            <UiIcon :name="copiedKey === `${entry.id}:${j}` ? 'check' : 'copy'" :size="11" />
          </button>
        </div>
        <div v-if="subOpen.has(`${entry.id}:${j}`)" class="sub-think-text">
          {{ c.text }}
        </div>
      </div>
      <div v-else-if="c.kind === 'text'" class="sub-text">{{ c.text }}</div>
      <div v-else class="sub-tool">
        <button class="sub-tool-head" @click="toggleSub(`${entry.id}:${j}`)">
          <span class="row-caret" :class="{ open: subOpen.has(`${entry.id}:${j}`) }">▸</span>
          <span class="sub-tool-lbl">{{ toolLabel(c.name) }}</span>
          <span v-if="!c.done" class="tool-state">执行中…</span>
        </button>
        <div v-if="subOpen.has(`${entry.id}:${j}`)" class="tool-call-line sub-tool-call">
          tool_call: {{ argsSummary(c.args) }}
        </div>
      </div>
    </template>
    <div v-if="entry.result" class="tool-result-text">{{ entry.result }}</div>
    <div v-else-if="entry.done && !entry.images.length" class="tool-result-text">(无输出)</div>
    <!-- load_video:视频 part 体积巨大,只显示静态提示,不做播放器 -->
    <div v-if="entry.hasVideo" class="tool-video-note">
      <UiIcon name="video" :size="12" />
      <span>已加载完整视频(降帧)</span>
    </div>
    <div v-if="entry.images.length" class="tool-imgs">
      <img
        v-for="(u, j) in entry.images"
        :key="`${entry.id}:${j}`"
        :src="u"
        alt=""
        loading="lazy"
        @click="emit('preview', u)"
      />
    </div>
    <!-- track_suspects 取证产物:叠加视频小播放器 + 可复制目录路径 -->
    <template v-if="trackView">
      <video
        v-if="trackView.videoSrc && !overlayBroken"
        class="tool-track-video"
        :src="trackView.videoSrc"
        controls
        preload="metadata"
        @error="overlayBroken = true"
      />
      <button
        v-if="trackView.dir"
        type="button"
        class="tool-artifacts-dir"
        title="复制取证目录路径"
        @click="onCopyArtifactsDir"
      >
        <UiIcon :name="dirCopied ? 'check' : 'copy'" :size="10" />
        <span>{{ trackView.dir }}</span>
      </button>
    </template>
  </div>
</template>

<style scoped>
/* ---- 明细容器:结果弱化排版(等宽小字,限高滚动;缩进由宿主上下文给) ---- */
.tool-detail {
  padding: 2px 0 var(--space-xs);
  white-space: pre-wrap;
  word-break: break-word;
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  line-height: 1.6;
  color: var(--color-text2);
  max-height: 240px;
  overflow-y: auto;
}

/* ---- 首行 tool_call 参数摘要(mono 继承 .tool-detail,弱化色) ---- */
.tool-call-line {
  color: var(--color-text2);
  word-break: break-all;
}

/* ---- 子代理迷你时间线(spawn_subagent 展开区内,与主干节点同构折叠) ---- */
.sub-think {
  margin: 2px 0;
}

.sub-row {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  min-width: 0;
}

/* 折叠头(按钮)→ 像素,与思考过程折叠同款 */
.sub-think-head,
.sub-tool-head {
  display: inline-flex;
  align-items: center;
  gap: var(--space-xs);
  min-width: 0;
  padding: 0;
  border: none;
  background: none;
  color: var(--color-text2);
  font-size: var(--text-sm);
  font-family: var(--font-pixel);
  cursor: pointer;
  text-align: left;
}

.sub-think-head:hover,
.sub-tool-head:hover {
  color: var(--color-accent);
}

.row-caret {
  flex: 0 0 auto;
  transition: transform 0.15s ease;
}

.row-caret.open {
  transform: rotate(90deg);
}

/* 折叠摘要占满剩余宽度截断;内容文本 → sans */
.sub-think-head :deep(.think-line) {
  flex: 1 1 auto;
  width: auto;
  min-width: 0;
  font-family: var(--font-sans);
  font-size: var(--text-xs);
}

/* 悬停显现的复制按钮(同主干思考节点) */
.row-copy {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  flex: 0 0 auto;
  padding: 0;
  border: none;
  border-radius: var(--radius-sm);
  background: none;
  color: var(--color-text2);
  cursor: pointer;
  opacity: 0;
  transition: opacity var(--dur-fast) var(--ease-out);
}

.sub-row:hover .row-copy,
.sub-row:focus-within .row-copy {
  opacity: 1;
}

.row-copy:hover {
  color: var(--color-accent);
}

.sub-think-text {
  margin: 2px 0 0 var(--space-sm);
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--color-text2);
  font-size: var(--text-sm);
  line-height: 1.6;
}

.sub-text {
  margin: 2px 0;
  white-space: pre-wrap;
  word-break: break-word;
}

/* 子工具行 label = 工具条目头同类 → 像素;展开的参数行缩进对齐 label */
.sub-tool {
  margin: 2px 0;
  min-width: 0;
}

.sub-tool-lbl {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sub-tool-call {
  margin: 2px 0 0 var(--space-sm);
}

.tool-state {
  flex: 0 0 auto;
  font-size: var(--text-xs);
  color: var(--color-blue);
}

/* ---- 结果图片行(extract_frames/draw_boxes 抽帧/标注图,点击进画廊) ---- */
.tool-imgs {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-xs);
}

.tool-result-text + .tool-imgs {
  margin-top: var(--space-xs);
}

.tool-imgs img {
  width: 160px;
  height: 120px;
  object-fit: cover;
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-border);
  cursor: zoom-in;
}

/* load_video:视频 part 不做播放器,仅静态提示行 */
.tool-video-note {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  margin-top: var(--space-xs);
  color: var(--color-text2);
  font-family: var(--font-sans);
  font-size: var(--text-xs);
}

/* track_suspects:跟踪叠加片段小播放器(宽度规则同 user 气泡视频) */
.tool-track-video {
  display: block;
  width: min(320px, 100%);
  margin-top: var(--space-xs);
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-border);
  background: var(--color-stage-bg);
}

/* 取证目录路径:低调等宽文本按钮,点击复制 */
.tool-artifacts-dir {
  display: inline-flex;
  align-items: center;
  gap: var(--space-xs);
  max-width: 100%;
  margin-top: var(--space-xs);
  padding: 0;
  border: none;
  background: none;
  color: var(--color-text2);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  cursor: pointer;
  word-break: break-all;
  text-align: left;
}

.tool-artifacts-dir:hover {
  color: var(--color-accent);
}
</style>
