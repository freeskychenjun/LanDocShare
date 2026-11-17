# LanDocShare（一期核心）

局域网个人资料库共享的**核心工程**：把加密/明文的办公文档转成 Markdown、建 FTS5 索引、通过只读 HTTP 服务给同事的 AI 助手检索。

设计与计划的来源：
- 设计：`docs/superpowers/specs/2026-11-16-lan-personal-library-design.md`
- 计划：`docs/superpowers/plans/2026-11-16-lan-library-core.md`

本仓库含一期核心（转换 + 索引 + 检索服务）和二期第一批：托盘程序、开机自启、空闲门控、PyInstaller 打包。登记中心（registry）与客户端技能 `lan-lib-search` 还没做。

## 快速开始

```bash
# 建库（共享目录可多选，重复 --folder）
python -m landoc init --lib-dir D:\MyLib --name "我的资料库" --folder "G:\2025年工作" --folder "G:\成果报告"

# 体检（抽样看明/密比例、本机有没有 Office / Acrobat）
python -m landoc probe --lib-dir D:\MyLib

# 转换第一批（增量：再跑只处理有变化的文件）
python -m landoc convert --lib-dir D:\MyLib

# 起服务（默认 8766，被占用自动顺延到 8768 起——8767 留给二期 registry）
python -m landoc serve --lib-dir D:\MyLib

# 看统计
python -m landoc status --lib-dir D:\MyLib

# 维护允许名单（姓名只作备注，真正按 IP 放行）：CSV 两列 姓名,IP
python -m landoc people --lib-dir D:\MyLib --csv 名单.csv     # 合并导入
python -m landoc people --lib-dir D:\MyLib                    # 看名单
```

检索接口（只读，只给片段与表格，永不给全文/下载）：

同事直接用浏览器打开 `http://<内网IP>:8766/` 就是检索页（搜索框 + 目录树 + 表格查看，纯静态无外链）；下面这些是给 AI 助手/脚本用的。

```bash
curl --get "http://<内网IP>:8766/api/search" --data-urlencode "q=马斯京根" --data-urlencode "limit=5"
curl --get "http://<内网IP>:8766/api/tables" --data-urlencode "path=成果/乐平.docx.md"
```

端点：`/`（网页）、`/api/info`、`/api/health`、`/api/meta`、`/api/stats`、`/api/search`、`/api/tree`、`/api/list`、`/api/tables`。

收紧共享（隐私开关，默认都开；`init` 时一次设好，之后 `status` 会显示当前策略）：

```bash
# 只把每篇前 20000 字写进索引（检索/片段只在这段内命中，超出部分等于没共享）
python -m landoc init --lib-dir D:\MyLib --name "我的资料库" --no-store-content
# 嫌 20000 不够保召回就调大（≥200；越大越保检索，越少防"整库明文被拷走"）
python -m landoc init --lib-dir D:\MyLib --name "我的资料库" --no-store-content --snippet-only-chars 50000
# 不落 md 副本（表格改从索引正文里抽；关掉后重跑 convert 会清掉旧副本）
python -m landoc init --lib-dir D:\MyLib --name "我的资料库" --no-render-md
```

本机的取舍是**召回优先、外泄控制放松**，所以 `snippet_only_chars` 默认给足 20000 字，多数简报/汇报/规程可整篇留下；真要收紧再往下调。但要知道这开关换的正是召回：截断之外的正文搜不到、表格也可能只在 md 副本里完整（`--no-render-md` 时以正文为准）。

改完开关重跑 `convert` 就会按新策略收紧已有文档；把 `--render-md` 打开再跑会补回副本。

## 托盘与打包（P1）

```bash
# 源码方式跑托盘（调参时用）
python -m landoc.tray --lib-dir D:\MyLib

# 出包（onedir；产物 dist\LanDocShare\LanDocShare.exe，另有 -debug.exe 带控制台）
python -m PyInstaller packaging\LanDocShare.spec --noconfirm --clean
```

双击 `LanDocShare.exe` 之后：起检索服务 + 后台等**空闲**（5 分钟无键鼠输入，或已锁屏）才做增量转换，不跟人抢 Office；重复双击不会起第二个服务，只把已运行实例的状态窗口打开。托盘菜单：显示状态窗口 / 打开库主页 / 立即更新 / 修改建库设置… / 暂停共享 / 权限设置 / 开机自启 / 查看日志 / 退出。

- **首次运行自动弹建库向导**（`landoc/wizard.py`，tkinter）：没有 `config.json` 时（或 `--wizard`）先问库名、要共享的文件夹、谁能查，选完才起服务。选文件夹时当场估"约多少篇 · 多大 · 要多久"（只 walk + stat，不打开文件）。已经建过库的，从托盘菜单「修改建库设置…」重开：字段预填当前值，改完**当场生效**（服务端每请求现读共享范围、调度线程每轮现读文件夹），不必重启；存储策略（`store_content` 等）不在向导里，重开向导不会把它冲掉。
- **「谁能查」三档**：公司内网（默认网段 10.91.0.0/16，**可改**，如只给本院填 10.91.1.0/24）/ **仅指定名单** / 仅本机。名单是"**按 IP 放行、姓名只作备注**"：名单里填了 IP 的机器一打开网页就能查（不用报姓名），审计日志里记那条对应的姓名；姓名的两种用途是①IP 会变的人可以不填 IP、只登记姓名，这时必须报姓名才放行；②日志里好看。填了 IP 会校验落在允许网段内（否则提示"这个人会连不上"）。名单只存本机 `config.json`，`/api/meta` 只回 `name_required`（只有存在"没登记 IP"的人时才是 true）、不回名单本身。命令行维护：`people --csv 名单.csv`（`姓名,IP` 两列，可带表头；`--replace` 整体替换、`--set 姓名=IP`、`--remove 姓名`、`--export` 导出给客户端分发）；命令行等价物还有 `init --allow-cidrs 10.91.1.0/24,10.91.10.0/24`。改名单**即时生效**，不用重启服务。
- **状态窗口**：启动即弹出的小窗（`landoc/panel.py`，tkinter，无新依赖），一眼看到状态、篇数、共享范围、服务地址、上次扫描，带「打开检索页 / 立即更新 / 暂停共享 / 复制地址」四个按钮。关窗只是隐藏，双击托盘图标可再打开；真正退出在托盘菜单里。开机自启的 `--autostart` 模式不弹窗、不抢浏览器，只驻留托盘并弹一条"已在托盘运行"的提示。
- Win11 会把新图标收进任务栏的 `^` 溢出区：点开 `^`，把纸页图标拖到任务栏上就常驻了。

- **单实例**：命名互斥量 `Local\LanDocShare-single-instance`。
- **开机自启**：菜单里勾选，写 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`，不需要管理员。
- **记忆库目录**：上次打开的库记在 `%LOCALAPPDATA%\LanDocShare\last_lib.txt`，双击回落 `D:\MyLib` 或开向导。
- **包体**：约 175 MB（onedir）。开发机上装着一堆无关重型包（torch/cv2/scipy），`packaging/LanDocShare.spec` 里显式排掉了——不排会膨胀到 1.2 GB。换机器打包若报缺模块，看 `LanDocShare-debug.exe` 的控制台输出再补 `hiddenimports`。

## 关键机制（都来自实测，别按直觉改）

| 现象 | 机制 | 实现位置 |
|---|---|---|
| 加密 docx 用 python 读会 `BadZipFile` | 透明加密驱动只对白名单进程（winword/wps/acrobat/acad…）透明解密 | `probe.classify` / `office_bridge` |
| Office 另存为的中间文件读不出来 | **加密与否取决于"谁写"**：白名单进程写盘一律是密文 | 故一律"Office 内存取字符串 → python 落盘" |
| 加密 docx 如何转 md | `doc.Content.WordOpenXML` 取 Flat OPC → python 补 `[Content_Types].xml` 重建 docx → `python-docx` 解析 | `office_bridge` |
| 加密 pdf 如何转文本 | **本机有 Acrobat 就用它**（`PDDoc.Open` + 逐页 `CreateTextSelect`/`GetText`，21 页 0.8 秒、不落明文副本）；否则用伪装名 `acrobat.exe` 解**临时副本**，抽完即删。工具目录用 `init --acrobat-tool-dir` 指定（不给则自动探测 `D:\应用软件\右键解密`） | `pdf_conv` |
| Acrobat 取文本为什么要拼接 | `GetText(i)` 返回的是**单个字符**，直接 join 会得到逐字断行的碎片，trigram 命中率归零；`_join_tokens` 做中文不加空格/ASCII 补空格 | `pdf_conv._join_tokens` |
| 一批文档为什么共用一个 Office | 每篇重启 Office 要白付 6 秒；`OfficeSession` 把整批固定在同一 COM 线程 | `office_bridge` |
| `.doc/.ppt/.dps/.wps` 为什么也走 COM | 这些是 OLE 容器，python-docx/python-pptx 不认；判据是"是否加密 或 不是 zip" | `pipeline.convert_one` |
| 中文检索为什么用 trigram | jieba 分词边界对不上就零命中（查"马斯京根"vs 索引里"马斯京根法"）；trigram 做子串匹配 | `index.build_match_query` |
| 2 字查询（"乐平""洪水"） | 低于 trigram 最小长度，改走 `instr` 子串扫描 | `index._like_search` |
| 想"只共享片段、不落盘" | `--no-store-content` 只把前 `snippet_only_chars` 字（默认 20000，可调）写进索引（超出部分搜不到是刻意的）；`--no-render-md` 不落 md 副本、清掉旧副本 | `pipeline.run` / `Config` |

## 实测基准（本机，Python 3.14 / WPS + Office 2019）

三份真实加密样本（一份 880 KB docx、一份 2.3 MB pptx、一份 389 KB pdf）：

- 端到端 `init → convert → 索引 → 检索`：**3 篇（docx+pptx+pdf）/ 27 秒**（三种格式各起一次 Office 实例，热路径 ~1.9 秒/篇）
- 同一类批量（5 份加密 docx）：**15.4 秒** = Office 启动 6 秒 + 约 1.9 秒/篇（一批共用一个 Office 会话；若不共用，每篇要重付 6 秒）
- 加密 docx：31,545 字正文 + 13 张表格，Heading 1/2/3 完整
- 加密 pptx：逐页形状文本 + 表格
- 加密 pdf：Acrobat 路线 **5.1 秒（含进程启动）/ 21 页 8,456 字**，文本干净、无需 OCR、不留明文副本；服务端 `水安全保障`/`十五五` 均命中并高亮

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
  server.py        只读检索服务（CIDR 权限 / 名单按 IP 放行 / 限流 / 审计）
  cli.py           命令行入口
  people.py        允许名单（姓名 → IP）的 CSV 解析、合并与导出
  app.py           托盘背后的调度：起服务 + 空闲时增量转换 + 暂停/恢复共享
  idle.py          空闲判定（GetLastInputInfo / 锁屏检测）
  autostart.py     开机自启（HKCU Run 键）
  wizard.py        建库向导（tkinter 单屏）
  panel.py         状态窗口（tkinter，关窗=隐藏）
  netinfo.py       内网地址探测（CLI 与状态窗口共用）
  tray.py          托盘入口 + 单实例互斥量（也是打包入口）
```
