"""Extract plain text from uploaded documents."""
import os
import base64


def _call_kimi_vision(image_path: str) -> str:
    """Call Kimi vision API (via 百炼 Coding Plan) to describe an image."""
    import openai

    api_key = os.environ.get("BAILIAN_API_KEY", "")
    if not api_key:
        raise ValueError("BAILIAN_API_KEY 环境变量未设置")

    with open(image_path, "rb") as f:
        b64_data = base64.b64encode(f.read()).decode("utf-8")

    ext = os.path.splitext(image_path)[1].lower().lstrip(".")
    mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                "webp": "image/webp", "bmp": "image/bmp"}
    mime = mime_map.get(ext, "image/png")

    client = openai.OpenAI(
        api_key=api_key,
        base_url="https://coding.dashscope.aliyuncs.com/apps/anthropic/v1",
    )
    resp = client.chat.completions.create(
        model="kimi-k2.5",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64_data}"}},
                    {"type": "text", "text": "请详细描述这张图片的内容，包括所有文字、数据、图表信息。"},
                ],
            }
        ],
        max_tokens=2048,
    )
    return resp.choices[0].message.content or ""


def _transcribe_funasr(audio_path: str) -> str:
    """Transcribe audio using local FunASR model."""
    import re
    from funasr import AutoModel

    model = AutoModel(
        model="iic/SenseVoiceSmall",
        vad_model="fsmn-vad",
        punc_model="ct-punc",
        disable_update=True,
    )
    res = model.generate(input=audio_path, batch_size_s=300)
    if not res:
        return ""
    text = res[0].get("text", "")
    # Remove SenseVoice emotion/language tags like <|zh|><|NEUTRAL|>
    text = re.sub(r"<\|[^|]+\|>", "", text).strip()
    return text


_FOE_MAP_PROMPT = """\
你是严谨的信息分析师。对下面的文本片段做摘要，并对每个关键论断用FOE三测试标注：
- [F] 事实：可通过公认方法验证真假
- [O] 观点：两个同等知情者可能合理分歧，需注明发布方和隐含假设
- [E] 证据：为支撑某判断而引用，需注明质量层级（一级=审计/官方 二级=权威二手 三级=专家报告 四级=类比先例 五级=观点用作证据）

输出格式：
## Chunk摘要
### 核心事实
- [F] ...
### 关键观点
- [O] ... —— 发布方：... | 隐含假设：...
### 证据链
- [E] ... —— 质量层级：X级 | 支撑观点：...
### 本段主旨
[一句话，明确区分F/O成分]

文本片段：
{text}"""

_FOE_REDUCE_PROMPT = """\
下面是一篇长文各段落的FOE标注摘要。请合并为最终结构化摘要。

合并规则：
1. 事实去重，相同事实保留更精确版本
2. 观点聚合，标注支撑强度（强≥2条一/二级证据；中=1条高质量或多条三级；弱=仅四/五级；无=纯断言）
3. 冲突的事实/观点明确标注矛盾，保留双方
4. 将分散证据重新关联到对应观点

输出格式：
## 一、事实摘要
（按主题聚类，去重）

## 二、观点图谱
| 观点 | 发布方 | 支撑强度 | 隐含假设 |
|------|--------|----------|---------|

## 三、证据评估
- 整体论证质量：
- 证据缺口：
- 观点伪装为证据的环节：

## 四、信息缺口
（应讨论但未讨论的方面）

## 五、全文摘要
（3-5句，每句标注[F]或[O]）

---
各段落摘要：
{text}"""


def foe_summarize(raw_text: str, llm_cfg: dict) -> str:
    """对超长文本执行 LangChain MapReduce + FOE 三测试摘要。

    返回 FOE 结构化摘要字符串（用于聊天上下文）。
    原始文本由调用方单独保存到知识库，数据不丢失。

    llm_cfg: llm_gateway.resolve_config(db, slot_key) 返回的 dict，
             包含 api_base / api_key / model_id 等字段。
    """
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    from langchain_openai import ChatOpenAI
    from langchain_core.messages import HumanMessage

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=4000,
        chunk_overlap=500,
        separators=["\n\n", "\n", "。", "，", " "],
    )
    chunks = splitter.split_text(raw_text)

    llm = ChatOpenAI(
        base_url=llm_cfg.get("api_base", "https://api.deepseek.com/v1"),
        api_key=llm_cfg.get("api_key", ""),
        model=llm_cfg.get("model_id", "deepseek-chat"),
        temperature=0.1,
        max_tokens=2000,
    )

    # Map: 每个 chunk 独立做 FOE 摘要
    chunk_summaries = []
    for chunk in chunks:
        prompt = _FOE_MAP_PROMPT.format(text=chunk)
        resp = llm.invoke([HumanMessage(content=prompt)])
        chunk_summaries.append(resp.content)

    # Reduce: 合并所有 chunk 摘要
    combined = "\n\n---\n\n".join(chunk_summaries)
    reduce_prompt = _FOE_REDUCE_PROMPT.format(text=combined)
    final = llm.invoke([HumanMessage(content=reduce_prompt)])
    return final.content


def extract_html(file_path: str) -> str:
    """从文件提取 HTML 格式内容，保留富文本格式（标题/加粗/列表/表格等）。
    用于前端云文档编辑器渲染。不支持的格式回退到 extract_text() 包装。"""
    ext = os.path.splitext(file_path)[1].lower()

    if ext in (".docx",):
        import mammoth
        with open(file_path, "rb") as f:
            result = mammoth.convert_to_html(f, style_map=[
                "p[style-name='Heading 1'] => h1:fresh",
                "p[style-name='Heading 2'] => h2:fresh",
                "p[style-name='Heading 3'] => h3:fresh",
                "p[style-name='标题 1'] => h1:fresh",
                "p[style-name='标题 2'] => h2:fresh",
                "p[style-name='标题 3'] => h3:fresh",
            ])
            return result.value

    elif ext in (".md",):
        import markdown as md_lib
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        return md_lib.markdown(text, extensions=["tables", "fenced_code", "nl2br", "sane_lists"])

    elif ext in (".pptx",):
        # PPTX: wrap each slide as a section with headings
        from pptx import Presentation
        prs = Presentation(file_path)
        html_parts = []
        for i, slide in enumerate(prs.slides, 1):
            slide_texts = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    t = shape.text.strip()
                    if t:
                        slide_texts.append(t)
            if slide_texts:
                title = slide_texts[0]
                body = slide_texts[1:] if len(slide_texts) > 1 else []
                html_parts.append(f"<h2>{title}</h2>")
                for line in body:
                    html_parts.append(f"<p>{line}</p>")
        return "\n".join(html_parts)

    elif ext in (".xlsx", ".xls", ".csv"):
        # Tabular data → HTML table
        plain = extract_text(file_path)
        lines = plain.strip().split("\n")
        html_parts = []
        in_table = False
        for line in lines:
            if line.startswith("[Sheet:"):
                if in_table:
                    html_parts.append("</tbody></table>")
                    in_table = False
                html_parts.append(f"<h3>{line.strip('[]')}</h3>")
            elif "\t" in line:
                if not in_table:
                    html_parts.append("<table><tbody>")
                    in_table = True
                cells = line.split("\t")
                html_parts.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
            elif line.strip():
                if in_table:
                    html_parts.append("</tbody></table>")
                    in_table = False
                html_parts.append(f"<p>{line}</p>")
        if in_table:
            html_parts.append("</tbody></table>")
        return "\n".join(html_parts)

    elif ext in (".txt",):
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        return "\n".join(f"<p>{line or '<br>'}</p>" for line in text.split("\n"))

    else:
        # 其他格式（PDF/图片/音频等）：纯文本包装为 <p>
        try:
            plain = extract_text(file_path)
            return "\n".join(f"<p>{line or '<br>'}</p>" for line in plain.split("\n"))
        except ValueError:
            return ""


def extract_text(file_path: str) -> str:
    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".txt":
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    elif ext == ".pdf":
        import pdfplumber
        texts = []
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    texts.append(t)
        return "\n\n".join(texts)

    elif ext in (".docx",):
        from docx import Document
        from docx.oxml.ns import qn

        doc = Document(file_path)
        parts = []

        def iter_block_items(parent):
            """按文档顺序迭代段落和表格。"""
            from docx.oxml.ns import qn as _qn
            from docx.table import Table
            from docx.text.paragraph import Paragraph
            parent_elm = parent.element.body if hasattr(parent, 'element') else parent
            for child in parent_elm.iterchildren():
                if child.tag == qn('w:p'):
                    yield Paragraph(child, parent)
                elif child.tag == qn('w:tbl'):
                    yield Table(child, parent)

        def extract_table(table):
            rows = []
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells]
                # 合并相邻重复单元格（跨列合并会重复出现）
                deduped = []
                for c in cells:
                    if not deduped or c != deduped[-1]:
                        deduped.append(c)
                row_text = "\t".join(c for c in deduped if c)
                if row_text:
                    rows.append(row_text)
            return "\n".join(rows)

        for block in iter_block_items(doc):
            from docx.table import Table
            from docx.text.paragraph import Paragraph
            if isinstance(block, Paragraph):
                t = block.text.strip()
                if t:
                    parts.append(t)
            elif isinstance(block, Table):
                t = extract_table(block)
                if t:
                    parts.append(t)

        return "\n\n".join(parts)

    elif ext in (".pptx",):
        from pptx import Presentation
        prs = Presentation(file_path)
        texts = []
        for slide in prs.slides:
            for shape in slide.shapes:
                if shape.has_text_frame:
                    texts.append(shape.text.strip())
        return "\n\n".join(t for t in texts if t)

    elif ext in (".md",):
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    elif ext in (".xlsx", ".xls"):
        import openpyxl
        wb = openpyxl.load_workbook(file_path, data_only=True)
        parts = []
        for sheet in wb.worksheets:
            rows = []
            for row in sheet.iter_rows(values_only=True):
                cells = [str(c) if c is not None else "" for c in row]
                if any(c.strip() for c in cells):
                    rows.append("\t".join(cells))
            if rows:
                parts.append(f"[Sheet: {sheet.title}]\n" + "\n".join(rows))
        return "\n\n".join(parts)

    elif ext in (".csv",):
        import csv
        rows = []
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            reader = csv.reader(f)
            for row in reader:
                rows.append("\t".join(row))
        return "\n".join(rows)

    elif ext in (".jpg", ".jpeg", ".png", ".webp", ".bmp"):
        return _call_kimi_vision(file_path)

    elif ext in (".mp3", ".wav", ".m4a", ".ogg", ".flac"):
        return _transcribe_funasr(file_path)

    else:
        raise ValueError(
            f"Unsupported file type: {ext}. "
            "Supported: .txt .pdf .docx .pptx .md .xlsx .xls .csv "
            ".jpg .jpeg .png .webp .bmp .mp3 .wav .m4a .ogg .flac"
        )
