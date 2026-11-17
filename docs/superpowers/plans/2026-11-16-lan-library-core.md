# LanDocShare 一期·核心（转换 + 索引 + 检索服务）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 做出一个可在任意同事机器上跑起来的核心工程 `landoc`：把加密/明文 Office 与 PDF 文档转成 Markdown、建 FTS5 索引、并通过局域网只读检索 HTTP 服务对外提供片段与表格。

**Architecture:** 单进程 Python 工程，三层：`probe`（读文件头分流明/密/受阻）→ `pipeline`（按类型选转换器，Office 走 COM 取内存 OOXML、python 落盘明文）→ `index`（每库一个 sqlite，FTS5 + jieba 分词）。`server` 是对现有 `F:\md_share\share_server.py` 的等价重写，保留其全部执行细节（覆盖索引两步查、UTF-8/GBK 双解码、限流、审计）。本计划**不含**托盘 UI、开机自启、PyInstaller 打包、registry 中心、客户端 skill —— 那是二期计划，其接口依赖本计划的 `server` 契约。

**Tech Stack:** Python 3.14、sqlite3(FTS5)、jieba、python-docx、python-pptx、pymupdf、pywin32(win32com)、pytest。

**Spec:** `docs/superpowers/specs/2026-11-16-lan-personal-library-design.md`（本计划的每个"为什么"都在该文档里；执行者必须两份都读）

## Global Constraints

- Python 版本：3.14（本机 `C:\Python314`）。
- 加密判定：文件前 4 字节 == `17DA5FA0` → 加密；`PK03 04` / `D0CF11E0` / `%PDF` → 明文。
- 转换铁律：**文本只能从 Office 进程内存取出、由 python 落盘**；任何"让 Office 另存为中间文件再读"的写法都是错的（白名单进程写盘即加密）。
- 单文档转换超时：120 秒；超时即杀 Office 进程并记失败，不阻塞队列。
- 批注必须剔除（Flat OPC 里的 `comments.xml` 不得进入 md）。
- 不转换 `.xls/.xlsx/.et`（跳过并计入统计）。
- 检索服务只输出片段与表格，**永不输出全文/下载/`source_path`**。
- 端口默认 8766；被占用时自动 8767、8768…，实际端口写入 `meta`。
- 权限默认 `allow_cidrs=["10.91.0.0/16"]`；越权 403、姓名未登记 401。
- 审计：JSONL，一行一事件，本地保留 30 天。
- 中文界面与中文错误信息；代码标识符英文。
- 所有落盘写操作必须原子（临时文件 + `os.replace`）。

## Review Focus

1. **带打开口令/受保护的文档**：`Documents.Open` 会弹密码框并阻塞 —— 必须超时强杀并记 `password_protected`，整批继续。
2. **密文文档但本机没有 Office**：不得静默产出空 md；必须记 `no_office`，体检报告与失败清单都可见。
3. **路径含中文/空格/超长/符号链接**：md 落位与 `rel_path` 必须一致，索引查得到、`/api/tables` 取得到。
4. **超大文档**（>50 MB 或 >500 页）：走 120 s 超时路径并记 `timeout`，不得让单文件拖死整批。
5. **查询词含 FTS5 保留字或引号**（`and`/`or`/`near`/`"`）：必须转义成合法 MATCH 表达式，不得 500。
6. **同一批混合明密文**：分流正确，明文走快路径、密文走 COM，互不影响。

---

## 文件结构（本计划锁定的分解）

```
D:\AICoding\LanDocShare\
├── pyproject.toml              依赖与 pytest 配置（slow 标记）
├── landoc/
│   ├── __init__.py
│   ├── config.py               配置模型 + 读写 <lib>/config.json（原子写）
│   ├── probe.py                文件头分类 + 抽样体检报告
│   ├── office_bridge.py        COM 取 WordOpenXML；Flat OPC → docx 字节
│   ├── render_docx.py          docx 字节 → Markdown（标题/表格/剔批注）
│   ├── pptx_conv.py            pptx/ppt → Markdown（明/密两条路）
│   ├── pdf_conv.py             pdf → 文本（pymupdf / Acrobat / acrobat.exe 兜底）
│   ├── pipeline.py             扫描 + 分流 + 调度 + 检查点 + 失败清单
│   ├── index.py                sqlite FTS5：建库、增量、search/tree/list/tables
│   ├── server.py               只读检索 HTTP 服务（8 个端点 + 限流 + 审计 + CIDR）
│   └── cli.py                  init / probe / convert / serve / status
└── tests/
    ├── test_probe.py
    ├── test_office_bridge.py
    ├── test_render_docx.py
    ├── test_pptx_conv.py
    ├── test_pdf_conv.py
    ├── test_index.py
    ├── test_server.py
    ├── test_pipeline.py
    ├── test_cli.py
    └── testdata/               二进制夹具（加密 docx/pptx/pdf 各一份 + 一个 flat OPC xml）
```

---

### Task 1: 工程骨架 + 配置模型 + 文件头体检

**Files:**
- Create: `pyproject.toml`、`landoc/__init__.py`、`landoc/config.py`、`landoc/probe.py`
- Test: `tests/test_probe.py`、`tests/test_config.py`

**Interfaces:**
- Produces:
  - `landoc.config.Config`（dataclass）字段：`name: str`、`lib_dir: Path`、`folders: list[Path]`、`allow_cidrs: list[str]`、`allow_people: dict[str,str]`、`port: int = 8766`、`snippet_tokens: int = 48`、`store_content: bool = True`、`snippet_only_chars: int = 20000`、`render_md: bool = True`
  - `Config.load(lib_dir: Path) -> Config`、`Config.save(self) -> None`（原子写 `config.json`）
  - `landoc.probe.classify(path: Path) -> str`，返回 `"plain" | "encrypted" | "blocked"`
  - `landoc.probe.sample_report(folders: list[Path], n: int = 200) -> dict`，返回 `{"sampled": int, "plain": int, "encrypted": int, "blocked": int, "has_office": bool, "has_acrobat": bool}`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_probe.py
def test_classify_encrypted(tmp_path):
    p = tmp_path / "a.docx"; p.write_bytes(bytes.fromhex("17da5fa01633cd9a") + b"x" * 64)
    assert probe.classify(p) == "encrypted"

def test_classify_plain_docx(tmp_path):
    p = tmp_path / "b.docx"; p.write_bytes(b"PK\x03\x04" + b"x" * 64)
    assert probe.classify(p) == "plain"

def test_classify_blocked_file_missing(tmp_path):
    assert probe.classify(tmp_path / "nope.docx") == "blocked"

def test_sample_report_counts(tmp_path):
    (tmp_path / "p.docx").write_bytes(b"PK\x03\x04" + b"x" * 32)
    (tmp_path / "e.docx").write_bytes(bytes.fromhex("17da5fa01633cd9a") + b"x" * 32)
    r = probe.sample_report([tmp_path], n=10)
    assert (r["plain"], r["encrypted"]) == (1, 1)
```

```python
# tests/test_config.py
def test_config_roundtrip(tmp_path):
    cfg = Config(name="测试库", lib_dir=tmp_path, folders=[tmp_path / "docs"], allow_cidrs=["10.91.0.0/16"])
    cfg.save()
    assert Config.load(tmp_path).folders == [tmp_path / "docs"]
```

- [ ] **Step 2: 跑测试确认失败** — Run: `python -m pytest tests/test_probe.py tests/test_config.py -v`；Expected: FAIL（`ModuleNotFoundError: landoc.config`）

- [ ] **Step 3: 实现** — `classify` 只读前 4 字节：`17Da5FA0`→encrypted，`PK\x03\x04`/`\xd0\xcf\x11\xe0`/`%PDF`→plain，其它→`"blocked"`；`OSError`→`"blocked"`。`has_office` 用 `win32com.client.DispatchEx` 探测 `KWPS.Application` 或 `Word.Application`（失败即 False，不抛）；`has_acrobat` 查注册表 `HKCR\AcroExch.App` 与 `C:\Program Files\Adobe\Acrobat DC\Acrobat\Acrobat.exe`。`Config.save` 用 `tmp` + `os.replace`。

- [ ] **Step 4: 跑测试确认通过** — Run: `python -m pytest tests/test_probe.py tests/test_config.py -v`；Expected: PASS

- [ ] **Step 5: 提交** — `git add -A && git commit -m "feat: 工程骨架、配置模型与文件头体检"`

---

### Task 2: Office COM 桥（WordOpenXML → 合法 docx 字节）

**Files:**
- Create: `landoc/office_bridge.py`、`tests/testdata/flat_opc_sample.xml`（从真实加密 docx 抓取一次，存为夹具）
- Test: `tests/test_office_bridge.py`

**Interfaces:**
- Produces:
  - `office_bridge.extract_wordopenxml(path: Path, timeout: float = 120.0) -> str`
  - `office_bridge.flat_opc_to_docx_bytes(xml: str) -> bytes`
  - 异常：`OfficeTimeout`、`OfficeUnavailable`、`PasswordProtected`（均为 `RuntimeError` 子类）

- [ ] **Step 1: 写失败测试**

```python
def test_flat_opc_to_docx_bytes_produces_valid_zip(flat_opc):
    data = office_bridge.flat_opc_to_docx_bytes(flat_opc)
    assert data[:4] == b"PK\x03\x04"
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert "[Content_Types].xml" in names          # 必须自动合成
    assert "word/document.xml" in names

def test_flat_opc_to_docx_bytes_parsable_by_python_docx(flat_opc, tmp_path):
    p = tmp_path / "r.docx"; p.write_bytes(office_bridge.flat_opc_to_docx_bytes(flat_opc))
    assert len(docx.Document(p).paragraphs) > 10

def test_extract_wordopenxml_on_plaintext_docx(plain_docx_path):
    assert "<pkg:package" in office_bridge.extract_wordopenxml(plain_docx_path)

def test_extract_wordopenxml_times_out(monkeypatch, plain_docx_path):
    monkeypatch.setattr(office_bridge, "_open_document", lambda *a, **k: time.sleep(3))
    with pytest.raises(office_bridge.OfficeTimeout):
        office_bridge.extract_wordopenxml(plain_docx_path, timeout=0.5)
```

- [ ] **Step 2: 跑测试确认失败** — Run: `python -m pytest tests/test_office_bridge.py -v`；Expected: FAIL

- [ ] **Step 3: 实现** — `flat_opc_to_docx_bytes`：`ElementTree` 遍历 `pkg:part`，`pkg:xmlData` 取子元素 `tostring`，`pkg:binaryData` 做 base64；**按各 part 的 `pkg:contentType` 合成 `[Content_Types].xml`**（`Default` 按扩展名 + 每个 part 一条 `Override`），插入为第一项。`extract_wordopenxml`：`DispatchEx` 依次试 `KWPS.Application`、`Word.Application`；`Visible=False`、`DisplayAlerts=0`；`Documents.Open(str(path), ReadOnly=True, AddToRecentFiles=False)`；取 `doc.Content.WordOpenXML`；`finally` 里 `Close(False)` + `Quit()`。超时用 `threading.Timer` 记标志并在 `finally` 杀进程（`taskkill /F /IM wps.exe` 或 `WINWORD.EXE`）；返回 `None` 文档 → `PasswordProtected`。

- [ ] **Step 4: 跑测试确认通过** — Run: `python -m pytest tests/test_office_bridge.py -v`；Expected: PASS

- [ ] **Step 5: 真机验收（slow）** — Run: `set LANSHARE_IT=1 && python -m pytest tests/test_office_bridge.py -v -m slow`；夹具为桌面上那份加密 docx，Expected: PASS 且 `len(xml) > 1_000_000`

- [ ] **Step 6: 提交** — `git commit -m "feat: Office COM 桥与 Flat OPC 重建"`

---

### Task 3: docx → Markdown 渲染

**Files:**
- Create: `landoc/render_docx.py`
- Test: `tests/test_render_docx.py`

**Interfaces:**
- Produces: `render_docx.docx_bytes_to_md(data: bytes) -> str`

- [ ] **Step 1: 写失败测试**

```python
def test_headings_become_markdown(docx_with_headings):
    md = render_docx.docx_bytes_to_md(docx_with_headings)
    assert "## 2.1  设计洪水" in md or "## 2.1 设计洪水" in md
    assert "# 1  工程概况" in md or "# 1 工程概况" in md

def test_tables_become_gfm(docx_with_table):
    md = render_docx.docx_bytes_to_md(docx_with_table)
    assert md.count("|") >= 8 and "| --- |" in md.replace("---|", "--- |")

def test_comments_are_stripped(encrypted_docx_with_comments_bytes):
    md = render_docx.docx_bytes_to_md(encrypted_docx_with_comments_bytes)
    assert "评审意见" not in md      # 夹具里批注的固定文字
```

- [ ] **Step 2: 跑测试失败** — `python -m pytest tests/test_render_docx.py -v`；Expected: FAIL

- [ ] **Step 3: 实现** — `python-docx` 读 bytes（`io.BytesIO`）；按文档流顺序遍历 `document.element.body`，`w:p` 用 `style.name` 映射 `Heading N`→`#`×N（`Heading 1`…`Heading 4`，其余→正文），空段跳过；`w:tbl` 用 `docx.table.Table` 渲染 GFM（首行表头 + 分隔行 + 其余行，单元格内 `|` 转义为 `\|`、换行压成空格、合并单元格缺位补空）。批注天然不在 `w:body` 内，只需保证不额外读取 `comments.xml`。

- [ ] **Step 4: 跑测试通过** — Expected: PASS

- [ ] **Step 5: 提交** — `git commit -m "feat: docx → Markdown 渲染（标题/表格/剔批注）"`

---

### Task 4: pptx/ppt → Markdown

**Files:**
- Create: `landoc/pptx_conv.py`
- Test: `tests/test_pptx_conv.py`

**Interfaces:**
- Produces: `pptx_conv.pptx_to_md(path: Path, encrypted: bool, timeout: float = 120.0) -> str`
- Consumes: `office_bridge` 的超时与杀进程辅助（本任务可先内联实现，Task 5 再复用）

- [ ] **Step 1: 写失败测试**

```python
def test_plain_pptx_extracts_shape_text(plain_pptx_path):
    md = pptx_conv.pptx_to_md(plain_pptx_path, encrypted=False)
    assert "## 第 1 页" in md and "汇报人" in md

def test_encrypted_pptx_goes_through_com(encrypted_pptx_path):
    md = pptx_conv.pptx_to_md(encrypted_pptx_path, encrypted=True)
    assert "## 第 1 页" in md and len(md) > 5000
```

- [ ] **Step 2: 跑测试失败** — Expected: FAIL

- [ ] **Step 3: 实现** — 明文走 `python-pptx`（遍历 slides/shapes 的 `text_frame.text` 与 `shape.has_table`）；密文走 `DispatchEx('KWPP.Application')` 或 `PowerPoint.Application`，`Presentations.Open(str(path), ReadOnly=True, WithWindow=False)`，逐页 `Slides(i).Shapes(j)` 取 `TextFrame.TextRange.Text` 与 `Table.Cell(r,c).Shape.TextFrame.TextRange.Text`；页首写 `## 第 i 页`。

- [ ] **Step 4: 跑测试通过** — Expected: PASS

- [ ] **Step 5: 提交** — `git commit -m "feat: pptx/ppt → Markdown"`

---

### Task 5: PDF 转换（三条路：明文直读 / Acrobat / acrobat.exe 兜底）

**Files:**
- Create: `landoc/pdf_conv.py`
- Test: `tests/test_pdf_conv.py`

**Interfaces:**
- Produces:
  - `pdf_conv.PDFResult`（dataclass）：`text: str`、`via: str`（`"pymupdf" | "acrobat" | "acrobat_disguise" | "skipped"`）、`reason: str = ""`
  - `pdf_conv.pdf_to_text(path: Path, cfg: Config | None = None, timeout: float = 120.0) -> PDFResult`
  - `pdf_conv.decrypt_via_disguise(path: Path, tool_dir: Path, timeout: float = 120.0) -> Path`（返回**临时解密副本**路径，调用方负责删除）

- [ ] **Step 1: 写失败测试**

```python
def test_plain_pdf_uses_pymupdf(plain_pdf_path):
    r = pdf_conv.pdf_to_text(plain_pdf_path, cfg=None)
    assert r.via == "pymupdf" and "附件" in r.text

def test_encrypted_pdf_without_acrobat_uses_disguise(encrypted_pdf_path, tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_conv, "has_acrobat", lambda: False)
    r = pdf_conv.pdf_to_text(encrypted_pdf_path, cfg=cfg_with_tool_dir(tmp_path))
    assert r.via == "acrobat_disguise" and len(r.text) > 3000
    assert list((tmp_path / "temp").glob("*.pdf")) == []      # 明文副本已删除

def test_encrypted_pdf_without_tool_is_skipped(encrypted_pdf_path, tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_conv, "has_acrobat", lambda: False)
    r = pdf_conv.pdf_to_text(encrypted_pdf_path, cfg=cfg_without_tool(tmp_path))
    assert r.via == "skipped" and r.reason == "no_reader"
```

- [ ] **Step 2: 跑测试失败** — Expected: FAIL

- [ ] **Step 3: 实现** — `classify(path)=="plain"` → `fitz.open` 逐页 `get_text()` 拼接；`text` 总长 < 50 判为无文本层 → `reason="no_text_layer"`（本轮不 OCR）。密文：`has_acrobat()` 为真 → 走 Acrobat 路径（一期先实现为 `reason="acrobat_todo"` 并在 spec 的 S2 spike 完成后补全，**不得假装成功**）；否则若 `cfg` 里 `acrobat_tool_dir` 存在 → `decrypt_via_disguise` 复制到 `tempfile.mkdtemp()` 下、以 `subprocess.run([tool_dir/"acrobat.exe", str(copy)], timeout=timeout, cwd=tool_dir)` 解密、校验头变 `%PDF` 后 `fitz` 抽取，`finally` 删除副本与临时目录。

- [ ] **Step 4: 跑测试通过** — Expected: PASS

- [ ] **Step 5: 提交** — `git commit -m "feat: PDF 转换（明文/Acrobat/兜底）"`

---

### Task 6: 转换调度器

**Files:**
- Create: `landoc/pipeline.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `probe.classify`、`office_bridge.*`、`render_docx.*`、`pptx_conv.*`、`pdf_conv.*`
- Produces:
  - `pipeline.RunReport`（dataclass）：`converted: int`、`skipped: dict[str,int]`、`failed: list[tuple[str,str]]`
  - `pipeline.run(cfg: Config, limit: int | None = None) -> RunReport`
  - 约定：每篇产出 `md/<rel_path>.md`（frontmatter 含 `source_path`、`converted_at`、`via`），并把 `(rel_path, title, ext, source_path, src_size, src_mtime, content)` 交给 `index.Index.upsert`

- [ ] **Step 1: 写失败测试**

```python
def test_mixed_batch_routes_by_classification(tmp_path, monkeypatch):
    # 目录里放 1 个明文 docx、1 个密文 docx（用 monkeypatch 假装 COM 走到 render）
    rep = pipeline.run(cfg)
    assert rep.converted == 2 and rep.failed == []

def test_xlsx_is_skipped_with_reason(tmp_path):
    (tmp_path/"a.xlsx").write_bytes(b"PK\x03\x04")
    rep = pipeline.run(cfg)
    assert rep.skipped["xls"] == 1

def test_no_office_records_reason_not_empty_md(tmp_path, monkeypatch):
    monkeypatch.setattr(office_bridge, "extract_wordopenxml", lambda *a, **k: (_ for _ in ()).throw(office_bridge.OfficeUnavailable("no office")))
    rep = pipeline.run(cfg)
    assert rep.failed and rep.failed[0][1] == "no_office"
    assert not (cfg.lib_dir/"md"/"a.md").exists()

def test_incremental_skips_unchanged(tmp_path):
    pipeline.run(cfg); r2 = pipeline.run(cfg)
    assert r2.converted == 0

def test_checkpoint_survives_keyboard_interrupt(tmp_path):
    # 中途 KeyboardInterrupt，meta.last_scan_ts 仍应被写出
```

- [ ] **Step 2: 跑测试失败** — Expected: FAIL

- [ ] **Step 3: 实现** — `rglob` 遍历 `cfg.folders`，跳过 `~$*`、`.git`、`System Volume Information`、`$RECYCLE.BIN`、隐藏目录；扩展名映射到转换器；`classify` 分流；每篇 try/except 归入 `skipped`/`failed`；单篇超时由各转换器负责；`meta.last_scan_ts` 在 `finally` 写。

- [ ] **Step 4: 跑测试通过** — Expected: PASS

- [ ] **Step 5: 提交** — `git commit -m "feat: 转换调度器（分流/跳过/失败清单/检查点）"`

---

### Task 7: 索引（FTS5 + jieba）

**Files:**
- Create: `landoc/index.py`
- Test: `tests/test_index.py`

**Interfaces:**
- Produces:
  - `index.Index(db_path: Path)`：`upsert(rel_path, title, ext, source_path, src_size, src_mtime, content) -> None`、`commit()`、`stats() -> dict`、`search(q, dir_filter="", limit=20) -> tuple[list[dict], bool]`、`tree(rel="") -> list[dict]`、`list_dir(rel="", limit=500) -> list[dict]`、`get_content(rel_path) -> str | None`、`tables(rel_path, max_bytes=300_000) -> list[dict] | None`
  - schema 与现有 `F:\md_share\shared_index.db` 完全一致：`documents(rowid, rel_path, title, ext, source_path, src_size, src_mtime, content, indexed_at)` + `documents_fts`（FTS5，`content=`，tokenize `unicode61`，`bm25(fts,10.0,1.0)`）+ `meta(key, value)`

- [ ] **Step 1: 写失败测试**

```python
def test_search_and_or_fallback(idx):
    idx.upsert("a.md", "马斯京根演算", "docx", "/s/a.docx", 10, 1.0, "马斯京根 河道演算")
    rows, fell_back = idx.search("马斯京根演算")
    assert rows and not fell_back

def test_reserved_words_are_escaped(idx):
    idx.upsert("b.md", "and or near", "docx", "/s/b.docx", 10, 1.0, "and or near 引号 \" 测试")
    rows, _ = idx.search("and or near")
    assert isinstance(rows, list)                      # 不抛 sqlite3.OperationalError

def test_tree_and_list_use_index_only(idx):
    idx.upsert("2019年/04-东堡闸/a.md", "东堡闸", "docx", "/s/x.docx", 10, 1.0, "东堡")
    assert idx.tree("")[0]["name"] == "2019年"
    assert idx.list_dir("2019年/04-东堡闸")[0]["title"] == "东堡闸"

def test_tables_from_disk_markdown(idx, md_root):
    (md_root/"c.md").write_text("# 章\n\n| a | b |\n|---|---|\n| 1 | 2 |\n", encoding="utf-8")
    idx.upsert("c.md", "c", "docx", "/s/c.docx", 10, 1.0, "全文")
    assert idx.tables("c.md")[0]["markdown"].startswith("| a | b |")

def test_tables_rejects_path_outside_index(idx):
    assert idx.tables("../etc/passwd") is None
```

- [ ] **Step 2: 跑测试失败** — Expected: FAIL

- [ ] **Step 3: 实现** — 移植 `F:\md_share\share_server.py` 的 `build_match_query`、`like_escape`、`_run_query`（含 `rowid IN (子查询)` 两步查，注释写明"直接 LIKE+ORDER BY 会全表扫含正文的行，实测 30 s 超时"）、`tree`、`list_dir`、`extract_tables`（GFM 管道表 + HTML 表，剥 frontmatter，`max_bytes` 截断）。`search` 实现 AND→OR 降级。

- [ ] **Step 4: 跑测试通过** — Expected: PASS

- [ ] **Step 5: 提交** — `git commit -m "feat: FTS5 索引与检索（含表格抽取）"`

---

### Task 8: 只读检索 HTTP 服务

**Files:**
- Create: `landoc/server.py`
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: `index.Index`、`Config`
- Produces: `server.make_server(cfg: Config) -> ThreadingHTTPServer`（`server_address=("0.0.0.0", port)`），端点：
  `GET /`、`/api/health`、`/api/meta`、`/api/stats`、`/api/search?q&dir&limit`、`/api/tree?path=`、`/api/list?path=`、`/api/tables?path=`

- [ ] **Step 1: 写失败测试**

```python
def test_health_returns_count_and_version(client):
    r = client.get("/api/health").json()
    assert r["status"] == "online" and "doc_count" in r and "version" in r

def test_search_requires_allowed_cidr(client_cfg_denied):
    assert client_for(client_cfg_denied).get("/api/search?q=x").status_code == 403

def test_unknown_name_is_401_when_allowlist_used(cfg_with_people, client):
    assert client.get("/api/search?q=x&name=陌生人").status_code == 401

def test_gbk_encoded_query_is_decoded(client, idx):
    # 模拟 Git Bash 下 curl 传 GBK 字节再 URL 编码
    assert client.get("/api/search?q=" + gbk_urlencode("马斯京根")).json()["count"] >= 1

def test_source_path_never_leaks(client, idx):
    body = client.get("/api/search?q=马斯京根").text
    assert "/s/" not in body and "source_path" not in body

def test_rate_limit_returns_429(client):
    for _ in range(61): r = client.get("/api/stats")
    assert r.status_code == 429

def test_audit_written_per_request(client, audit_path):
    client.get("/api/stats")
    assert '"api": "/api/stats"' in audit_path.read_text(encoding="utf-8")
```

- [ ] **Step 2: 跑测试失败** — Expected: FAIL

- [ ] **Step 3: 实现** — `ThreadingHTTPServer` + `BaseHTTPRequestHandler`；`parse_query` 用 `unquote_to_bytes` 后 UTF-8→GBK 双解码；CIDR 校验用 `ipaddress.ip_network(...).__contains__`；限流 `RateLimiter`（每 IP 60/60 s，走认证之前）；`Audit` 写 JSONL；所有响应 `Cache-Control: no-store`；错误统一 `{"error": "..."}`。

- [ ] **Step 4: 跑测试通过** — Expected: PASS

- [ ] **Step 5: 提交** — `git commit -m "feat: 只读检索 HTTP 服务（权限/限流/审计）"`

---

### Task 9: CLI

**Files:**
- Create: `landoc/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `python -m landoc init|probe|convert|serve|status`：
  - `init --lib-dir D:\MyLib --name "我的资料库" --folder F:\成果 ...`
  - `probe --lib-dir`（打印 `sample_report`）
  - `convert --lib-dir [--limit N]`
  - `serve --lib-dir [--port N]`
  - `status --lib-dir`

- [ ] **Step 1: 写失败测试**

```python
def test_init_creates_config_and_db(tmp_path, capsys):
    cli.main(["init", "--lib-dir", str(tmp_path), "--name", "测试", "--folder", str(tmp_path)])
    assert (tmp_path/"config.json").exists() and (tmp_path/"library.db").exists()

def test_serve_picks_next_free_port(tmp_path, monkeypatch):
    # 8766 被占时应用 8767
```

- [ ] **Step 2: 跑测试失败** — Expected: FAIL

- [ ] **Step 3: 实现** — `argparse` 子命令；`serve` 先探测端口占用（`socket.connect_ex`）并顺延；`init` 同时建库目录与空索引。**不实现旧库导入**（D4：旧库保持独立存在）。

- [ ] **Step 4: 跑测试通过** — Expected: PASS

- [ ] **Step 5: 提交** — `git commit -m "feat: CLI"`

---

### Task 10: 端到端验收

**Files:**
- Create: `tests/test_e2e.py`、`README.md`

**Interfaces:** 无新接口；本任务证明整条链路。

- [ ] **Step 1: 写端到端测试**

```python
def test_real_encrypted_docx_pptx_pdf_end_to_end(tmp_path, real_fixtures):
    cli.main(["init", "--lib-dir", str(tmp_path), "--name", "验收", "--folder", str(real_fixtures)])
    cli.main(["convert", "--lib-dir", str(tmp_path)])
    idx = Index(tmp_path/"library.db")
    assert idx.search("乐平")[0]                       # 加密 docx
    assert idx.search("溪霞")[0]                       # 加密 pptx
    assert idx.stats()["total"] >= 3
```

- [ ] **Step 2: 跑测试确认失败** — Run: `set LANSHARE_IT=1 && python -m pytest tests/test_e2e.py -v -m slow`；Expected: FAIL

- [ ] **Step 3: 让链路跑通**（修 bug，不改测试语义）

- [ ] **Step 4: 跑测试通过** — Expected: PASS；并把耗时写进 `README.md`（文档数 / 总耗时 / 每份均时）

- [ ] **Step 5: 手工验收 curl** — Run:
```bash
python -m landoc serve --lib-dir D:\MyLib &
curl -s "http://127.0.0.1:8766/api/search?q=%E9%A9%AC%E6%96%AF%E4%BA%AC%E6%A0%B9&limit=5"
```
Expected: JSON 含 `results[]`，每项有 `path/title/snippet`，**无** `source_path`

- [ ] **Step 6: 提交** — `git commit -m "test: 端到端验收（加密 docx/pptx/pdf）"`

---

## 二期计划（不在本计划范围，接口已在此锁定）

`registry.py`（中心）、`tray.py` + 开机自启 + **§6.7 的空闲门控调度（最近 5 分钟无键鼠/锁屏才推进批次）** + PyInstaller 打包、`skill/lan-lib-search`（含 `query.py`）—— 它们依赖本计划的 `Config`、`/api/health`、`/api/search` 契约。二期开工前需先完成 spec 的 S1/S2/S4。

旧库接管（把 `F:\历年工作资料库_md` 注册成新库）按 D4 暂不做；若日后要做，只需给 CLI 加一个 `import` 子命令（读 md frontmatter 的 `source_path` 后 `Index.upsert`），不影响本计划任何接口。
