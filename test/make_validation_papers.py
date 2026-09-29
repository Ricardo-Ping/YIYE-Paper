"""生成 5 篇基础验收论文(调研文档 P1:单栏、双栏、公式、表格、扫描件)。

用法:
    .venv\\Scripts\\python.exe test/make_validation_papers.py [输出目录]

生成的论文全部为可合法分发的合成内容,用于验收翻译链路:
    01-单栏文本      常规单栏结构(标题/摘要/章节/参考文献)
    02-双栏排版      双栏学术排版
    03-公式与引用    数学记号、引用编号、DOI、URL、{v1} 占位符
    04-大表格        网格表格(表头+多行数据)
    05-扫描件        整页图片、无文字层(验证 OCR workaround 与预检警告)
"""

import sys
from pathlib import Path

import pymupdf

FONT = "times-roman"
BODY = "Deep learning models have transformed natural language processing, enabling systems that translate, summarize and reason over long documents with remarkable accuracy. "


def textbox(page, rect, text, size=10, font=FONT):
    page.insert_textbox(rect, text, fontsize=size, fontname=font)


def paper_single_column(path: Path):
    doc = pymupdf.open()
    page = doc.new_page()
    textbox(page, pymupdf.Rect(72, 72, 540, 110), "A Study of Single-Column Academic Paper Translation", 16)
    textbox(page, pymupdf.Rect(72, 116, 540, 150), "Abstract. " + BODY * 2, 10)
    y = 160
    for section in ["1. Introduction", "2. Related Work", "3. Method", "4. Experiments"]:
        textbox(page, pymupdf.Rect(72, y, 540, y + 16), section, 12)
        textbox(page, pymupdf.Rect(72, y + 20, 540, y + 96), BODY * 2, 10)
        y += 104
    textbox(page, pymupdf.Rect(72, y + 4, 540, y + 120),
            "References\\n[1] J. Smith, Deep Learning Basics, 2024.\\n[2] A. Lee, Translation Survey, 2025.\\n[3] R. Chen, Evaluation Metrics, 2026.", 9)
    doc.save(path)
    doc.close()


def paper_two_column(path: Path):
    doc = pymupdf.open()
    page = doc.new_page()
    textbox(page, pymupdf.Rect(72, 60, 540, 96), "Two-Column Layout Evaluation Paper for Translation Pipelines", 15)
    left = pymupdf.Rect(56, 110, 294, 760)
    right = pymupdf.Rect(316, 110, 554, 760)
    textbox(page, left, "Introduction. " + BODY * 4 + " Related work spans translation engines and layout analysis. " + BODY, 9)
    textbox(page, right, "Method. We evaluate layout preservation across engines. " + BODY * 3 +
            " Experiments. Tables, figures and formulas are checked after translation. " + BODY, 9)
    doc.save(path)
    doc.close()


def paper_formula_citations(path: Path):
    doc = pymupdf.open()
    page = doc.new_page()
    textbox(page, pymupdf.Rect(72, 72, 540, 100), "Formulas, Citations and Placeholder Robustness", 15)
    body = (
        "The attention score is computed as softmax(QK^T / sqrt(d_k)) V, and the loss "
        "L = -sum(y log(y_hat)) is minimized during training. Prior studies [1][2][12] "
        "report p < 0.05 significance. The dataset is available at "
        "https://doi.org/10.1000/fake-doi and https://example.com/dataset. "
        "As shown in {v1}, the styled segment <style id='1'>keeps markup</style> intact. "
        "Contact: author@example.com. Code returns exit code %d with %s format strings. "
    ) * 3
    textbox(page, pymupdf.Rect(72, 110, 540, 700), body, 10)
    doc.save(path)
    doc.close()


def paper_table(path: Path):
    doc = pymupdf.open()
    page = doc.new_page()
    textbox(page, pymupdf.Rect(72, 60, 540, 84), "Benchmark Results", 15)
    cols = [72, 190, 300, 410, 520]
    rows = [96, 130, 164, 198, 232, 266]
    for x in cols:
        page.draw_line(pymupdf.Point(x, rows[0]), pymupdf.Point(x, rows[-1]))
    for y in rows:
        page.draw_line(pymupdf.Point(cols[0], y), pymupdf.Point(cols[-1], y))
    headers = ["Model", "BLEU", "COMET", "Latency"]
    for i, h in enumerate(headers):
        textbox(page, pymupdf.Rect(cols[i] + 4, rows[0] + 4, cols[i + 1] - 4, rows[1] - 4), h, 10)
    data = [
        ["Engine A", "41.2", "0.86", "12.4"],
        ["Engine B", "39.8", "0.84", "9.1"],
        ["Engine C", "43.5", "0.88", "21.7"],
        ["Engine D", "37.1", "0.81", "6.3"],
    ]
    for r, row in enumerate(data):
        for c, cell in enumerate(row):
            textbox(page, pymupdf.Rect(cols[c] + 4, rows[r + 1] + 4, cols[c + 1] - 4, rows[r + 2] - 4), cell, 10)
    textbox(page, pymupdf.Rect(72, 290, 540, 330), "Table 1: Translation quality and cost comparison across engines.", 9)
    textbox(
        page,
        pymupdf.Rect(72, 336, 540, 372),
        "As shown in Table 1, Engine C attains the best BLEU and COMET scores, "
        "while Engine D offers the lowest latency at some cost to quality.",
        10,
    )
    doc.save(path)
    doc.close()


def paper_scanned(path: Path):
    # 先把文字渲染成图片,再把整页图片包进新 PDF —— 无文字层,模拟扫描件
    tmp_img = path.with_suffix(".png")
    doc = pymupdf.open()
    page = doc.new_page()
    textbox(page, pymupdf.Rect(72, 72, 540, 220), "This page simulates a scanned document. " * 6, 13)
    textbox(page, pymupdf.Rect(72, 240, 540, 620), "Scanned pages contain no machine-readable text layer, so translation engines must rely on OCR workarounds. " * 5, 12)
    pix = page.get_pixmap(dpi=110)
    pix.save(str(tmp_img))
    doc.close()

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(page.rect, filename=str(tmp_img))
    doc.save(path)
    doc.close()
    tmp_img.unlink()


GENERATORS = [
    ("01-单栏文本.pdf", paper_single_column),
    ("02-双栏排版.pdf", paper_two_column),
    ("03-公式与引用.pdf", paper_formula_citations),
    ("04-大表格.pdf", paper_table),
    ("05-扫描件.pdf", paper_scanned),
]


def main():
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "validation-papers"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, generator in GENERATORS:
        target = out_dir / name
        generator(target)
        # 验证可打开与页数
        check = pymupdf.open(str(target))
        print(f"{name}: {check.page_count} 页")
        check.close()
    print(f"验收论文集已生成到 {out_dir}")


if __name__ == "__main__":
    main()
