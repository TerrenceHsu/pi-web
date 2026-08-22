"""Session file tools: list_files / view_file / write_file unit tests.

覆盖：
- list_files 返回当前 session 文件列表
- list_files 不返回 path
- list_files 在 file_store 未配置时返回 error
- view_file 读取 md / html / csv / parquet 文件
- view_file 对大文本截断
- view_file 对 PDF 返回元信息和未解析提示
- view_file 对图片返回 unsupported
- view_file 对二进制返回元信息
- view_file 不返回 path
- view_file 跨 session 返回 error
- view_file missing file 返回 error
- view_file max_bytes / max_rows 参数生效
- 工具结果 JSON serializable
- parquet 缺 pyarrow 时返回明确 error
"""
from __future__ import annotations

import io
import json
import sys

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from pi_agent_core_py.tools import (
    create_list_files_tool,
    create_view_file_tool,
    create_write_file_tool,
)
from pi_agent_core_py.web.files import VirtualFileStore

# ============================================================================
# fixtures
# ============================================================================


def _upload(content: bytes, filename: str = "test.txt", content_type: str | None = None):
    headers = Headers({"content-type": content_type} if content_type else {})
    return UploadFile(filename=filename, file=io.BytesIO(content), headers=headers)


@pytest.fixture
async def file_store(tmp_path):
    s = VirtualFileStore(tmp_path / "uploads")
    await s.init()
    return s


def _make_tool_pair(file_store, current_sid: str | None = "sess-1"):
    """构造 (list_files_tool, view_file_tool) 对，session 固定为 current_sid。"""
    state = {"sid": current_sid}

    def getter() -> str | None:
        return state["sid"]

    list_tool = create_list_files_tool(file_store=file_store, session_id_getter=getter)
    view_tool = create_view_file_tool(file_store=file_store, session_id_getter=getter)
    return list_tool, view_tool, state


async def _save(file_store, sid: str, content: bytes, name: str, mime: str | None = None):
    """保存一个文件，返回 FileRef。"""
    return await file_store.save(sid, _upload(content, name, mime))


def _make_write_tool(file_store, current_sid: str | None = "sess-1"):
    return create_write_file_tool(
        file_store=file_store,
        session_id_getter=lambda: current_sid,
    )


# ============================================================================
# write_file
# ============================================================================


@pytest.mark.asyncio
async def test_write_file_creates_file_for_current_session(file_store):
    write_tool = _make_write_tool(file_store)
    result = await write_tool.execute(
        "tc-write",
        {"filename": "agent-notes.md", "content": "# Notes\nCreated by agent."},
    )

    assert result.is_error is False
    assert result.details["name"] == "agent-notes.md"
    assert result.details["format"] == "markdown"
    assert "path" not in result.details
    assert "content" not in result.details

    ref = await file_store.get_for_session("sess-1", result.details["file_id"])
    assert __import__("pathlib").Path(ref.path).read_text(encoding="utf-8") == (
        "# Notes\nCreated by agent."
    )


@pytest.mark.asyncio
async def test_write_file_output_is_visible_to_list_and_view(file_store):
    write_tool = _make_write_tool(file_store)
    list_tool, view_tool, _ = _make_tool_pair(file_store)
    written = await write_tool.execute(
        "tc-write",
        {"filename": "answer.txt", "content": "session-local content"},
    )

    listed = await list_tool.execute("tc-list", {})
    assert [item["id"] for item in listed.details["files"]] == [
        written.details["file_id"]
    ]
    viewed = await view_tool.execute(
        "tc-view",
        {"file_id": written.details["file_id"]},
    )
    assert viewed.details["content"] == "session-local content"


@pytest.mark.asyncio
async def test_write_file_puts_code_in_scripts_and_reports_revision(file_store):
    write_tool = _make_write_tool(file_store)
    written = await write_tool.execute(
        "tc-code",
        {"filename": "main.py", "content": "print('ok')", "folder": "demo"},
    )

    assert written.details["logical_path"] == "scripts/demo/main.py"
    assert written.details["workspace_revision"] == 1
    list_tool, _, _ = _make_tool_pair(file_store)
    listed = await list_tool.execute("tc-list", {})
    assert listed.details["workspace_revision"] == 1


@pytest.mark.asyncio
async def test_write_file_rejects_invalid_arguments_and_missing_session(file_store):
    write_tool = _make_write_tool(file_store)
    assert (await write_tool.execute("tc-1", {})).details["error_type"] == (
        "InvalidArguments"
    )
    assert (
        await write_tool.execute("tc-2", {"filename": "x.txt", "content": 1})
    ).details["error_type"] == "InvalidArguments"

    no_session_tool = _make_write_tool(file_store, None)
    missing = await no_session_tool.execute(
        "tc-3",
        {"filename": "x.txt", "content": "x"},
    )
    assert missing.is_error is True
    assert missing.details["error_type"] == "NoActiveSession"


@pytest.mark.asyncio
async def test_write_file_isolated_to_active_session(file_store):
    result = await _make_write_tool(file_store, "sess-1").execute(
        "tc-write",
        {"filename": "private.txt", "content": "private"},
    )
    assert len(await file_store.list_session("sess-1")) == 1
    assert await file_store.list_session("sess-2") == []

    _, other_view, _ = _make_tool_pair(file_store, "sess-2")
    denied = await other_view.execute(
        "tc-view",
        {"file_id": result.details["file_id"]},
    )
    assert denied.is_error is True
    assert denied.details["error_type"] == "AccessDenied"


# ============================================================================
# list_files
# ============================================================================


@pytest.mark.asyncio
async def test_list_files_returns_current_session_files(file_store):
    await _save(file_store, "sess-1", b"hello", "a.txt")
    await _save(file_store, "sess-1", b"world", "b.csv", "text/csv")
    list_tool, _, _ = _make_tool_pair(file_store, "sess-1")

    result = await list_tool.execute("tc-1", {})
    assert not result.is_error
    files = result.details["files"]
    assert len(files) == 2
    names = [f["name"] for f in files]
    assert set(names) == {"a.txt", "b.csv"}
    # format 自动分类
    fmt_map = {f["name"]: f["format"] for f in files}
    assert fmt_map["a.txt"] == "text"
    assert fmt_map["b.csv"] == "csv"


@pytest.mark.asyncio
async def test_list_files_does_not_return_path(file_store):
    await _save(file_store, "sess-1", b"x", "a.txt")
    list_tool, _, _ = _make_tool_pair(file_store, "sess-1")

    result = await list_tool.execute("tc-1", {})
    for f in result.details["files"]:
        assert "path" not in f


@pytest.mark.asyncio
async def test_list_files_no_active_session_returns_error(file_store):
    list_tool, _, _ = _make_tool_pair(file_store, current_sid=None)

    result = await list_tool.execute("tc-1", {})
    assert result.is_error is True
    assert result.details["error_type"] == "NoActiveSession"


@pytest.mark.asyncio
async def test_list_files_isolated_by_session(file_store):
    """list_files 只列当前 session 的文件。"""
    await _save(file_store, "sess-1", b"a", "a.txt")
    await _save(file_store, "sess-2", b"b", "b.txt")
    list_tool, _, _ = _make_tool_pair(file_store, "sess-1")

    result = await list_tool.execute("tc-1", {})
    names = [f["name"] for f in result.details["files"]]
    assert names == ["a.txt"]


@pytest.mark.asyncio
async def test_list_files_image_marked_unsupported(file_store):
    await _save(file_store, "sess-1", b"\x89PNG fake", "x.png", "image/png")
    list_tool, _, _ = _make_tool_pair(file_store, "sess-1")

    result = await list_tool.execute("tc-1", {})
    f = result.details["files"][0]
    assert f["format"] == "image_unsupported"


# ============================================================================
# view_file: text / markdown
# ============================================================================


@pytest.mark.asyncio
async def test_view_file_reads_markdown(file_store):
    ref = await _save(file_store, "sess-1", b"# Title\n\nhello world", "notes.md")
    _, view_tool, _ = _make_tool_pair(file_store, "sess-1")

    result = await view_tool.execute("tc-1", {"file_id": ref.id})
    assert not result.is_error
    assert result.details["kind"] == "text"
    assert result.details["format"] == "markdown"
    assert result.details["name"] == "notes.md"
    assert "hello world" in result.details["content"]
    assert "path" not in result.details


@pytest.mark.asyncio
async def test_view_file_reads_text_with_code_extension(file_store):
    ref = await _save(file_store, "sess-1", b"print('hi')", "script.py")
    _, view_tool, _ = _make_tool_pair(file_store, "sess-1")

    result = await view_tool.execute("tc-1", {"file_id": ref.id})
    assert result.details["format"] == "text"
    assert "print('hi')" in result.details["content"]


@pytest.mark.asyncio
async def test_view_file_truncates_large_text(file_store):
    big = "x" * (200 * 1024)  # 200 KB
    ref = await _save(file_store, "sess-1", big.encode("utf-8"), "big.txt")
    _, view_tool, _ = _make_tool_pair(file_store, "sess-1")

    result = await view_tool.execute("tc-1", {
        "file_id": ref.id, "max_bytes": 1024,
    })
    assert result.details["truncated"] is True
    assert len(result.details["content"]) <= 1024


@pytest.mark.asyncio
async def test_view_file_max_bytes_param_clamps(file_store):
    ref = await _save(file_store, "sess-1", b"0123456789", "n.txt")
    _, view_tool, _ = _make_tool_pair(file_store, "sess-1")

    result = await view_tool.execute("tc-1", {
        "file_id": ref.id, "max_bytes": 5,
    })
    assert result.details["content"] == "01234"
    assert result.details["truncated"] is True


# ============================================================================
# view_file: html
# ============================================================================


@pytest.mark.asyncio
async def test_view_file_reads_html_and_extracts_text(file_store):
    html = (
        "<html><head><style>body{color:red}</style></head>"
        "<body><h1>Title</h1><p>Hello <b>world</b></p>"
        "<script>alert('ignored')</script></body></html>"
    )
    ref = await _save(file_store, "sess-1", html.encode("utf-8"), "page.html")
    _, view_tool, _ = _make_tool_pair(file_store, "sess-1")

    result = await view_tool.execute("tc-1", {"file_id": ref.id})
    assert result.details["kind"] == "html"
    assert result.details["format"] == "html"
    text = result.details["content_text"]
    assert "Title" in text
    assert "Hello" in text
    assert "world" in text
    # script 内容应被忽略
    assert "alert" not in text
    # style 内容应被忽略
    assert "color:red" not in text
    # 原始 excerpt 也保留
    assert "<html>" in result.details["content_html_excerpt"]


# ============================================================================
# view_file: csv
# ============================================================================


@pytest.mark.asyncio
async def test_view_file_reads_csv_columns_and_rows(file_store):
    csv_text = "a,b,c\n1,2,3\n4,5,6\n7,8,9\n"
    ref = await _save(file_store, "sess-1", csv_text.encode("utf-8"), "data.csv")
    _, view_tool, _ = _make_tool_pair(file_store, "sess-1")

    result = await view_tool.execute("tc-1", {"file_id": ref.id})
    assert result.details["kind"] == "table"
    assert result.details["format"] == "csv"
    assert result.details["columns"] == ["a", "b", "c"]
    assert len(result.details["rows"]) == 3
    assert result.details["rows"][0] == {"a": "1", "b": "2", "c": "3"}


@pytest.mark.asyncio
async def test_view_file_csv_max_rows_truncates(file_store):
    rows = "\n".join(f"{i},{i*2}" for i in range(20)) + "\n"
    csv_text = f"x,y\n{rows}"
    ref = await _save(file_store, "sess-1", csv_text.encode("utf-8"), "data.csv")
    _, view_tool, _ = _make_tool_pair(file_store, "sess-1")

    result = await view_tool.execute("tc-1", {
        "file_id": ref.id, "max_rows": 5,
    })
    assert len(result.details["rows"]) == 5
    assert result.details["truncated"] is True


@pytest.mark.asyncio
async def test_view_file_csv_empty_returns_empty_rows(file_store):
    """空 CSV（仅 header）不应崩。"""
    ref = await _save(file_store, "sess-1", b"h1,h2\n", "empty.csv")
    _, view_tool, _ = _make_tool_pair(file_store, "sess-1")

    result = await view_tool.execute("tc-1", {"file_id": ref.id})
    assert result.details["columns"] == ["h1", "h2"]
    assert result.details["rows"] == []


@pytest.mark.asyncio
async def test_view_file_csv_tsv_with_sniffer(file_store):
    """tab 分隔 CSV——Sniffer 推断 delimiter。"""
    tsv = "a\tb\n1\t2\n"
    ref = await _save(file_store, "sess-1", tsv.encode("utf-8"), "data.tsv")
    _, view_tool, _ = _make_tool_pair(file_store, "sess-1")

    result = await view_tool.execute("tc-1", {"file_id": ref.id})
    # Sniffer 不一定能推断 tab，但至少不崩
    assert result.details["kind"] == "table"
    assert len(result.details["columns"]) >= 1


# ============================================================================
# view_file: parquet
# ============================================================================


@pytest.fixture
def pyarrow_available():
    """pytest.importorskip 等价物——返回 pyarrow 模块或 skip。"""
    try:
        import pyarrow  # noqa: F401
    except ImportError:
        pytest.skip("pyarrow not installed")


def _make_parquet_bytes() -> bytes:
    """构造一个最小 parquet 文件 bytes。"""
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.table({
        "id": pa.array([1, 2, 3], type=pa.int64()),
        "name": pa.array(["a", "b", "c"], type=pa.string()),
    })
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_view_file_reads_parquet_schema_and_rows(file_store, pyarrow_available):
    raw = _make_parquet_bytes()
    ref = await _save(
        file_store, "sess-1", raw, "data.parquet",
        "application/vnd.apache.parquet",
    )
    _, view_tool, _ = _make_tool_pair(file_store, "sess-1")

    result = await view_tool.execute("tc-1", {"file_id": ref.id})
    assert result.details["kind"] == "table"
    assert result.details["format"] == "parquet"
    assert result.details["row_count"] == 3
    assert "id" in result.details["schema"]
    assert "name" in result.details["schema"]
    assert result.details["schema"]["id"] == "int64"
    assert result.details["schema"]["name"] == "string"
    assert len(result.details["rows"]) == 3
    assert result.details["rows"][0] == {"id": 1, "name": "a"}


@pytest.mark.asyncio
async def test_view_file_parquet_max_rows_truncates(file_store, pyarrow_available):
    raw = _make_parquet_bytes()  # 3 rows
    ref = await _save(file_store, "sess-1", raw, "data.parquet")
    _, view_tool, _ = _make_tool_pair(file_store, "sess-1")

    result = await view_tool.execute("tc-1", {
        "file_id": ref.id, "max_rows": 2,
    })
    assert len(result.details["rows"]) == 2
    assert result.details["truncated"] is True


@pytest.mark.asyncio
async def test_view_file_parquet_missing_pyarrow_returns_clear_error(
    file_store, monkeypatch,
):
    """模拟 pyarrow 缺失：patch sys.modules 让 import 失败。

    用一段 fake parquet bytes（不用 pyarrow 写真实文件）——本测试只关心
    view_file 内部 import pyarrow 失败时的错误路径。
    """
    raw = b"PAR1" + b"\x00" * 32  # 假装是 parquet；只要分类成 parquet 即可
    ref = await _save(
        file_store, "sess-1", raw, "data.parquet",
        "application/vnd.apache.parquet",
    )
    _, view_tool, _ = _make_tool_pair(file_store, "sess-1")

    # 强制让 view_file 内部 import pyarrow 失败
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    monkeypatch.setitem(sys.modules, "pyarrow.parquet", None)

    result = await view_tool.execute("tc-1", {"file_id": ref.id})
    assert result.is_error is True
    assert result.details["error_type"] == "PyArrowNotAvailable"
    assert "pyarrow" in result.content[0].text.lower()


# ============================================================================
# view_file: pdf / image / binary / unsupported
# ============================================================================


@pytest.mark.asyncio
async def test_view_file_pdf_returns_unparsed_message(file_store):
    ref = await _save(
        file_store, "sess-1", b"%PDF-1.4 fake pdf", "doc.pdf", "application/pdf",
    )
    _, view_tool, _ = _make_tool_pair(file_store, "sess-1")

    result = await view_tool.execute("tc-1", {"file_id": ref.id})
    assert result.details["kind"] == "pdf"
    assert result.details["format"] == "pdf"
    assert "未解析" in result.details["message"]


@pytest.mark.asyncio
async def test_view_file_image_returns_unsupported(file_store):
    ref = await _save(
        file_store, "sess-1", b"\x89PNG fake", "img.png", "image/png",
    )
    _, view_tool, _ = _make_tool_pair(file_store, "sess-1")

    result = await view_tool.execute("tc-1", {"file_id": ref.id})
    assert result.details["kind"] == "unsupported"
    assert result.details["format"] == "image_unsupported"
    assert "不支持图片" in result.details["message"]
    assert "path" not in result.details


@pytest.mark.asyncio
async def test_view_file_binary_returns_metadata_only(file_store):
    """octet-stream + 未知扩展名 → binary / unsupported。"""
    ref = await _save(
        file_store, "sess-1", b"\x00\x01\x02\x03 binary", "blob.dat",
        "application/octet-stream",
    )
    _, view_tool, _ = _make_tool_pair(file_store, "sess-1")

    result = await view_tool.execute("tc-1", {"file_id": ref.id})
    assert result.details["kind"] in ("binary", "unsupported")
    assert "message" in result.details
    assert "path" not in result.details


# ============================================================================
# view_file: errors
# ============================================================================


@pytest.mark.asyncio
async def test_view_file_missing_file_returns_error(file_store):
    _, view_tool, _ = _make_tool_pair(file_store, "sess-1")

    result = await view_tool.execute("tc-1", {"file_id": "ghost-id"})
    assert result.is_error is True
    assert result.details["error_type"] == "FileNotFound"


@pytest.mark.asyncio
async def test_view_file_cross_session_returns_error(file_store):
    ref = await _save(file_store, "sess-1", b"x", "a.txt")
    _, view_tool, _ = _make_tool_pair(file_store, "sess-2")

    result = await view_tool.execute("tc-1", {"file_id": ref.id})
    assert result.is_error is True
    assert result.details["error_type"] == "AccessDenied"


@pytest.mark.asyncio
async def test_view_file_invalid_arguments_returns_error(file_store):
    _, view_tool, _ = _make_tool_pair(file_store, "sess-1")

    result = await view_tool.execute("tc-1", {})
    assert result.is_error is True
    assert result.details["error_type"] == "InvalidArguments"


@pytest.mark.asyncio
async def test_view_file_no_active_session_returns_error(file_store):
    _, view_tool, _ = _make_tool_pair(file_store, current_sid=None)

    result = await view_tool.execute("tc-1", {"file_id": "any"})
    assert result.is_error is True
    assert result.details["error_type"] == "NoActiveSession"


# ============================================================================
# JSON serializable
# ============================================================================


@pytest.mark.asyncio
async def test_view_file_result_is_json_serializable(file_store, pyarrow_available):
    """工具 details 必须可 JSON 序列化（包含 parquet 的特殊类型）。"""
    raw = _make_parquet_bytes()
    ref = await _save(file_store, "sess-1", raw, "data.parquet")
    _, view_tool, _ = _make_tool_pair(file_store, "sess-1")

    result = await view_tool.execute("tc-1", {"file_id": ref.id})
    # model_dump → JSON
    s = json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
    assert isinstance(s, str)


@pytest.mark.asyncio
async def test_list_files_result_is_json_serializable(file_store):
    await _save(file_store, "sess-1", b"x", "a.txt")
    list_tool, _, _ = _make_tool_pair(file_store, "sess-1")

    result = await list_tool.execute("tc-1", {})
    s = json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
    assert isinstance(s, str)


# ============================================================================
# format classification sanity
# ============================================================================


def test_classify_format_known_types():
    from pi_agent_core_py.tools.view_file import _classify_format

    assert _classify_format("a.md", "text/markdown") == "markdown"
    assert _classify_format("a.html", "text/html") == "html"
    assert _classify_format("a.csv", "text/csv") == "csv"
    assert _classify_format("a.parquet", "application/octet-stream") == "parquet"
    assert _classify_format("a.png", "image/png") == "image_unsupported"
    assert _classify_format("a.pdf", "application/pdf") == "pdf"
    assert _classify_format("a.txt", "text/plain") == "text"
    assert _classify_format("a.py", "text/x-python") == "text"
    assert _classify_format("a.json", "application/json") == "text"
    assert _classify_format("a.unknown", "application/x-strange") == "unsupported"
