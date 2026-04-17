
import sys
import re
import os
import io
from typing import Any, Dict, List, Tuple

import fitz

# OCR support
try:
    import pytesseract
    from PIL import Image
    TESSERACT_EXE = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if os.path.exists(TESSERACT_EXE):
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_EXE
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

_WS_RE          = re.compile(r"\s+")
_HASH_RE        = re.compile(r"(\[\d+\])[a-z0-9]{20,}$")
_NESTED_HASH_RE = re.compile(r"(\[\d+\])\.[0-9][a-z0-9]{18,}$")
_NA_RE          = re.compile(r"\bN\s*/\s*A\b", re.IGNORECASE)
OCR_TEXT_THRESHOLD = 50

def _norm(s):
    return _WS_RE.sub(" ", (s or "").strip())

def _safe_md(s):
    return (s or "").replace("\r", "").replace("\t", " ").strip()

def _empty(s):
    return _norm(s) == ""

def _rect_list(r):
    return [float(r.x0), float(r.y0), float(r.x1), float(r.y1)]

def _clean_name(name):
    name = _HASH_RE.sub(r"\1", name or "")
    name = _NESTED_HASH_RE.sub(r"\1", name)
    return name

def _collapse_na(s):
    return _NA_RE.sub("N/A", s or "")

def humanize_field_name(raw_name):
    name = _HASH_RE.sub(r"\1", raw_name or "")
    name = _NESTED_HASH_RE.sub(r"\1", name)
    leaf = name.rsplit(".", 1)[-1]
    idx_match = re.search(r"\[(\d+)\]$", leaf)
    idx = f" [{idx_match.group(1)}]" if idx_match else ""
    leaf_no_idx = re.sub(r"\[\d+\]$", "", leaf)
    leaf_clean = re.sub(
        r"^(?:(?:(?:[A-Z]\d+_)*(?:Line\d+[a-z]?_))|(?:Pt\d+Line\d+[a-z]?_)|(?:[A-Z]\d+Line\d+[a-z]?_)|(?:Part\d+_)|(?:HSupLine\d+[a-z]?_)|(?:OandPSuppLine\d+[a-z]?_?)|(?:LSuppLine\d+[a-z]?_?)|(?:Preparer_))+",
        "", leaf_no_idx,
    )
    if not leaf_clean:
        leaf_clean = leaf_no_idx
    spaced = re.sub(r"(\d)([A-Za-z])", r"\1 \2", leaf_clean)
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", spaced)
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)
    spaced = spaced.replace("_", " ").strip()
    spaced = re.sub(r"\b(\w+?)(of|or|and|to|in|for|at|by)\b", r"\1 \2", spaced, flags=re.IGNORECASE)
    spaced = re.sub(r"\s{2,}", " ", spaced).strip()
    return f"{spaced}{idx}" if spaced else leaf_no_idx + idx

def get_drawn_checkboxes(page):
    clip = fitz.Rect(210, 183, 420, 268)
    d = page.get_text("dict", clip=clip)
    results = []
    for b in d.get("blocks", []):
        if b.get("type") != 0:
            continue
        for ln in b.get("lines", []):
            for sp in ln.get("spans", []):
                text = sp.get("text", "").strip()
                if not text or text in ("At:",):
                    continue
                origin_x = sp.get("origin", (0, 0))[0]
                origin_y = sp.get("origin", (0, 0))[1]
                if origin_x < 225 or not (189 < origin_y < 260):
                    continue
                font = sp.get("font", "")
                is_bold = "Bold" in font or "bold" in font
                results.append((text, is_bold))
    return results

def _widget_value(value, field_type):
    ftype = (field_type or "").lower()
    if "check" in ftype:
        if value is None: return "N/A"
        if isinstance(value, bool): return "Checked" if value else "Unchecked"
        if isinstance(value, (int, float)): return "Checked" if value else "Unchecked"
        s = str(value).strip()
        return "Unchecked" if s in ("/Off", "Off", "") else "Checked"
    if isinstance(value, str):
        s = value.strip()
        return "N/A" if s == "" else _norm(s)
    if isinstance(value, (int, float)): return str(value)
    if isinstance(value, bool): return "Checked" if value else "Unchecked"
    return "N/A"

def _name_fallback(raw_name):
    name = _clean_name(raw_name)
    parts = name.rsplit(".", 1)
    last = parts[-1] if parts else name
    last = re.sub(r"^\[.*?\]\.", "", last)
    last = re.sub(r"([A-Z])", r" \1", last).strip()
    return last or name or "N/A"

def _guess_label(page, rect_list, field_name):
    try:
        rect = fitz.Rect(rect_list) if rect_list else None
    except Exception:
        rect = None
    if rect is None:
        return _name_fallback(field_name)
    left_band  = fitz.Rect(max(0, rect.x0 - 350), rect.y0 - 12, rect.x0 - 2, rect.y1 + 12)
    above_band = fitz.Rect(rect.x0 - 15, max(0, rect.y0 - 100), rect.x1 + 15, rect.y0 - 2)
    candidates = []
    for band, weight in ((left_band, 1.0), (above_band, 1.2)):
        text = _norm(page.get_text("text", clip=band))
        text = _collapse_na(text)
        if text:
            candidates.append((weight, text))
    if not candidates:
        return _name_fallback(field_name)
    candidates.sort(key=lambda x: -x[0])
    raw = candidates[0][1].strip(" :.-")
    if len(raw) > 100:
        raw = raw[-100:]
    return raw or _name_fallback(field_name)

def _extract_fields(page):
    out = []
    for wid in (page.widgets() or []):
        try:
            wtype = getattr(wid, "field_type_string", None) or getattr(wid, "field_type", None)
            name  = getattr(wid, "field_name",  None)
            label = getattr(wid, "field_label", None)
            rect  = getattr(wid, "rect",        None)
            value = getattr(wid, "field_value", None)
            clean = _clean_name(str(name) if name is not None else "")
            rlist = _rect_list(rect) if rect is not None else None
            label_str = str(label) if label is not None else None
            needs_guess = (not label_str or _empty(label_str) or label_str.lower().startswith("form"))
            if needs_guess:
                label_str = _guess_label(page, rlist or [], clean)
            out.append({"type": str(wtype) if wtype is not None else "unknown", "name": clean or "N/A", "label": label_str, "value": value, "rect": rlist})
        except Exception:
            continue
    out.sort(key=lambda r: ((r["rect"] or [0,0,0,0])[1], (r["rect"] or [0,0,0,0])[0]))
    return out

def _compress_spaced_chars(s):
    tokens = s.split(" ")
    if len(tokens) > 1 and all(len(t) == 1 for t in tokens):
        return "".join(tokens)
    return s

def _is_single(s):
    return len(s) == 1 and s.isprintable() and not s.isspace()

def _block_to_lines(block):
    raw = []
    for ln in block.get("lines", []):
        parts = []
        for sp in ln.get("spans", []):
            t = sp.get("text", "").replace("\u00a0", " ")
            if _empty(t): continue
            t = _compress_spaced_chars(t.strip())
            if t: parts.append(t)
        if not parts: continue
        joined = _norm(" ".join(parts))
        joined = _compress_spaced_chars(joined)
        if joined: raw.append(joined)

    bracket_merged = []
    i = 0
    while i < len(raw):
        tok = raw[i]
        if tok and tok[-1] in "([" and i + 2 < len(raw):
            next_tok  = raw[i + 1]
            after_tok = raw[i + 2]
            if len(next_tok) <= 3 and _is_single(after_tok):
                j = i + 1
                while j < len(raw) and len(raw[j]) <= 3:
                    j += 1
                merged = tok + "".join(raw[i + 1:j])
                if tok[-1] == "[" and not merged.endswith("]"): merged += "]"
                elif tok[-1] == "(" and not merged.endswith(")"): merged += ")"
                bracket_merged.append(merged)
                i = j
                continue
        bracket_merged.append(tok)
        i += 1

    result = []
    i = 0
    while i < len(bracket_merged):
        if _is_single(bracket_merged[i]):
            j = i
            while j < len(bracket_merged) and _is_single(bracket_merged[j]):
                j += 1
            result.append("".join(bracket_merged[i:j]))
            i = j
        else:
            result.append(bracket_merged[i])
            i += 1
    return [_collapse_na(line) for line in result]

def _is_scanned_page(page, text):
    has_images = len(page.get_images()) > 0
    text_too_short = len(text.strip()) < OCR_TEXT_THRESHOLD
    return has_images and text_too_short

def _ocr_page(page):
    if not OCR_AVAILABLE:
        return ["[OCR not available — install pytesseract and pillow]"]
    try:
        mat = fitz.Matrix(2, 2)
        pix = page.get_pixmap(matrix=mat)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        raw = pytesseract.image_to_string(img, config="--psm 3")
        lines = [_norm(l) for l in raw.split("\n") if _norm(l)]
        lines = [_collapse_na(l) for l in lines]
        return lines
    except Exception as e:
        return [f"[OCR error: {e}]"]

def _extract_text(page):
    d = page.get_text("dict")
    blocks = d.get("blocks", [])
    blocks.sort(key=lambda b: (b.get("bbox", [0,0,0,0])[1], b.get("bbox", [0,0,0,0])[0]))
    lines_out = []
    for b in blocks:
        if b.get("type") != 0: continue
        block_lines = _block_to_lines(b)
        lines_out.extend(block_lines)
        if lines_out and lines_out[-1] != "":
            lines_out.append("")
    while lines_out and lines_out[-1] == "":
        lines_out.pop()
    full_text = " ".join(lines_out)
    if _is_scanned_page(page, full_text):
        ocr_lines = _ocr_page(page)
        return ocr_lines, True
    return lines_out, False

def pdf_to_markdown(input_pdf, output_md):
    doc = fitz.open(input_pdf)
    md = [
        "# PDF Extraction", "",
        f"- **File:** `{_safe_md(input_pdf)}`",
        f"- **Pages:** {doc.page_count}", "",
    ]
    for i in range(doc.page_count):
        page = doc.load_page(i)
        page_no = i + 1
        md += ["---", f"## Page {page_no}", "", f"- Size: {int(page.rect.width)} x {int(page.rect.height)}", ""]
        if page_no == 1:
            drawn_cbs = get_drawn_checkboxes(page)
            if drawn_cbs:
                md.append("### USCIS Use Only (Drawn Checkboxes)")
                for label, checked in drawn_cbs:
                    mark = "[X]" if checked else "[ ]"
                    md.append(f"- {mark} **{_safe_md(label)}**")
                md.append("")
        fields = _extract_fields(page)
        md.append("### Form Fields")
        if not fields:
            md.append("_None detected_")
        else:
            for f in fields:
                label = _safe_md(f.get("label") or "N/A")
                name  = _safe_md(humanize_field_name(f.get("name") or ""))
                ftype = _safe_md(f.get("type") or "unknown")
                value = _safe_md(_widget_value(f.get("value"), f.get("type", "")))
                md.append(f"- **{label}**")
                md.append(f"  - name: `{name}`")
                md.append(f"  - type: `{ftype}`")
                md.append(f"  - value: **{value}**")
        md.append("")
        text_lines, used_ocr = _extract_text(page)
        md.append("### Content" + (" (OCR)" if used_ocr else ""))
        if not text_lines:
            md.append("_No text extracted_")
        else:
            md.extend(text_lines)
        md.append("")
    with open(output_md, "w", encoding="utf-8") as fh:
        fh.write("\n".join(md))
    doc.close()

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python pdf_to_markdown.py input.pdf output.md")
        raise SystemExit(2)
    pdf_to_markdown(sys.argv[1], sys.argv[2])
    print(f"Done -> {sys.argv[2]}")
