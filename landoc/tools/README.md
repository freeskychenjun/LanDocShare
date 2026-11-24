# landoc/tools —— 随包分发的扫描件 OCR 工具

这个目录只有一个用途：让「这台机器没装中文 OCR」的电脑也能把扫描件 PDF 转出文字。
`landoc/ocr.py` 优先用 Windows 自带的系统 OCR（零体积、质量更好），系统 OCR 不可用时
才回落到这里的 Tesseract。

## 内容

| 路径 | 说明 | 体积 |
| --- | --- | --- |
| `ocr_win.ps1` | 调 Windows.Media.Ocr 的桥脚本（PowerShell 5.1，-Mode langs / batch） | 11 KB |
| `tesseract/` | Tesseract 5.5.0 的完整运行集（exe + 35 个 DLL，见下） | 24.9 MB |
| `tessdata/` | `chi_sim` + `eng` 两个 fast 模型（不带 osd，走 `--psm 3`） | 6.3 MB |

`tesseract/` 里的文件：`tesseract.exe`、`libtesseract-5.dll`、`libleptonica-6.dll`、
`libstdc++-6.dll`、`libgcc_s_seh-1.dll`、`libwinpthread-1.dll`，外加 libtesseract /
libleptonica 依赖的一整套 MinGW/GNOME 库（`libpng16-16`、`libjpeg-8`、`libtiff-6`、
`libwebp-7`、`libarchive-13`、`libcurl-4`、`zlib1`、`libcrypto-3-x64` 等 29 个）。

这份清单是**试出来的，不是抄的**：把 UB Mannheim 安装目录的 DLL 全拷进来，逐个改名跑
一次 `tesseract probe.png stdout -l eng`，少了就崩的那 29 个留下。少一个的表现是
`0xC0000135`（找不到 DLL）。ICU 三个大 DLL（`libicudt75`、`libicuin75`、`libicuuc75`）
确实不需要，简体中文识别不受影响。

验法（别在装着 Tesseract 的机器上只跑 `tesseract --version`，那样会借系统里的同名 DLL）：

```powershell
python -c "from landoc import ocr; print(ocr.verify_bundled_tesseract())"
```

它会把 PATH 净成「本目录 + System32」再跑，返回 `(True, 'tesseract v5.5.0.20241111')`
才算过。`packaging/release.py` 出包前会自动跑这一条。

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
