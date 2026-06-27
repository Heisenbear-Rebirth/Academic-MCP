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


def _is_known_boilerplate_line(line: str) -> bool:
    compact = re.sub(r"\s+", " ", line or "").strip()
    low = compact.lower()
    if not compact:
        return False
    if (
        "authorized licensed use limited to:" in low
        and "ieee xplore" in low
        and "restrictions apply" in low
    ):
        return True
    return False


def _strip_known_boilerplate(md_text: str) -> str:
    lines = [
        line.rstrip()
        for line in (md_text or "").splitlines()
        if not _is_known_boilerplate_line(line)
    ]
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return f"{cleaned}\n" if cleaned else ""


def _effective_content_text(md_text: str) -> str:
    cleaned = _strip_known_boilerplate(md_text)
    cleaned = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", cleaned)
    return "".join(re.findall(r"[A-Za-z0-9\u4e00-\u9fff]+", cleaned))


def _text_is_unusable(md_text: str, *, threshold: float = 0.30) -> bool:
    cleaned = _strip_known_boilerplate(md_text)
    content = [c for c in cleaned if c not in _MD_STRUCTURAL]
    if not content:
        return True
    fffd = sum(1 for c in content if c == "�")
    return (fffd / len(content)) > threshold


def markdown_is_low_signal(md_text: str, *, min_effective_chars: int = 120) -> bool:
    """Return True when Markdown contains no useful body text.

    This catches publisher-watermark-only conversions, notably old IEEE PDFs
    where pymupdf4llm may keep the authorization footer but drop the article
    body even though PyMuPDF can still extract it.
    """
    if _text_is_unusable(md_text):
        return True
    return len(_effective_content_text(md_text)) < min_effective_chars


def _native_text_markdown(pdf: Path) -> str:
    stem = safe_stem(pdf.name)
    doc = fitz.open(str(pdf))
    try:
        lines = [
            f"# {stem}",
            "",
            "> Primary layout extraction produced no usable body text; this file was converted with PyMuPDF native text extraction.",
            "",
        ]
        for page_index, page in enumerate(doc, start=1):
            text = page.get_text("text", sort=True) or ""
            text = _strip_known_boilerplate(text)
            text = re.sub(r"[ \t]+\n", "\n", text)
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            if not text:
                continue
            lines.extend([f"## Page {page_index}", "", text, ""])
        md_text = "\n".join(lines).strip()
        return f"{md_text}\n" if md_text else ""
    finally:
        doc.close()


def _page_images_markdown(pdf: Path, images: Path, image_format: str, note: str) -> str:
    stem = safe_stem(pdf.name)
    doc = fitz.open(str(pdf))
    try:
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
        return "\n".join(lines).strip() + "\n"
    finally:
        doc.close()


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
    md_text = _strip_known_boilerplate(md_text)
    image_only = False

    if markdown_is_low_signal(md_text):
        native_md = _native_text_markdown(pdf)
        if not markdown_is_low_signal(native_md, min_effective_chars=200):
            md_text = native_md
        else:
            image_only = True
            garbled = bool(md_text.strip() or native_md.strip())
            note = (
                "> The PDF's embedded fonts lack a Unicode mapping, so the text"
                " layer extracted as garbage; pages were rendered as images instead."
                if garbled
                else "> No usable extractable body text was found in this PDF; pages were rendered as images."
            )
            md_text = _page_images_markdown(pdf, images, image_format, note)

    if not md_text.strip() or _text_is_unusable(md_text):
        image_only = True
        note = "> No usable extractable body text was found in this PDF; pages were rendered as images."
        md_text = _page_images_markdown(pdf, images, image_format, note)

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
