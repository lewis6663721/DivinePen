"""产物预览：生成出来的图/片/音必须能在详情页直接看见、听见。

这件事的难点不是 ``<video>`` 标签，而是**字节有没有在成功那一刻抓回本地**。
阿里系的文档写死了结果 URL 只活 24 小时：不落盘的话，第二天页面一片裂图，
而任务记录还喜气洋洋写着 SUCCEEDED —— 比失败更难查。

所以分三层钉住：
* ``_localize_artifacts``：落盘成功换成自家 ``/media/``；落盘失败退回购原地址
  并**留下会失效的提示**，且绝不因为抓不下来就把整个任务弄失败；
* ``_artifact_view``：修复前的老账目只有一串 URL，也得判出类型、出预览；
* ``/media/`` 出图口本身：拿得到、猜得中 MIME、挡得住目录穿越。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from divinepen.core.artifact import Artifact
from divinepen_server.db import SqliteStore
from divinepen_server.runtime import LocalArtifactStore
from divinepen_server.settings import get_settings
from divinepen_server.store import Repo, SecretBox
from divinepen_server.web import _artifact_view, _localize_artifacts

# ── 纯函数层：账目 → 页面视图 ─────────────────────────────────────


def test_legacy_url_only_row_still_gets_a_player():
    """修复前写下的账目只有 artifacts 一串 URL，历史任务不该因此白屏。"""
    view = _artifact_view({"artifacts": ["https://oss-24h.example/a.mp4"]})
    assert [item["kind"] for item in view] == ["video"]
    assert view[0]["stored"] is False
    assert "24 小时" in view[0]["note"], "没落盘的远端地址必须显式提示会失效"
    assert view[0]["url"] == "https://oss-24h.example/a.mp4"


def test_stored_detail_passes_through_untouched():
    detail = [
        {
            "name": "生成视频",
            "kind": "video",
            "url": "/media/ab/clip.mp4",
            "source_url": "https://oss-24h.example/a.mp4",
            "stored": True,
            "size": 2048,
        }
    ]
    view = _artifact_view({"artifacts": ["/media/ab/clip.mp4"], "artifacts_detail": detail})
    assert view == detail


def test_pipeline_artifacts_found_in_output_final_artifacts():
    view = _artifact_view({"output": {"final_artifacts": ["https://oss-24h.example/x.png"]}})
    assert [item["kind"] for item in view] == ["image"]


def test_single_step_artifact_found_in_output_uri():
    view = _artifact_view({"output": {"uri": "https://oss-24h.example/s.wav"}})
    assert [item["kind"] for item in view] == ["audio"]


def test_multi_artifacts_are_numbered():
    view = _artifact_view(
        {"artifacts": ["https://oss-24h.example/1.png", "https://oss-24h.example/2.png"]}
    )
    assert [item["name"] for item in view] == ["图片 1", "图片 2"]


# ── 落盘层：成功那一刻把字节抓回来 ────────────────────────────────


@pytest.fixture
def store(tmp_path) -> LocalArtifactStore:
    return LocalArtifactStore(tmp_path / "media")


@pytest.fixture
def instance(store) -> SimpleNamespace:
    """_localize_artifacts 只用到 instance.artifacts 的两个方法。"""
    return SimpleNamespace(artifacts=store)


def _remote_result(*uris: str, names: tuple[str | None, ...] = ()) -> SimpleNamespace:
    arts = [
        Artifact(uri=uri, name=(names[i] if i < len(names) else None)) for i, uri in enumerate(uris)
    ]
    return SimpleNamespace(artifacts=arts, output=None)


def _fake_fetch(store: LocalArtifactStore):
    async def fetch(uri, *, label=None, filename=None, kind=None, mime=None, timeout=120.0):
        local = await store.put(b"\x00\x00\x00\x18ftypmp42", kind=kind, name=filename, mime=mime)
        return local.model_copy(update={"name": label})

    return fetch


def test_localized_artifact_served_from_own_media_route(monkeypatch, instance, store):
    monkeypatch.setattr(store, "fetch", _fake_fetch(store))
    records = asyncio.run(
        _localize_artifacts(instance, "t_abc123", _remote_result("https://oss-24h.example/a.mp4"))
    )

    only = records[0]
    assert only["stored"] is True
    assert only["url"].startswith("/media/"), "页面必须指向本机，不能指向 24 小时就死的地址"
    assert only["url"].endswith(".mp4"), "无后缀的 mp4 会被当 octet-stream，<video> 不出画面"
    assert only["source_url"] == "https://oss-24h.example/a.mp4", "原地址留着备查"
    assert only["kind"] == "video"
    assert (store.root / Path(only["url"]).relative_to("/media")).is_file(), (
        "记录里的地址要真的存在"
    )


def test_download_failure_falls_back_to_source_with_expiry_note(monkeypatch, instance, store):
    async def boom(*args, **kwargs):
        raise RuntimeError("OSS 拉不动")

    monkeypatch.setattr(store, "fetch", boom)
    records = asyncio.run(
        _localize_artifacts(instance, "t_abc123", _remote_result("https://oss-24h.example/a.png"))
    )

    only = records[0]
    assert only["stored"] is False
    assert only["url"] == "https://oss-24h.example/a.png", "抓不下来也得让人能点开原地址"
    assert "24 小时" in only["note"]
    assert "OSS 拉不动" in only["note"], "失败原因要落到记录里，不然没人知道第二天为什么裂了"


def test_download_failure_does_not_fail_the_task(monkeypatch, instance, store):
    """抓不回产物是"预览缺一块"，不是"这次生成失败" —— 别把 SUCCEEDED 改成 FAILED。"""

    async def boom(*args, **kwargs):
        raise TimeoutError("拉超时")

    monkeypatch.setattr(store, "fetch", boom)
    records = asyncio.run(
        _localize_artifacts(instance, "t_abc123", _remote_result("https://oss-24h.example/a.mp4"))
    )
    assert len(records) == 1


def test_catalog_name_kept_auto_slug_becomes_chinese(monkeypatch, instance, store):
    """目录里写的 name（「生成视频」）是给人看的；``video_url-1`` 这种自动 slug 不是。"""
    monkeypatch.setattr(store, "fetch", _fake_fetch(store))
    records = asyncio.run(
        _localize_artifacts(
            instance,
            "t_abc123",
            _remote_result(
                "https://oss-24h.example/a.mp4",
                "https://oss-24h.example/b.mp4",
                names=("生成视频", "video_url-1"),
            ),
        )
    )
    assert records[0]["name"] == "生成视频 1"
    assert records[1]["name"] == "视频 2", "英文自动名用户读不懂，换中文"


def test_local_artifact_is_not_downloaded_again(monkeypatch, instance, store):
    async def must_not_call(*args, **kwargs):
        raise AssertionError("本地产物不该再去下载一次")

    monkeypatch.setattr(store, "fetch", must_not_call)
    records = asyncio.run(
        _localize_artifacts(instance, "t_abc123", _remote_result("/tmp/not-used.mp4"))
    )
    assert records[0]["source_url"] is None
    assert records[0]["stored"] is True


def test_fetch_downloads_real_bytes_over_http(tmp_path):
    """前面的落盘用例都 stub 了 fetch；这里用本机 HTTP 服务喂真字节。

    不受 stub 保护的那一段（httpx、Content-Type、后缀）才是真出事的地方：
    抓到是能抓，但如果后缀丢了，``<video>`` 依然播不了。
    """
    import threading
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    served = tmp_path / "served"
    served.mkdir()
    (served / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(served), **kwargs)

        def log_message(self, *args):  # 别把访问日志打到测试输出里
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        local_store = LocalArtifactStore(tmp_path / "media")
        url = f"http://127.0.0.1:{httpd.server_address[1]}/clip.mp4"
        got = asyncio.run(local_store.fetch(url, filename="t_x-1.mp4", label="生成视频"))

        assert got.path().read_bytes() == b"\x00\x00\x00\x18ftypmp42"
        assert got.name == "生成视频"
        assert got.meta["source_url"] == url
        assert local_store.url_for(got).endswith(".mp4")
        assert got.mime == "video/mp4", "MIME 从响应头带回来，页面才知道怎么播"
    finally:
        httpd.shutdown()


# ── 出图口：/media/ ───────────────────────────────────────────


@pytest.fixture(scope="module")
def client():
    from divinepen_server.web import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


def test_media_route_serves_artifact_with_right_mime(client):
    target = get_settings().media_root / "ab" / "clip.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x00\x00\x00\x18ftypmp42")

    resp = client.get("/media/ab/clip.mp4")
    assert resp.status_code == 200
    assert resp.content == b"\x00\x00\x00\x18ftypmp42"
    assert resp.headers["content-type"] == "video/mp4"
    assert "immutable" in resp.headers.get("cache-control", "")


def test_media_route_blocks_path_traversal(client):
    """``/media/../.env`` 就是一把钥匙，必须 404 而不是把仓库读出去。"""
    secret = get_settings().media_root.parent / "media-traversal-canary.txt"
    secret.write_text("不该被读到")
    try:
        for attempt in (
            "/media/..%2Fmedia-traversal-canary.txt",
            "/media/%2e%2e%2fmedia-traversal-canary.txt",
        ):
            resp = client.get(attempt)
            assert resp.status_code == 404, f"{attempt} 竟然返回了 {resp.status_code}"
            assert "不该被读到" not in resp.text
    finally:
        secret.unlink(missing_ok=True)


def test_media_route_404_for_missing_file(client):
    assert client.get("/media/nope/none.png").status_code == 404


# ── 端到端：详情页真的画出了播放器 ────────────────────────────────


def _insert_succeeded(result: dict, *, capability_id: str = "video.t2v", model: str = "wan2.7-t2v"):
    repo = Repo(
        SqliteStore(get_settings().db_dsn.removeprefix("sqlite://")),
        SecretBox("test-key-for-artifact-preview"),
    )
    return asyncio.run(
        repo.create_task(
            {
                "capability_id": capability_id,
                "status": "SUCCEEDED",
                "model": model,
                "result": result,
            }
        )
    )


def test_detail_page_renders_video_player(client):
    target = get_settings().media_root / "cd" / "clip.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x00\x00\x00\x18ftypmp42")

    task_id = _insert_succeeded(
        {
            "artifacts": ["/media/cd/clip.mp4"],
            "artifacts_detail": [
                {
                    "name": "生成视频",
                    "kind": "video",
                    "url": "/media/cd/clip.mp4",
                    "source_url": "https://oss-24h.example/a.mp4",
                    "stored": True,
                    "size": 24,
                }
            ],
        }
    )

    page = client.get(f"/tasks/{task_id}").text
    assert 'class="art-video"' in page
    assert 'src="/media/cd/clip.mp4"' in page
    assert "生成视频" in page
    assert "下载" in page, "结果得能存下来，光能看不够"


def test_detail_page_renders_image(client):
    target = get_settings().media_root / "ef" / "pic.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x89PNG\r\n\x1a\n")

    task_id = _insert_succeeded(
        {
            "artifacts": ["/media/ef/pic.png"],
            "artifacts_detail": [
                {"name": "生成图片", "kind": "image", "url": "/media/ef/pic.png", "stored": True}
            ],
        },
        capability_id="image.t2i",
        model="qwen-image-3.0",
    )

    page = client.get(f"/tasks/{task_id}").text
    assert 'class="art-img"' in page
    assert 'src="/media/ef/pic.png"' in page


def test_detail_page_warns_when_artifact_not_stored(client):
    task_id = _insert_succeeded(
        {"artifacts": ["https://oss-24h.example/a.png"]},
        capability_id="image.t2i",
        model="qwen-image-3.0",
    )
    page = client.get(f"/tasks/{task_id}").text
    assert "24 小时" in page
    assert 'class="art-card"' in page
