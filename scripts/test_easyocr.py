"""测试 EasyOCR 可用性"""

import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def test_easyocr():
    """测试 EasyOCR 安装和初始化"""
    print("=" * 60)
    print("【1】EasyOCR 安装检查")
    print("=" * 60)

    # 检查 EasyOCR 是否安装
    try:
        import easyocr

        print(f"  ✅ EasyOCR 已安装（版本: {easyocr.__version__}）")
    except ImportError:
        print("  ❌ EasyOCR 未安装")
        print("  → 安装命令: pip install easyocr torch torchvision")
        return

    # 检查 PyTorch 是否安装
    try:
        import torch

        print(f"  ✅ PyTorch 已安装（版本: {torch.__version__}）")
    except ImportError:
        print("  ❌ PyTorch 未安装")
        return

    print()
    print("=" * 60)
    print("【2】EasyOCR 初始化测试")
    print("=" * 60)

    try:
        print("  → 正在初始化 EasyOCR（首次使用会下载模型，约 500MB）...")
        reader = easyocr.Reader(
            ["ch_sim", "en"],  # 简体中文 + 英文
            gpu=False,  # 使用 CPU
            download_enabled=True,
            verbose=False,
        )
        print("  ✅ EasyOCR 初始化成功")

        # 获取模型信息
        print("  → 使用 CPU 模式")

    except Exception as e:
        print(f"  ❌ EasyOCR 初始化失败: {e}")
        return

    print()
    print("=" * 60)
    print("【3】OCR 提取器测试")
    print("=" * 60)

    try:
        from src.ml.ocr import get_ocr_extractor

        extractor = get_ocr_extractor()

        if extractor.is_available:
            print("  ✅ OCR 提取器初始化成功")
            print("  → OCR 功能可用")
        else:
            print("  ❌ OCR 提取器不可用")

    except Exception as e:
        print(f"  ❌ OCR 提取器初始化失败: {e}")
        import traceback

        traceback.print_exc()

    print()
    print("=" * 60)
    print("测试完成")
    print("=" * 60)


if __name__ == "__main__":
    test_easyocr()
