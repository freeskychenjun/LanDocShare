# LanDocShare（一期核心）

局域网个人资料库共享的**核心工程**：把加密/明文的办公文档转成 Markdown、建 FTS5 索引、通过只读 HTTP 服务给同事的 AI 助手检索。

设计与计划的来源：
- 设计：`docs/superpowers/specs/2026-11-16-lan-personal-library-design.md`
- 计划：`docs/superpowers/plans/2026-11-16-lan-library-core.md`

本仓库只含一期核心（转换 + 索引 + 检索服务）。托盘程序、开机自启、PyInstaller 打包、登记中心（registry）、客户端技能 `lan-lib-search` 属于二期。

## 快速开始

```bash
# 建库（共享目录可多选，重复 --folder）
python -m landoc init --lib-dir D:\MyLib --name "我的资料库" --folder "G:\2025年工作" --folder "G:\成果报告"

# 体检（抽样看明/密比例、本机有没有 Office / Acrobat）
python -m landoc probe --lib-dir D:\MyLib

# 转换第一批（增量：再跑只处理有变化的文件）
python -m landoc convert --lib-dir D:\MyLib

# 起服务（默认 8766，被占用自动顺延）
python -m landoc serve --lib-dir D:\MyLib

# 看统计
python -m landoc status --lib-dir D:\MyLib
```

检索接口（只读，只给片段与表格，永不给全文/下载）：

```bash
curl --get "http://<内网IP>:8766/api/search" --data-urlencode "q=马斯京根" --data-urlencode "limit=5"
curl --get "http://<内网IP>:8766/api/tables" --data-urlencode "path=成果/乐平.md"
```

端点：`/`、`/api/health`、`/api/meta`、`/api/stats`、`/api/search`、`/api/tree`、`/api/list`、`/api/tables`。

## 关键机制（都来自实测，别按直觉改）

| 现象 | 机制 | 实现位置 |
|---|---|---|
| 加密 docx 用 python 读会 `BadZipFile` | 透明加密驱动只对白名单进程（winword/wps/acrobat/acad…）透明解密 | `probe.classify` / `office_bridge` |
| Office 另存为的中间文件读不出来 | **加密与否取决于"谁写"**：白名单进程写盘一律是密文 | 故一律"Office 内存取字符串 → python 落盘" |
| 加密 docx 如何转 md | `doc.Content.WordOpenXML` 取 Flat OPC → python 补 `[Content_Types].xml` 重建 docx → `python-docx` 解析 | `office_bridge` |
| 加密 pdf 如何转文本 | 本机有 Acrobat 优先；否则用伪装名 `acrobat.exe` 解**临时副本**，抽完即删 | `pdf_conv` |
| 中文检索为什么用 trigram | jieba 分词边界对不上就零命中（查"马斯京根"vs 索引里"马斯京根法"）；trigram 做子串匹配 | `index.build_match_query` |
| 2 字查询（"乐平""洪水"） | 低于 trigram 最小长度，改走 `instr` 子串扫描 | `index._like_search` |

## 实测基准（本机，Python 3.14 / WPS + Office 2019）

三份真实加密样本（一份 880 KB docx、一份 2.3 MB pptx、一份 389 KB pdf）：

- 端到端 `init → convert → 索引 → 检索`：**3 篇 / 26.8 秒**（含 WPS 启动 6 秒；单篇约 1–2 秒，加密 pdf 走兜底工具约 3 秒）
- 加密 docx：31,545 字正文 + 13 张表格，Heading 1/2/3 完整
- 加密 pptx：逐页形状文本 + 表格
- 加密 pdf：21 页 / 8,451 字，文本层完好（不需要 OCR）

## 测试

```bash
python -m pytest tests -q                 # 单元测试（不需要 Office）
set LANSHARE_IT=1 && python -m pytest tests -q   # 追加真机用例（真实加密文档 + Office COM + 兜底工具）
```

## 目录

```
landoc/
  config.py        配置读写（原子写）
  probe.py         文件头体检（明文/密文/受阻）+ 抽样报告
  office_bridge.py COM 取 WordOpenXML、Flat OPC → docx 字节、超时与进程清理
  render_docx.py   docx → Markdown（标题/GFM 表格/剔批注）
  pptx_conv.py     pptx/ppt → Markdown（明文 python-pptx / 密文 COM）
  pdf_conv.py      pdf → 文本（pymupdf / Acrobat / 兜底临时副本）
  pipeline.py      扫描 → 分流 → 转换 → 落 md → 写索引 → 检查点
  index.py         FTS5 索引（trigram + 短查询兜底 + 表格抽取）
  server.py        只读检索服务（CIDR 权限 / 限流 / 审计）
  cli.py           命令行入口
```
