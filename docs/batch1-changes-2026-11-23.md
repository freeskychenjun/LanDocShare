# 第 1 批改动清单（2026-11-23）

对应 `docs/optimization-plan-2026-11-23.md` 的第 1 批：2.1、2.2、2.3、3.1、3.2，另加 4.1、4.4、3.6。

- 基线：`python -m pytest tests -q` → 538 passed, 17 skipped
- 验收：原有 538 条全绿 + 本清单新增 15 条（用例名见各节），不加版本号（发版时统一动 `pyproject.toml:3`）
- 本批不碰：1.1（单独发版）、1.2（先做发布检查）、1.3、2.4（另做）、3.5（只做服务端负分）、3.7/3.8/3.9

## 提交拆分

1. `fix(pipeline): 撤回标记只在「没撤回 → 撤回」那一刻写库` — 2.1
2. `fix: 同名的共享文件夹在建库前就拒掉` — 2.2
3. `fix(index): 全大写查询词也高亮、也居中` — 2.3
4. `fix(ocr): 临时目录在所有出口都清掉，页码按原始页序` — 3.1 + 3.2
5. `feat(web): 列表带类型标签、表格按转义竖线切格` — 4.1 + 4.4
6. `feat(app): 心跳回包说有新版本就立刻检查一次` — 3.6

---

## 1. 2.1 撤回标记只写一次

**现状**：`landoc/pipeline.py:667-686`。撤回分支每轮都对每篇已撤回文档调 `mark_withdrawn(..., True)`，`documents_au` 触发器把整篇正文在 FTS 里删一遍再插一遍；旁边的 `source_missing` 分支（`672-676`）有「已经是这个状态就不动」的判断，两个分支不对称。

**改法**：只把写库的三行放进判断。

```python
            item = files.get(rel)
            if not isinstance(item, dict):
                item = {}
                files[rel] = item
            if not item.get("withdrawn"):
                item["withdrawn"] = True
                item["withdrawn_since"] = time.strftime("%Y-%m-%d %H:%M:%S")
                mark = getattr(index, "mark_withdrawn", None)
                if mark is not None:
                    mark(_md_rel(rel), True)
            report.skip("withdrawn")
```

`report.skip("withdrawn")` 留在判断外：状态窗口那句「跳过 N 篇」的口径不变，仍然报「当前有 N 篇处于撤回状态」，只是不再重复写库。要改成「只有切换那一刻才计数」也可以，但要同步改 `tests/test_pipeline.py:330-331` 的断言——我倾向不动，最小改动。

**测试**：`tests/test_pipeline.py` 新增 `test_withdrawal_marks_the_index_only_once`
- `StubIndex.__init__` 加 `self.withdraw_calls: list[tuple[str, bool]] = []`，`mark_withdrawn` 里 append
- 建库 → `cfg.folders = []` → `pipeline.run` 两轮
- 断言第一轮后 `len(withdraw_calls) == 2`（两篇），第二轮后仍是 2（不增长）

**风险**：低。`withdrawn_since` 从 `setdefault` 改成直接赋值，效果相同（进到这个分支时 `withdrawn` 必为假）。

---

## 2. 2.2 两个共享文件夹同名

库内键是「顶层文件夹名 + 相对路径」（`landoc/pipeline.py:291`），`D:\工作\资料\a.docx` 与 `E:\备份\资料\a.docx` 同为 `资料/a.docx.md`，每轮互相覆盖、无限重转。向导只拦完全相同的路径（`landoc/wizard.py:479`），`cli init --folder` 也不查。

### 2.1 加一个共享的判断函数

`landoc/pipeline.py`，放在 `_configured_tops`（`108`）后面：

```python
def duplicate_tops(folders) -> list[str]:
    """共享列表里出现不止一个同名顶层文件夹时，返回重复的名字（顺序稳定、不重复）。"""
    seen: dict[str, Path] = {}
    dup: list[str] = []
    for folder in folders or []:
        try:
            base = Path(folder).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        top = _top_name(base)
        first = seen.get(top)
        if first is None:
            seen[top] = base
        elif first != base and top not in dup:
            dup.append(top)
    return dup
```

同一个路径重复出现在配置里（`add_folder` 已挡，但配置可能被手改）不算冲突。

### 2.2 `_iter_docs` 里兜一道

`landoc/pipeline.py:270-293`，加 `seen_tops`：

```python
    seen_tops: set[str] = set()
    for folder in folders:
        ...
        top = _top_name(folder)
        if top in seen_tops:
            # 同名的两个文件夹会让库内键撞车：每轮互相覆盖、永远重转一篇。
            # 向导和 CLI 都拦住了，这里只做「先到先得」的确定性兜底。
            continue
        seen_tops.add(top)
```

**偏离方案的一处**：方案建议「重名时在 `app.log` 里记一行」。`pipeline.py` 目前没有任何日志设施，`log_event` 在 `app.py:44`，反向依赖会成环；而每 30 秒一轮的日志也会刷屏。所以只做确定性丢弃 + 文档注释，提示交给向导和 CLI（它们才是能让人改回来的地方）。

### 2.3 向导

`landoc/wizard.py` 新增一个可单测的纯函数（放在 `validate_folder` 附近）：

```python
def same_home_name_conflict(folders: list[Path]) -> str | None:
    """返回「两个文件夹同名」的说明；不冲突返回 None。口径与 pipeline._top_name 一致。"""
    from landoc.pipeline import duplicate_tops  # 延迟导入，别把转换链拉进向导启动路径

    dup = duplicate_tops(folders)
    if not dup:
        return None
    names = "、".join(f"「{name}」" for name in dup)
    return (
        f"{names} 有不止一个同名文件夹。\n\n"
        "库内按「顶层文件夹名 + 相对路径」存文档，同名的会互相覆盖、每轮重转一篇。\n"
        "请给文件夹改名，或只共享它们的上级目录。"
    )
```

- `add_folder`（`471-481`）：`validate_folder` 之后加
  ```python
        conflict = same_home_name_conflict([*listbox.get(0, "end"), Path(chosen)])
        if conflict:
            messagebox.showwarning("两个文件夹同名", conflict)
            return
  ```
- `confirm`（`626-630`）：`if not folders: ...` 之后加同一段检查。这一处是**必需的**——改建库设置时列表是预填的，配置里已经存在的重名只有在这里能拦住。

### 2.4 CLI

`landoc/cli.py` `_cmd_init`（`115`）合并文件夹那段，在 `cfg.folders = merged` 之前：

```python
        dup = pipeline.duplicate_tops(merged)
        if dup:
            print(
                f"[错误] 两个共享文件夹同名：{'、'.join(dup)}；库内会互相覆盖，请改名或改共享上级目录",
                file=sys.stderr,
            )
            return 2
```

`cli.py:16` 已经 `from landoc import ... pipeline`，不需要新 import。

**测试**（4 条）
- `tests/test_wizard.py::test_same_home_name_conflict_detects_two_folders_with_the_same_leaf`：`tmp/a/资料`、`tmp/b/资料` → 返回串里含 `「资料」`
- `tests/test_wizard.py::test_same_home_name_conflict_is_quiet_for_distinct_names`：`资料`、`成果` 与「同一路径写两遍」都返回 `None`
- `tests/test_cli.py::test_init_refuses_two_folders_with_the_same_name`：`cli.main([...]) == 2`，`capsys.readouterr().err` 含 `同名`
- `tests/test_pipeline.py::test_same_named_folders_only_index_the_first`：两个 `资料` 目录各一篇不同文档，`cfg.folders = [a, b]`，跑一轮后 `len(index.rows) == 1` 且 `rel_path` 以 `资料/` 开头

**风险**：老库若已经配了两个同名文件夹，升级后第二轮起只收第一个（另一份的文档会走 `source_missing` 而不是 `withdrawn`，因为它仍在共享列表里）。这种情况的修法是让用户改名后重新加——`confirm` 会拦着他，不会静默。

---

## 3. 2.3 大写查询词也要高亮

`landoc/index.py:90`：

```python
    terms = [t for t in _query_terms(match) if t and not t.isupper()]
```

改成

```python
    terms = [t for t in _query_terms(match) if t]
```

理由：`build_match_query`（`index.py:68-77`）只给检索词加引号，`AND`/`OR` 这类操作符根本不会出现在 `_query_terms` 的结果里，这个过滤只剩副作用。高亮本身是 `re.IGNORECASE` 做的（`index.py:107`），去掉过滤后大小写都能命中。

**测试**：`tests/test_index.py` 新增 `test_uppercase_query_is_highlighted_like_lowercase`
- 入库一篇含 `GIS 在水利上的应用` 的文档，`idx.search("GIS")`
- 断言片段里含 `<<GIS>>`，且片段不是文档开头（用 `…` 前缀或 `find` 位置判断）

**风险**：低。现有测试没有一条依赖这个过滤（`tests/` 里搜不到相关断言）。

---

## 4. 3.1 OCR 临时目录

### 4.1 `_ocr_text`（`landoc/pdf_conv.py:330-368`）

现在的结构：`mkdtemp` 之后 `try:` 里有两个 `return None`，`finally: doc.close()`，然后第二个 `try/finally: rmtree`。前两个出口在 rmtree 之前。

改成一层 `try/finally` 管住整个函数体：

```python
    tmpdir = Path(tempfile.mkdtemp(prefix="landoc_ocr_"))
    try:
        try:
            total_pages = doc.page_count
            if not any(doc[index].get_images(full=True) for index in range(min(total_pages, 3))):
                return None
            for index in range(min(total_pages, max_pages)):
                target = tmpdir / f"p{index + 1:04d}.png"
                doc[index].get_pixmap(dpi=dpi).save(str(target))
                images.append((f"p{index + 1:04d}", target))
        except Exception:  # noqa: BLE001 - 渲染失败就放弃 OCR，别把建库拖死
            return None
        finally:
            doc.close()
        if not images:
            return None
        raw = ocr.ocr_images(images)
        if not raw:
            return None
        ...（页码、提示行、tidy，见第 5 节）
        return ...
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
```

要点：`rmtree` 必须在 `ocr_images` **之后**（图片就在这个目录里），所以只能放在最外层 `finally`。

### 4.2 `decrypt_via_disguise`（`landoc/pdf_conv.py:84-98`）

工具超时（`94` 行抛 `TimeoutExpired`）时目录不删：

```python
    tmpdir = Path(tempfile.mkdtemp(prefix="landoc_pdf_"))
    copy = tmpdir / (Path(path).stem + ".pdf")
    try:
        shutil.copy2(path, copy)
        (runner or _run_tool)(tool, copy, Path(tool_dir), timeout)
        if classify(copy) != "plain":
            raise PDFDecryptError(f"工具执行后文件仍是密文：{copy}")
    except BaseException:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    return copy
```

原有那条「工具啥也没改」的清理可以删掉（并进 `except`），行为不变。

**测试**（3 条，`tests/test_pdf_conv.py`）
- `test_ocr_temp_dir_is_removed_when_pages_have_no_images`：monkeypatch `tempfile.mkdtemp` 记录返回的目录 → 一页纯文字 PDF 调 `_ocr_text`，返回 `None` 后断言目录已不存在
- `test_ocr_temp_dir_is_removed_when_rendering_fails`：扫描页 PDF + `monkeypatch.setattr(fitz.Page, "get_pixmap", 抛 RuntimeError)`，返回 `None` 后断言目录已不存在（这条守的是「渲染到一半失败，明文页图留在 %TEMP%」）
- `test_disguise_timeout_leaves_no_temp_dir`：`runner` 抛 `subprocess.TimeoutExpired`，断言 `pytest.raises` 且目录已不存在

**风险**：低。`tempfile.mkdtemp` 是模块级函数，测试里 patch 会在一整个进程内生效，用 `monkeypatch` 自动还原即可。

---

## 5. 3.2 页码与「只识别了前 N 页」

`landoc/pdf_conv.py:357-368`：

```python
    pages = [page.strip() for page in raw.split("\f")]
    if not any(pages):
        return None
    body = "\n\n".join(
        f"<!-- page {index + 1} -->\n{page}" for index, page in enumerate(pages) if page
    )
    header = OCR_NOTICE
    tried = len(images)
    if total_pages > tried:
        header += f"（只识别了前 {tried} 页，共 {total_pages} 页）"
    return f"{header}\n\n{ocr.tidy(body)}"
```

两处改动：
1. 不再先丢空页再重编号 —— 空页只跳过不编号，`<!-- page N -->` 用原始下标
2. 提示语的 `N` 从「有字的页数」改成「尝试过的页数」（`len(images)`，也就是 `min(total_pages, max_pages)`）

**测试**（2 条）
- `test_ocr_page_numbers_follow_original_order`：4 页扫描件 + `ocr_images` 返回 `"第一页\f\f\f第四页"` → 结果含 `<!-- page 1 -->` 与 `<!-- page 4 -->`，不含 `<!-- page 2 -->`
- `test_ocr_notice_counts_pages_it_tried`：4 页 + `cfg.ocr_max_pages = 2` → 提示行是「只识别了前 2 页，共 4 页」

---

## 6. 4.1 列表带上类型标签

页面已经在读 `f.ext`（`landoc/web/index.html:236`、`255`），但 `/api/list`、`/api/recent` 的行里没有这个键，标签一直空着。`documents` 表本来就有 `ext` 列（`landoc/index.py` 建表语句），只是这两条 SQL 没取。

- `landoc/index.py:467-474` `list_dir`：`SELECT rel_path, title, ext, src_size`，返回 `{"path": r[0], "title": r[1], "ext": r[2] or "", "size": r[3]}`
- `landoc/index.py:484-490` `recent`：同上
- `landoc/server.py:311-324` 原样透传，不用改

**测试**（2 条）
- `tests/test_index.py::test_list_dir_and_recent_carry_the_source_extension`：`upsert(..., "docx", ...)` 后两者的 `ext == "docx"`
- `tests/test_server.py:122` **必须改**：`assert set(payload["files"][0]) == {"path", "title", "size"}` → 加上 `"ext"`。这条断言会直接红，别漏。

**风险**：前端其它地方不读这个键；对外 JSON 加键是向后兼容的（`skill/lan-lib-search/scripts/query.py` 不读 `/api/list`）。

---

## 7. 4.4 表格按转义竖线切格

服务端把单元格里的 `|` 转义成 `\|`（`landoc/render_docx.py:33`），页面直接 `split('|')`（`landoc/web/index.html:279`），含竖线的单元格会错位。

`index.html:279` 换成逐字符扫描：

```js
        // 单元格里的竖线在服务端被转义成 \|（render_docx.py 那边），这里要还回去，
        // 不能直接 split('|')，否则带竖线的单元格会把一格切成两格。
        const cells = (line) => {
          const inner = line.replace(/^\s*\|/, '').replace(/\|\s*$/, '');
          const out = [];
          let cur = '';
          for (let i = 0; i < inner.length; i += 1) {
            const ch = inner[i];
            if (ch === '\\' && inner[i + 1] === '|') { cur += '|'; i += 1; continue; }
            if (ch === '|') { out.push(cur.trim()); cur = ''; continue; }
            cur += ch;
          }
          out.push(cur.trim());
          return out;
        };
```

**测试**：这一处没有自动化测试（仓库里没有 JS 测试设施，`tests/` 下也没有前端用例）。验证靠手测：拿 `tests/test_render_docx.py:31` 那条用例造的「带竖线的表格」文档丢进库里，浏览器打开「查看表格」，确认格子数量与内容对得上。如果以后要自动化，得先把切格逻辑挪到服务端（`/api/tables` 直接返回二维数组），那是另一个改动。

---

## 8. 3.6 心跳发现新版本

中心在 `/register` 的回包里就带着 `latest_version` 和 `upgrade`（`landoc/registry.py:1377-1385`），`_heartbeat`（`landoc/app.py:586-608`）只读了 `server_time`。

### 8.1 状态字段与节流

`app.py` `__init__`（`161` 附近，`self._heartbeat_seconds` 旁边）加：

```python
        self._last_update_ping = 0.0  # 心跳触发更新检查的节流（monotonic 秒）
```

`_heartbeat` 里读完 `stamp` 之后加一行 `self._maybe_start_update_check(reply)`，并新增方法：

```python
    _UPDATE_PING_GAP = 30 * 60.0

    def _maybe_start_update_check(self, reply) -> None:
        """中心在心跳回包里说「有新版」就立刻查一次，别等 6 小时那一轮。

        只在本地还没到「下载中 / 已就绪 / 需手动重装」时触发；同一个状态下半小时
        最多一次，免得下载一直失败时每分钟重来一遍。
        """
        if not isinstance(reply, dict) or not reply.get("upgrade"):
            return
        if not str(reply.get("latest_version") or ""):
            return
        if self.status.update_phase in ("downloading", "ready", "manual"):
            return
        now = time.monotonic()
        if now - self._last_update_ping < self._UPDATE_PING_GAP:
            return
        self._last_update_ping = now
        self.check_updates_async()
```

`check_updates_async`（`366`）本来就在后台线程里跑 `checker.check()`，心跳线程不会被它卡住。

### 8.2 文案

README.md:169 那句「启动 25 秒后检查一次，之后每 6 小时一次」在这条落地后要补一句「中心心跳说有新版时立刻查一次」。`landoc/release.py` 里「同事侧 6 小时内自动发现」的说法一并改（发版流程的提示语，不改会误导）。

**测试**：`tests/test_app.py` 新增 `test_heartbeat_starts_update_check_when_registry_says_upgrade`
- poster 返回 `{"ok": True, "server_time": ..., "latest_version": "9.9.9", "upgrade": True}`
- `app.check_updates_async = lambda **kw: calls.append(1)`（`heartbeat_seconds=0.05`）
- `wait_for(calls)`，断言只触发一次（再等两轮心跳仍是 1）
- 反向：`upgrade: False` 的回包不触发（复用 `test_heartbeat_registers_with_registry` 的 poster 即可，可合并成一条带两段的用例）

**风险**：中低。中心版本号比本地新但本地已经是最新（版本号脏了）时，会退化成「每 30 分钟查一次」，这正是想要的降级。`check_updates_async` 在没配登记中心时直接返回，不报错。

---

## 9. 需要你拍板的三处

1. **2.1 的 `report.skip("withdrawn")` 口径**：保持「每轮报当前撤回数」（我倾向），还是改成「只在切换那一刻报数」（要同步改 `tests/test_pipeline.py:330-331`）。
2. **2.2 中「已配了两个同名文件夹的老库」**：升级后只收第一个、另一份走 `source_missing`（可在库里看到「源已不在」），而不是撤回。要不要在状态窗口额外提示一句？
3. **3.6 的触发间隔**：30 分钟是否合适（0 就是每次心跳都查、也就是 60 秒一次；中心侧每次查更新会走 `/update` 请求）。

## 10. 收尾验证

```powershell
python -m pytest tests -q          # 期望 553 passed（538 + 本批 15 条）, 17 skipped
```

## 11. 落实结果（2026-11-23 实施完毕）

三处待拍板项按推荐默认执行：2.1 保持每轮报撤回数、2.2 老库不做额外状态提示（只靠 `confirm` 拦）、3.6 节流 30 分钟。

实施中对清单的两处偏离：
- 3.1 的 `decrypt_via_disguise` 用 `except BaseException: rmtree; raise`（清单原文只写超时，实际把「复制失败/工具被杀/仍是密文」一并覆盖）。
- 3.6 的测试额外把 `app.cfg.check_updates` 置 False，避免测试里真起检查器线程去连网。

手测清单（改完自己走一遍）：
- 移出一个共享文件夹 → 连等两轮 → 任务管理器里 `app.log` 不应再出现 FTS 重建；索引里 `withdrawn=1` 的文档数不变
- 向导里加两个同名文件夹 → 第二次「添加…」被拦，提示要改名
- `landoc init --lib-dir X --name Y --folder D:\a\资料 --folder E:\b\资料` → 退出码 2，报同名
- 搜 `GIS` → 片段命中处有 `<<GIS>>`
- 打开一篇扫描 PDF 的库，`%TEMP%` 下不再留 `landoc_ocr_*` 目录
- 浏览器点「查看表格」，找一篇带竖线的表格，格子不错位
- 中心发了新版 → 一分钟内本机状态窗口出现「发现新版本」
