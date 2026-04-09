"""飞书文档导入器单元测试 — 覆盖 URL 解析、类型映射、策略分发。"""
import pytest
from app.services.lark_doc_importer import (
    LarkDocImporter,
    _LARK_URL_RE,
    _LARK_WIKI_RE,
    _URL_PATH_TO_API_TYPE,
    _EXPORTABLE_TYPES,
    _DIRECT_DOWNLOAD_TYPES,
    _EXPORT_EXT_MAP,
    _normalize_type,
)


@pytest.fixture
def importer():
    return LarkDocImporter()


# ── URL 正则测试 ─────────────────────────────────────────────────────────


class TestUrlRegex:
    """_LARK_URL_RE 应匹配所有已知飞书 URL 路径格式。"""

    @pytest.mark.parametrize("url,expected_type,expected_token", [
        # 基本文档类型
        ("https://abc.feishu.cn/docx/AbcDef123", "docx", "AbcDef123"),
        ("https://abc.feishu.cn/doc/AbcDef123", "doc", "AbcDef123"),
        ("https://abc.feishu.cn/sheets/AbcDef123", "sheets", "AbcDef123"),
        ("https://abc.feishu.cn/sheet/AbcDef123", "sheet", "AbcDef123"),
        ("https://abc.feishu.cn/base/AbcDef123", "base", "AbcDef123"),
        ("https://abc.feishu.cn/bitable/AbcDef123", "bitable", "AbcDef123"),
        ("https://abc.feishu.cn/file/AbcDef123", "file", "AbcDef123"),
        # 不可导出类型
        ("https://abc.feishu.cn/slides/AbcDef123", "slides", "AbcDef123"),
        ("https://abc.feishu.cn/mindnote/AbcDef123", "mindnote", "AbcDef123"),
        ("https://abc.feishu.cn/board/AbcDef123", "board", "AbcDef123"),
        ("https://abc.feishu.cn/minutes/AbcDef123", "minutes", "AbcDef123"),
        ("https://abc.feishu.cn/survey/AbcDef123", "survey", "AbcDef123"),
        # drive 路径已移除（/drive/folder/xxx 是文件夹，不是文件）
        # larksuite 域名
        ("https://abc.larksuite.com/docx/Token123", "docx", "Token123"),
        ("https://abc.larksuite.com/sheets/Token123", "sheets", "Token123"),
        # wiki 套壳（wiki/TYPE/TOKEN 格式）
        ("https://abc.feishu.cn/wiki/docx/AbcDef123", "docx", "AbcDef123"),
        ("https://abc.feishu.cn/wiki/sheets/AbcDef123", "sheets", "AbcDef123"),
        # 带路径后缀的 URL
        ("https://abc.feishu.cn/docx/AbcDef123?query=1", "docx", "AbcDef123"),
        # token 含下划线和连字符
        ("https://abc.feishu.cn/docx/Abc_Def-123", "docx", "Abc_Def-123"),
    ])
    def test_matches_known_urls(self, url, expected_type, expected_token):
        m = _LARK_URL_RE.search(url)
        assert m is not None, f"未匹配: {url}"
        assert m.group("type") == expected_type
        assert m.group("token") == expected_token

    def test_pure_wiki_url(self):
        """纯 wiki 链接（/wiki/TOKEN）应由 _LARK_WIKI_RE 匹配。"""
        url = "https://abc.feishu.cn/wiki/AbcDef123"
        # 主正则也会匹配 wiki/docx 等，但纯 wiki 链接靠 _LARK_WIKI_RE
        m = _LARK_WIKI_RE.search(url)
        assert m is not None
        assert m.group("token") == "AbcDef123"

    def test_drive_folder_not_matched(self):
        """文件夹链接不应被主正则匹配。"""
        assert _LARK_URL_RE.search("https://abc.feishu.cn/drive/folder/abc123") is None

    def test_invalid_url_no_match(self):
        """非飞书链接不应匹配。"""
        assert _LARK_URL_RE.search("https://google.com/docx/abc") is None
        assert _LARK_URL_RE.search("https://feishu.cn/unknown_path/abc") is None


# ── 类型映射测试 ─────────────────────────────────────────────────────────


class TestTypeMapping:
    """_normalize_type 应将 URL 路径名正确映射为 API 类型名。"""

    @pytest.mark.parametrize("url_type,api_type", [
        ("sheets", "sheet"),
        ("base", "bitable"),
        ("form", "survey"),
        ("docx", "docx"),
        ("doc", "doc"),
        ("file", "file"),
        ("slides", "slides"),
        ("mindnote", "mindnote"),
    ])
    def test_normalize(self, url_type, api_type):
        assert _normalize_type(url_type) == api_type

    def test_unknown_type_passthrough(self):
        """未知类型应原样返回。"""
        assert _normalize_type("unknown_xyz") == "unknown_xyz"


# ── parse_lark_url 测试 ─────────────────────────────────────────────────


class TestParseLarkUrl:

    def test_docx_url(self, importer):
        token, api_type, _ = importer.parse_lark_url("https://abc.feishu.cn/docx/AbcDef123")
        assert token == "AbcDef123"
        assert api_type == "docx"

    def test_sheets_url_normalizes(self, importer):
        """sheets URL 应归一化为 sheet。"""
        token, api_type, _ = importer.parse_lark_url("https://abc.feishu.cn/sheets/Token123")
        assert api_type == "sheet"

    def test_base_url_normalizes(self, importer):
        """base URL 应归一化为 bitable。"""
        token, api_type, _ = importer.parse_lark_url("https://abc.feishu.cn/base/Token123")
        assert api_type == "bitable"

    def test_pure_wiki_url(self, importer):
        token, api_type, _ = importer.parse_lark_url("https://abc.feishu.cn/wiki/WikiToken123")
        assert token == "WikiToken123"
        assert api_type == "wiki"

    def test_share_form_url(self, importer):
        """问卷分享链接应解析为 survey。"""
        token, api_type, _ = importer.parse_lark_url(
            "https://qnyspu28uo.feishu.cn/share/base/form/shrcnEppSqiaiCN"
        )
        assert token == "shrcnEppSqiaiCN"
        assert api_type == "survey"

    def test_drive_folder_rejected(self, importer):
        """文件夹链接应明确拒绝。"""
        with pytest.raises(ValueError, match="文件夹不支持"):
            importer.parse_lark_url("https://abc.feishu.cn/drive/folder/abc123")

    def test_invalid_url_raises(self, importer):
        with pytest.raises(ValueError, match="无法解析飞书链接"):
            importer.parse_lark_url("https://google.com/doc/abc")

    def test_invalid_url_friendly_message(self, importer):
        with pytest.raises(ValueError, match="支持的格式"):
            importer.parse_lark_url("not-a-url")


# ── 策略分类测试 ─────────────────────────────────────────────────────────


class TestStrategyClassification:
    """验证各类型分到正确的策略。"""

    def test_exportable_types(self):
        assert _EXPORTABLE_TYPES == {"doc", "docx", "sheet", "bitable"}

    def test_direct_download_types(self):
        assert _DIRECT_DOWNLOAD_TYPES == {"file"}

    def test_non_exportable_not_in_either(self):
        """mindnote/slides 等既不在可导出也不在直接下载集合中 → 走策略C。"""
        for t in ("mindnote", "slides", "board", "minutes", "survey"):
            assert t not in _EXPORTABLE_TYPES
            assert t not in _DIRECT_DOWNLOAD_TYPES

    def test_export_ext_map_keys_are_api_types(self):
        """_EXPORT_EXT_MAP 的 key 应该是 API 类型名，不是 URL 路径名。"""
        assert "sheets" not in _EXPORT_EXT_MAP  # URL 路径名不应出现
        assert "base" not in _EXPORT_EXT_MAP
        assert "sheet" in _EXPORT_EXT_MAP
        assert "bitable" in _EXPORT_EXT_MAP


# ── 策略分发集成测试（mock API 调用）────────────────────────────────────


class TestStrategyDispatch:
    """验证 import_doc 根据类型调用正确的策略方法。"""

    @pytest.mark.asyncio
    async def test_exportable_type_calls_strategy_export(self, importer, monkeypatch):
        """docx 链接应走策略A。"""
        called = {}

        async def mock_export(self, *args, **kwargs):
            called["export"] = True
            return "entry_a"

        monkeypatch.setattr(LarkDocImporter, "_strategy_export", mock_export)
        monkeypatch.setattr(LarkDocImporter, "_strategy_download", lambda *a, **k: None)
        monkeypatch.setattr(LarkDocImporter, "_strategy_link_reference", lambda *a, **k: None)

        result = await importer.import_doc(
            db=None, user=None, url="https://abc.feishu.cn/docx/Token123"
        )
        assert called.get("export") is True
        assert result == "entry_a"

    @pytest.mark.asyncio
    async def test_file_type_calls_strategy_download(self, importer, monkeypatch):
        """file 链接应走策略B。"""
        called = {}

        async def mock_download(self, *args, **kwargs):
            called["download"] = True
            return "entry_b"

        monkeypatch.setattr(LarkDocImporter, "_strategy_export", lambda *a, **k: None)
        monkeypatch.setattr(LarkDocImporter, "_strategy_download", mock_download)
        monkeypatch.setattr(LarkDocImporter, "_strategy_link_reference", lambda *a, **k: None)

        result = await importer.import_doc(
            db=None, user=None, url="https://abc.feishu.cn/file/Token123"
        )
        assert called.get("download") is True
        assert result == "entry_b"

    @pytest.mark.asyncio
    async def test_slides_type_calls_strategy_link_reference(self, importer, monkeypatch):
        """slides 链接应走策略C。"""
        called = {}

        async def mock_link_ref(self, *args, **kwargs):
            called["link_ref"] = True
            return "entry_c"

        monkeypatch.setattr(LarkDocImporter, "_strategy_export", lambda *a, **k: None)
        monkeypatch.setattr(LarkDocImporter, "_strategy_download", lambda *a, **k: None)
        monkeypatch.setattr(LarkDocImporter, "_strategy_link_reference", mock_link_ref)

        result = await importer.import_doc(
            db=None, user=None, url="https://abc.feishu.cn/slides/Token123"
        )
        assert called.get("link_ref") is True
        assert result == "entry_c"

    @pytest.mark.asyncio
    async def test_wiki_resolves_then_dispatches(self, importer, monkeypatch):
        """wiki 链接应先解析 wiki node，再根据 obj_type 分发。"""
        called = {}

        async def mock_get_wiki_node(token):
            called["wiki"] = True
            return {"obj_token": "RealToken456", "obj_type": "sheet", "title": "Wiki表格"}

        async def mock_export(self, *args, **kwargs):
            called["export"] = True
            # 验证 token 已被替换为 obj_token
            # args: db, user, url, token, api_type, title, wiki_title, folder_id, category
            assert args[3] == "RealToken456"  # token
            assert args[4] == "sheet"  # api_type
            assert args[6] == "Wiki表格"  # wiki_title
            return "entry_wiki"

        # Mock lark_client.get_wiki_node
        import app.services.lark_doc_importer as mod
        monkeypatch.setattr(LarkDocImporter, "_strategy_export", mock_export)

        # 需要 mock lark_client 的导入
        class FakeLarkClient:
            async def get_wiki_node(self, token):
                return await mock_get_wiki_node(token)

        monkeypatch.setattr(mod, "lark_client", FakeLarkClient(), raising=False)
        # 由于 import_doc 里是 from ... import lark_client，需要在函数内 mock
        # 改用 monkeypatch 在 module 级别 mock

        # 实际上 import_doc 里是 lazy import，我们需要 mock 模块
        import sys
        from app.services.lark_client import LarkPermissionError
        fake_client_module = type(sys)("fake_lark_client")
        fake_client_module.lark_client = FakeLarkClient()
        fake_client_module.LarkPermissionError = LarkPermissionError
        monkeypatch.setitem(sys.modules, "app.services.lark_client", fake_client_module)

        result = await importer.import_doc(
            db=None, user=None, url="https://abc.feishu.cn/wiki/WikiToken789"
        )
        assert called.get("wiki") is True
        assert called.get("export") is True
        assert result == "entry_wiki"


# ── sheets → sheet, base → bitable 端到端验证 ───────────────────────────


class TestEndToEndTypeNormalization:
    """确保 URL 中的路径名在整个链路中正确归一化。"""

    @pytest.mark.asyncio
    async def test_sheets_url_passes_sheet_to_export(self, importer, monkeypatch):
        """sheets URL 传给 export API 的 type 应该是 sheet。"""
        captured_api_type = {}

        async def mock_export(self, db, user, url, token, api_type, *args, **kwargs):
            captured_api_type["value"] = api_type
            return "entry"

        monkeypatch.setattr(LarkDocImporter, "_strategy_export", mock_export)

        await importer.import_doc(
            db=None, user=None, url="https://abc.feishu.cn/sheets/Token123"
        )
        assert captured_api_type["value"] == "sheet"

    @pytest.mark.asyncio
    async def test_base_url_passes_bitable_to_export(self, importer, monkeypatch):
        """base URL 传给 export API 的 type 应该是 bitable。"""
        captured_api_type = {}

        async def mock_export(self, db, user, url, token, api_type, *args, **kwargs):
            captured_api_type["value"] = api_type
            return "entry"

        monkeypatch.setattr(LarkDocImporter, "_strategy_export", mock_export)

        await importer.import_doc(
            db=None, user=None, url="https://abc.feishu.cn/base/Token123"
        )
        assert captured_api_type["value"] == "bitable"
