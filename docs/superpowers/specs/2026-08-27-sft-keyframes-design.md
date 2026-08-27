# SFT 详情页关键帧面板 — 设计文档

日期:2026-08-27 状态:已获用户批准(2026-08-27)

## 需求

SFT 标注详情卡(chunk 元信息行下方)新增关键帧面板:

- **候选帧**:平均采样 10 帧,hover 右上角「+」加入事件关键帧
- **事件关键帧**:chunk 级一组 2-5 帧(覆盖该视频所有事件),hover 右上角「−」删除,拖拽调整时间顺序
- 所有图片点击放大
- qwen3.8 智能挑选 2-5 帧,三条触发路径:详情页手动、推理后自动、批量触发(与事件检测解耦,不动已人工核实的文本标注)
- 前端增删改即时同步到 `analysis/<stem>/关键帧/` 文件夹,并写入 SFT 样本 JSON

## 关键决策(用户确认)

- 关键帧为 **chunk 级一组**,非每事件各一组
- 智能挑选:**手动 + 自动 + 批量** 三路径都要;批量功能源于本批数据文本已人工核实、不能重跑推理
- 关键帧 **写入 SFT 样本 JSON**(`keyframes` 字段),供训练数据收集方读取
- 文件夹用中文名「关键帧」;关键帧操作即时落盘,不挂「保存」按钮

## 设计

### 前端(frontend/)

- SftEditor.vue 的 `.sft-meta` 行下方插入 `KeyframePanel` 组件,两行:上排候选帧(10),下排事件关键帧(2-5)+「智能挑选」按钮
- 候选帧:调 `/api/videos/{stem}/meta` 得帧数,前端算 10 个均匀索引,图片直接用现有 `/api/videos/{stem}/frame?index=N`
- 图片放大:naive-ui `NImage`/`NImageGroup` 自带;拖拽排序:HTML5 原生 drag&drop,不加依赖
- 关键帧操作即时调 API,响应携带新 `file_sig` 同步 sft store 的 base_sig(乐观锁)
- 手动「智能挑选」已有关键帧时弹确认覆盖
- 文件列表工具栏(TreeToolbar)加「批量关键帧」按钮,复用 jobs 进度体系;默认跳过已有关键帧的视频,提供「覆盖重挑」选项

### 后端(traffic_analyzer/web/keyframes.py,新模块)

- `GET /api/videos/{stem}/keyframes/candidates`:返回 10 个均匀候选帧 `[{index, time_sec}]`
- `GET /api/results/{stem}/keyframes`:列出 `关键帧/` 下有序关键帧 `[{order, filename, frame_index, time_sec}]`,图片经现有 `GET /api/results/{stem}/file?path=` 服务
- `POST /api/results/{stem}/keyframes` `{frame_index, time_sec}`:抽帧存 `关键帧/NN_t{sec}s.jpg` → 重排序号 → 更新 SFT JSON → 返回列表+新 file_sig
- `DELETE /api/results/{stem}/keyframes/{filename}`:删文件 → 重排 → 更新 JSON
- `PUT /api/results/{stem}/keyframes/order` `{filenames: [...]}`:重命名重排 → 更新 JSON
- `POST /api/videos/{stem}/keyframes/auto_pick`:核心智能挑选(三路径共用)

### 智能挑选(auto_pick)

- 输入:10 张候选帧图(现有 chat/qa 链路的 OpenAI 兼容多图消息构造方式)+ 该视频事件结论(从 SFT JSON 摘)
- 输出:2-5 个候选序号;严格解析校验(范围、数量),失败回退不改动现有关键帧
- 推理后自动:分析完成且无关键帧时跑一次,失败仅记日志
- 批量:仅对已有分析结果的勾选视频跑 auto_pick,不动文本标注

### Schema 变更

- `SftSample`(web/evidence_schema.py)加 `keyframes: List[{filename, frame_index, time_sec}]`,默认空
- sft_api.py PUT 文本保存的 model_dump exclude 集合加 `keyframes`(同 last_edited_by 处理),防止文本保存误删

## 验证

- 后端:模块 import + 端点冒烟(候选列表/增删排序/auto_pick 失败回退)
- 前端:`npm run build` 通过
- 重启 web 服务后用户手动测试
