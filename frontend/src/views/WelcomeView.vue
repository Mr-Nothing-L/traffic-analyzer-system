<script setup lang="ts">
/** 工作台欢迎卡(文件树主区默认页,迁移自 legacy preview.js renderWelcome)。 */
import { NButton, NCard } from 'naive-ui'
import { useWorkspaceStore } from '../stores/workspace'

const ws = useWorkspaceStore()
</script>

<template>
  <!-- 未选择工作区 -->
  <n-card v-if="!ws.hasWorkspace" class="welcome-card">
    <template #header><span class="card-head">高速交通事件分析台</span></template>
    <p>请先点击顶部「选择工作区…」按钮,选择包含视频文件的目录。</p>
  </n-card>
  <!-- 已选择但未加载:自动加载中显示加载态;失败/未触发时可手动点 -->
  <n-card v-else-if="!ws.loaded" class="welcome-card">
    <template #header><span class="card-head">高速交通事件分析台</span></template>
    <p>当前工作区:<span class="hint-kbd">{{ ws.path }}</span></p>
    <p v-if="ws.treeLoading" class="welcome-hint">正在加载工作区,请稍候…</p>
    <p v-else>
      <n-button type="primary" class="hero-cta" @click="ws.loadTree()">加载工作区</n-button>
    </p>
    <p class="welcome-hint">大工作区加载需要一些时间。</p>
  </n-card>
  <!-- 已加载:真实计数 + 操作提示 -->
  <n-card v-else class="welcome-card">
    <template #header><span class="card-head">高速交通事件分析台</span></template>
    <p>当前工作区:<span class="hint-kbd">{{ ws.path }}</span></p>
    <p>共 {{ ws.videos.length }} 个视频,已勾选 {{ ws.checked.size }} 个。</p>
    <p>在左侧勾选视频后点击顶部「开始推理」;点击视频名查看 SFT 标注、分析报告与可视化证据。</p>
  </n-card>
</template>

<style scoped>
.welcome-card {
  max-width: 1060px;
  box-shadow: var(--shadow);
  /* 卡片进场(legacy layout.css:112-119 card-in)+ 既有 hover 升影保留 */
  animation: card-in var(--dur-med) var(--ease-out) both;
  transition: box-shadow var(--dur-med) var(--ease-out);
}

.welcome-card:hover {
  box-shadow: var(--shadow-hover);
}

.hint-kbd {
  font-family: var(--font-mono);
  font-size: var(--text-sm);
  background: var(--color-surface-3);
  border-radius: var(--radius-sm);
  padding: 1px 6px;
}

.welcome-hint {
  color: var(--color-text2);
  font-size: var(--text-sm);
}

.hero-cta {
  margin: var(--space-xs) 0;
}
</style>
