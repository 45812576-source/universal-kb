"""Extract plain text from uploaded documents."""
import os


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
        doc = Document(file_path)
        return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())

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

    else:
        raise ValueError(f"Unsupported file type: {ext}. Supported: .txt .pdf .docx .pptx .md")
