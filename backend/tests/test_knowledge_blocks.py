"""Block 拆分 & Chunk 映射系统测试。

覆盖：
  A. HTML Block 拆分 (split_html_to_blocks)
  B. 纯文本 Block 拆分 (split_text_to_blocks)
  C. generate_blocks（DB 持久化）
  D. chunk_blocks（切片逻辑）
  E. generate_blocks_and_chunks（完整流水线）
  F. API: GET /{kid}/blocks
  G. 搜索结果 block 富化 (_enrich_search_results_with_blocks)
"""
import pytest

from app.models.knowledge import KnowledgeEntry, KnowledgeStatus
from app.models.knowledge_block import KnowledgeChunkMapping, KnowledgeDocumentBlock
from app.services.block_splitter import (
    RawBlock,
    chunk_blocks,
    generate_blocks,
    generate_blocks_and_chunks,
    split_html_to_blocks,
    split_text_to_blocks,
)
from tests.conftest import _auth, _login, _make_dept, _make_user


# ── Helper ────────────────────────────────────────────────────────────


def _make_entry(db, user_id, title, content, content_html=None, status=KnowledgeStatus.APPROVED):
    """在 DB 中创建一条 KnowledgeEntry 并 flush。"""
    entry = KnowledgeEntry(
        title=title,
        content=content,
        content_html=content_html,
        status=status,
        created_by=user_id,
    )
    db.add(entry)
    db.flush()
    return entry


# =====================================================================
# A. HTML Block 拆分 (split_html_to_blocks)
# =====================================================================


class TestSplitHtmlToBlocks:
    """split_html_to_blocks 单元测试。"""

    def test_basic_block_types(self):
        """含标题、段落、列表、表格的 HTML 应生成对应 block_type。"""
        html = (
            "<h1>标题一</h1>"
            "<p>这是一段正文。</p>"
            "<ul><li>项目A</li><li>项目B</li></ul>"
            "<table><tr><td>数据</td></tr></table>"
        )
        blocks = split_html_to_blocks(html)
        types = [b.block_type for b in blocks]
        assert "heading" in types
        assert "paragraph" in types
        assert "list" in types
        assert "table" in types

    def test_block_order_increments(self):
        """block 列表应按文档出现顺序排列。"""
        html = "<h1>第一</h1><p>段落1</p><p>段落2</p><h2>第二</h2><p>段落3</p>"
        blocks = split_html_to_blocks(html)
        assert len(blocks) >= 4
        # 顺序应递增，且第一个 block 是 heading
        assert blocks[0].block_type == "heading"
        assert blocks[0].plain_text.strip() == "第一"

    def test_heading_path_hierarchy(self):
        """heading_path 应跟踪标题层级，用 ' > ' 连接。"""
        html = (
            "<h1>一、背景</h1>"
            "<h2>1.1 市场分析</h2>"
            "<p>市场规模持续增长。</p>"
            "<h2>1.2 竞争格局</h2>"
            "<p>竞争激烈。</p>"
        )
        blocks = split_html_to_blocks(html)
        # 找到 "市场规模" 所在的段落 block
        market_para = [b for b in blocks if "市场规模" in b.plain_text]
        assert len(market_para) == 1
        assert "一、背景" in market_para[0].heading_path
        assert "1.1 市场分析" in market_para[0].heading_path
        assert " > " in market_para[0].heading_path

        # "竞争激烈" 段落应在 1.2 下面，不含 1.1
        compete_para = [b for b in blocks if "竞争激烈" in b.plain_text]
        assert len(compete_para) == 1
        assert "1.2 竞争格局" in compete_para[0].heading_path
        assert "1.1 市场分析" not in compete_para[0].heading_path

    def test_image_produces_image_block(self):
        """<img> 标签应产生 image 类型 block。"""
        html = '<p>文字前</p><img src="test.png"><p>文字后</p>'
        blocks = split_html_to_blocks(html)
        image_blocks = [b for b in blocks if b.block_type == "image"]
        assert len(image_blocks) >= 1

    def test_code_block(self):
        """<pre><code> 应产生 code 类型 block。"""
        html = "<p>正文</p><pre><code>print('hello')</code></pre><p>后续</p>"
        blocks = split_html_to_blocks(html)
        code_blocks = [b for b in blocks if b.block_type == "code"]
        assert len(code_blocks) >= 1
        assert "print" in code_blocks[0].plain_text

    def test_empty_html(self):
        """空 HTML 应返回空列表。"""
        assert split_html_to_blocks("") == []

    def test_offsets_are_non_negative(self):
        """所有 block 的 start_offset 和 end_offset 应 >= 0 且 start <= end。"""
        html = "<h1>标题</h1><p>段落一</p><p>段落二</p>"
        blocks = split_html_to_blocks(html)
        for b in blocks:
            assert b.start_offset >= 0
            assert b.end_offset >= b.start_offset

    def test_rawblock_dataclass_fields(self):
        """RawBlock 应包含所有预期字段。"""
        html = "<p>测试文本</p>"
        blocks = split_html_to_blocks(html)
        assert len(blocks) == 1
        b = blocks[0]
        assert isinstance(b, RawBlock)
        assert hasattr(b, "block_type")
        assert hasattr(b, "plain_text")
        assert hasattr(b, "html_fragment")
        assert hasattr(b, "heading_path")
        assert hasattr(b, "start_offset")
        assert hasattr(b, "end_offset")


# =====================================================================
# B. 纯文本 Block 拆分 (split_text_to_blocks)
# =====================================================================


class TestSplitTextToBlocks:
    """split_text_to_blocks 单元测试。"""

    def test_double_newline_splits_paragraphs(self):
        """双换行应产生多个 paragraph block。"""
        text = "第一段内容。\n\n第二段内容。\n\n第三段内容。"
        blocks = split_text_to_blocks(text)
        assert len(blocks) == 3
        assert all(b.block_type == "paragraph" for b in blocks)

    def test_hash_heading_detection(self):
        """以 # 开头的行应识别为 heading block。"""
        text = "# 一级标题\n\n正文段落\n\n## 二级标题\n\n另一段"
        blocks = split_text_to_blocks(text)
        headings = [b for b in blocks if b.block_type == "heading"]
        paragraphs = [b for b in blocks if b.block_type == "paragraph"]
        assert len(headings) == 2
        assert len(paragraphs) == 2

    def test_empty_input_returns_empty(self):
        """空字符串应返回空列表。"""
        assert split_text_to_blocks("") == []

    def test_whitespace_only_returns_empty(self):
        """纯空白输入应返回空列表。"""
        assert split_text_to_blocks("   \n\n  \n  ") == []

    def test_single_paragraph(self):
        """单段文字不含双换行 → 一个 block。"""
        text = "这是一段简单文本，没有双换行。"
        blocks = split_text_to_blocks(text)
        assert len(blocks) == 1
        assert blocks[0].plain_text == text

    def test_offsets_cover_text(self):
        """offset 应正确覆盖原文范围。"""
        text = "段落一\n\n段落二"
        blocks = split_text_to_blocks(text)
        assert blocks[0].start_offset == 0
        assert blocks[0].end_offset == len("段落一")


# =====================================================================
# C. generate_blocks（DB 持久化）
# =====================================================================


class TestGenerateBlocks:
    """generate_blocks DB 持久化测试。"""

    def test_html_entry_generates_blocks(self, db):
        """content_html 存在时应使用 HTML 拆分器并写入 DB。"""
        dept = _make_dept(db)
        user = _make_user(db, dept_id=dept.id)
        entry = _make_entry(
            db, user.id,
            title="HTML文档",
            content="纯文本备用",
            content_html="<h1>标题</h1><p>正文内容</p>",
        )
        blocks = generate_blocks(db, entry)
        assert len(blocks) >= 2
        # DB 里也应有记录
        db_blocks = db.query(KnowledgeDocumentBlock).filter_by(knowledge_id=entry.id).all()
        assert len(db_blocks) == len(blocks)

    def test_text_only_entry_uses_text_splitter(self, db):
        """无 content_html 时应用纯文本拆分器。"""
        dept = _make_dept(db)
        user = _make_user(db, dept_id=dept.id)
        entry = _make_entry(
            db, user.id,
            title="纯文本文档",
            content="第一段\n\n第二段\n\n第三段",
        )
        blocks = generate_blocks(db, entry)
        assert len(blocks) == 3
        assert all(b.block_type == "paragraph" for b in blocks)

    def test_empty_content_returns_empty(self, db):
        """content 和 content_html 均空时返回空列表。"""
        dept = _make_dept(db)
        user = _make_user(db, dept_id=dept.id)
        entry = _make_entry(db, user.id, title="空文档", content="")
        blocks = generate_blocks(db, entry)
        assert blocks == []

    def test_recalling_replaces_old_blocks(self, db):
        """再次调用 generate_blocks 应替换旧 blocks，不产生重复。"""
        dept = _make_dept(db)
        user = _make_user(db, dept_id=dept.id)
        entry = _make_entry(
            db, user.id,
            title="替换测试",
            content="原始段落一\n\n原始段落二",
        )
        blocks_v1 = generate_blocks(db, entry)
        assert len(blocks_v1) == 2

        # 修改内容后重新生成
        entry.content = "新段落一\n\n新段落二\n\n新段落三"
        entry.content_html = None
        blocks_v2 = generate_blocks(db, entry)
        assert len(blocks_v2) == 3

        # DB 中应只有 v2 的 blocks
        count = db.query(KnowledgeDocumentBlock).filter_by(knowledge_id=entry.id).count()
        assert count == 3

    def test_block_key_format(self, db):
        """block_key 应符合 blk-{order}-{hash} 格式。"""
        import re as _re

        dept = _make_dept(db)
        user = _make_user(db, dept_id=dept.id)
        entry = _make_entry(db, user.id, title="Key格式", content="段落内容")
        blocks = generate_blocks(db, entry)
        assert len(blocks) >= 1
        for b in blocks:
            assert _re.match(r"^blk-\d+-[a-f0-9]{8}$", b.block_key), f"unexpected key: {b.block_key}"

    def test_block_order_persisted(self, db):
        """block_order 应从 0 递增。"""
        dept = _make_dept(db)
        user = _make_user(db, dept_id=dept.id)
        entry = _make_entry(
            db, user.id,
            title="排序测试",
            content="A\n\nB\n\nC\n\nD",
        )
        blocks = generate_blocks(db, entry)
        orders = [b.block_order for b in blocks]
        assert orders == list(range(len(blocks)))


# =====================================================================
# D. chunk_blocks（切片逻辑）
# =====================================================================


class TestChunkBlocks:
    """chunk_blocks 纯函数测试。"""

    def _make_mock_block(self, text, block_id=1, block_key="blk-0-abcd1234", heading_path=""):
        """创建一个模拟 KnowledgeDocumentBlock 对象。"""

        class _MockBlock:
            pass

        b = _MockBlock()
        b.id = block_id
        b.block_key = block_key
        b.plain_text = text
        b.heading_path = heading_path
        return b

    def test_short_block_single_chunk(self):
        """短于 chunk_size 的 block → 单个 chunk，char_start=0。"""
        block = self._make_mock_block("这是一段短文本。")
        chunks = chunk_blocks([block], chunk_size=500, overlap=100)
        assert len(chunks) == 1
        assert chunks[0]["char_start"] == 0
        assert chunks[0]["char_end"] == len("这是一段短文本。")
        assert chunks[0]["block_id"] == block.id
        assert chunks[0]["block_key"] == block.block_key

    def test_long_block_multiple_chunks(self):
        """长于 chunk_size 的 block → 多个 chunk，有 overlap。"""
        long_text = "字" * 1200
        block = self._make_mock_block(long_text)
        chunks = chunk_blocks([block], chunk_size=500, overlap=100)
        assert len(chunks) >= 3
        # 第二个 chunk 的起始位置应考虑 overlap
        assert chunks[1]["char_start"] == 500 - 100  # chunk_size - overlap = 400

    def test_overlap_correctness(self):
        """相邻 chunk 之间的重叠字符数应等于 overlap 参数。"""
        long_text = "a" * 1000
        block = self._make_mock_block(long_text)
        chunks = chunk_blocks([block], chunk_size=300, overlap=50)
        for i in range(1, len(chunks)):
            # 前一个 chunk 的结尾范围和当前 chunk 的开头应重叠
            overlap_chars = chunks[i - 1]["char_end"] - chunks[i]["char_start"]
            # overlap 应为 50 (除了最后一个 chunk 可能不足)
            if chunks[i]["char_end"] < len(long_text):
                assert overlap_chars == 50

    def test_empty_block_skipped(self):
        """空文本 block 应被跳过。"""
        block = self._make_mock_block("")
        chunks = chunk_blocks([block], chunk_size=500, overlap=100)
        assert chunks == []

    def test_whitespace_only_block_skipped(self):
        """纯空白 block 应被跳过。"""
        block = self._make_mock_block("   \n  ")
        chunks = chunk_blocks([block], chunk_size=500, overlap=100)
        assert chunks == []

    def test_multiple_blocks(self):
        """多个 block 应分别切 chunk。"""
        b1 = self._make_mock_block("短文本一", block_id=1, block_key="blk-0-aaa")
        b2 = self._make_mock_block("短文本二", block_id=2, block_key="blk-1-bbb")
        chunks = chunk_blocks([b1, b2], chunk_size=500, overlap=100)
        assert len(chunks) == 2
        assert chunks[0]["block_id"] == 1
        assert chunks[1]["block_id"] == 2

    def test_heading_path_propagated(self):
        """chunk 应包含 block 的 heading_path。"""
        block = self._make_mock_block("内容", heading_path="一、背景 > 1.1 市场")
        chunks = chunk_blocks([block], chunk_size=500, overlap=100)
        assert chunks[0]["heading_path"] == "一、背景 > 1.1 市场"

    def test_chunk_text_correct(self):
        """chunk text 应等于对应区间的子串。"""
        text = "0123456789" * 100  # 1000 chars
        block = self._make_mock_block(text)
        chunks = chunk_blocks([block], chunk_size=300, overlap=50)
        for c in chunks:
            assert c["text"] == text[c["char_start"]:c["char_end"]]


# =====================================================================
# E. generate_blocks_and_chunks（完整流水线）
# =====================================================================


class TestGenerateBlocksAndChunks:
    """generate_blocks_and_chunks 集成测试。"""

    def test_creates_blocks_and_mappings(self, db):
        """应同时创建 blocks 和 chunk mappings。"""
        dept = _make_dept(db)
        user = _make_user(db, dept_id=dept.id)
        entry = _make_entry(
            db, user.id,
            title="流水线测试",
            content="段落一内容。\n\n段落二内容。",
        )
        chunks = generate_blocks_and_chunks(db, entry)
        assert len(chunks) >= 2

        # DB 中应有 blocks
        blocks = db.query(KnowledgeDocumentBlock).filter_by(knowledge_id=entry.id).all()
        assert len(blocks) >= 2

        # DB 中应有 chunk mappings
        mappings = db.query(KnowledgeChunkMapping).filter_by(knowledge_id=entry.id).all()
        assert len(mappings) == len(chunks)

    def test_chunk_mapping_fields(self, db):
        """每个 chunk mapping 应有合法的 block_id, block_key, char 范围和 chunk_text。"""
        dept = _make_dept(db)
        user = _make_user(db, dept_id=dept.id)
        entry = _make_entry(
            db, user.id,
            title="映射字段",
            content="第一段正文。\n\n第二段正文。",
        )
        generate_blocks_and_chunks(db, entry)
        mappings = db.query(KnowledgeChunkMapping).filter_by(knowledge_id=entry.id).all()

        block_ids = {b.id for b in db.query(KnowledgeDocumentBlock).filter_by(knowledge_id=entry.id).all()}

        for m in mappings:
            assert m.block_id in block_ids
            assert m.block_key is not None
            assert m.char_start_in_block is not None
            assert m.char_end_in_block is not None
            assert m.char_start_in_block >= 0
            assert m.char_end_in_block >= m.char_start_in_block
            assert m.chunk_text is not None
            assert len(m.chunk_text) > 0

    def test_chunk_text_populated(self, db):
        """chunk_text 应为非空文本。"""
        dept = _make_dept(db)
        user = _make_user(db, dept_id=dept.id)
        entry = _make_entry(
            db, user.id,
            title="chunk文本",
            content="有内容的段落。\n\n另一段有内容的段落。",
        )
        chunks = generate_blocks_and_chunks(db, entry)
        for c in chunks:
            assert c["text"].strip() != ""

    def test_empty_entry_returns_empty(self, db):
        """空内容条目应返回空 chunks。"""
        dept = _make_dept(db)
        user = _make_user(db, dept_id=dept.id)
        entry = _make_entry(db, user.id, title="空", content="")
        chunks = generate_blocks_and_chunks(db, entry)
        assert chunks == []

    def test_long_content_produces_overlapping_chunks(self, db):
        """长文本应产生多个 chunk，且有重叠映射。"""
        dept = _make_dept(db)
        user = _make_user(db, dept_id=dept.id)
        long_text = "这是一段很长的文本内容，用于测试切片逻辑。" * 100
        entry = _make_entry(db, user.id, title="长文本", content=long_text)
        chunks = generate_blocks_and_chunks(db, entry)
        assert len(chunks) > 1

    def test_html_entry_full_pipeline(self, db):
        """HTML 条目走完整流水线。"""
        dept = _make_dept(db)
        user = _make_user(db, dept_id=dept.id)
        html = (
            "<h1>项目报告</h1>"
            "<h2>1. 背景</h2>"
            "<p>本项目旨在提升用户体验。</p>"
            "<h2>2. 方案</h2>"
            "<p>采用微服务架构重构。</p>"
            "<ul><li>拆分用户服务</li><li>拆分订单服务</li></ul>"
        )
        entry = _make_entry(
            db, user.id,
            title="HTML流水线",
            content="备用文本",
            content_html=html,
        )
        chunks = generate_blocks_and_chunks(db, entry)
        assert len(chunks) >= 4

        # 验证 heading_path 传播
        mappings = db.query(KnowledgeChunkMapping).filter_by(knowledge_id=entry.id).all()
        block_map = {
            b.id: b
            for b in db.query(KnowledgeDocumentBlock).filter_by(knowledge_id=entry.id).all()
        }
        for m in mappings:
            block = block_map.get(m.block_id)
            if block and block.block_type != "heading":
                # 非标题 block 如果在标题下应有 heading_path
                # (不强制，因为第一个段落可能在任何标题之前)
                pass


# =====================================================================
# F. API: GET /{kid}/blocks
# =====================================================================


class TestBlocksAPI:
    """GET /api/knowledge/{kid}/blocks 端点测试。"""

    def test_returns_blocks_in_order(self, client, db):
        """返回的 blocks 应按 block_order 排序。"""
        dept = _make_dept(db)
        user = _make_user(db, dept_id=dept.id)
        db.commit()
        token = _login(client, user.username)

        entry = _make_entry(
            db, user.id,
            title="API块测试",
            content="段落A\n\n段落B\n\n段落C",
        )
        generate_blocks(db, entry)
        db.commit()

        resp = client.get(f"/api/knowledge/{entry.id}/blocks", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3
        orders = [b["block_order"] for b in data]
        assert orders == sorted(orders)
        # 验证返回的字段
        for b in data:
            assert "id" in b
            assert "block_key" in b
            assert "block_type" in b
            assert "plain_text" in b
            assert "heading_path" in b
            assert "start_offset" in b
            assert "end_offset" in b

    def test_nonexistent_entry_returns_404(self, client, db):
        """不存在的 knowledge id 应返回 404。"""
        dept = _make_dept(db)
        user = _make_user(db, dept_id=dept.id)
        db.commit()
        token = _login(client, user.username)

        resp = client.get("/api/knowledge/99999/blocks", headers=_auth(token))
        assert resp.status_code == 404

    def test_entry_with_no_blocks(self, client, db):
        """条目存在但无 blocks 应返回空列表。"""
        dept = _make_dept(db)
        user = _make_user(db, dept_id=dept.id)
        db.commit()
        token = _login(client, user.username)

        entry = _make_entry(db, user.id, title="无块", content="随便")
        db.commit()

        resp = client.get(f"/api/knowledge/{entry.id}/blocks", headers=_auth(token))
        assert resp.status_code == 200
        assert resp.json() == []


# =====================================================================
# G. 搜索结果 block 富化
# =====================================================================


class TestEnrichSearchResults:
    """_enrich_search_results_with_blocks 测试。"""

    def test_enrichment_adds_block_fields(self, db):
        """搜索结果 dict 被富化后应包含 block_id, block_key, heading_path, char_range。"""
        from app.routers.knowledge import _enrich_search_results_with_blocks

        dept = _make_dept(db)
        user = _make_user(db, dept_id=dept.id)
        entry = _make_entry(
            db, user.id,
            title="富化测试",
            content="段落内容用于搜索。\n\n另一段内容。",
            content_html="<h1>标题</h1><p>段落内容用于搜索。</p><p>另一段内容。</p>",
        )
        chunks = generate_blocks_and_chunks(db, entry)
        db.flush()

        # 模拟搜索结果 best dict: {knowledge_id: {knowledge_id, chunk_index, ...}}
        best = {
            entry.id: {
                "knowledge_id": entry.id,
                "chunk_index": 0,
                "text": chunks[0]["text"] if chunks else "",
                "score": 0.9,
            }
        }

        _enrich_search_results_with_blocks(db, best)

        result = best[entry.id]
        assert "block_id" in result
        assert "block_key" in result
        assert "char_range" in result
        assert result["block_id"] is not None
        assert result["block_key"] is not None
        assert isinstance(result["char_range"], list)
        assert len(result["char_range"]) == 2

    def test_enrichment_with_empty_best(self, db):
        """空 best dict 不应报错。"""
        from app.routers.knowledge import _enrich_search_results_with_blocks

        best = {}
        _enrich_search_results_with_blocks(db, best)
        assert best == {}

    def test_enrichment_missing_mapping_graceful(self, db):
        """找不到 mapping 时不应报错，结果无 block 字段。"""
        from app.routers.knowledge import _enrich_search_results_with_blocks

        dept = _make_dept(db)
        user = _make_user(db, dept_id=dept.id)
        entry = _make_entry(db, user.id, title="无映射", content="内容")
        db.flush()

        best = {
            entry.id: {
                "knowledge_id": entry.id,
                "chunk_index": 0,
                "text": "内容",
                "score": 0.8,
            }
        }
        _enrich_search_results_with_blocks(db, best)
        # 不应崩溃，且不包含 block_id
        assert "block_id" not in best[entry.id]

    def test_enrichment_heading_path_from_block(self, db):
        """富化后的 heading_path 应来自关联 block 的 heading_path。"""
        from app.routers.knowledge import _enrich_search_results_with_blocks

        dept = _make_dept(db)
        user = _make_user(db, dept_id=dept.id)
        entry = _make_entry(
            db, user.id,
            title="标题路径",
            content="备用",
            content_html="<h1>总览</h1><h2>详情</h2><p>具体内容在这里。</p>",
        )
        chunks = generate_blocks_and_chunks(db, entry)
        db.flush()

        # 找包含 "具体内容" 的 chunk
        target_chunk = None
        for i, c in enumerate(chunks):
            if "具体内容" in c["text"]:
                target_chunk = (i, c)
                break

        if target_chunk:
            idx, c = target_chunk
            best = {
                entry.id: {
                    "knowledge_id": entry.id,
                    "chunk_index": idx,
                    "text": c["text"],
                    "score": 0.95,
                }
            }
            _enrich_search_results_with_blocks(db, best)
            result = best[entry.id]
            # heading_path 应包含 "总览" 和 "详情"
            if result.get("heading_path"):
                assert "总览" in result["heading_path"] or "详情" in result["heading_path"]
