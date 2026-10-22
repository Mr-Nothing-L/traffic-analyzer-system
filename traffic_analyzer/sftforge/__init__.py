"""SFT Forge — 基于文件名真值的多轮纠错 SFT label 生成器。

[文件说明]
作用:对视频本体直接 VLM 推理生成 <think>/<answer> 训练样本,与文件名前缀
  真值 diff 后做双向纠错(漏检 correct_missing 定向重推、误报 review_extra
  复核剔除),段落级合并得到最终 SFT label;仍不一致的样本隔离 quarantine。
上游:scripts/sft_forge.py(CLI);后续 M2 的 web 层复用 SftForgeEngine。
下游:core/vlm_engine.py 的 provider 配置体系、core/vlm_cache.py 磁盘缓存、
  evaluation/extract_gt_from_filename 真值解析、config/prompts/sft_forge.yaml
  三个 prompt 模板(forward/correct_missing/review_extra)。
设计文档:docs/superpowers/specs/2026-10-22-sft-forge-design.md。
"""

from traffic_analyzer.sftforge.models import RoundTrace, VideoForgeResult

__all__ = ["RoundTrace", "VideoForgeResult"]
