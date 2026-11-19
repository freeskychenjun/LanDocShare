# landoc/tools —— 随包分发的扫描件 OCR 工具

这个目录只有一个用途：让「这台机器没装中文 OCR」的电脑也能把扫描件 PDF 转出文字。
`landoc/ocr.py` 优先用 Windows 自带的系统 OCR（零体积、质量更好），系统 OCR 不可用时
才回落到这里的 Tesseract。

## 内容

| 路径 | 说明 | 体积 |
| --- | --- | --- |
| `ocr_win.ps1` | 调 Windows.Media.Ocr 的桥脚本（PowerShell 5.1，-Mode langs / batch） | 11 KB |
| `tesseract/` | Tesseract 5.5.0 的最小运行集（去掉 ICU 后 6 个文件） | 8.4 MB |
| `tessdata/` | `chi_sim` + `eng` 两个 fast 模型（不带 osd，走 `--psm 3`） | 6.3 MB |

`tesseract/` 里的文件：`tesseract.exe`、`libtesseract-5.dll`、`libleptonica-6.dll`、
`libstdc++-6.dll`、`libgcc_s_seh-1.dll`、`libwinpthread-1.dll`。少了 ICU 三个大 DLL
（`libicudt75`、`libicuin75`、`libicuuc75`）也能跑，简体中文识别不受影响。

## 来源与许可

- **Tesseract OCR 5.5.0**（<https://github.com/tesseract-ocr/tesseract>）：Apache License 2.0。
  二进制来自 UB Mannheim 的 Windows 构建（<https://github.com/UB-Mannheim/tesseract/wiki>）。
- **tessdata_fast 模型**（<https://github.com/tesseract-ocr/tessdata_fast>）：Apache License 2.0。
  fast 模型比 best 小一个数量级、速度快，代价是生僻字和表格线密集时错字更多。
- **`ocr_win.ps1`**：本项目原创，随主仓库一起按 MIT 授权。

完整许可见各上游仓库的 `LICENSE`；分发本项目的安装包时，请一并保留本文件的署名与许可说明。

## 换模型 / 加语言

把对应语言的 `*.traineddata` 丢进 `tessdata/`，再把 `landoc/ocr.py` 里 `_lang_codes()`
的语言映射补上即可（Tesseract 的语言码是 `chi_sim` / `chi_tra` / `eng` 这种，和
Windows OCR 的 BCP-47 标签不是一套）。体积每加一种语言大约 +2~4 MB。
