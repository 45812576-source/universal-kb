"""BitableReader 核心故障场景测试 — 覆盖自适应降级、最终失败、知识库 fallback。"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from app.services.bitable_reader import BitableReader, BitableRecordError
from app.services.lark_doc_importer import LarkDocImporter


@pytest.fixture
def reader():
    return BitableReader()


# ── fetch_records_adaptive: 降级成功 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_adaptive_degrade_success(reader):
    """page_size=500 失败 → 降到 100 成功，返回完整数据。"""
    call_count = 0

    async def mock_fetch_page(token, app_token, table_id, page_size, page_token=None, since_ts=None):
        nonlocal call_count
        call_count += 1
        if page_size == 500:
            raise BitableRecordError("Something went wrong", feishu_code=99916000, feishu_msg="Something went wrong")
        # page_size=100, 返回 2 页
        if page_token is None:
            return {
                "items": [{"record_id": f"r{i}", "fields": {"name": f"n{i}"}} for i in range(100)],
                "has_more": True,
                "page_token": "page2",
            }
        else:
            return {
                "items": [{"record_id": f"r{i}", "fields": {"name": f"n{i}"}} for i in range(100, 150)],
                "has_more": False,
            }

    reader.fetch_records_page = mock_fetch_page
    records, stats = await reader.fetch_records_adaptive("tok", "app", "tbl")

    assert len(records) == 150
    assert stats["degraded"] is True
    assert stats["effective_page_size"] == 100
    assert len(stats["errors"]) == 1
    assert stats["errors"][0]["from"] == 500
    assert stats["errors"][0]["to"] == 100


# ── fetch_records_adaptive: 所有档位失败 → 抛异常 ────────────────────────


@pytest.mark.asyncio
async def test_adaptive_all_fail_raises(reader):
    """所有 page_size 都失败时，必须抛 BitableRecordError，不返回部分数据。"""

    async def mock_fetch_page(token, app_token, table_id, page_size, page_token=None, since_ts=None):
        raise BitableRecordError(
            f"fail at {page_size}",
            feishu_code=99916000,
            feishu_msg="Something went wrong",
        )

    reader.fetch_records_page = mock_fetch_page

    with pytest.raises(BitableRecordError, match="所有分页档位均失败"):
        await reader.fetch_records_adaptive("tok", "app", "tbl")


# ── fetch_records_adaptive: 中途失败（前几页成功后失败）→ 抛异常 ─────────


@pytest.mark.asyncio
async def test_adaptive_midway_fail_raises(reader):
    """前 2 页成功，第 3 页所有档位都失败 → 抛异常，不返回部分数据。"""
    page_num = 0

    async def mock_fetch_page(token, app_token, table_id, page_size, page_token=None, since_ts=None):
        nonlocal page_num
        # 第 3 页（page_token="page3"）全部档位失败
        if page_token == "page3":
            raise BitableRecordError(
                f"fail at page3 size={page_size}",
                feishu_code=99916000,
                feishu_msg="Something went wrong",
            )
        page_num += 1
        if page_num == 1:
            return {
                "items": [{"record_id": "r1", "fields": {}}],
                "has_more": True,
                "page_token": "page2",
            }
        else:
            return {
                "items": [{"record_id": "r2", "fields": {}}],
                "has_more": True,
                "page_token": "page3",
            }

    reader.fetch_records_page = mock_fetch_page

    with pytest.raises(BitableRecordError, match="所有分页档位均失败"):
        await reader.fetch_records_adaptive("tok", "app", "tbl")


# ── fetch_records_adaptive: 中途降级成功 → 正常返回 ──────────────────────


@pytest.mark.asyncio
async def test_adaptive_midway_degrade_success(reader):
    """前 2 页 page_size=500 成功，第 3 页 500 失败 → 降到 100 成功。"""
    call_count = 0

    async def mock_fetch_page(token, app_token, table_id, page_size, page_token=None, since_ts=None):
        nonlocal call_count
        call_count += 1
        if page_token == "page3" and page_size == 500:
            raise BitableRecordError("fail", feishu_code=99916000, feishu_msg="fail")
        if page_token is None:
            return {"items": [{"record_id": "r1", "fields": {}}], "has_more": True, "page_token": "page2"}
        if page_token == "page2":
            return {"items": [{"record_id": "r2", "fields": {}}], "has_more": True, "page_token": "page3"}
        if page_token == "page3":
            return {"items": [{"record_id": "r3", "fields": {}}], "has_more": False}
        return {"items": [], "has_more": False}

    reader.fetch_records_page = mock_fetch_page
    records, stats = await reader.fetch_records_adaptive("tok", "app", "tbl")

    assert len(records) == 3
    assert stats["degraded"] is True


# ── fetch_table_list ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_table_list(reader):
    """fetch_table_list 返回正确的表列表。"""
    import httpx

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "code": 0,
        "data": {
            "items": [
                {"table_id": "tbl1", "name": "表一"},
                {"table_id": "tbl2", "name": "表二"},
            ]
        }
    }

    with patch("app.services.bitable_reader.httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = mock_client

        tables = await reader.fetch_table_list("tok", "app123")
        assert len(tables) == 2
        assert tables[0]["table_id"] == "tbl1"
        assert tables[1]["name"] == "表二"


# ── flatten_value ────────────────────────────────────────────────────────


class TestFlattenValue:

    def test_none(self):
        assert BitableReader.flatten_value(None) is None

    def test_text_list(self):
        v = [{"text": "hello "}, {"text": "world"}]
        assert BitableReader.flatten_value(v) == "hello world"

    def test_dict_text(self):
        assert BitableReader.flatten_value({"text": "abc"}) == "abc"

    def test_plain_value(self):
        assert BitableReader.flatten_value(42) == 42
        assert BitableReader.flatten_value("str") == "str"

    def test_dict_json(self):
        import json
        v = {"link_record_ids": ["r1", "r2"]}
        result = BitableReader.flatten_value(v)
        assert json.loads(result) == v


# ── sanitize_col ─────────────────────────────────────────────────────────


class TestSanitizeCol:

    def test_chinese(self):
        assert BitableReader.sanitize_col("字段名") == "字段名"

    def test_special_chars(self):
        assert BitableReader.sanitize_col("my-field (1)") == "my_field__1_"


# ── 知识库 bitable fallback: 缺 table_id 自动列表 ───────────────────────


@pytest.mark.asyncio
async def test_bitable_fallback_auto_resolve_table_id():
    """缺 table_id 时应自动调用 fetch_table_list 选第一个表。"""
    importer = LarkDocImporter()

    mock_reader = MagicMock()
    mock_reader.get_token = AsyncMock(return_value="fake_token")
    mock_reader.fetch_table_list = AsyncMock(return_value=[
        {"table_id": "tblABC", "name": "自动选中表"},
    ])
    mock_reader.fetch_fields = AsyncMock(return_value=[
        {"field_name": "名称", "type": 1},
    ])
    mock_reader.fetch_records_adaptive = AsyncMock(return_value=(
        [{"record_id": "r1", "fields": {"名称": [{"text": "值1"}]}}],
        {"effective_page_size": 500, "degraded": False, "errors": [], "total_records": 1},
    ))
    mock_reader.records_to_html_table = MagicMock(return_value="<table><tr><th>名称</th></tr></table>")
    mock_reader.records_to_text = MagicMock(return_value="名称\n---\n值1")

    mock_db = MagicMock()
    mock_db.add = MagicMock()
    mock_db.flush = MagicMock()
    mock_user = MagicMock(id=1, department_id=1)

    # patch at module level so the local import inside the method picks up the mock
    import app.services.bitable_reader as br_module
    original = br_module.bitable_reader
    br_module.bitable_reader = mock_reader

    try:
        with patch.object(importer, "_build_entry") as mock_build, \
             patch.object(importer, "_ai_enrich", new_callable=AsyncMock) as mock_enrich, \
             patch.object(importer, "_enqueue_jobs") as mock_enqueue:

            mock_entry = MagicMock()
            mock_entry.content = "test"
            mock_build.return_value = mock_entry

            # Also patch the local import of submit_knowledge
            with patch("app.services.knowledge_service.submit_knowledge", return_value=mock_entry):
                result = await importer._bitable_records_fallback(
                    mock_db, mock_user, "https://x.feishu.cn/base/appXYZ",
                    "appXYZ", "bitable", None, None, None, "experience",
                    {},  # extra_params 无 table_id
                    None,  # access_token
                    export_error=RuntimeError("export failed"),
                )

            # 验证调用了 fetch_table_list
            mock_reader.fetch_table_list.assert_awaited_once_with("fake_token", "appXYZ")
            # 验证用了自动选中的 table_id
            mock_reader.fetch_fields.assert_awaited_once_with("fake_token", "appXYZ", "tblABC")
            mock_reader.fetch_records_adaptive.assert_awaited_once_with("fake_token", "appXYZ", "tblABC")
    finally:
        br_module.bitable_reader = original


# ── parse_lark_url: bitable 额外参数 ────────────────────────────────────


class TestParseLarkUrlExtra:

    def test_bitable_with_table_param(self):
        importer = LarkDocImporter()
        token, api_type, extra = importer.parse_lark_url(
            "https://abc.feishu.cn/base/AppToken123?table=tblXYZ"
        )
        assert token == "AppToken123"
        assert api_type == "bitable"
        assert extra == {"table_id": "tblXYZ"}

    def test_bitable_without_table_param(self):
        importer = LarkDocImporter()
        token, api_type, extra = importer.parse_lark_url(
            "https://abc.feishu.cn/base/AppToken123"
        )
        assert token == "AppToken123"
        assert api_type == "bitable"
        assert extra == {}

    def test_non_bitable_no_extra(self):
        importer = LarkDocImporter()
        token, api_type, extra = importer.parse_lark_url(
            "https://abc.feishu.cn/docx/DocToken123"
        )
        assert api_type == "docx"
        assert extra == {}
