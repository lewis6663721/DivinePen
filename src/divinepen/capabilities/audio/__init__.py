"""音频能力：一期**故意留空**。

阿里系 TTS / ASR 走 WebSocket 流式（``/api-ws/v1/inference``），不是 HTTP
请求-响应，硬套 ``ApiSpec(mode="sync")`` 会是错的。模型在目录里登记了但标
"未接入"，能力等传输层实现后再加 —— 宁可缺，不要错。
"""
