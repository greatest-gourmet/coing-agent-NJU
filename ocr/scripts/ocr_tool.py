# -*- coding: utf-8 -*-
"""
本地 OCR 能力封装（离线，无需联网）· 高精度版
=================================================
引擎优先级（自动选择，均本地离线）：
  1. Windows 原生 OCR（Windows.Media.Ocr，微软官方引擎，中文精度最高）★首选
  2. RapidOCR（若已安装，基于 PaddleOCR 模型，复杂版式/表格效果好）
  3. Tesseract（系统已装，回退兜底）

识别能力：
  - 图片（png/jpg/jpeg/bmp/webp/tiff/gif）文字
  - PDF 文字（逐页转图片后 OCR）
  - 图片预处理（放大/灰度/二值化/降噪/对比度增强）提升识别率

调用方式：
  1. 命令行：  python ocr_tool.py <file_path> [--json] [--engine auto|win|rapid|tess] [--preprocess]
  2. Python：  from ocr_tool import ocr_file
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

IMG_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff", ".gif"}
PDF_EXTS = {".pdf"}

# 全局引擎状态
_rapid_engine = None
_rapid_available = None
_win_available = None
_win_langs = None

# Windows OCR 语言
WIN_LANG = "zh-Hans"


# ── 引擎可用性检测 ───────────────────────────────────────────────

def _winocr_ready():
    """检测 Windows 原生 OCR 是否可用"""
    global _win_available, _win_langs
    if _win_available is not None:
        return _win_available
    try:
        import winocr
        _win_langs = [x.language_tag
                      for x in winocr.OcrEngine.available_recognizer_languages]
        _win_available = True
    except Exception:
        _win_available = False
    return _win_available


def _winocr_pick_lang(preferred="zh-Hans"):
    """从系统可用 OCR 语言中挑选中文语言标签"""
    if not _winocr_ready():
        return None
    for tag in _win_langs:
        low = tag.lower()
        if "zh" in low or "hans" in low or "cn" in low:
            return tag
    return _win_langs[0] if _win_langs else None


def _find_tesseract():
    """定位 tesseract 可执行文件"""
    candidates = [
        "tesseract",
        r"C:\Users\udaiw063\scoop\shims\tesseract.exe",
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    ]
    for c in candidates:
        try:
            r = subprocess.run([c, "--version"], capture_output=True, text=True, timeout=20)
            if r.returncode == 0:
                return c
        except Exception:
            continue
    return None


def _rapidocr_ready():
    """检查 rapidocr 是否可用（可选增强）"""
    global _rapid_available
    if _rapid_available is None:
        try:
            import rapidocr  # noqa
            _rapid_available = True
        except Exception:
            _rapid_available = False
    return _rapid_available


# ── 图片预处理（提升识别率）────────────────────────────────────

def preprocess_image(img, scale=2.0):
    """放大 + 灰度 + 对比度增强 + 自适应二值化，提升小字/低清图识别率。"""
    from PIL import Image, ImageEnhance, ImageOps, ImageFilter
    # 放大
    if scale > 1.0:
        w, h = img.size
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    # 转 RGB
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    # 灰度
    gray = ImageOps.grayscale(img)
    # 自动对比度拉伸
    gray = ImageOps.autocontrast(gray)
    # 对比度增强
    gray = ImageEnhance.Contrast(gray).enhance(1.5)
    # 轻微降噪
    gray = gray.filter(ImageFilter.GaussianBlur(0.5))
    return gray


# ── 各引擎识别实现 ──────────────────────────────────────────────

def _clean_win_text(t):
    """清理 Windows OCR 输出：去掉 CJK 字符间的空格，保留英文/数字单词间空格。"""
    import re
    # 去掉 CJK 字符之间的空格（如 '探 索 未 至 之 境' -> '探索未至之境'）
    t = re.sub(r'(?<=[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]) (?=[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef])', '', t)
    # 去掉 CJK 与标点间的空格
    t = re.sub(r'(?<=[\u4e00-\u9fff]) (?=[，。！？；：、""''（）])', '', t)
    t = re.sub(r'(?<=[，。！？；：、""''（）]) (?=[\u4e00-\u9fff])', '', t)
    return t


def ocr_image_win(img, lang=None):
    """Windows 原生 OCR。img 为 PIL Image 或路径。返回 [(text, score), ...]"""
    import winocr
    from PIL import Image
    if lang is None:
        lang = _winocr_pick_lang() or WIN_LANG
    if isinstance(img, str):
        img = Image.open(img)
    res = winocr.recognize_pil_sync(img, lang)
    lines = []
    for ln in (res.get("lines") or []):
        t = _clean_win_text((ln.get("text") or "").strip())
        if t:
            lines.append((t, 100.0))
    return lines


def ocr_image_tesseract(img, lang="chi_sim+eng"):
    """用 tesseract 识别。img 为 PIL Image 或路径。返回 [(text, score), ...]"""
    tess = _find_tesseract()
    if not tess:
        return []
    from PIL import Image
    tmp_in = os.path.join(tempfile.gettempdir(), "_ocr_ts_in.png")
    tmp_out = os.path.join(tempfile.gettempdir(), "_ocr_ts")
    if isinstance(img, str):
        import shutil
        shutil.copy(img, tmp_in)
    else:
        img.convert("RGB").save(tmp_in)
    cmd = [tess, tmp_in, tmp_out, "-l", lang, "--psm", "3", "tsv"]
    try:
        subprocess.run(cmd, capture_output=True, timeout=120)
    except Exception:
        return []
    tsv = tmp_out + ".tsv"
    if not os.path.exists(tsv):
        return []
    lines = []
    with open(tsv, encoding="utf-8", errors="replace") as f:
        rows = list(f)
    if not rows:
        return []
    header = rows[0].rstrip("\n").split("\t")
    try:
        ci_text = header.index("text")
        ci_conf = header.index("conf")
        ci_left = header.index("left")
        ci_top = header.index("top")
    except ValueError:
        return []
    grouped = {}
    for row in rows[1:]:
        cols = row.rstrip("\n").split("\t")
        if len(cols) <= ci_top:
            continue
        text = cols[ci_text] if len(cols) > ci_text else ""
        conf = cols[ci_conf] if len(cols) > ci_conf else ""
        top = cols[ci_top] if len(cols) > ci_top else "0"
        left = cols[ci_left] if len(cols) > ci_left else "0"
        if not text or text.isspace():
            continue
        try:
            conf_f = float(conf) if conf else 0.0
            top_i = int(float(top))
            left_i = int(float(left))
        except ValueError:
            continue
        key = top_i // 8
        if key not in grouped:
            grouped[key] = []
        grouped[key].append((left_i, text, conf_f))
    for key in sorted(grouped.keys()):
        row_items = sorted(grouped[key], key=lambda x: x[0])
        row_text = "".join(t for _, t, _ in row_items)
        avg_conf = sum(c for _, _, c in row_items) / len(row_items)
        lines.append((row_text, avg_conf))
    for suffix in (".tsv", ".txt"):
        p = tmp_out + suffix
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass
    return lines


def ocr_image_rapid(img):
    """用 rapidocr 识别 -> [(text, score), ...]"""
    from rapidocr import RapidOCR
    global _rapid_engine
    if _rapid_engine is None:
        _rapid_engine = RapidOCR()
    result = _rapid_engine(img)
    out = []
    if result is None:
        return out
    txts = getattr(result, "txts", None) or []
    scores = getattr(result, "scores", None) or []
    for i, txt in enumerate(txts):
        if txt and str(txt).strip():
            out.append((str(txt), float(scores[i]) if i < len(scores) else 0.0))
    return out


def ocr_image(img, engine="auto", preprocess=False):
    """识别单张图片。engine: auto|win|rapid|tess"""
    from PIL import Image
    is_path = isinstance(img, str)
    work_img = img

    # Tesseract 对低清图敏感，始终预处理
    if preprocess or engine == "tess":
        try:
            if is_path:
                work_img = Image.open(img)
            else:
                work_img = img
            work_img = preprocess_image(work_img)
        except Exception:
            work_img = img

    # Windows OCR 也放大（保留色彩）
    if engine in ("auto", "win") and not isinstance(work_img, str):
        try:
            from PIL import Image as _I
            w, h = work_img.size
            work_img = work_img.resize((int(w * 1.5), int(h * 1.5)), _I.LANCZOS)
        except Exception:
            pass

    if engine == "win" or (engine == "auto" and _winocr_ready()):
        try:
            items = ocr_image_win(work_img)
            if items:
                return items
        except Exception:
            pass
    if engine == "rapid" or (engine == "auto" and _rapidocr_ready()):
        try:
            items = ocr_image_rapid(work_img)
            if items:
                return items
        except Exception:
            pass
    return ocr_image_tesseract(work_img)


# ── PDF 处理 ────────────────────────────────────────────────────

def pdf_to_images(pdf_path, dpi=200):
    """把 PDF 转成图片列表（PIL Image）"""
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(pdf_path)
        return [pdf[i].render(scale=dpi / 72.0).to_pil() for i in range(len(pdf))]
    except Exception:
        pass
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(pdf_path)
        from PIL import Image
        imgs = []
        for page in doc:
            pix = page.get_pixmap(dpi=dpi)
            imgs.append(Image.frombytes("RGB", [pix.width, pix.height], pix.samples))
        return imgs
    except Exception:
        return None


def ocr_pdf(pdf_path, engine="auto", preprocess=False):
    """识别 PDF -> [(page_no, text), ...]"""
    imgs = pdf_to_images(pdf_path)
    if imgs is None:
        try:
            from pypdf import PdfReader
            reader = PdfReader(pdf_path)
            return [(i + 1, (p.extract_text() or "").strip())
                    for i, p in enumerate(reader.pages)]
        except Exception:
            return []
    pages = []
    for i, img in enumerate(imgs):
        items = ocr_image(img, engine=engine, preprocess=preprocess)
        text = "\n".join(t for t, _ in items)
        pages.append((i + 1, text))
    return pages


def ocr_file(path, engine="auto", preprocess=False):
    """统一入口：识别图片或 PDF，返回结构化结果 dict"""
    path = os.path.abspath(path)
    ext = os.path.splitext(path)[1].lower()
    if not os.path.exists(path):
        return {"ok": False, "error": f"文件不存在: {path}"}
    try:
        if ext in PDF_EXTS:
            pages = ocr_pdf(path, engine=engine, preprocess=preprocess)
            return {
                "ok": True,
                "type": "pdf",
                "engine": engine,
                "pages": [{"page": p, "text": t} for p, t in pages if t],
            }
        elif ext in IMG_EXTS:
            items = ocr_image(path, engine=engine, preprocess=preprocess)
            return {
                "ok": True,
                "type": "image",
                "engine": engine,
                "lines": [{"text": t, "score": s} for t, s in items],
                "text": "\n".join(t for t, _ in items),
            }
        else:
            return {"ok": False, "error": f"不支持的扩展名 {ext}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def main():
    ap = argparse.ArgumentParser(description="本地 OCR 识别工具（高精度）")
    ap.add_argument("path", nargs="?", help="图片或 PDF 文件路径")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--engine", default="auto",
                    choices=["auto", "win", "rapid", "tess"],
                    help="引擎：auto(默认)|win(Windows原生)|rapid|tess")
    ap.add_argument("--preprocess", action="store_true", help="图片预处理(放大/增强)")
    ap.add_argument("--selftest", action="store_true", help="自测（对比各引擎）")
    args = ap.parse_args()

    if args.selftest:
        run_selftest()
        return
    if not args.path:
        ap.print_help()
        return

    result = ocr_file(args.path, engine=args.engine, preprocess=args.preprocess)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if result.get("ok"):
            if result.get("type") == "pdf":
                for pg in result["pages"]:
                    print(f"--- 第 {pg['page']} 页 ---")
                    print(pg["text"])
            else:
                print(result.get("text", ""))
        else:
            print(f"[错误] {result.get('error')}")
            sys.exit(1)


def run_selftest():
    """生成含中文/英文测试图，对比各引擎识别效果"""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (800, 260), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    texts = ["探索未至之境", "Hello RapidOCR 12345", "中文识别精度测试",
             "Supplier: 延锋 ASQE", "零件编号 P/N: 1234567890"]
    y = 15
    for t in texts:
        try:
            font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 32)
        except Exception:
            font = ImageFont.load_default()
        draw.text((25, y), t, fill=(0, 0, 0), font=font)
        y += 48
    tmp = os.path.join(tempfile.gettempdir(), "ocr_selftest2.png")
    img.save(tmp)
    print(f"[自测] 测试图: {tmp}\n")

    engines = []
    if _winocr_ready():
        engines.append(("win", "Windows原生OCR"))
    if _rapidocr_ready():
        engines.append(("rapid", "RapidOCR"))
    if _find_tesseract():
        engines.append(("tess", "Tesseract"))
    if not engines:
        print("[自测] 无可用引擎")
        return

    for eng, name in engines:
        print(f"===== {name} =====")
        items = ocr_image(tmp, engine=eng, preprocess=True)
        for t, s in items:
            print(f"  {t}  (conf={s:.1f})")
        print()
    print("[自测] 完成。对比各引擎输出即可判断精度。")


if __name__ == "__main__":
    main()
