#!/usr/bin/env python3
"""底座冒烟脚本：证明"注册 → 目录 → dry-run → 真跑"四条路都活着。

    make smoke        # 或：PYTHONPATH=src python scripts/smoke.py

全程零成本：只跑本地 ffmpeg / ffprobe，不发任何供应商请求。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import anyio

import divinepen

LINE = "─" * 74


def banner(title: str) -> None:
    print(f"\n{LINE}\n  {title}\n{LINE}")


def make_sample_video(target: Path) -> bool:
    if shutil.which("ffmpeg") is None:
        return False
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=640x360:rate=25:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(target),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        print("  ! 造样例视频失败：", proc.stderr.strip()[-300:])
    return proc.returncode == 0 and target.is_file()


async def main() -> int:
    banner("1 / 4 · 能力注册表（内置能力 + 已安装插件自动发现）")
    registry = divinepen.registry()
    for info in registry.catalog():
        print(
            f"  {info['id']:<16} {info['name']:<12} category={info['category']:<7} "
            f"requires={info['requires'] or '-'}"
        )

    banner("2 / 4 · 表单 schema 由 Pydantic 派生（页面的控件来源）")
    form = registry.get("media.probe").form_schema()
    for group in form.groups:
        print(f"  [{group.key}] {group.label}")
        for field in group.fields:
            flags = " ".join(
                f for f, on in (("必填", field.required), ("计费", field.billed)) if on
            )
            print(f"      {field.key:<22} {field.label:<10} → {field.control:<12} {flags}")

    banner("3 / 4 · dry-run：把「将要执行什么」完全暴露出来，一分钱不花")
    workdir = Path(tempfile.mkdtemp(prefix="divinepen-smoke-"))
    sample = workdir / "sample.mp4"
    made = make_sample_video(sample)
    source = str(sample) if made else "https://example.com/demo.mp4"
    if not made:
        print("  · 未检测到可用的 ffmpeg，跳过真实执行，仅演示 dry-run")

    ctx_dry = divinepen.local_context(media_root=workdir / "media", dry_run=True)
    planned = await divinepen.run("media.probe", {"source": source}, ctx=ctx_dry)
    print(f"  status = {planned.status.value}")
    print(f"  摘要   = {planned.plan.describe()}")  # type: ignore[union-attr]
    for step in planned.plan.steps:  # type: ignore[union-attr]
        print(f"    · {step}")
    for req in planned.plan.requests:  # type: ignore[union-attr]
        print(f"    命令 → {' '.join(req.command or [])}")

    banner("4 / 4 · 真跑（免费 · 本地 · 同步）")
    if not made:
        return 0
    ctx_live = divinepen.local_context(media_root=workdir / "media")
    result = await divinepen.run("media.probe", {"source": source}, ctx=ctx_live)
    out = result.output
    print(f"  status      = {result.status.value}  ({result.elapsed_ms} ms)")
    print(f"  duration    = {out.duration}s")
    print(
        f"  resolution  = {out.width}x{out.height}  @ fps={out.streams[0].fps if out.streams else '-'}"
    )
    print(f"  video/audio = {out.video_codec} / {out.audio_codec}")
    print(f"  streams     = {[(s.index, s.codec_type, s.codec_name) for s in out.streams]}")
    print(f"  usage 记账  = {ctx_live.usage.records}  （免费能力不记账）")
    return 0


if __name__ == "__main__":
    raise SystemExit(anyio.run(main))
