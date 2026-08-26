<script setup lang="ts">
/** 工具条目:参数摘要 + 结果折叠(文本/图片/子代理),失败红字。 */
import { computed } from 'vue'
import type { AgentEntry } from '../../stores/agentchat'
import type { AgentToolEntry } from '../../stores/agentchat'
import UiIcon from '../UiIcon.vue'
import { toolLabel, toolErrorSummary } from '../../utils/chatDisplay'

const props = defineProps<{
  entry: AgentEntry
  toolOpen: boolean
  subThinkOpen: Set<string>
}>()

const emit = defineEmits<{
  'toggle-tool': []
  'toggle-sub-think': [key: string]
  preview: [url: string]
}>()

const entry = computed(() => props.entry as AgentToolEntry)

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
</script>

<template>
            <div class="tool">
              <button class="tool-head" @click="emit('toggle-tool')">
                <UiIcon
                  name="up"
                  :size="10"
                  class="think-caret"
                  :class="{ open: toolOpen }"
                />
                <span class="tool-title">工具调用:{{ toolLabel(entry.name) }}</span>
                <span class="tool-args">{{ argsSummary(entry.args) }}</span>
                <span v-if="!entry.done" class="tool-state">执行中…</span>
                <span
                  v-else-if="entry.isError"
                  class="tool-state err tool-err-text"
                  :title="entry.result"
                >{{ toolErrorSummary(entry.result) }}</span>
                <span v-else class="tool-state ok">完成</span>
              </button>
              <div v-if="toolOpen && (entry.done || entry.children.length)" class="tool-result">
                <!-- 子代理迷你时间线(spawn_subagent:think/text 聚合块 + 子工具一行小字) -->
                <template v-for="(c, j) in entry.children" :key="`${entry.id}:${j}`">
                  <div v-if="c.kind === 'think'" class="sub-think">
                    <button class="think-head" @click="emit('toggle-sub-think', `${entry.id}:${j}`)">
                      <UiIcon
                        name="up"
                        :size="10"
                        class="think-caret"
                        :class="{ open: subThinkOpen.has(`${entry.id}:${j}`) }"
                      />
                      <span>子代理思考</span>
                    </button>
                    <div v-if="subThinkOpen.has(`${entry.id}:${j}`)" class="think-text">{{ c.text }}</div>
                  </div>
                  <div v-else-if="c.kind === 'text'" class="sub-text">{{ c.text }}</div>
                  <div v-else class="sub-tool">
                    工具调用:{{ toolLabel(c.name) }}
                    <span class="tool-args">{{ argsSummary(c.args) }}</span>
                    <span v-if="!c.done" class="tool-state">执行中…</span>
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
              </div>
            </div>
</template>

<style scoped>
/* ---- 思考过程折叠 ---- */
.think {
  margin-bottom: var(--space-xs);
}

.think-head {
  display: inline-flex;
  align-items: center;
  gap: var(--space-xs);
  padding: 0;
  border: none;
  background: none;
  color: var(--color-text2);
  font-size: var(--text-sm);
  cursor: pointer;
}

.think-head:hover {
  color: var(--color-accent);
}

.think-caret {
  transform: rotate(180deg); /* 收起:向下 */
  transition: transform 0.15s ease;
}

.think-caret.open {
  transform: rotate(0deg); /* 展开:向上 */
}

.think-text {
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--color-text2);
  font-size: var(--text-sm);
  line-height: 1.6;
}
/* ---- 工具条目(弱化:无卡片边框/底色,小字 muted,与思考过程同款) ---- */
.tool {
  margin: var(--space-xs) 0;
}

.tool-head {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  width: 100%;
  padding: 0;
  border: none;
  background: none;
  color: var(--color-text2);
  font-size: var(--text-sm);
  cursor: pointer;
  text-align: left;
}

.tool-head:hover {
  color: var(--color-accent);
}

/* 工具名:正文 sans 小字,不突出;中文映射由 toolLabel 给出 */
.tool-title {
  flex: 0 0 auto;
}

.tool-args {
  flex: 1;
  min-width: 0;
  font-size: var(--text-xs);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tool-state {
  flex: 0 0 auto;
  font-size: var(--text-xs);
  color: var(--color-blue);
}

.tool-state.ok {
  color: var(--color-sage);
}

.tool-state.err {
  color: var(--color-red);
}

/* 失败摘要:直接显示错误内容首行(红色),占满剩余宽度并省略截断 */
.tool-err-text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-align: left;
}

.tool-result {
  padding: 2px 0 var(--space-xs) calc(10px + var(--space-xs));
  white-space: pre-wrap;
  word-break: break-word;
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  line-height: 1.6;
  color: var(--color-text2);
  max-height: 240px;
  overflow-y: auto;
}

/* 工具结果图片行(extract_frames/draw_boxes 抽帧/标注图,点击进画廊) */
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

/* ---- 子代理迷你时间线(spawn_subagent 展开区内,同工具结果弱化风格) ---- */
.sub-think {
  margin: 2px 0;
}

.sub-text {
  margin: 2px 0;
  white-space: pre-wrap;
  word-break: break-word;
}

.sub-tool {
  margin: 2px 0;
  display: flex;
  align-items: baseline;
  gap: var(--space-xs);
  min-width: 0;
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
</style>
