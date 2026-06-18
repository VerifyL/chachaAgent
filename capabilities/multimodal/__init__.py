"""
capabilities/multimodal/ — 多模态能力预留目录（v1.5+）。

计划工具：
  - screenshot_parser  截图识别（OCR + 视觉 LLM 解析 UI / 图表 / 错误提示）
  - audio_transcriber  语音转文字（Whisper / 服务端 STT）
  - image_generator    文生图 / 图生图（DALL·E / SD）
  - video_analyzer     视频关键帧提取 + 摘要

当前: 预留目录，多模态模型接入后逐步实现。

TODO(v1.5): 接入多模态 LLM（gpt-4o / claude-sonnet-vision）
TODO(v1.5): 实现 BaseTool 子类（screenshot_parser / audio_transcriber）
TODO(v1.5): 实现 TokenCounter 多模态计数（图片 token 精确计算）
"""
