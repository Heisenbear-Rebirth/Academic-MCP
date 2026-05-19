import os
import re
from dataclasses import dataclass
from pathlib import Path

import fitz
import pymupdf4llm


@dataclass
class MarkdownConversion:
    pdf_path: str
    md_path: str
    md_text: str
    image_dir: str
    image_only: bool = False

    @property
    def preview(self) -> str:
        return self.md_text[:1000]


def safe_stem(name: str, default: str = "document") -> str:
    stem = Path(name or default).stem
    stem = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" .")
    return stem[:140] or default


# Some publisher PDFs (notably a chunk of IEEE conference proceedings) embed
# subsetted fonts without a ToUnicode CMap. They render visually but every
# glyph extracts as U+FFFD, so pymupdf4llm yields a non-empty Markdown that is
# pure mojibake. The plain `not md_text.strip()` check never catches this, so
# we measure the replacement-char density against real (non-whitespace,
# non-Markdown-structural) content and treat a garbled extraction the same as
# an empty one.
_MD_STRUCTURAL = set("#*->|`[]()!_ \t\r\n")


def _text_is_unusable(md_text: str, *, threshold: float = 0.30) -> bool:
    content = [c for c in md_text if c not in _MD_STRUCTURAL]
    if not content:
        return True
    fffd = sum(1 for c in content if c == "�")
    return (fffd / len(content)) > threshold


def convert_pdf_to_markdown(pdf_path: str, output_dir: str, *, image_format: str = "png") -> MarkdownConversion:
    """Convert a PDF to Markdown and never silently accept an empty result."""
    pdf = Path(pdf_path)
    out = Path(output_dir)
    if not pdf.exists():
        raise FileNotFoundError(f"The provided PDF path does not exist: {pdf_path}")
    if pdf.suffix.lower() != ".pdf":
        raise ValueError(f"The provided file is not a PDF: {pdf_path}")

    out.mkdir(parents=True, exist_ok=True)
    images = out / "images"
    images.mkdir(parents=True, exist_ok=True)

    md_text = pymupdf4llm.to_markdown(
        doc=str(pdf),
        write_images=True,
        image_path=str(images),
        image_format=image_format,
    )
    image_only = False

    if not md_text.strip() or _text_is_unusable(md_text):
        image_only = True
        garbled = bool(md_text.strip())
        doc = fitz.open(str(pdf))
        stem = safe_stem(pdf.name)
        note = (
            "> The PDF's embedded fonts lack a Unicode mapping, so the text"
            " layer extracted as garbage; pages were rendered as images instead."
            if garbled
            else "> No extractable text was found in this PDF; pages were rendered as images."
        )
        lines = [
            f"# {stem}",
            "",
            note,
            "",
        ]
        for page_index, page in enumerate(doc, start=1):
            image_name = f"{stem}-page-{page_index:04d}.{image_format}"
            image_path = images / image_name
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            pix.save(str(image_path))
            lines.extend([f"## Page {page_index}", "", f"![](images/{image_name})", ""])
        md_text = "\n".join(lines).strip() + "\n"

    md_path = out / f"{safe_stem(pdf.name)}.md"
    md_path.write_text(md_text, encoding="utf-8")

    if not md_path.exists() or md_path.stat().st_size == 0:
        raise RuntimeError(f"Markdown conversion produced an empty file: {md_path}")

    return MarkdownConversion(
        pdf_path=str(pdf),
        md_path=str(md_path),
        md_text=md_text,
        image_dir=str(images),
        image_only=image_only,
    )
