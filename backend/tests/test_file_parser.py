from pathlib import Path

from app.utils.file_parser import extract_html, extract_text


def test_extract_text_supports_html(tmp_path: Path):
    file_path = tmp_path / "sample.html"
    file_path.write_text(
        "<html><body><h1>标题</h1><p>第一段<br>第二行</p><script>bad()</script></body></html>",
        encoding="utf-8",
    )

    text = extract_text(str(file_path))

    assert "标题" in text
    assert "第一段" in text
    assert "第二行" in text
    assert "bad()" not in text


def test_extract_html_returns_raw_html_for_html_file(tmp_path: Path):
    file_path = tmp_path / "sample.htm"
    raw_html = "<html><body><p>可编辑 HTML</p></body></html>"
    file_path.write_text(raw_html, encoding="utf-8")

    html = extract_html(str(file_path))

    assert html == raw_html
