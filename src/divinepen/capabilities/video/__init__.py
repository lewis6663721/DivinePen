"""视频生成能力（video.t2v / i2v / r2v / edit / all）。

P2 实现，对应 ``agents/docs/阿里系/`` 下的 wan2.7 ×4、wan3.0、happyhorse ×4。
全部为异步任务（提交 → 轮询 → 收尾），因此都必须实现"提交即落库"。
"""

__all__: list[str] = []
