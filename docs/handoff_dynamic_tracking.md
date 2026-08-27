# 交接文案:动态交通事件定向跟踪(track_suspects)

> 用途:交给另一个 agent 做实现/继续探讨。描述「动态事件(违停/逆行/倒车/应急车道)定向跟踪工具」的设计草案与场景先验,不含实现。
> 项目:traffic-analyzer(/media/wanji/Elements/大模型应用/traffic-agent-local-version),TS agent 运行时 + Python toolserver 架构。
>
> **状态:v2 已实施**(2026-08-27,commit 830ad2b + 6192895)。下方「v2 修订」节为头脑风暴后的最终决策,与初稿冲突处以 v2 为准;实现位于 `traffic_analyzer/toolserver/tracking/` 与 `agent/src/tools/builtin/trackSuspects.ts`。

## 背景一句话

统一对话 agent(TS 运行时)通过工具调用做交通事件检测;动态事件(违停 1 / 应急车道 2 / 逆行倒车 8)精度不足,需要一个"先定位疑似目标、再定向跟踪"的专用工具挂在 toolserver(Python)上,agent 自主决定何时调用。

## 核心流程(两段式)

1. **疑似定位(agent 侧)**:agent 初看整片(load_video 整段视频直传,或粗抽帧)后,给出 ≤5 个疑似目标锚点 `{box(0-1 归一化), timestamp, 自然语言描述}`,作为 track_suspects 的输入。不做全场景跟踪。
2. **定向跟踪(toolserver 侧,Python 确定性编排)**:
   - 可疑时段内 5~10fps 抽帧,滑窗 5 帧/次 VLM 调用(本地 qwen3.8-27b-fp8,OpenAI 兼容);窗 prompt 携带目标描述 + 上一窗框位,模型只更新这 ≤5 个目标的框(封闭任务);
   - 轨迹后处理三件套(旧代码直接复用,见下):IoU 段间缝合、匀速外推合并、断裂感知滑动平均平滑;
   - 每条轨迹算**数值档案**:位移矢量、方向角、平均速度、静止时长、bbox 面积变化趋势(靠近/远离镜头)、与环境流速(同片其他轨迹平均速度)对比。

## 场景先验(用户确认,高速固定规则,做成可配置)

- **左侧来向、右侧去向**:中国高速场景,相对摄像头永远左侧是来向车(应靠近镜头,**bbox 随时间增大**),右侧是去向车(应远离,**bbox 缩小**)。这是方向判定的基准,不用车流估计、不用标定。
- **目标在隔离带哪一侧**:由 VLM 看轨迹叠加图视觉判定,不用几何计算。
- **方向一致性数值初判** = 轨迹方向角 × 所在侧期望方向 + bbox 趋势旁证。
- **逆行 vs 倒车**:方向与期望相反后,按反向运动的持续距离/时长二分(长距持续 → 逆行;短距 → 倒车),阈值在几何层定。
- **违停 vs 拥堵**:目标静止但环境流速正常 → 违停(事件 1);环境流速≈0 → 拥堵(事件 6),不算违停。
- **互证防跑飞**:数值档案与轨迹图矛盾(如档案称静止但轨迹线很长)→ 判定该轨迹跟踪跑飞,剔除。

## 输出契约(工具 → agent)

```json
{
  "tracks": [{"id", "描述", "数值档案": {"方向角", "平均速度", "静止时长", "bbox趋势", "环境流速比"}, "所在侧(来/去)", "方向一致性初判"}],
  "annotated_image": "速度染色轨迹叠加图(红=静止/黄=缓行/绿=正常,带方向箭头+轨迹ID,jpeg dataURL)",
  "evidence_frames": [关键帧时间戳]
}
```

**agent 裁决契约**(写进系统 prompt):数值问题(停多久、方向角)只准引用数值档案;语义问题(车道归属、是否在应急车道)看叠加图;两者互证。裁决进 submit_detection(已支持逐事件 boxes + annotated_image 渲染)。

## 成本控制

只跑 agent 指定的可疑时段;车辆级目标 5fps 足够;目标 ≤5 个。一次定向跟踪约 10~25 次 VLM 调用(对比:全片 10fps 无差别跟踪 ≈ 50 次/20s),且仅在疑似时发生。

## 可复用旧代码(用户资产,已读)

位于 `/media/wanji/Elements/大模型应用/给房工急活20260820/pipeline/`:

- `track.py` — 10fps 抽帧 + 滑窗 prompt(窗 5 帧、stride 4、0-1000 归一化坐标)+ IoU 段间缝合(`_iou` 可直接抄)
- `merge_tracks.py` — 匀速外推轨迹缝合(gap≤20 帧、距离 <1.5×bbox 对角线)
- `smooth.py` — 断裂感知滑动平均(窗 5)
- `visualize.py` — bbox 插值渲染、轨迹拖尾、CSV 导出(轨迹叠加图的基础)
- `llm_client.py` — 重试 + 容错 JSON 提取的 VLM 客户端
- `lane_cv.py` — `median_frame()` 时域中位图(滤除运动车辆,后续车道线用)

旧实验教训(重要):全片无差别 10fps 跟踪被判"不可行"(小目标漏检、ID 跑飞);本方案刻意改为**按需、定向、≤5 目标、可疑时段限定**,车辆级近距离目标精度已验证可接受。

## 接入点(现有系统事实)

- toolserver:`traffic_analyzer/toolserver/server.py`,FastAPI,多允许根(POST /config/roots 热注册),现有端点 /tools/video_meta、extract_frames、draw_boxes、prepare_video;新工具按同一模式加端点。
- agent 工具层:`agent/src/tools/builtin/`,ExecutableTool 契约(resolveExecution/accesses/approvalRule),新工具注册进 registerBuiltinTools;工具描述进 agent/config/toolset.json。
- 输出渲染:工具结果 ContentPart[](text + image_url)前端工具气泡自动渲染;submit_detection 已支持 events[].boxes/box_frame → 服务端自动生成逐事件标注图。

## 未决问题(交给实现方决定)

1. 滑窗参数默认值(fps=5 还是 10、窗长 5、stride 4)是否按视频时长自适应;
2. 逆行/倒车的持续距离阈值具体取值(需样例视频标定);
3. 跟踪失败(ID 跑飞)的检测除互证外是否要数值自检(如轨迹瞬移超阈值自动断裂);
4. 车道线事件(应急车道 2、实线变道 11)后续接用户的生成式车道线模型,接口预留:extract_lane_lines → 车道几何,届时车道归属从"VLM 视觉判侧"升级为几何判定。

## v2 修订(2026-08-27 头脑风暴收敛,已实施)

1. **跟踪模式:混合式**。传播式为主(窗 prompt 带目标描述+上一窗框位);**每 5 窗强制 re-anchor**(重检测模式,与传播结果 IoU<0.3 判跑飞);**瞬移即时断裂**(相邻窗中心点跳变 >1.5×bbox 对角线)。原未决 3 升级为必做。
2. **环境流速来源:窗 prompt 扩展**。每窗顺带框 2-3 辆正常行驶参照车(不占 ≤5 目标预算、不做 ID 关联),位移中位数作环境流速。修复初稿"环境流速没有来源"的逻辑漏洞。
3. **窗参数**:默认 fps=5、窗 5 帧/stride 4(初稿成本账按 fps=10 会到 ~50 次调用/20s,超预算);高速运动目标自适应升 10fps。原未决 1 定案。
4. **数值档案**:位移/静止阈值一律按 bbox 对角线比例归一(不用绝对像素,防远处目标误判静止——违停误报重灾区);bbox 面积趋势仅为方向判定的最低权重旁证。互证规则具体化为:净位移≈0(绕圈漂移)或锚点声明静止但轨迹超长 → 跑飞。
5. **输出契约补充**:每条轨迹附 1-2 张 best-frame crop(直接作 submit_detection 的 boxes/box_frame 来源);跟踪失败(全跑飞/抽帧失败)明确返回 `failed:true`+原因,专家退回纯视觉判断,不硬给结论。
6. **debug bundle(给人看,不进对话上下文)**:`.agent/tracks/<stem>/<ts>/` 下——可疑时段跟踪叠加视频(逐帧框+ID+拖尾+时间戳+自检事件标记)、tracks.csv、windows.jsonl(每窗 VLM 请求/响应)、run.json(数值档案+运行参数快照)。前端工具气泡可播放叠加视频。
7. **缓存**:按 `(视频路径, 规范化锚点集合(按位置排序、描述不进键))` 缓存,事件 1/2 专家共享一次跟踪。
8. **超时**:agent 工具 `timeoutMs=900_000`(契约字段,同 spawn_subagent 模式);toolserver 端点同步 900s 总超时。
9. **vLLM prefix caching**:确认部署开启后,多轮 tool call 的前缀 prefill 由 vLLM APC 自动加速;`.vlm_cache.db` 应用层缓存保留(跨进程/重跑的完全相同请求),两者正交。
10. **配套改动**:submit_detection 定位框改「一律提供」(事件主体明确的框主体、场景级事件框大致范围),meta 两级 `missing_boxes`/`annotation_failed`;前端无框事件「未定位」标记。
11. **后续路线(未实施)**:批量专家 tool-enabled 循环(路线 A,抽帧 fps=1、循环 ≤10 次)时本工具经 toolserver 同一端点接入批量;事件 10(背景建模提名)、事件 11(车道几何)方案暂缓。原未决 2(逆行/倒车阈值)待样例视频标定,原未决 4(车道线模型)接口预留不变。
