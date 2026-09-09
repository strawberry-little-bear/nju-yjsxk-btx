"""Simple open-source captcha OCR using ddddocr.

Can be used as a module:
    from captcha_ocr import recognize_image, recognize_bytes
    code = recognize_image("captcha_screenshots/1.png")

Or as a CLI:
    python captcha_ocr.py            # use the latest image in captcha_screenshots
    python captcha_ocr.py some.png   # use a specific image path
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import ddddocr

IMAGE_DIRECTORY = Path("captcha_screenshots")
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

_OCR = None


def _get_ocr():
    """懒加载单例，避免每次识别都重新加载模型。"""
    global _OCR
    if _OCR is None:
        _OCR = ddddocr.DdddOcr(show_ad=False)
    return _OCR


def normalize(result: str) -> str:
    """强制大写，并只保留字母和数字。"""
    return re.sub(r"[^A-Z0-9]", "", result.upper())


def recognize_bytes(image_bytes: bytes, *, uppercase: bool = True) -> str:
    """识别图片字节数据，默认返回强制大写后的验证码文本。"""
    raw_result = _get_ocr().classification(image_bytes)
    return normalize(raw_result) if uppercase else raw_result


def recognize_image(image_path, *, uppercase: bool = True) -> str:
    """识别图片文件，默认返回强制大写后的验证码文本。"""
    return recognize_bytes(Path(image_path).read_bytes(), uppercase=uppercase)


def pick_image(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.exists():
            raise FileNotFoundError(f"找不到图片：{path}")
        return path
    if not IMAGE_DIRECTORY.exists():
        raise FileNotFoundError(f"缺少图片目录：{IMAGE_DIRECTORY}")
    images = sorted(
        p for p in IMAGE_DIRECTORY.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
    )
    if not images:
        raise FileNotFoundError(f"{IMAGE_DIRECTORY} 中没有图片文件")
    return images[-1]


def main() -> int:
    image_path = pick_image(sys.argv[1] if len(sys.argv) > 1 else None)
    print(f"[图片] {image_path}")
    result = recognize_image(image_path)
    print(f"[识别] {result}")
    Path("ocr_output.txt").write_text(result + "\n", encoding="utf-8")
    print("[保存] 已写入 ocr_output.txt")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, ValueError) as exc:
        print(f"[错误] {exc}")
        raise SystemExit(1)
