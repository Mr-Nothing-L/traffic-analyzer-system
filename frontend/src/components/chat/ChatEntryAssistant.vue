<script setup lang="ts">
/** assistant 气泡:思考折叠 + markdown 正文,底部行复制。
 * hideThink=true / hideText=true(该轮有面板承接:进行中或有 detection)时分别
 * 不渲染思考/正文——思考改由分析链路面板的思考节点、正文改由说明节点按时间序
 * 呈现(两者独立控制:submit_detection 之后的收尾正文在面板区间外,hideText=false
 * 仍作普通气泡跟在检测卡后);纯问答轮次保持普通气泡。 */
import { computed } from 'vue'
import type { AgentEntry } from '../../stores/agentchat'
import type { AgentAssistantEntry } from '../../stores/agentchat'
import UiIcon from '../UiIcon.vue'
import ChatMessageBase from './ChatMessageBase.vue'
import MdStream from './MdStream.vue'
import ThinkLine from './ThinkLine.vue'

const props = defineProps<{
  entry: AgentEntry
  copied: boolean
  streaming: boolean
  thinkOpen: boolean
  /** true=该轮思考已并入链路面板,气泡内不再重复渲染。 */
  hideThink?: boolean
  /** true=该轮正文已并入链路面板「说明」节点,气泡内不再重复渲染。 */
  hideText?: boolean
  time: string
}>()

const emit = defineEmits<{
  copy: [text: string]
  'toggle-think': []
}>()

const entry = computed(() => props.entry as AgentAssistantEntry)
const isThinkLive = computed(() => props.streaming && !entry.value.text)
</script>

<template>
            <!-- 思考已并入链路面板且无正文的条目不留空壳气泡 -->
            <ChatMessageBase
              v-if="(entry.text && !hideText) || (entry.think && !hideThink)"
              :time="time"
            >
              <div v-if="entry.think && !hideThink" class="think">
                <button class="think-head" @click="emit('toggle-think')">
                  <UiIcon
                    name="up"
                    :size="10"
                    class="think-caret"
                    :class="{ open: thinkOpen }"
                  />
                  <span>思考过程</span>
                </button>
                <div v-if="thinkOpen" class="think-text">{{ entry.think }}</div>
                <!-- 折叠态摘要:运行中(思考仍在流入)显示末行并横向跟随滚动,结束后显示首行 -->
                <ThinkLine v-else :think="entry.think" :live="isThinkLive" />
              </div>
              <!-- 正文:流式期间增量渲染(冻结已完成块),定格后一次性完整渲染 -->
              <MdStream v-if="entry.text && !hideText" :text="entry.text" :streaming="streaming" />
              <template #actions>
                <button class="msg-act" title="复制" @click="emit('copy', entry.text)">
                  <UiIcon :name="copied ? 'check' : 'copy'" :size="12" />
                </button>
              </template>
            </ChatMessageBase>
</template>

<style scoped>
.bubble-text {
  white-space: pre-wrap;
  word-break: break-word;
  font-size: var(--text-md);
  line-height: 1.6;
}

.video-chip {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  max-width: 320px;
  margin-bottom: var(--space-xs);
  color: var(--color-text2);
  font-size: var(--text-xs);
}

.video-chip-name {
  font-family: var(--font-mono);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

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
  font-family: var(--font-pixel); /* 折叠头(按钮)→ 像素 */
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
</style>
