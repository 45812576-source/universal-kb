import builtins
import zipfile
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


def _write_minimal_xlsx(file_path: Path):
    files = {
        "[Content_Types].xml": """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
</Types>
""",
        "xl/workbook.xml": """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="收入表" sheetId="1" r:id="rId1"/>
  </sheets>
</workbook>
""",
        "xl/_rels/workbook.xml.rels": """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>
""",
        "xl/sharedStrings.xml": """<?xml version="1.0" encoding="UTF-8"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="3" uniqueCount="3">
  <si><t>姓名</t></si>
  <si><t>金额</t></si>
  <si><t>胡瑞</t></si>
</sst>
""",
        "xl/worksheets/sheet1.xml": """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1">
      <c r="A1" t="s"><v>0</v></c>
      <c r="B1" t="s"><v>1</v></c>
    </row>
    <row r="2">
      <c r="A2" t="s"><v>2</v></c>
      <c r="B2"><v>123</v></c>
    </row>
  </sheetData>
</worksheet>
""",
    }
    with zipfile.ZipFile(file_path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)


def _block_openpyxl(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "openpyxl":
            raise ModuleNotFoundError("openpyxl unavailable")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_extract_text_supports_xlsx_without_openpyxl(tmp_path: Path, monkeypatch):
    file_path = tmp_path / "sample.xlsx"
    _write_minimal_xlsx(file_path)
    _block_openpyxl(monkeypatch)

    text = extract_text(str(file_path))

    assert "[Sheet: 收入表]" in text
    assert "姓名	金额" in text
    assert "胡瑞	123" in text


def test_extract_html_supports_xlsx_without_openpyxl(tmp_path: Path, monkeypatch):
    file_path = tmp_path / "sample.xlsx"
    _write_minimal_xlsx(file_path)
    _block_openpyxl(monkeypatch)

    html = extract_html(str(file_path))

    assert "<h3>Sheet: 收入表</h3>" in html
    assert "<table><tbody>" in html
    assert "<td>胡瑞</td>" in html
    assert "<td>123</td>" in html
