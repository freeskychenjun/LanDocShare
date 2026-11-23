# LanDocShare 优化方案 · 第三轮（2026-11-23）

> 评审范围：0.2.14 → 0.2.22 的全部增量（OCR、看门狗修复、向导重排、默认网段现算、脱敏发布），
> 加上前两轮没细读的模块（`ocr` / `netinfo` / `discover` / `panel` / `index.html` / `query.py`）
> 和整条打包发布链。测试基线：`538 passed, 17 skipped, 197s`（上一轮 498 / 9）。
>
> 本轮的做法和前两轮不同：**每条结论先做实验再写**。
> - 【实验】= 在本机用临时目录跑出来的，文中数字都是实测值；
> - 【读码】= 只读代码得出、需要真机确认。涉及 Office COM 的一条都没跑，
>   因为评审时这台机器上 WPS 正在使用；
> - 第五节列出「查过但不成立」的几条，免得以后再花时间。
>
> 本轮只研究、没有改任何代码；实验脚本都放在系统临时目录，没有进仓库（见附录）。

## 结论一览

| 编号 | 问题 | 证据 | 级别 | 预估 |
|---|---|---|---|---|
| 1.1 | 转换进行中点「清理…」：界面卡 5 秒、报「已清理」，索引行却还在、同事照常搜得到 | 实验 | P0 | 2 h |
| 1.2 | Tesseract 兜底依赖 29 个不在仓库里的 DLL，换台机器出包就静默失效 | 实验 | P0 | 半天 |
| 1.3 | PPT 的 COM 路径不关演示文稿；会话结束无条件 Quit | 读码 | P0（待确认） | 1 h |
| 2.1 | 撤回共享后，每个空闲轮次都把全部已撤回文档重打一遍标记 | 实验 | P1 | 0.5 h |
| 2.2 | 两个共享文件夹同名时，文档每轮互相覆盖、无限重转 | 实验 | P1 | 1 h |
| 2.3 | 全大写的查询词（GIS、HEC-RAS）片段不居中、不高亮 | 实验 | P1 | 0.5 h |
| 2.4 | docx 转换丢修订插入、内容控件、文本框、页眉页脚；一张坏表废掉整篇 | 实验 | P1 | 半天 |
| 3.1 | OCR 的临时目录在两个出口不清理，其中一条会把页面图片留在盘上 | 实验 | P2 | 0.5 h |
| 3.2 | OCR 文本的页码标错、「只识别了前 N 页」算错 | 实验 | P2 | 0.5 h |
| 3.3 | OCR 两条路各拿一份时间预算，单篇最坏 420 秒 | 实验 | P2 | 1 h |
| 3.4 | 源文件已删除的失败记录永远不清，「N 篇转换失败」只增不减 | 实验 | P2 | 1 h |
| 3.5 | 两字查询跨库合并时没有相关度信号，靠后的库被按名字截掉 | 实验 | P2 | 2 h |
| 3.6 | 新版本发布后同事最长 6 小时才发现，心跳回包里的 `upgrade` 没人读 | 读码 | P2 | 1 h |
| 3.7 | 包体里约 47 MB 可以去掉（numpy、Pythonwin、重复 DLL） | 实验 | P2 | 2 h |
| 3.8 | psutil 是事实依赖但没有声明 | 实验 | P2 | 0.5 h |
| 3.9 | 文档和帮助文案里默认网段写错、脱敏有残留 | 检索 | P2 | 1 h |
| 3.10 | 空闲时每 30 秒整份重写一次 `state.json`、重新解析三次 | 实验 | P2 | 2 h |

---

## 〇、上一轮（2026-11-19）方案的落实情况

上一轮 14 条：11 条已采纳（本轮逐条在代码里复核过：`tray.CLI_COMMANDS` 含 `prune`、
`netinfo.cached_local_ips`、两个服务的 HTTP/1.1、`SHORT_QUERY_CACHE`、`AppStatus.failed`、
向导的拒绝网段行、`_write_failures` 镜像）；1 条不采纳（3.1 目录级增量扫描）；
2 条顺延或不动（4.5 xlsx 仍是 `SKIP_EXTS`，留给 0.3；4.6 只改措辞）。

复核时带出三件事，已经并入本轮：

- 上一轮的「清理按钮」（1.1）有并发漏洞 → 本轮 1.1；
- 上一轮的「prune 清 failures」（2.3）只覆盖进过库的文档，没进过库的不在其中 → 本轮 3.4；
- 上一轮的两字查询缓存（3.3）实测有效（命中 0.01 ms），两字查询本身也不慢，见第五节。

---

## 一、P0

### 1.1 转换进行中点「清理…」：索引行删不掉，界面还报成功【实验 A3 / A3b】

`清理…` 按钮在 `status.gone > 0` 时就亮（`panel.py:526-533`），不看是否正在建库。
它直接在 Tk 线程里调 `pipeline.prune_gone()`，和转换线程之间没有任何互斥。

复现（1 篇「源已不在」的文档，另一条线程像转换线程那样占着写事务）：

```
prune_gone() returned removed=1 after 5.6 s (busy timeout is 5 s)
index row still present: True | md copy deleted: True | state entry gone: True
-> still searchable by colleagues: True
```

三个问题叠在一起：

1. **写锁。** `index.upsert()` 开启的写事务要到 `_flush()` 才提交（每 20 篇或 60 秒，
   `pipeline.py:50-51`），转换期间锁几乎一直被占着。`Index.conn` 是线程本地的，
   Tk 线程用自己的连接执行 `index.delete()`，等满 5 秒 busy timeout 后抛
   `database is locked`——而 `prune_gone` 把它吞了（`pipeline.py:745-748`）。
   结果是 md 删了、`state.json` 里的条目删了、索引行还在，界面显示「已清理 1 篇」。
   那一行 `withdrawn = 0`，同事照常搜得到；状态里的条目已经没了，以后再也清不掉。
2. **卡界面。** 每删一篇要等满 5 秒，Tk 线程这段时间不响应。
3. **`state.json` 被覆盖。** `pipeline.run` 整轮把 `state` 攥在内存里，收尾时整份写回
   （`pipeline.py:374` 读、`706` 写）。清理落在轮次中间时，被删的条目会在轮次结束时
   复活：实验里清理后 `gone=0`，轮次一结束又变回 2，用户得再点一次才对得上。

命令行 `LanDocShare.exe prune --gone` 在托盘程序运行时执行，是同样的两条竞争
（`cli.py:248-261` 不检查单实例互斥量）——这一条是读码得出，没有单独跑。

建议：

- `prune_gone` 和 `pipeline.run` 串行化。最省事的做法是把清理请求排进 worker 线程，
  在两轮之间执行；或者用一把锁把「一轮转换」和「一次清理」包起来。
- 转换期间把「清理…」置灰。
- `prune_gone` 不要吞 `sqlite3.OperationalError`：删不掉就抛出来，让界面说「没清理成功」。
- 命令行路径先 `acquire_single_instance()`，托盘在跑就拒绝并提示先退出。

测试：一条线程占着写事务时调 `prune_gone`，断言要么等到锁、要么明确失败，
并且索引行与 `state.json` 的状态一致；轮次进行中清理后，轮次结束 `gone` 不得复活。

### 1.2 Tesseract 兜底依赖 29 个不在仓库里的 DLL【实验】

`landoc/tools/README.md` 写的是「最小运行集（exe + 6 个 DLL），少了 ICU 也能跑」。
这个结论只在这台开发机上成立，原因有两个，实验都坐实了：

**(a) 6 个文件单独拿出来的确跑不起来。** PE 导入表显示：

| 文件 | 静态依赖、但不在 `tools/tesseract/` 里 |
|---|---|
| `tesseract.exe` | `libarchive-13`、`libcurl-4`、`libtiff-6` |
| `libtesseract-5.dll` | `libarchive-13`、`libcurl-4` |
| `libleptonica-6.dll` | `libgif-7`、`libjpeg-8`、`libopenjp2-7`、`libpng16-16`、`libtiff-6`、`libwebp-7`、`libwebpmux-3`、`zlib1` |

连同它们的传递依赖，完整的闭包是 **6 个随包文件 + 29 个 DLL（16.5 MB）**，全部不在仓库里。
净化环境实跑（PATH 只留 `System32`）：

```
A. clean machine: PATH=System32 only (6-file set)              rc=3221225781 (0xC0000135)
B. packaged layout: PATH also has _internal (accidental DLLs)  rc=0  tesseract v5.5.0
C. dev machine: PATH also has C:\Program Files\Tesseract-OCR   rc=0
```

`0xC0000135` 是 `STATUS_DLL_NOT_FOUND`。之前验证「能跑」，是因为开发机的 PATH 里
本来就有系统安装的 Tesseract。

**(b) 现在的包能跑，是靠两件偶然的事。** PyInstaller 把 `landoc/tools/tesseract/` 里的
exe 和 DLL 当成了二进制而不是数据（`build/LanDocShare/Analysis-00.toc` 的 binaries 段），
于是顺着导入表去开发机的 `C:\Program Files\Tesseract-OCR` 把那 29 个 DLL 拖进了 `_internal\`
顶层；而打包版的子进程能找到 `_internal` 下的 DLL。端到端验证过：把打包版的 PATH 净化到
只剩 `System32`（PowerShell 也找不到，于是只能走 Tesseract 路），一篇纯图片 PDF 照样
收进库（`via=ocr`，2.5 秒，无失败记录）。

也就是说：**今天的 0.2.22 在干净机器上是好的，但它带不走。** 在一台没装 Tesseract 的机器上
出包（新电脑、干净的虚拟机、以后的 CI），这 29 个 DLL 不会进包，Tesseract 一启动就是
`0xC0000135`，被 `ocr._tesseract_ocr` 当成普通失败吞掉，扫描件记成 `no_text_layer`——
而且只发生在「系统没有中文 OCR 包」的那些同事机器上，正是唯一需要兜底的地方。

现有的测试拦不住：`tests/test_ocr.py` 里真跑 `tesseract.exe` 的用例挂着 `LANSHARE_IT` 门控，
平时跳过；真跑时又是在开发机的 PATH 下跑的。

建议（三件事一起做）：

1. 把那 29 个 DLL 收进 `landoc/tools/tesseract/`，让这个目录自洽。它们现在本来就在包里
   （只是位置在 `_internal\` 顶层），所以包不会变大。二进制进仓库约 16.5 MB，
   不想进 git 的话，也可以写成 `packaging/` 下的取数脚本，但那样「换机器出包」的问题还在，
   所以更建议直接收进来。
2. spec 里把 `tools/` 从 `a.binaries` 里过滤掉，只留 `datas` 那一份（具体写法要在 spec 里试，
   出包后核对 `_internal` 顶层不再有这批 DLL）。现在 5 个随包 DLL 在顶层多了一份
   逐字节相同的副本，白占 8.3 MB。
3. 加一条不跳过的发布检查：在净化 PATH 下跑 `tesseract.exe --version`，断言返回 0
   （60 毫秒的事，放在 `tests/test_ocr.py`，去掉门控）。`packaging/release.py` 出包前也跑一次。

顺带改掉三处过时的说法：`landoc/tools/README.md:12,17`、`landoc/ocr.py:6`、`README.md:319`。

### 1.3 PPT 的 COM 路径不关演示文稿，会话结束时无条件 Quit【读码，需真机确认】

- `pptx_conv._com_job`（`pptx_conv.py:94-135`）打开 `pres` 之后逐页取文本，返回前没有
  `pres.Close()`。Word 路（`office_bridge.py:264-270`）和 PDF 路（`pdf_conv.py:178-183`）
  都有 `finally: Close`，只有 PPT 没有。一轮最多尝试 100 篇（`app.py:136`），
  期间每一篇都留在 PowerPoint / WPP 进程里。
- `OfficeSession.close()`（`office_bridge.py:274-291`）在会话有效时无条件 `app.Quit()`；
  `_dispatch_ppt` 对拿到的实例设 `DisplayAlerts = 0`（`pptx_conv.py:82-84`）。
  这套逻辑默认 `DispatchEx` 一定新起一个进程——Word 是这样，但 MS PowerPoint 是单实例应用，
  `DispatchEx` 会连到已经开着的那个实例。如果库主人正开着一个没保存的演示文稿，
  空闲建库或「立即重建索引」转到一篇密文 `.pptx` 时，就会在主人的实例上关掉提示、
  隐藏打开、最后 Quit，没保存的稿子有被静默丢弃的风险。
  WPS 的 `KWPP.Application` 是否也复用进程，我没有查到可靠资料。

这条我没有验证：机器上 WPS 正在运行，起 COM 会干扰正在编辑的文档。建议在一台测试机上确认：

1. 打开 WPS 演示（或 PowerPoint），新建一份演示文稿，敲几个字，不保存；
2. 对一个含加密 `.pptx` 的目录点「立即重建索引」；
3. 看 `tasklist` 里 `wpp.exe` / `POWERPNT.EXE` 的数量有没有加一，以及建库结束后
   那个没保存的窗口还在不在。

不管结果如何，有两点都该改：`pres.Close()` 放进 `finally`；`close()` 只在「本会话确实新起了
进程」（`_office_pids() - self._before` 非空）时才 Quit，否则只关自己打开的文稿，
并把 `DisplayAlerts` 改回原值。

---

## 二、P1

### 2.1 撤回共享后，每个空闲轮次都把全部已撤回文档重打一遍标记【实验 A2 / B5】

撤回分支（`pipeline.py:677-686`）没有判断「是不是已经撤回过」，每一轮都对每一篇
`mark_withdrawn(..., True)`。值没变，但 `documents_au` 触发器照常触发，
把整篇正文在 FTS 里删一遍再插一遍。旁边的 `source_missing` 分支（`672-676`）是有判断的，
两个分支不对称。

把一个 1500 篇的文件夹移出共享列表之后：

| 内容大小 | 每个空闲轮次 | 写盘 |
|---|---|---|
| 1500 篇 × 1 千字 | 约 0.2 秒 | WAL 增长 2~7.8 MB |
| 1500 篇 × 1.6 万字 | **2.4 秒** | **WAL 增长 48.8 MB** |

空闲时每 30 秒一轮，后者相当于空闲一小时写 5.9 GB。本机真实的库（1107 篇，平均 3.9 万字）
现在就有 24 篇已撤回，按上面的比例估，每个空闲轮次大约白烧 0.1 秒、写 2 MB
——这一句是按线性比例估的，不是单独实测。

修法和测试都简单：

```python
if not item.get("withdrawn"):          # 只在「没撤回 → 撤回」那一刻写库
    item["withdrawn"] = True
    item.setdefault("withdrawn_since", time.strftime("%Y-%m-%d %H:%M:%S"))
    mark(_md_rel(rel), True)
```

测试：移出文件夹后连跑两轮，第二轮 `index.commit()` 之后 WAL 的增长必须是 0。

### 2.2 两个共享文件夹同名时，文档每轮互相覆盖、无限重转【实验 A1】

库内的键是「顶层文件夹名 + 相对路径」（`pipeline.py:291`）。向导添加文件夹时只拦完全相同的
路径（`wizard.py:479`），`cli init --folder` 也不查。于是 `D:\工作\资料\a.docx` 和
`E:\备份\资料\a.docx` 的键同为 `资料/a.docx.md`。

实验：两个同名文件夹各放一篇 `报告.docx`，内容不同，磁盘上再不动任何东西，连跑 5 轮：

```
round 1: attempted=2 converted=2 | round 2: attempted=1 | round 3: attempted=1
round 4: attempted=1 | round 5: attempted=1   （健康的库从第 2 轮起 attempted=0）
索引里 D 盘那篇和 E 盘那篇轮流出现
```

每轮重转一篇、库里的内容在两篇之间来回翻，永远停不下来。另外 `_configured_tops`
也分不出这两个文件夹，把其中一个移出共享时，它的文档不会变成「已撤回」，而是记成
「源已不在」继续对同事可见——后面这半句是读码得出。

建议：向导 `add_folder` / `confirm` 和 `cli._cmd_init` 里算一遍顶层名，重名就拒绝并说明
「两个文件夹同名，请只共享它们的上级目录，或给文件夹改名」；`pipeline._iter_docs` 里再加一道
保险（重名时只取第一个并在 `app.log` 里记一行）。本机真实的库共享了 10 个文件夹，
顶层名各不相同，目前没有踩到。

### 2.3 全大写的查询词，片段不居中、不高亮【实验 B1】

`_make_snippet`（`index.py:90`）把全大写的词从高亮列表里剔掉了：

```python
terms = [t for t in _query_terms(match) if t and not t.isupper()]
```

| 查询 | 命中 | 高亮 | 片段落在命中处 |
|---|---|---|---|
| `演算` | 是 | 是 | 是 |
| `gis` | 是 | 是 | 是 |
| `GIS` | 是 | **否** | **否**（给了文档开头） |
| `HEC-RAS` | 是 | **否** | **否** |

这个过滤应该是早期为了避开 `AND` / `OR` 操作符留下的，但 `build_match_query` 只给检索词
加引号，操作符从来不会出现在 `_query_terms` 的结果里，过滤已经没有用处，只剩副作用。
水利设计里的缩写大多是大写的（GIS、DEM、BIM、CAD、HEC-RAS），命中了却看不到命中位置。
修法是去掉 `not t.isupper()`，补一条 `q="GIS"` 的片段测试。

### 2.4 docx 转换丢内容；一张坏表废掉整篇【实验 C4 + 真实库】

`render_docx.docx_bytes_to_md` 只读 `w:body` 的直接子节点 `w:p` / `w:tbl`，
段落文本取 `para.text`。构造一份包含各种结构的 docx，结果：

| 内容 | 结果 |
|---|---|
| 普通段落 | 保留 |
| 修订功能**插入**的文字（`w:ins`） | **丢失** |
| 内容控件里的段落（正文级 `w:sdt`） | **丢失** |
| 文本框里的文字（`w:txbxContent`） | **丢失** |
| 页眉、页脚 | **丢失** |

修订插入最要紧：一份还在评审流程里的文档，别人搜不到新加的内容，而库主人自己看不出
任何异常。删除的内容（`w:delText`）本来就不该进库，现状是对的，别一起改掉。

坏表：`_table_to_gfm`（`render_docx.py:36-46`）遍历 `table.rows → row.cells`，
外面没有任何兜底，python-docx 一抛异常整篇文档就失败了。本机真实库的 20 条失败记录里
有一条的原因原样是 python-docx 的内部报错 `no 'tc' element at grid_offset=38`
（一篇 `.doc`，纵向合并的表格）。我用 5 种合成形态都没能复现，所以这条的证据只有真实库里的
那一条记录；修的时候可以拿它做回归样本。

建议：

- 段落文字改成取段落下所有 `w:t`（不含 `w:del` 子树），`w:ins` / `w:sdt` / `w:hyperlink`
  里的内容自然就进来了；
- 正文级的 `w:sdt` 递归进 `w:sdtContent`，把里面的 `w:p` / `w:tbl` 当正文处理；
- `_table_to_gfm` 外面包一层 `try`：失败时退回「把表格里所有 `w:t` 用「 / 」连起来」，
  保证这一篇的其余部分照常入库，并在 md 里留一行说明；
- 文本框、页眉页脚是否收录是产品取舍：工程特性表、图题常在文本框里，文号常在页眉里。
  建议收，但单独成一个开关，默认先关。

---

## 三、P2

### 3.1 OCR 的临时目录在两个出口不清理【实验 C2 / C3】

`pdf_conv._ocr_text` 在第 336 行建了临时目录。后面有三个返回点，其中「一页都没渲染出来」
那个在 `try / finally` 里（`351-356`），会删；前两个在 `finally` 之前，不会删：

| 出口 | 留下了什么 |
|---|---|
| 前三页没有图（`341-342`） | 一个空目录——每一篇没有文字层的短 PDF 留一个 |
| 渲染中途失败（`347-348`） | **已经渲染出来的页面图片**。实验里第 3 页渲染失败，`p0001.png`、`p0002.png` 留在 `%TEMP%` |

第二条是隐私问题：密文 PDF 经兜底工具解成临时副本后再渲染，失败时明文页面图片会一直留在
临时目录里，和「明文副本用完即删」的设计相反。`decrypt_via_disguise`（`pdf_conv.py:91-94`）
也有同样的问题：兜底工具超时时，装着副本的目录不会被删（副本这时还是密文，只是占地方）。

修法：把 `mkdtemp` 之后的全部代码放进一个 `try / finally: shutil.rmtree(tmpdir, ignore_errors=True)`。

### 3.2 OCR 文本的页码标错、「只识别了前 N 页」算错【实验 C1】

`pdf_conv.py:359-366` 先把空页丢掉，再按丢掉之后的顺序重新编号。一篇 4 页的扫描件，
第 2、3 页没识别出字，结果是：

```
（扫描件：以下文字由 OCR 识别，可能有错字）（只识别了前 2 页，共 4 页）
<!-- page 1 -->  第一页正文
<!-- page 2 -->  第四页正文        ← 应该是 page 4
```

其实 4 页都试过了。扫描件里的空白页、整页印章很常见，页码是读者回去翻原件的唯一线索。
修法：按原始页序保留位置，输出时跳过空页但用原始下标编号；提示语用「尝试过的页数」
（`len(images)`），不是「有字的页数」。

### 3.3 OCR 两条路各拿一份时间预算【实验 C7】

`ocr_images` 把同一份 `budget` 先给 Windows 路、再原样给 Tesseract 路（`ocr.py:230-237`）：

| 页数 | 单路预算 | 两路加起来 |
|---|---|---|
| 5 | 120 秒 | 240 秒 |
| 60 | 210 秒 | **420 秒** |

而 `should_stop` / `budget` 只在篇与篇之间检查。也就是说用户点了「停止建库」或者回来了，
最多还要等 7 分钟（另加渲染时间）这一篇才停。正常情况下用不了这么多——实测 Windows OCR
是 2.3 秒进程启动 + 每页 0.16 秒，60 页约 12 秒——会吃满预算的只有卡死和 Tesseract 路。

修法：`ocr_images` 只收一个截止时刻，Tesseract 路拿「剩余时间」；管道按配置传一个单篇 OCR
预算进来（建议默认 180 秒，写进 `config.json`）。

### 3.4 源文件已经删除的失败记录永远不清【实验 A4】

`run` 的收尾只处理 `files`（`pipeline.py:667`），`failures` 不在其中。一篇坏文件转失败后
被用户删掉，它的失败记录会一直留着：

```
after deleting the source + 3 rounds: failures=1（状态窗口一直显示「1 篇转换失败」）
「清理…」 removes 0（它只看 files[] 里 source_missing 的，不看 failures[]）
```

真实库的 20 条失败目前源文件都还在，所以这条现在还没造成困扰，但它只增不减。
建议让 `failures` 和 `files` 用同一套规则：文件夹仍在共享列表里但文件没了的，标上
`source_missing`，不计入状态窗口的「失败 N 篇」，由「清理…」一并处理；文件夹已移出共享
列表的，直接删掉这条记录。

### 3.5 两字查询跨库合并时没有相关度信号【实验 B3】

两字查询走 `_like_search`，所有结果的 `score` 都是 `0.0`（`index.py:419`）。skill 的
`merge()` 靠分数归一化排序（`query.py:213-216`），全是 0 时并列，最终顺序变成「按库主人
的名字排序」：

```
4 libraries x 6 hits each, take=20 -> 安工 6 条、李工 6 条、王工 6 条、赵工 2 条
ranks are all identical: True
```

「乐平」「洪水」这类两字词恰恰是水文最常用的查询，库一多，名字靠后的同事的库基本进不了
前 20。两处都可以改，互不依赖：

- 服务端：两字路按库内名次给一个负分数（`-1, -2, …`），「越负越好」和 bm25 同向，
  老版本的 skill 不用改就能正确归一化；
- skill：所有分数相同时各库轮流取（或直接把 `take` 调大），别让排序退化成字典序。

### 3.6 新版本发布后，同事最长 6 小时才发现【读码】

`UpdateChecker` 每 6 小时检查一次（`updater.py:50`），`release.py` 的提示也写着「同事侧
6 小时内自动发现」。其实库端每 60 秒的心跳回包里就带着 `latest_version` 和 `upgrade`
（`registry.py:1377-1385`），只是 `_heartbeat`（`app.py:586-608`）只读了 `server_time`
——`app.py` 里 `upgrade` 和 `latest_version` 出现 0 次。

修法：心跳回包说有新版本、且本地还没有进入下载/就绪状态时，调一次 `check_updates_async()`。
发布后一分钟内全员发现新版本，中心侧不用改。

### 3.7 包体里约 47 MB 可以去掉【实验】

`_internal` 解压后 164 MB，zip 96 MB，1159 个文件。逐项追到来源：

| 内容 | 体积 | 来源 | 能不能去 |
|---|---|---|---|
| `numpy/` + `numpy.libs/` | 26.6 MB（其中 OpenBLAS 一个 DLL 就 20 MB） | 不在依赖清单里；Pillow 有 3 个文件写着可选的 `import numpy`（`PIL/Image.py` 等），PyInstaller 顺着它收了进来 | 源码层屏蔽 `numpy` 后 fitz / PIL / pptx / docx / pystray / win32com 六个包都能导入，fitz 渲染取字正常 |
| `Pythonwin/` + `mfc140u.dll` | 11.8 MB | `win32com.client.makepy → pywin.mfc → win32ui` | 同上，六个包都能导入；但 `Dispatch` 的运行时路径没跑过，要打包版冒烟确认 |
| 随包的 5 个 Tesseract DLL | 8.3 MB | 见 1.2，逐字节相同的第二份 | 能，见 1.2 |
| `PIL._avif` | 7.5 MB | Pillow 的 AVIF 插件 | 可选，这个库不处理 AVIF |

前三项合计 46.7 MB，约占 `_internal` 的 28%。zip 能小多少没有实测。
做法是在 spec 的 `EXCLUDES` 里加 `numpy`、`win32ui`，出包后拿「docx / pptx / pdf / 扫描件各转
一篇 + 一次 COM 转换」的冒烟跑一遍再发。

### 3.8 psutil 是事实依赖，但没有声明【实验】

`netinfo.local_subnets()` 用 psutil 读网卡并按名字过滤虚拟网卡（`netinfo.py:135-147`），
它决定向导和 CLI 的默认允许网段。psutil 不在 `pyproject.toml` 的依赖里，也不在 spec 的
`hiddenimports` 里——它现在在包里，只是因为这台开发机装了它，PyInstaller 顺着
`import psutil` 把它收了进来（`_internal/psutil/_psutil_windows.pyd`）。

后果和 1.2 是同一类：换一台没装 psutil 的机器出包，会走 `local_ips()` 的回退路径，
「虚拟网卡不算本站」这段逻辑静默失效（WSL、Docker 的网段会被写进默认允许范围）。
修法二选一：把 `psutil` 写进 `dependencies` 和 `hiddenimports`；或者去掉它，用
`ctypes` 调 `GetAdaptersAddresses`。另外那张虚拟网卡名单（`netinfo.py:114-124`）是按名字
匹配的，ZeroTier、Tailscale 这类不在里面——这一条是读码得出，内网风险很低，记一笔即可。

### 3.9 文档和帮助文案：默认网段写错、脱敏有残留【检索】

- **默认网段的说法过时了。** 0.2.22 起默认值是「本机所在网段现算」，但
  `cli.py:476` 的帮助、`server.py:5` 的模块说明、`README.md:79` 还写着固定网段。
  `cli.py:476` 这一条是 `landoc init --help` 直接显示给用户的，属于文案错误。
- **脱敏残留。** `wizard.py` 的注释写着「源码里不留任何一个单位的具体网段」，但登记中心的
  内网地址和内网网段还留在 9 个文件共 25 行里：`README.md`（7 行）、
  `docs/manual/user-guide.html`（8 行）、`landoc/cli.py`（2 行）、`landoc/registry.py`（2 行）、
  `skill/lan-lib-search/SKILL.md`（2 行），以及 `landoc/config.py`、`landoc/server.py`、
  `landoc/wizard.py`、`skill/lan-lib-search/scripts/query.py` 各 1 行。
  其中 `query.py:35` 的 `DEFAULT_REGISTRY` 是功能性的默认值，不能简单删掉；
  可以改成从环境变量或 `config.json` 读，或者由 `/skill.zip` 生成时写入
  （`build_skill_zip` 现在已经会写入 `scripts/config.json`）。改不改是发布策略问题，
  这里只列出位置。

### 3.10 空闲时每 30 秒整份重写一次 `state.json`、重新解析三次【实验 B6】

空闲时 worker 每 30 秒跑一轮（`app.py:138`）。没有任何事可做的一轮也会：

- 在 `run` 开头解析一次 `state.json`（`pipeline.py:374`）；
- 在 `finally` 里 `_flush(force=True)`，整份重写 `state.json` 和 `failures.json`（`706`）；
- 收尾后 `_refresh_counts` 再解析一次（`app.py:556`），`missing_count` 又解析一次（`app.py:563`）。

唯一的实际变化是 `last_scan_ts`，而它已经单独写进索引的 meta 了（`pipeline.py:708`）。

| 库的篇数 | `state.json` | 写一次 | 解析一次 | 每个空闲轮次（写 1 次 + 解析 3 次） | 24 小时空闲 |
|---|---|---|---|---|---|
| 1500 | 0.19 MB | 8 ms | 3 ms | 约 16 ms，0.19 MB | 约 0.8 CPU 分钟，0.5 GB |
| 7500 | 0.92 MB | 33 ms | 11 ms | 约 65 ms，0.92 MB | 约 3 CPU 分钟，2.7 GB |
| 50000 | 6.15 MB | 197 ms | 81 ms | 约 440 ms，6.2 MB | 约 21 CPU 分钟，17.7 GB |

「每个空闲轮次」和「24 小时」两列是按实测的单次开销算出来的（`failures.json` 很小，按忽略计），
不是单独计时。本机真实的库是 1107 篇、`state.json` 249 KB，照此估算每个空闲轮次约 20 毫秒、
每天约 0.7 GB——不痛，但库变大后会线性变痛。

修法：给 `state` 加一个「脏」标记，只有真的变了（收录、失败、撤回、源丢失、环境指纹）才写；
`last_scan` 只写索引 meta；`_refresh_counts` 和 `missing_count` 共用一次解析，
或者由 `run` 在落盘时顺手写一个几十字节的 `status.json` 摘要给状态窗口读。

---

## 四、P3：顺手清单

| # | 事项 | 位置 | 说明 |
|---|---|---|---|
| 4.1 | 「最近收录」和目录列表里的类型标签是空的【实验 B4】 | `index.html:236,255` | 页面打印 `f.ext`，但 `/api/recent`、`/api/list` 的行只有 `path` / `title` / `size`。服务端补一列，或页面从路径里取 |
| 4.2 | 一个目录超过 500 篇时静默截断【实验 B4】 | `index.py:460-474` | 520 篇的目录返回 `count=500`，回包里没有任何「被截断了」的信号。补 `truncated` / `total`，或者支持 `offset` |
| 4.3 | 列表按 Unicode 码位排序【实验 B4】 | `index.py:471` | 实测顺序：安装方案、张三报告、李四报告、沉沙池设计、波涌分析、王五报告、陈六报告。中文标题读起来像乱序；可以按拼音排 |
| 4.4 | 网页表格按 `\|` 切格，转义过的 `\|` 会被切开 | `index.html:279` | 服务端把单元格里的竖线转义成 `\|`（`render_docx.py:33`），页面直接 `split('\|')`，含竖线的单元格会错位 |
| 4.5 | `ocr.tidy` 把汉字和英文、数字之间的空格也吃掉了【实验 C6】 | `ocr.py:211-212` | `设计洪水位 198.5 m` → `设计洪水位198.5 m`。只在两侧都是汉字时才去掉空格 |
| 4.6 | Tesseract 只用 `chi_sim`，随包的 `eng` 模型从不参与【实验 C7】 | `ocr.py:296` | 包里带着 `eng.traineddata`（3.9 MB），命令行实际是 `-l chi_sim`。要么改成 `chi_sim+eng`（对英文和数字一般更好，需拿真实样本量），要么把 eng 从包里去掉 |
| 4.7 | 启动时在主线程同步起 PowerShell 探测 OCR | `app.py:218-222` | 实测一次 2.0 秒（注释写的是 0.4 秒），服务和托盘图标都要晚 2 秒出现。挪到 worker 线程；缓存 5 分钟过期后每轮还会再付一次 |
| 4.8 | 状态窗口每 120 毫秒刷一次，隐藏时也在刷 | `panel.py:465-497` | 注释写的是 1.5 秒。隐藏时只需抽队列，不必重算 8 个标签 |
| 4.9 | 「可访问的库」在中心不可达时串行探活 | `discover.py:449-455` | 每个离线库等 1.2 秒。skill 那边已经改成并发了（`0652fd2`），这里没跟上。在后台线程里跑，不卡界面，只是慢 |
| 4.10 | 限流写死 60 次/分钟/IP，静态页也计数，本机不豁免 | `server.py:213-215,372` | 没有配置项，撞上了用户没有办法。做成可配置，本机地址豁免 |
| 4.11 | 状态窗口只有「N 篇转换失败」一个数 | `app.py:93-94` | 真实库的 20 条失败是：无文字层 7、不可读 7、空文本 4、超时 1、解析 1。按原因分一下类，用户就知道该找谁 |
| 4.12 | xlsx / et 仍然整类跳过 | `pipeline.py:35` | 第二轮的 4.5，继续留给 0.3 |
| 4.13 | 混合 PDF 不做 OCR | `pdf_conv.py:379-386` | 全文够 50 个字就直接返回，后面附的扫描页（批复、签字页）不会识别。读码得出，产品项 |

---

## 五、查过、不成立或不建议动

这几条都认真查过或量过，结论是**不用改**，记在这里免得以后重复投入。

| 猜测 | 实际情况 |
|---|---|
| Windows OCR 有 2600 像素的尺寸上限，300 dpi 的 A4（2480×3509）会被拒 | 不成立。实测 2480×3509 的页面识别成功（rc=0，83 字） |
| 两字查询很慢，需要 bigram 索引或更大的缓存 | 不成立。真实库（1107 篇、4360 万字）上「洪水」「水库」「设计」各 107~129 毫秒；合成的 7500 篇 × 8000 字是 197~204 毫秒；缓存命中 0.01 毫秒。按这个比例要到 6 亿字左右才会超过 skill 的 2 秒超时，现有的库离那还远 |
| PPT 里的组合形状中的文字会丢 | 没有证据。两份能打开的真实样本（19 页、49 页）组合形状数都是 0，python-pptx 路径取全了顶层文字。代码里确实没有递归进组合（`pptx_conv.py:49`），但目前的样本里没有触发，先不动 |
| 300 dpi 比 200 dpi 值得 | 看不出差别。每页 0.16 秒对 0.08 秒；合成样本上 300 dpi 反而少认对一个字。样本太小，不下结论，只记下数字 |
| 合成一张坏表来复现整篇失败 | 没复现。`gridBefore`、纵向合并缺续格、`gridSpan` 超宽、行内格数多于网格、行内格数少于网格这 5 种形态，python-docx 1.2.0 都能处理。2.4 里坏表的证据只有真实库的那一条记录 |
| 更新器和看门狗 | 复核后没有新问题。两次改名、健康标记回滚、退出码 0 / 3 的特判这些维持原判，别动 |
| 登记中心每次心跳都落盘 | 是这样（`registry.py:449-452`），但文件只有几十 KB，几十个库每分钟几十次小写入，量级上可以接受 |
| 第二轮采纳的几项 | 逐条复核过，都还在、都有效 |

---

## 六、建议实施顺序

| 批次 | 内容 | 说明 |
|---|---|---|
| 第 1 批（各自半小时到两小时，互不依赖） | 2.1 撤回标记只在变化时写、2.2 同名文件夹校验、2.3 大写词高亮、3.1 临时目录、3.2 页码 | 都是改动小、有实验撑着的，可以合成一版发 |
| 第 2 批 | 1.1 清理与转换互斥、3.4 失败清单、3.6 心跳触发更新检查 | 1.1 动的是删库路径，测试要写足 |
| 第 3 批（发布链，一次做完） | 1.2 Tesseract 依赖收进仓库 + 净化 PATH 检查、3.7 包体、3.8 psutil | 改完必须在净化 PATH 下把打包版的四种文档各转一遍再发 |
| 第 4 批 | 2.4 docx 内容、1.3 PPT 的 COM 路径 | 2.4 先做修订插入和坏表兜底；1.3 先在测试机上确认，再按结论改 |
| 第 5 批 | 3.3 OCR 预算、3.5 跨库合并、3.10 空转写盘、3.9 文案、第四节 | 按节奏排 |

**本轮确认不要动的**：FTS external content 的迁移链；`PDFSession` / `OfficeSession`
的单线程 COM 模型（1.3 改的是「谁拥有这个进程」的判断，不是模型本身）；调度器的
「尝试篇数 + 预算 + 每篇收工检查」语义（3.3 只是把 OCR 的单篇预算收拢，不改调度）；
`_like_search` 的两步查询和 `_make_snippet` 的 `<<>>` 标记协议（2.3 只删一个过滤条件）；
更新器与看门狗的状态机。

---

## 附：验证方式

- 基线：`python -m pytest tests -q`，当前 `538 passed, 17 skipped`，约 3 分 17 秒。
- 1.1：一条线程 `index.upsert()` 后持锁 7 秒，另一条线程调 `prune_gone`；断言索引行、md、
  `state.json` 三者一致。再把 `convert_one` 换成睡 2 秒的替身，清理落在轮次中间，
  断言轮次结束后 `gone` 不复活。
- 1.2：环境变量只留 `PATH=C:\Windows\System32;C:\Windows`，直接运行
  `_internal\landoc\tools\tesseract\tesseract.exe --version`，返回码必须是 0。
  再用同一个环境跑 `LanDocShare.exe init` + `convert`，收一篇纯图片 PDF，
  `state.json` 里应是 `via=ocr` 且没有失败记录。
- 1.3：按 1.3 里的三步在测试机上做，重点看没保存的窗口还在不在。
- 2.1：移出文件夹后连跑两轮，第二轮提交后 WAL 文件大小的增量为 0。
- 2.2：两个同名文件夹各放一篇同名文件，第二轮起 `attempted` 必须是 0（修完后应当被校验拦住）。
- 2.3：`search("GIS")` 的片段里要有 `<<GIS>>`。
- 2.4：用含 `w:ins`、`w:sdt`、`w:txbxContent` 的 docx 断言三种内容都出现在 md 里；
  坏表用真实库里那一篇做回归样本。
- 3.1：把 `%TEMP%` 指到临时目录，走两个出口各一次，目录数前后不变；兜底工具超时那一条同样。
- 3.7 / 3.8：改完 spec 后出一次包，在净化 PATH 下跑 docx / pptx / pdf / 扫描件各一篇。

实验脚本（都是只读仓库、在临时目录里跑的独立脚本）放在
`%TEMP%\landoc-research\`，没有进仓库；上面的每一条都可以照着改写成单测。
临时目录可能会被系统清掉，需要的话应当把对应的断言落到 `tests/` 里，而不是留着脚本。
