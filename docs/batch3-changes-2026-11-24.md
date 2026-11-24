# 第 3 批改动（2026-11-24）

对应 `docs/optimization-plan-2026-11-23.md` 第六节第 3 批（发布链）：**1.2 Tesseract DLL 闭包**、
**3.7 打包瘦身**、**3.8 psutil 声明依赖**。

基线：0.2.22 + 第 1 批 + 第 2 批（HEAD `06077ff`），`python -m pytest tests -q` → 562 passed / 17 skipped。
本批结束后：**563 passed / 17 skipped**（新增 1 条 OCR 用例）。

---

## 1. 1.2 随包的 Tesseract 补齐 DLL 闭包

### 1.1 清单是试出来的，不是抄的

`landoc/tools/tesseract/` 原来只有 6 个文件（exe + 5 个 DLL）。方案说缺 29 个 DLL，但没说哪 29 个。
在本机 `C:\Program Files\Tesseract-OCR`（与随包那份是同一构建：6 个文件字节数逐一相同，
`tesseract v5.5.0.20241111`）里做了探测：

1. 把安装目录的 56 个 DLL 全部拷进临时目录（连同随包的 6 个）；
2. 用**净化 PATH**（只留临时目录 + `System32` + `SystemRoot`，确保不会偷偷用到安装目录）跑真 OCR：
   `tesseract.exe probe.png stdout -l eng --tessdata-dir <repo>/landoc/tools/tessdata`；
3. 能跑通后逐个把 DLL 改名，跑不起来的那个就是必需的。

结论：**29 个 DLL / 17,264,192 B（16.5 MB）**，与方案给的数字一致。探测脚本留在
`%TEMP%\tess_closure.py`（不进仓库）。

### 1.2 收进仓库的文件

`landoc/tools/tesseract/` 现在 35 个文件 / 24.87 MB：

| 类别 | 文件 |
|---|---|
| 原有 | `tesseract.exe`、`libtesseract-5.dll`、`libleptonica-6.dll`、`libgcc_s_seh-1.dll`、`libstdc++-6.dll`、`libwinpthread-1.dll` |
| 新增 29 个 | libarchive-13、libb2-1、libbrotlicommon、libbrotlidec、libbz2-1、libcrypto-3-x64、libcurl-4、libdeflate、libexpat-1、libgif-7、libiconv-2、libidn2-0、libintl-8、libjbig-0、libjpeg-8、libLerc、liblz4、liblzma-5、libopenjp2-7、libpng16-16、libpsl-5、libsharpyuv-0、libssh2-1、libtiff-6、libunistring-5、libwebp-7、libwebpmux-3、libzstd、zlib1 |

ICU 三个（libicudt75 / libicuin75 / libicuuc75）实测不需要，没收。

### 1.3 净 PATH 自检：`ocr.verify_bundled_tesseract()`

```python
from landoc import ocr
print(ocr.verify_bundled_tesseract())   # (True, 'tesseract v5.5.0.20241111')
```

- 把 `PATH` 净成 `[exe 所在目录, %SystemRoot%\System32, %SystemRoot%]` 再跑 `tesseract.exe --version`；
- 返回码 `0xC0000135`（及其有符号形式 `-1073741515`）时在 detail 里点明「多半是 DLL 闭包不全」；
- 为什么要净化：开发机上 `C:\Program Files\Tesseract-OCR` 在 PATH 里，不净化的话
  缺 DLL 也能跑起来，问题只在同事机器上炸——这正是第 1 批遗留的「发布检查」要防的。

### 1.4 发布前强制自检

`packaging/release.py` 新增 `_check_bundled_tesseract()`，`build_dist()` 第一行调用：

- 通过 → 打印 `      随包 Tesseract 自检通过：tesseract v5.5.0.20241111`；
- 失败 → `RuntimeError: 随包的 Tesseract 跑不起来：…（净 PATH 下要能 --version）`，出包中止。

`tests/test_ocr.py` 新增 `test_bundled_tesseract_runs_without_the_installed_one`
（exe 不在就 skip，在就必须在净 PATH 下跑通）。这条用例是「不再跳过」的落点：
以前 `tests/test_ocr.py` 对 tesseract 的检查在没装 Tesseract 的机器上直接 skip，等于没检查。

---

## 2. 3.7 打包瘦身

### 2.1 `packaging/LanDocShare.spec` 三处改动

1. `EXCLUDES` 加 `"numpy"`、`"numpy.linalg"`、`"win32ui"`；
2. `hiddenimports` 加 `"psutil"`（第 3 批 3.8 的落脚点，见第 3 节）；
3. `Analysis(...)` 之后按**目标路径**过滤 `a.binaries`，把 `landoc/tools/` 里的东西从
   binaries 里剔掉、只留 `datas` 那一份：

```python
_TOOLS = str(ROOT / "landoc" / "tools")
a.binaries = [
    entry for entry in a.binaries
    if not (str(entry[1]).startswith(_TOOLS)
            and not str(entry[0]).replace("\\", "/").startswith("landoc/tools/"))
]
```

原来 PyInstaller 把随包的 DLL 同时当成两份收：`datas` 一份（`_internal\landoc\tools\tesseract\`）、
binaries 一份（`_internal\` 顶层）。顶层那份是白占 8.3 MB 的重复。

### 2.2 踩到的坑：按源路径过滤会砍掉要留的那份

第一版写成按**源**路径过滤（`not str(entry[1]).startswith(_TOOLS)`），结果把该留的那份也砍了：
TOC 里合法条目的 `dest` 是 `landoc\tools\tesseract\libtesseract-5.dll`、**源也在 tools 目录**，
两者都指向 tools。所以判别必须看 `dest` 的前缀是不是 `landoc/tools/`，不能只看源。

复现方式：`dist\LanDocShare\_internal\landoc\tools\tesseract` 整个目录消失 →
`ocr.verify_bundled_tesseract()` 立刻报错（这正是 1.4 那条发布检查的价值）。

### 2.3 体积对比

同一个 spec、同一台机器，`dist`（改动前，0.2.22 + 第 1/2 批）vs `dist-next1`（改动后）：

| 指标 | 改动前 | 改动后 | 差 |
|---|---|---|---|
| 文件数 | 1159 | 1125 | −34 |
| 总大小 | 183.5 MB | **138.5 MB** | **−45.0 MB** |
| `_internal\numpy` | 有（23 文件） | 无 | −26.6 MB |
| `_internal\Pythonwin` | 有（2 文件，含 mfc140u.dll） | 无 | −11.8 MB |
| `_internal` 顶层重复 DLL | 有（38 个，含 libtesseract-5、libarchive-13…） | 无 | −8.3 MB |
| `_internal\landoc\tools\tesseract\` | 6 文件 | 35 文件 | +16.5 MB |

`_internal` 顶层现在剩 52 个 DLL，全是必须的（api-ms-win-*、python314、tcl86t/tk86t、
sqlite3、libcrypto-3/libssl-3、VCRUNTIME140 等），没有 libtesseract，也没有 numpy/Pythonwin。

> 注：`numpy` 那 26.6 MB 是 `pymupdf` 带进来的可选依赖，程序里没有 `import numpy`；
> `win32ui` 同理（只用到 pywin32 的 `win32com`/`win32api`）。出包后四类文档都转过（见第 5 节），
> 没有因为少它们炸。

---

## 3. 3.8 psutil 声明依赖

`landoc/netinfo.py` 用 psutil 算「本机所在网段」当默认允许网段，但 `pyproject.toml` 里没声明它：
开发机装着所以看不出问题，干净环境装完包启动就 `ImportError`。

两选一，这里选**声明依赖**（另一条路是改成 ctypes 调 `GetAdaptersAddresses`，要重写网卡枚举）：

- `pyproject.toml` → `dependencies` 加 `"psutil"`；
- spec → `hiddenimports` 加 `"psutil"`（PyInstaller 有时抓不到纯运行时导入的模块）。

---

## 4. 测试

| 文件 | 用例 | 断言 |
|---|---|---|
| `tests/test_ocr.py` | `test_bundled_tesseract_runs_without_the_installed_one` | 净化 PATH 下 `tesseract.exe --version` 返回 0 |

全量：`python -m pytest tests -q` → **563 passed / 17 skipped**。

---

## 5. 打包与冒烟验证

构建（**没有覆盖 `dist/`**，因为用户托盘正跑着 `dist\LanDocShare\LanDocShare.exe`）：

```
python -m PyInstaller packaging\LanDocShare.spec --noconfirm --clean --distpath dist-next1 --workpath build-probe
```

冒烟脚本 `%TEMP%\landoc_smoke_b3.py`，13 项全过：

| # | 检查 | 结果 |
|---|---|---|
| 1 | dist 里 `landoc\tools\tesseract\` 有 35 个文件 | 35 |
| 2 | 打包产物里的 tesseract 在**净 PATH** 下能跑 | `rc=0 tesseract v5.5.0.20241111` |
| 3 | 样本就位（docx / pdf / pptx / csv） | 4 篇 |
| 4 | 打包版 `init` 成功 | rc=0 |
| 5 | 打包版 `convert` 成功 | `[OK] 新转换 3 篇` |
| 6-8 | docx / pdf / pptx 各自转出 md | `报告.docx.md`、`说明.pdf.md`、`演示.pptx.md` |
| 9 | 打包版 `status` 能跑（psutil 已随包、无 ImportError） | rc=0 |
| 10 | `status` 输出里没有 ImportError | 无 |
| 11 | 包里没有 numpy | 无 |
| 12 | 包里没有 Pythonwin | 无 |
| 13 | `_internal` 顶层没有 libtesseract-5.dll（重复已消） | 无 |

csv 没进 md 是因为它不在转换清单里（0.2.22 只转 doc/docx/pdf/ppt/pptx/wps），样本里留着只为凑齐「四类文档」的现场。

---

## 6. 没做的事 / 注意事项

1. **`dist/` 没动**：本批验证都在 `dist-next1/`（已在 `.gitignore`）。用户托盘退出后跑
   `python packaging\release.py` 才会重新产出正式 `dist/`；`build-probe/` 已删。
2. **16.5 MB 二进制进了 git**：29 个 DLL 是仓库新增，push 后仓库会变大。方案 1.2 的结论就是
   「DLL 收进仓库」（否则每个同事的机器都得先装 Tesseract，而 Tesseract 的中文模型本来就在仓库里）。
   若以后觉得仓库太大，可改 Git LFS——但那是另一件事。
3. **ICU 没进**：实测不需要。以后升级 Tesseract 版本要重跑 `%TEMP%\tess_closure.py` 重新试。
4. **发布检查只在 `build_dist()` 里**：手工跑 `git archive` 或别的打包方式不会触发；
   正式的出包入口是 `packaging/release.py`。
5. 3.7 的 `numpy` 排除依赖「程序里没有 numpy 导入」这一事实；以后若有人 `import numpy`（比如新
   的表格处理），这个 EXCLUDES 会变成运行时 ImportError，出包后四类文档冒烟能兜住。
