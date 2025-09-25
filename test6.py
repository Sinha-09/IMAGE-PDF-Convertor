import os
import io
import streamlit as st
from PIL import Image
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader

# Optional: PDF -> Image
try:
    from pdf2image import convert_from_bytes
    PDF2IMAGE_OK = True
except Exception:
    PDF2IMAGE_OK = False

# macOS Homebrew poppler path (adjust if different)
POPPLER_PATH = "/opt/homebrew/opt/poppler/bin"

st.set_page_config(page_title="Exact Size File Converter", layout="centered")
st.title("📂 Exact Size File Converter")
st.write("Upload an **Image or PDF**, choose output type & target size (KB), then download the result.")

uploaded = st.file_uploader("Upload file", type=["jpg","jpeg","png","webp","bmp","tiff","pdf"])
out_type = st.selectbox("Output type", ["jpg", "png", "webp", "pdf"])
target_kb = st.number_input("Target size (KB)", min_value=10, max_value=5000, value=100)
run = st.button("Convert")

KB = 1024
TARGET_MIN_QUALITY = 10
TARGET_MAX_QUALITY = 95

# ----------------- helpers -----------------
def pad_file_to_size_safe(data: bytes, target_bytes: int, is_pdf=False) -> bytes:
    """Pad data to reach target size. Never truncate PDFs."""
    if len(data) >= target_bytes:
        return data if is_pdf else data[:target_bytes]
    return data + b" " * (target_bytes - len(data))

def save_with_format(img: Image.Image, pil_format: str, quality: int = 90) -> bytes:
    buf = io.BytesIO()
    if pil_format == "JPEG":
        if img.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            try:
                bg.paste(img, mask=img.split()[-1])
            except Exception:
                bg.paste(img)
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        img.save(buf, pil_format, quality=quality, optimize=True)
    elif pil_format == "WEBP":
        img.save(buf, pil_format, quality=quality)
    elif pil_format in ("PNG", "BMP", "TIFF"):
        try:
            img.save(buf, pil_format, optimize=True)
        except TypeError:
            img.save(buf, pil_format)
    else:
        img.save(buf, pil_format)
    return buf.getvalue()

def image_to_image_exact(data: bytes, out_fmt: str, target_bytes: int) -> bytes:
    fmt_map = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP", "bmp": "BMP", "tiff": "TIFF"}
    out_key = out_fmt.lower()
    if out_key not in fmt_map:
        raise ValueError("Unsupported output format: " + str(out_fmt))
    pil_format = fmt_map[out_key]

    img = Image.open(io.BytesIO(data))

    # 1) Try high-quality save
    out_bytes = save_with_format(img, pil_format, quality=90)
    if len(out_bytes) <= target_bytes:
        return pad_file_to_size_safe(out_bytes, target_bytes)

    # 2) Quality binary search (JPEG/WEBP)
    if pil_format in ("JPEG", "WEBP"):
        low, high = TARGET_MIN_QUALITY, TARGET_MAX_QUALITY
        best = None
        while low <= high:
            mid = (low + high) // 2
            candidate = save_with_format(img, pil_format, quality=mid)
            size = len(candidate)
            if size <= target_bytes:
                best = candidate
                low = mid + 1
            else:
                high = mid - 1
        if best:
            return pad_file_to_size_safe(best, target_bytes)

    # 3) Progressive resize
    w, h = img.size
    while w > 50 and h > 50:
        w = max(int(w * 0.9), 50)
        h = max(int(h * 0.9), 50)
        img = img.resize((w, h), Image.LANCZOS)
        candidate = save_with_format(img, pil_format, quality=90)
        if len(candidate) <= target_bytes:
            return pad_file_to_size_safe(candidate, target_bytes)

    # Best-effort
    return pad_file_to_size_safe(out_bytes, target_bytes)

def _build_pdf_from_image_bytes(img_bytes: bytes, iw: int, ih: int, scale: float) -> bytes:
    """
    Place an image (already encoded) onto an A4 canvas at a given scale.
    Returns PDF bytes.
    """
    pw, ph = A4
    draw_w = iw * scale
    draw_h = ih * scale
    x = (pw - draw_w) / 2
    y = (ph - draw_h) / 2

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    reader = ImageReader(io.BytesIO(img_bytes))
    c.drawImage(reader, x, y, width=draw_w, height=draw_h, preserveAspectRatio=True, mask="auto")
    c.showPage()
    c.save()
    return buf.getvalue()

def image_to_pdf_exact(data: bytes, target_bytes: int) -> bytes:
    """
    Create a single-page PDF from an image with **shrinking**:
    - Try binary-search JPEG quality for the embedded image
    - If still too large, downscale the image and try again
    - Never truncate PDFs; pad to exact if under target
    """
    # Load original image for dimensions
    pil_img = Image.open(io.BytesIO(data))
    orig_w, orig_h = pil_img.size

    # We’ll embed a JPEG version of the image into the PDF (good balance)
    # Try scales from 1.0 down to ~0.3
    for scale in [1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.45, 0.4, 0.35, 0.3]:
        # Resize for this scale (affects PDF size strongly)
        w = max(int(orig_w * scale), 1)
        h = max(int(orig_h * scale), 1)
        scaled_img = pil_img.resize((w, h), Image.LANCZOS)

        # Binary search JPEG quality for the embedded image
        low, high = 30, 95   # keep above 30 for acceptable visual quality
        best_pdf = None

        while low <= high:
            mid = (low + high) // 2
            # encode JPEG at this quality
            jpg_bytes = save_with_format(scaled_img, "JPEG", quality=mid)
            # make a PDF placing that JPEG at the same pixel scale
            pdf_bytes = _build_pdf_from_image_bytes(jpg_bytes, w, h, scale=1.0)  # image already scaled
            size = len(pdf_bytes)
            if size <= target_bytes:
                best_pdf = pdf_bytes
                low = mid + 1
            else:
                high = mid - 1

        if best_pdf:
            # We got under target at this scale; pad to exact and return
            return pad_file_to_size_safe(best_pdf, target_bytes, is_pdf=True)

    # If we couldn’t get ≤ target even at smallest scale/quality, generate the smallest and pad
    # Make a very small, decent-quality fallback
    fallback = pil_img.resize((max(orig_w // 3, 1), max(orig_h // 3, 1)), Image.LANCZOS)
    jpg_bytes = save_with_format(fallback, "JPEG", quality=60)
    pdf_bytes = _build_pdf_from_image_bytes(jpg_bytes, fallback.width, fallback.height, scale=1.0)
    # If still larger, we must return as-is (never truncate). Otherwise pad.
    if len(pdf_bytes) < target_bytes:
        return pad_file_to_size_safe(pdf_bytes, target_bytes, is_pdf=True)
    return pdf_bytes  # larger but valid

def pdf_to_pdf_exact(data: bytes, target_bytes: int) -> bytes:
    """Pad PDF safely (never truncate)."""
    return pad_file_to_size_safe(data, target_bytes, is_pdf=True)

def pdf_to_image(data: bytes, out_fmt: str, target_bytes: int) -> bytes:
    if not PDF2IMAGE_OK:
        raise RuntimeError("pdf2image not installed. Run: pip install pdf2image")
    pages = convert_from_bytes(
        data, dpi=200, first_page=1, last_page=1,
        poppler_path=POPPLER_PATH
    )
    if not pages:
        raise RuntimeError("Could not render PDF page")
    pil_img = pages[0]
    return image_to_image_exact(_pil_to_bytes(pil_img, "PNG"), out_fmt, target_bytes)

def _pil_to_bytes(img: Image.Image, fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()

# ----------------- main run -----------------
if run and uploaded:
    data = uploaded.read()
    target_bytes = int(target_kb) * KB

    try:
        in_mime = uploaded.type or ""
        ext_in = os.path.splitext(uploaded.name)[1].lower()

        if in_mime.startswith("image") or ext_in in (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif"):
            if out_type == "pdf":
                st.info("Converting Image → PDF (shrinking to target)")
                out_data = image_to_pdf_exact(data, target_bytes)
            else:
                st.info(f"Converting Image → {out_type.upper()}")
                out_data = image_to_image_exact(data, out_type, target_bytes)

        elif in_mime == "application/pdf" or ext_in == ".pdf":
            if out_type == "pdf":
                st.info("Converting PDF → PDF (pad only; no truncation)")
                out_data = pdf_to_pdf_exact(data, target_bytes)
            else:
                st.info("Converting PDF → Image")
                out_data = pdf_to_image(data, out_type, target_bytes)

        else:
            st.error("Unsupported input file type")
            st.stop()

        st.success("✅ Conversion done!")
        st.download_button("⬇️ Download file", data=out_data, file_name=f"output.{out_type}")

    except Exception as e:
        st.error(f"Error: {e}")
