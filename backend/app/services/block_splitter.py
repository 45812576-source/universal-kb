"""文档 Block 拆分服务：将 content_html/content 规范化为 document_blocks。

流水线：HTML → blocks → chunks（带映射）。
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

from app.models.knowledge import KnowledgeEntry
from app.models.knowledge_block import KnowledgeChunkMapping, KnowledgeDocumentBlock

logger = logging.getLogger(__name__)


@dataclass
class RawBlock:
    block_type: str          # heading / paragraph / list / table / code / image
    plain_text: str
    html_fragment: str
    heading_path: str = ""
    start_offset: int = 0
    end_offset: int = 0


def split_html_to_blocks(html: str) -> list[RawBlock]:
    """将 HTML 正文拆成结构化 block 列表。"""
    from html.parser import HTMLParser

    blocks: list[RawBlock] = []
    current_heading_path: list[str] = []  # 标题栈

    class _Parser(HTMLParser):
        def __init__(self):
            super().__init__()
            self._buf = ""
            self._html_buf = ""
            self._tag_stack: list[str] = []
            self._block_type = "paragraph"
            self._offset = 0

        def _flush(self):
            text = self._buf.strip()
            if not text and not self._html_buf.strip():
                return
            heading = " > ".join(current_heading_path) if current_heading_path else ""
            end_offset = self._offset + len(text)
            blocks.append(RawBlock(
                block_type=self._block_type,
                plain_text=text,
                html_fragment=self._html_buf.strip(),
                heading_path=heading,
                start_offset=self._offset,
                end_offset=end_offset,
            ))
            self._offset = end_offset
            self._buf = ""
            self._html_buf = ""
            self._block_type = "paragraph"

        def handle_starttag(self, tag, attrs):
            tag_lower = tag.lower()
            if tag_lower in ("h1", "h2", "h3", "h4", "h5", "h6"):
                self._flush()
                self._block_type = "heading"
                level = int(tag_lower[1])
                # 弹出同级及以下标题
                while len(current_heading_path) >= level:
                    current_heading_path.pop()
            elif tag_lower in ("p", "div", "section", "article", "blockquote"):
                self._flush()
            elif tag_lower in ("ul", "ol"):
                self._flush()
                self._block_type = "list"
            elif tag_lower == "table":
                self._flush()
                self._block_type = "table"
            elif tag_lower in ("pre", "code"):
                if not self._tag_stack or self._tag_stack[-1] != "pre":
                    self._flush()
                    self._block_type = "code"
            elif tag_lower == "img":
                self._flush()
                self._block_type = "image"

            self._tag_stack.append(tag_lower)
            attrs_str = " ".join(f'{k}="{v}"' for k, v in attrs) if attrs else ""
            self._html_buf += f"<{tag}{' ' + attrs_str if attrs_str else ''}>"

        def handle_endtag(self, tag):
            tag_lower = tag.lower()
            self._html_buf += f"</{tag}>"
            if self._tag_stack and self._tag_stack[-1] == tag_lower:
                self._tag_stack.pop()

            if tag_lower in ("h1", "h2", "h3", "h4", "h5", "h6"):
                heading_text = self._buf.strip()
                if heading_text:
                    current_heading_path.append(heading_text)
                self._flush()
            elif tag_lower == "table":
                self._flush()
            elif tag_lower in ("ul", "ol"):
                self._flush()

        def handle_data(self, data):
            self._buf += data
            self._html_buf += data

        def close(self):
            super().close()
            self._flush()

    parser = _Parser()
    parser.feed(html)
    parser.close()
    return blocks


def split_text_to_blocks(text: str) -> list[RawBlock]:
    """纯文本按段落拆 block。"""
    blocks: list[RawBlock] = []
    paragraphs = re.split(r"\n{2,}", text)
    offset = 0
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        end = offset + len(para)
        # 检测是否是标题行（以 # 或 数字. 开头）
        block_type = "paragraph"
        if re.match(r"^#{1,6}\s", para):
            block_type = "heading"
        blocks.append(RawBlock(
            block_type=block_type,
            plain_text=para,
            html_fragment=f"<p>{para}</p>",
            start_offset=offset,
            end_offset=end,
        ))
        offset = end
    return blocks


def _make_block_key(knowledge_id: int, order: int, text: str) -> str:
    """生成稳定的 block_key。"""
    digest = hashlib.md5(f"{knowledge_id}:{order}:{text[:50]}".encode()).hexdigest()[:8]
    return f"blk-{order}-{digest}"


def generate_blocks(db: Session, entry: KnowledgeEntry) -> list[KnowledgeDocumentBlock]:
    """为知识条目生成 document blocks 并持久化。先清除旧的 blocks。"""
    # 清除旧 blocks 和 mappings
    db.query(KnowledgeChunkMapping).filter(
        KnowledgeChunkMapping.knowledge_id == entry.id
    ).delete()
    db.query(KnowledgeDocumentBlock).filter(
        KnowledgeDocumentBlock.knowledge_id == entry.id
    ).delete()
    db.flush()

    # 选择拆分方式
    if entry.content_html:
        raw_blocks = split_html_to_blocks(entry.content_html)
    elif entry.content:
        raw_blocks = split_text_to_blocks(entry.content)
    else:
        return []

    db_blocks = []
    for i, rb in enumerate(raw_blocks):
        block_key = _make_block_key(entry.id, i, rb.plain_text)
        b = KnowledgeDocumentBlock(
            knowledge_id=entry.id,
            block_key=block_key,
            block_type=rb.block_type,
            block_order=i,
            plain_text=rb.plain_text,
            html_fragment=rb.html_fragment,
            heading_path=rb.heading_path,
            start_offset=rb.start_offset,
            end_offset=rb.end_offset,
        )
        db.add(b)
        db_blocks.append(b)

    db.flush()
    return db_blocks


def chunk_blocks(
    blocks: list[KnowledgeDocumentBlock],
    chunk_size: int = 500,
    overlap: int = 100,
) -> list[dict]:
    """在 block 内切 chunk，返回 chunk 列表（含 block 映射信息）。

    返回 [{"text": ..., "block_id": ..., "block_key": ...,
            "char_start": ..., "char_end": ..., "heading_path": ...}, ...]
    """
    chunks: list[dict] = []
    for block in blocks:
        text = block.plain_text or ""
        if not text.strip():
            continue

        if len(text) <= chunk_size:
            chunks.append({
                "text": text,
                "block_id": block.id,
                "block_key": block.block_key,
                "char_start": 0,
                "char_end": len(text),
                "heading_path": block.heading_path,
            })
        else:
            start = 0
            while start < len(text):
                end = min(start + chunk_size, len(text))
                chunks.append({
                    "text": text[start:end],
                    "block_id": block.id,
                    "block_key": block.block_key,
                    "char_start": start,
                    "char_end": end,
                    "heading_path": block.heading_path,
                })
                if end == len(text):
                    break
                start += chunk_size - overlap

    return chunks


def generate_blocks_and_chunks(db: Session, entry: KnowledgeEntry) -> list[dict]:
    """完整流水线：生成 blocks → 切 chunks → 写 chunk_mappings → 返回 chunks。"""
    blocks = generate_blocks(db, entry)
    if not blocks:
        return []

    chunks = chunk_blocks(blocks)

    # 写 chunk mappings
    for i, c in enumerate(chunks):
        mapping = KnowledgeChunkMapping(
            knowledge_id=entry.id,
            chunk_index=i,
            block_id=c["block_id"],
            block_key=c["block_key"],
            char_start_in_block=c["char_start"],
            char_end_in_block=c["char_end"],
            chunk_text=c["text"][:2000],
        )
        db.add(mapping)

    db.flush()
    return chunks
