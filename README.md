# LanDocShare（一期核心）

局域网个人资料库共享的**核心工程**：把加密/明文的办公文档转成 Markdown、建 FTS5 索引、通过只读 HTTP 服务给同事的 AI 助手检索。

设计与计划的来源：
- 设计：`docs/superpowers/specs/2026-11-16-lan-personal-library-design.md`
- 计划：`docs/superpowers/plans/2026-11-16-lan-library-core.md`

本仓库含一期核心（转换 + 索引 + 检索服务）和二期：托盘程序、开机自启、空闲门控、PyInstaller 打包、登记中心（registry，8767）、客户端技能 `lan-lib-search`，以及 `http://<中心>:8767/` 的一页式分发（下载库软件 / 下载写好地址的 skill 包）。

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

# 清理「源文件已不在」的文档（库只增不减，瘦身要人点头；不加 --gone 只报数）
python -m landoc prune --lib-dir D:\MyLib --gone

# 维护允许名单（姓名只作备注，真正按 IP 放行）：CSV 两列 姓名,IP
python -m landoc people --lib-dir D:\MyLib --csv 名单.csv     # 合并导入
python -m landoc people --lib-dir D:\MyLib                    # 看名单
```

检索接口（只读，只给片段与表格，永不给全文/下载）：

同事直接用浏览器打开 `http://<内网IP>:8766/` 就是检索页（搜索框 + 目录树 + 表格查看，纯静态无外链）。打开就先显示「最近收录」50 篇（不输关键词也有东西看），搜索时按钮变「搜索中…」、面板写「搜索中…」——库大时查一次要几百毫秒，没有动静会被当成卡死反复点（连点会撞上每 IP 每分钟 60 次的限流，页面会提示"查询太频繁"）。下面这些是给 AI 助手/脚本用的。

```bash
curl --get "http://<内网IP>:8766/api/search" --data-urlencode "q=马斯京根" --data-urlencode "limit=5"
curl --get "http://<内网IP>:8766/api/tables" --data-urlencode "path=成果/乐平.docx.md"
curl --get "http://<内网IP>:8766/api/recent" --data-urlencode "limit=20"
```

端点：`/`（网页）、`/api/info`、`/api/health`、`/api/meta`、`/api/stats`、`/api/search`、`/api/tree`、`/api/list`、`/api/recent`、`/api/tables`。

`/api/recent` 按入库时间倒序返回（默认 50 篇，上限 100），只含 `path`/`title`/`size` 三个字段——和 `/api/list` 一样不暴露源文件路径。网页一打开就调它显示「最近收录」，省得第一次用的人对着空面板以为没收录成功。

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

双击 `LanDocShare.exe` 之后：起检索服务 + 后台等**空闲**（5 分钟无键鼠输入，或已锁屏）才做增量转换，不跟人抢 Office；重复双击不会起第二个服务，只把已运行实例的状态窗口打开。托盘菜单：显示状态窗口 / 打开库主页 / **检查更新（发现新版后变成「下载新版本 v…」→「升级到 v…」）** / **立即重建索引（正在建库时变成「停止建库」）** / 修改建库设置… / 暂停共享 / 打开库目录 / 开机自启 / 查看日志 / 退出。

- **首次运行自动弹建库向导**（`landoc/wizard.py`，tkinter）：没有 `config.json` 时（或 `--wizard`）先问库名、要共享的文件夹、谁能查，选完才起服务。选文件夹时当场估"约多少篇 · 多大 · 要多久"（只 walk + stat，不打开文件）；已经建过库的从「修改建库设置…」打开时，同一行改报"当前库 N 篇（已收录 M，待补收 K 篇 · 约 T）"——拿 `library.db` 的清单跟磁盘对路径/体积/修改时间，只把还没收或源文件变过的算进"待补收"，不再把"从零建库要多久"当成待办（库里全收过就显示"都已收录（保存后只做增量检查）"）。已经建过库的，从托盘菜单「修改建库设置…」重开：字段预填当前值，改完**当场生效**（服务端每请求现读共享范围、调度线程每轮现读文件夹），不必重启；存储策略（`store_content` 等）不在向导里，重开向导不会把它冲掉。**新建库默认「仅指定名单」**（`wizard.default_scope`），但名单**不在向导里编**——它在托盘「授权名单…」那个专职窗体里维护，向导只告诉你现在几个人、去哪加。这样建库这一步不会被名单卡住：选「仅指定名单」且名单还空着时，`Config.roster_only` 会记下这个档，服务端据此认定「一个人都没加 = 谁都查不到」，而不是悄悄退回按网段放行。改已有库时按它现在的档预选，不替人做主，名单原样带过去（改个文件夹不会把已授权的同事清空）。窗口本身分三组（资料库 / 共享哪些文件夹 / 谁能查），每组带边框，左列标签统一 10 字宽、右缘对齐，输入框左缘成一条竖线；文件夹列表带横、竖两条滚动条（共享十几个目录、路径又长时不用猜后面还有什么）；弹出时按实际大小摆到**屏幕正中**（`panel.center_window` 支持传入还没映射的窗口尺寸——Tk 那时只会报 1×1，照它算就跑到左上角去了），多显示器跟着鼠标所在的屏、1080 屏上不压任务栏。
- **「谁能查」三档**：**仅指定名单**（新建库默认）/ 公司内网（网段按本机所在网段现算，**可改**）/ 仅本机。名单是"**按 IP 放行、姓名只作备注**"：填了 IP 的机器一打开网页就能查（不用报姓名），审计日志里记那条对应的姓名；**姓名只用于日志/分组，不能当作访问凭证**——空 IP 的条目一律不授权，知道姓名也进不来。填了 IP 会校验落在允许网段内（否则提示"这个人会连不上"）。名单只存本机 `config.json`，`/api/meta` 只回 `name_required`（恒 false，仅为旧客户端兼容）、不回名单本身。命令行维护：`people --csv 名单.csv`（`姓名,IP` 两列，可带表头；每行必须有 IP，空 IP 会整批拒绝并报行号；`--replace` 整体替换、`--set 姓名=IP`、`--remove 姓名`、`--export` 导出给客户端分发）；命令行等价物：`init --scope people --person 张三=10.91.1.23 --person 李四=10.91.1.24`、`init --scope local`、`init --allow-cidrs 172.16.1.0/24,172.16.10.0/24`。命令行建库默认仍是 `lan`（脚本化建库没法交互填名单）；`--scope people` 不带 `--person` 会直接报错退出（码 2），带上的会写进 `config.json` 并打开 `roster_only`。图形向导建完库、名单还空着时，同事一律 403（`roster empty`），主人在自己机器上不受影响。改名单**即时生效**，不用重启服务。
- **要“整段放行、只挡几台”**：`deny_cidrs` 是黑名单，命中即 403，**优先于允许网段和名单**（连 `127.0.0.1` 也会被挡，别把本机写进去）。`init --deny-cidrs 172.16.1.50,172.16.1.0/28` 设置，`--deny-cidrs ""`（或 `无`）清空；中心侧同名参数 `registry --deny-cidrs` 挡的是“能打开中心页面/清单”的来源。库端与中心都在路由分发**之前**判黑名单，网页、`/api/*`、skill 一视同仁。建库向导「允许的网段」下面一行就是「拒绝的网段」（留空=不拒绝，全角逗号/分号都认），改完当场生效；状态窗口的「共享范围」会显示「· 拒绝 N 段」，不用去翻 `config.json` 才知道黑名单有几条。
- **状态窗口**：启动即弹出的小窗（`landoc/panel.py`，tkinter，无新依赖），一眼看到状态、篇数（有转换失败时显示「N 篇 · M 篇转换失败」）、共享范围、服务地址、上次扫描、**扫描件 OCR 走哪条路**（`Windows 系统 OCR` / `Tesseract 兜底` / 两条都没有时这一行不显示）、**版本**，带「打开检索页 / 立即更新 / **检查更新（有新版时变「下载更新」/「升级到 v…」）** / 暂停共享 / 授权名单 / 复制地址 / **清理…**」按钮。主人把某个共享文件夹撤掉后，这里会多一行「撤回共享 N 篇（同事看不到，你仍可检索；文件夹加回共享即恢复）」。**清理…** 是「库只增不减」的唯一出口：源文件已不在的文档攒到 N 篇时按钮亮起、写「清理 N 篇」，点一下先问「要删掉这些文档的副本和索引行吗」，确认后才删（删之前源文档回来了就自动从名单里消失）；命令行等价物 `LanDocShare.exe prune --lib-dir … --gone`，打包版也走同一条路（`CLI_COMMANDS` 里已含 `prune`）。关窗只是隐藏，双击托盘图标可再打开；真正退出在托盘菜单里。**每次打开都回到屏幕正中**（`center_window`：按光标所在显示器的工作区居中，多屏时跟着鼠标走、任务栏不算在内；把窗口拖到别处后再打开，仍回正中）。开机自启的 `--autostart` 模式不弹窗、不抢浏览器，只驻留托盘并弹一条"已在托盘运行"的提示。
- **「立即重建索引」是真的立即**（旧版只是叫醒调度线程，仍要过空闲门控——正在用电脑时点了没反应）：点下去就置 `_force`，本轮门控与每篇开始前的收工检查都放行，**一直跑到没有新增为止**（不是跑一轮就收工），状态窗口同时显示"已转换 N 篇 · 还剩 M 篇（约 X 分钟）"。转换起的是独立的隐藏 Office/WPS 进程（`DispatchEx` + `Visible=False`），不碰你正开着的窗口，只占 CPU。跑完弹回音：有新增说"已收录 N 篇，可被检索"，没有新增说"已是最新：没有需要收录的新文档"（有失败则报失败篇数）——点了没动静和"按钮坏了"看着一样，所以必须有这句话。**停下**：建库期间托盘那一项和状态窗按钮都变成「停止建库」；「暂停共享」也会把它撤掉（恢复共享后不会自己又跑起来）。
- **扫描件 PDF 会 OCR**（0.2.16）：没有文字层的 PDF（扫描件）不再直接记失败——先用 Windows 自带的 OCR（`zh-Hans-CN` 语言包，0 体积），识别不出来再退回随包的 Tesseract（`landoc/tools/tesseract` 约 8.4 MB + `landoc/tools/tessdata` 的 fast 模型 6.3 MB，chi_sim+eng）。只对"整页是图"的 PDF 动手（文字少但没有图的短通知不白跑），默认 300 dpi、最多前 60 页；识别结果带 `<!-- page N -->` 锚点与提示行「（扫描件：以下文字由 OCR 识别，可能有错字）」。开关在 `config.json`：`ocr_scanned_pdfs`（默认 true，关掉就完全不渲染）、`ocr_dpi`、`ocr_max_pages`。体量参考：2 页意见函约 2.2 秒 / 1648 字；38 篇 175 页约 7 分钟。质量：打印体公文基本可读（`赣` 可能认成 `考` 这类），手写、表格线密的效果一般，所以正文里留了提示行——别把 OCR 结果当原文引用。
- Win11 会把新图标收进任务栏的 `^` 溢出区：点开 `^`，把纸页图标拖到任务栏上就常驻了。
- **托盘图标怎么才画得出来**（`landoc/notifyicon.py`）：pystray 0.19.5 发 `NOTIFYICONDATAW` 时传的是 `hID=`，而字段名是 `uID`，ctypes 不校验关键字 → **uID 永远是 0**，外壳只会留个空槽（没图标、右键点不出菜单）。程序启动时给 `Shell_NotifyIcon` 包了一层：uID 补成非零；`NIM_ADD` 失败（外壳残留上一条记录时常见）就删掉重加，还不行就补一条带 `NIF_ICON` 的 `NIM_MODIFY`，并在后台再试几次。排障可设 `LANDOC_TRAY_DEBUG=1`，日志在 `%LOCALAPPDATA%\LanDocShare\logs\tray.log`。

- **单实例**：命名互斥量 `Local\LanDocShare-single-instance`。
- **开机自启**：菜单里勾选，写 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`，不需要管理员。**默认不勾**；首次建库收录进第一批文档后会弹一次「要不要让它随开机自动启动」，答过就不再问（标记存索引 meta 的 `autostart_asked`，换台机器重建库会再问一次；`--autostart` 模式不弹窗，把这个机会留给下次手动打开时）。
- **记忆库目录**：上次打开的库记在 `%LOCALAPPDATA%\LanDocShare\last_lib.txt`；没有记录就开向导，库位置默认填 `<我的文档>\LanDocShare\<用户名>的资料库`（没有"文档"目录时退 `%LOCALAPPDATA%\LanDocShare\library`），可当场改成别的文件夹。
- **包体**：约 184 MB（onedir；其中 `landoc/tools` 的 OCR 工具与模型占 14.7 MB，zip 约 92 MB）。开发机上装着一堆无关重型包（torch/cv2/scipy），`packaging/LanDocShare.spec` 里显式排掉了——不排会膨胀到 1.2 GB。换机器打包若报缺模块，看 `LanDocShare-debug.exe` 的控制台输出再补 `hiddenimports`。

### 日志与失败文件（远程排障用）

库端现在有自己的一份运行日志 `<库目录>\logs\app.log`（单文件 1 MB，超了滚成 `app.log.1`，只留一份）：启动那行记端口与版本，每轮扫描失败记一行「扫描失败：…」，登记中心断线/恢复只在**状态翻转**时各记一行（断一晚也只留两行，不会被 60 秒一条的心跳刷满）。状态窗口只看得到最后一条 message，重启就没了，所以让同事"翻 `logs\app.log`"比让他复现一次靠谱。

除了启动与轮次摘要，`app.log` 还会记这几类（都按事件各一行）：

- **停止**：`库服务停止：托盘退出（本次收录 3 篇）`——"托盘退出 / 安装更新 / 命令行退出"分清；进程被强杀时**没有**这一行，正好用来区分。
- **崩溃**：`未捕获异常（landoc-worker 线程）：RuntimeError: …` + 栈尾 6 帧。主线程与后台线程都算，不再有"一条线程死了没人知道"。
- **更新**：发现新版本 / 开始下载 / 下载好等待升级 / 开始安装 / 启动更新器失败。更新细节（sha 校验、回滚）仍在 `%LOCALAPPDATA%\LanDocShare\updates\logs\update.log`。
- **失败明细**：`失败明细：资料/坏.docx（unreadable）、资料/带口令.docx（password_protected）`，最多 3 条，多出来的写「等 N 篇」。
- **访问被拒**：`访问被拒：401 /api/search，来源 10.91.1.178（不在名单）`，10 分钟内同类只记一行（扫描器刷不满日志）；状态窗口的「共享范围」行同时显示「最近有 N 次访问被拒」。

同目录另有 `access_log.jsonl`（谁查了什么，按 4 MB 轮转），轮转出来的是带时间戳的归档 `access_log-YYYYMMDD-HHMMSS.jsonl`，**不覆盖、不删除旧记录**（同事的检索史是要留的数据，一年多约 2~3 MB，要清就手工删）。每条带 `ip`/`name`/`api`/`status`/`count`，搜索还带明文 `q`、`dir`（在哪个目录下查的）和 `fallback`（这一查是不是退化成了 `LIKE`）。查"同事搜了但库里没有"的资料缺口：

```powershell
Select-String -Path logs\access_log-*.jsonl,logs\access_log.jsonl -Pattern '"count": 0'
```

`update.log` 在库目录里已经是旧版遗留，现行位置是 `%LOCALAPPDATA%\LanDocShare\updates\logs\update.log`。

转换失败的文件记在 `<库目录>\failures.json`，每篇带 `attempts`/`reason`/`mtime`/`size`：同一版本连续失败一次就不再自动重试，累计 3 次也停；装好 Office/Acrobat 后环境指纹变化会自动把 `no_office`/`no_reader` 这类失败放回队列。扫描件没有文字层又没装 OCR 时记 `no_ocr`（同样算环境原因，不白耗 3 次重试）；OCR 后来可用了（装语言包/换成带 Tesseract 的打包版），`no_ocr` 与旧的 `no_text_layer` 会一起放回队列重试。要人工重试某篇，删掉它在 `failures.json` 里的那条（或改一下源文件让 mtime 变化）即可。

## 登记中心（P2，8767）

让同事们**只发现对自己开放的库**：一台常开机器跑登记中心，各库每 60 s 报一次"我在线、我有多少篇、对谁开放"；中心按请求来源 IP 过滤（客户端带的姓名只用于核对，不能提升权限），才把库的地址/篇数返回给客户端。

```bash
# 在常开的那台机器上（本机就是 172.16.1.197）
python -m landoc.registry --port 8767            # 或 python -m landoc.cli registry
# 也可用打包版：LanDocShare.exe registry --port 8767

# 让某个库去登记（写进它的 config.json；之后每次启动/每 60 s 自动心跳）
python -m landoc.cli init --lib-dir D:\MyLib --name 赵六的资料库 --folder D:\资料 ^
    --registry-url http://172.16.1.197:8767
```

- `POST /register`：库端心跳（`{name, lib_name, port, doc_count, version, allow_cidrs, deny_cidrs, allow_people}`）。**IP 取连接对端，不信自报**；只接受 `172.16.0.0/16`（`--allow-cidrs` 可改，`--deny-cidrs` 可挡）。`allow_cidrs` / `deny_cidrs` / `allow_people` 是库主人当前的授权范围；**没上报授权范围的旧版库、以及暂停共享（空列表）的库一律不显示**。`deny_cidrs` 优先，命中即隐藏。返回 `latest_version` 与 `upgrade`。
- `GET /libraries?name=你的姓名`：只返回**对来源 IP 有权限**的在线库（5 分钟内有心跳 = 在线，30 分钟无心跳 = 移除）。姓名只用于核对，不填也按 IP 匹配；填错不会提升权限，反而可能因同名条目对不上而看不到该库。说明页、`GET /health`、`GET /api/info` 的库计数同样只算对你可见的。
- `GET /health`：中心自身状态。
- `GET /libraries` 响应里不回传各库的授权名单，只回库名/地址/篇数/在线状态。
- 数据落盘 `~/.landoc-registry/registry.json`（`--state-dir` 可改，原子写），重启不丢。
- 开机自启：`LanDocShare.exe registry --install-autostart`（写 `HKCU\...\Run` 的 `LanDocShare-registry` 值，不需要管理员；`--remove-autostart` 撤销）。它与库软件自己的自启值是两条，互不影响。**默认带看门狗**（见下条），并把这次启动实际用到的运行参数（`--package-zip` / `--manifest` / `--roster` / 非默认的 `--state-dir`、`--allow-cidrs`、`--deny-cidrs`、`--roster-public`、`--watchdog-log`、`--watchdog-interval`、`--watchdog-failures`，路径转绝对）一起写进命令——开机后再拉起来仍然是同一套分发配置，不会因为工作目录变了而找不到包，也不会把日志悄悄写到别处。
- **看门狗**（`--watchdog`，`landoc/watchdog.py`）：把中心当子进程管起来，中心退出就按 3/5/10/20/30/60 秒退避重启，活过 5 分钟退避归零；中心还活着但 `/health` 连续 3 次不应答（端口开着、请求没人答）判卡死，杀掉重启。`/health` 只要有人接电话就算活（中心按来源网段回 403 也算），不会误杀。看门狗自己的输出和中心的 stdout/stderr 都写 `%LOCALAPPDATA%\LanDocShare\logs\registry-watchdog.log`（`--watchdog-log` 可改）；中心的输出由看门狗逐行转抄（带 `[中心]` 前缀），子进程按 UTF-8、不缓冲写，所以日志只有一种编码、两条流也不会互相覆盖，被强杀时最后的输出也不再丢。开机自启时没有控制台，出问题就翻这个文件。`--watchdog-interval 0` 关掉探活，只管子进程退出。
- 中心因为**端口被占用**起不来时以退出码 3 退出，看门狗记一条日志后也退出，不会每分钟徒劳重启。
- **重打包前先优雅停**：`LanDocShare.exe registry --stop`（或 `python -m landoc.cli registry --stop`，`--state-dir` 要跟运行中的一致）只是在状态目录留一个 `stop.request` 记号，中心几秒内答完手头的请求再退出，看门狗看到退出码 0 也一起退——重新打包期间不会"刚停就被拉起来"，文件也不会被占用。打包完再启动一次即可；下次启动会自动清掉过期的记号。
- 只需要元数据，中心挂了不影响各库自身检索。
- **可见性口径与库端一致**：中心复用库端的 `_ip_allowed` + `authorize_person` 判断，库端会放行的来源才看得到这个库；中心因此会保存各库上报的授权名单（姓名/IP，不含文档内容）。这是"同事只能看到对自己开放的库"的必要代价；旧版库升级前不会出现在清单里。

## 分发（P2，`/` 一页装完）

登记中心同时是**分发入口**：同事打开 `http://172.16.1.197:8767/` 就能下载，不用谁手工发文件、填地址。

```bash
# 1) 打包库软件 zip（PyInstaller 之后跑一次；产物默认 distribution/LanDocShare.zip）
python packaging/make_package.py

# 2) 起中心时告诉它包在哪（不给就在 exe 同目录、distribution/ 下自动找）
LanDocShare.exe registry --port 8767 --package-zip D:\share\LanDocShare.zip
# 公网/DNS 场景可加 --advertise http://landoc.example:8767，默认按请求 Host 推断
```

- `GET /`：说明页（三步上手 + 在线库表 + 两个下载按钮 + 最新版本卡片）。
- `GET /skill.zip`：**现场生成**客户端 skill 包，里面 `scripts/config.json` 写好了本中心地址——同事解压到 skills 目录即可用，无需配置；`query.py` 读配置的顺序是「环境变量 > 用户目录 > 包内」。
- `GET /download`：流式发库软件 zip（不整包读内存）；没放包时返回 404 + 中文提示，说明页上也显示"暂未放好"而不是死链。
- `GET /download/<版本>`：指定版本下载，支持 `Range: bytes=N-` 断点续传，带 `X-Landoc-Sha256` 头；只放行最新版，老版本返回 404（提示已下架）。
- 升级提示：`/libraries` 带 `latest_version`（以 `latest.json` 为准），每个库带自己的 `version`；`query.py libs` 会把落后的库列一行（"N 个库版本落后，最新 0.2.0：赵六、老王"）。

## 自动更新（同事怎么升级）

从 v0.2.0 起，**同事不用再收 zip、不用手替换目录**：软件自己发现新版、自己下载，点一下「升级」就换好，启动失败还会自动回滚。

**发布方（你）一条命令：**

```bash
# 改好版本号（landoc/__init__.py 与 pyproject.toml），然后：
python packaging/release.py --notes "修了检索偶发 500" --notes "新增 xx"
# 做了代码改动就会先跑 PyInstaller（3-5 分钟），再压 zip、算 sha256、写 latest.json
# 只改说明、不想重新打包：加 --skip-build
# 发布成功后顺手删掉更早版本的 zip（只留这一版 + LanDocShare.zip）：加 --prune-old
# 让新同事建库就自动登记到你的中心：加 --registry-url http://<中心地址>:8767
#   （地址写进包里的 landoc/site_defaults.json 与 distribution/site_defaults.json；
#    源码里不写内网地址，所以只有打包时能注入）
```

产物在 `distribution/`：`LanDocShare-<版本>.zip`（版本化包）、`LanDocShare.zip`（给第一次装的人）、`latest.json`（版本真源）。
若中心与打包机不是同一台，把 `distribution/` 整个同步过去即可。**中心不用重启**——它按文件修改时间读 `latest.json`，下一个请求就生效。发布脚本拒绝降级和同版本覆盖（确实要覆盖加 `--force`）。

中心那边只要**升一次新版程序**（0.2.0 之前的中心进程没有 `/api/update` 与断点续传，一次就够），之后发布新版本都不用再动它。打包版中心启动时记得指到分发目录，否则它找不到包和清单：

```bash
LanDocShare.exe registry --port 8767 --package-zip D:\share\LanDocShare.zip   # latest.json 自动取同目录的
```

**同事侧（全自动，只有最后一步点一下）：**

1. 启动 25 秒后检查一次，之后每 6 小时一次；中心心跳回包说有新版时立刻再查一次（最长 1 分钟发现，半小时最多一次）。有新版自动下载（局域网千兆 1-2 秒，断网可续传，不会被算成"正在建库"）。
2. 就绪后托盘菜单变成 **「升级到 v0.3.0（重启程序）」**，状态窗口版本行显示 `v0.2.0 → v0.3.0（已下载，可升级）`，并弹一条气泡提示。
3. 点它 → 关服务、切换目录、重启新版，约 3 秒。资料库数据在独立目录（向导里选定的库位置），**程序升级不动数据**。

**失败怎么办：**

- 新版 25 秒内没起来（没写出健康标记），更新器自动回滚到旧版并把旧版重新启动，日志在 `%LOCALAPPDATA%\LanDocShare\updates\logs\update.log`。
- 程序装在 `Program Files` 这类不可写目录、或版本跨度超过 `min_from` 时，只提示"请手动下载重装"，不会硬来。
- 命令行也行：`LanDocShare.exe update`（查+下）、`update --check`（只查）、`update --install`（装，需先退出托盘）。

**注意**：v0.1.0 及更早的版本里没有更新器，**那批机器要手工装一次 v0.2.0**（先从托盘退出，再解压 zip 覆盖程序目录；库数据在 `lib_dir`，不受影响）；从 v0.2.0 往后才能真正"一次装好、以后自动升"。

## 授权名单（客户端界面，按 IP 认人）

「谁能查我」不再在建库向导里编辑名单：托盘菜单 **授权名单…** 或状态窗口的 **授权名单** 按钮，打开一个表格窗口，增删改都在里面完成，保存即生效（服务不用重启）。

- **按 IP 放行，姓名只作备注**：来源 IP 命中名单 → 放行，审计日志里记那条的姓名；姓名不能替代 IP，报错姓名反而会被 401。
- **空 IP 条目一律不授权**（知道姓名也进不来）；`127.0.0.1` 永远放行，本机调试不会被自己挡住。
- **部门 / 备注列**（`Config.people_departments`，`{姓名: 部门}`）：只用于分组查看，不参与授权。表格按部门聚在一起、部门内按 IP 排序，没填部门的沉底；部门之间的先后跟名单里第一次出现的顺序走（也就是导入 CSV 的行序），想调顺序就调 CSV 里的行序。窗口里能直接给某人填"规划一所""水文所"这类简称，也可在 CSV 的第三列带上。状态窗口和命令行会显示"6 个部门"这种统计。
- 界面里还能 **导入 CSV / 导出 CSV…**（列：`姓名,IP,部门`，部门可省），双击一行就能改；名单人数会实时反映到状态窗口的"共享范围"。
- **从中心拉取… / 发布到中心**：库主人把全部门名单发布到登记中心（`POST /roster`，**只接受中心所在那台机器**，同事改不了全院名单），其他库在窗口里点"从中心拉取…"就能拿到，选合并（同名以中心为准）或整体替换。中心把它存成 `<state-dir>/roster.csv`，说明页会显示"部门授权名单已发布：37 人：规划一所 12、水文所 8…"。**名单默认只在中心本机可下载**：各库的「从中心拉取…」需要管理员启动中心时加 `--roster-public` 才放行（等于把全院姓名+IP 暴露给中心允许网段，慎用）；不加时同事的拉取会提示"中心默认只允许本机下载名单"。`「发布到中心」按钮只在中心那台机器上出现`（按"本机地址是不是中心地址"判，`roster.is_center_machine()`）：同事的窗口里没有这个按钮，只留着"从中心拉取…"和一句"名单由管理员在中心那台机器上统一维护"，不会有人顺手把自己那份发上去覆盖全院名单。
- **中心本机的网页编辑页**（`http://127.0.0.1:8767/roster/edit`）：不想开客户端窗口时，直接在中心那台机器的浏览器里改——逐行填姓名/IP/部门（`+ 添加一行`、`✕` 删人），或展开「从 CSV 粘贴导入」整段粘贴（勾「整体替换」就覆盖，不勾按姓名合并），点「保存并发布」写回同一个 `roster.csv`，各库下次拉取即生效。保存后 **303 回页面**（刷新不会重复提交）并显示"已保存：N 人"；有错（IP 写错、重名、一行不剩）就红字提示并保留你填过的内容。说明页「四、部门授权名单」卡片上，**只有中心这台机器**的浏览器才会多出「编辑名单」链接，同事打开同一地址只会看到一页 403 说明（不泄露任何名单内容）。判据与 `POST /roster` 一致：来源 IP 在 `roster_admin_ips` 里（默认本机全部地址），不做任何"看起来像本机"的猜测。窄屏（手机/分屏）下表格会自动变成一人一张卡片，IP 不会被截断。

命令行等价物（脚本/批量用）：

```bash
python -m landoc.cli people --lib-dir D:\MyLib --pull                # 从 config 里的 registry_url 拉取
python -m landoc.cli people --lib-dir D:\MyLib --publish             # 发布到中心（同样只认中心那台机器）
python -m landoc.cli people --lib-dir D:\MyLib --dept 赵六=规划一所    # 给某人标部门（部门留空 = 清掉）
python -m landoc.cli people --lib-dir D:\MyLib                       # 查看（按部门分组、末尾给部门分布）
```

## 可访问的库（托盘里直接看）

不用开网页、也不用装 skill：托盘菜单 **可访问的资料库…**（或状态窗口第二行的 **可访问的库** 按钮）会拉一次登记中心的名单，列出**对你的来源 IP 开放**的库（姓名只用于核对，不能替代 IP）。

- 表格列：库主人 / 库名 / 篇数 / 状态 / 上次心跳 / 地址；在线的排前面，自己的库标"（本机）"，双击一行用浏览器打开对方的库主页。
- **姓名可以留空**：授权只按来源 IP，姓名只用于审计/同名匹配，**不能当口令**。姓名存在 `%LOCALAPPDATA%\LanDocShare\viewer_name.txt`（没填过会自动借用 skill 的 `identity.txt`）——**授权仍在中心/库那一侧算**，客户端不做任何本地放行。
- 中心连不上时退回 24 小时缓存，并对缓存里标"在线"的库逐个 `/api/health` 探活；缓存里记着查询用的姓名和中心地址——换了姓名或换了中心，缓存就作废，不会拿到别人的库单。
- 窗口开着时每 60 秒静默刷新一次，也能点 **刷新** 立刻重查；查询在后台线程跑，中心慢不会卡界面。
- 中心返回 403（比如本机用 `127.0.0.1` 访问、而中心只允许 `172.16.0.0/16`）会直接给出提示，换成局域网地址即可。
- 一个库都没有时窗口会说明可能的原因：不在对方授权名单里 / 对方暂停了共享 / 对方版本较旧、还没上报授权范围。
- **离线清单（自动）**：每次成功从中心查到名单，都会顺手把同一份名单写成本地网页 `%LOCALAPPDATA%\LanDocShare\库地址清单.html`——不用记得点任何按钮，中心哪天关了，这份文件就是手上的地址簿。按钮 **导出清单…** 只是把这份清单**另存一份**（比如存到共享盘发给同事，默认目录和文件名就是上面那个）。网页里有导出时间、来源（实时/缓存）、查询身份、在线数，以及库主人 / 库名 / 篇数 / 状态 / 地址表和一段纯文本地址；已离线的库整行置灰。**这是给中心掉线时兜底用的离线备份**：中心恢复后会自动更新；对方换了端口或暂停共享后地址就打不开了。库名、姓名等来自中心的数据都做了 HTML 转义，`javascript:` 之类的地址不会变成可点链接。

## 客户端 skill（`skill/lan-lib-search/`）

同事不用装库软件，拿到这个 skill 就能"全院一起查"：

```bash
python skill/lan-lib-search/scripts/query.py libs                 # 谁在线、各多少篇
python skill/lan-lib-search/scripts/query.py search "太湖水库 特征库容"   # 多词要加引号
python skill/lan-lib-search/scripts/query.py search 玉田水库 --lib 赵六
python skill/lan-lib-search/scripts/query.py tables --lib 赵六 --path "测试目录/xxx.docx.md"
```

- **只用标准库**，Python 3.8+ 直接跑；没有 Python 时按 `SKILL.md` 里的 curl 命令逐库查、AI 合并。
- 用法固定成**两步**（一次 `search` + 最多一次 `tables`）：实测检索 20 ms、取表 23 ms，慢的从来不是库，
  是多跑几轮命令。`libs`/`whoami` 只在排障时用。
- 合并规则：各库 score 先归一到本库最佳，再乘库级权重（`config.json` 的 `weights`），同库保持原序，取前 20。
- **单库超时按库自适应**：默认 1.2 s，连续超时的库减半到 0.6 s（每 10 分钟探一次），历史慢库放宽到 2 s；
  延迟记在 `%LOCALAPPDATA%\lan-lib-search\latency.json`。30 库（29 快 + 1 个睡着）实测 2023 ms → 625 ms。
- **跨库去重**：标题、类型、文件名三样都相同才算同一份文档，只留排名最高的一条，其余折成「共 N 处」。
- 中心连不上时用 24 h 缓存 + **并发**逐库 `/api/health` 探活（32 路，20 个库 20 秒 → 2 秒）；某个库没响应只记脚注"在线 X/Y 个库返回结果"。
- 输出按库分组，每条都带**库主人 + 相对路径 + 命中片段**；只给片段和表格，不给原文、不给下载。
- **仓库是唯一源**：改这个 skill 只改 `skill/lan-lib-search/`，然后
  `python packaging/sync-skill.py --install` 同步到本机 `~\.agents\skills\lan-lib-search`；
  `--check` 只对比不写（版本不一致时退出码非 0），`--pull` 反向把本机那份收回仓库。
  `/skill.zip` 是从这里现场打包的，所以同事拿到的永远是仓库这一版。


## 关键机制（都来自实测，别按直觉改）

| 现象 | 机制 | 实现位置 |
|---|---|---|
| 加密 docx 用 python 读会 `BadZipFile` | 透明加密驱动只对白名单进程（winword/wps/acrobat/acad…）透明解密 | `probe.classify` / `office_bridge` |
| Office 另存为的中间文件读不出来 | **加密与否取决于"谁写"**：白名单进程写盘一律是密文 | 故一律"Office 内存取字符串 → python 落盘" |
| 加密 docx 如何转 md | `doc.Content.WordOpenXML` 取 Flat OPC → python 补 `[Content_Types].xml` 重建 docx → `python-docx` 解析 | `office_bridge` |
| 加密 pdf 如何转文本 | **本机有 Acrobat 就用它**（`PDDoc.Open` + 逐页 `CreateTextSelect`/`GetText`，21 页 0.8 秒、不落明文副本）；否则用伪装名 `acrobat.exe` 解**临时副本**，抽完即删。工具目录查找顺序：`init --acrobat-tool-dir` → 环境变量 `LANDOC_ACROBAT_TOOL_DIR` → exe 同级的 `工具\`（打包版，同事机器用这个）→ 仓库根 `工具\` 与开发路径（仅源码运行时） | `pdf_conv` / `cli.acrobat_tool_candidates` |
| 扫描件 pdf 为什么也能转 | 取不到文字层时先试 **Windows 系统 OCR**（WinRT，经 `landoc/tools/ocr_win.ps1` 走 PS 5.1，不装任何 Python 包），结果太短再退回**随包 Tesseract**（最小 DLL 集 8.4 MB，不需要 ICU；fast 模型）。只认"页里有图"的 PDF（无图的短文本不渲染），默认 300 dpi、前 60 页，结果带页码锚点与提示行。两条路都没有 → `no_ocr` | `ocr.py` / `pdf_conv._ocr_text` |
| OCR 装没装怎么记 | 环境指纹多了 `ocr` 一项；`no_ocr` 属环境原因（装好就放回队列），且 OCR 刚变可用时连旧的 `no_text_layer` 一起放回——否则 0.2.15 之前攒下的扫描件失败永远不会重试 | `pipeline.env_signature` / `_drop_stale_env_failures` |
| Acrobat 取文本为什么要拼接 | `GetText(i)` 返回的是**单个字符**，直接 join 会得到逐字断行的碎片，trigram 命中率归零；`_join_tokens` 做中文不加空格/ASCII 补空格 | `pdf_conv._join_tokens` |
| 一批文档为什么共用一个 Office | 每篇重启 Office 要白付 6 秒；`OfficeSession` 把整批固定在同一 COM 线程 | `office_bridge` |
| 一批加密 pdf 为什么也共用一个 Acrobat | 同上但更贵：每篇 `Dispatch`+杀进程要白付 **5.5 秒（占 6.2 秒的 88%）**；`PDFSession` 用一次 `Dispatch` 连转一批、每篇只新建 `PDDoc`（复用 PDDoc 无收益），每 500 篇换新会话（内存约 +0.44 MB/篇） | `pdf_conv.PDFSession` |
| `.doc/.ppt/.dps/.wps` 为什么也走 COM | 这些是 OLE 容器，python-docx/python-pptx 不认；判据是"是否加密 或 不是 zip" | `pipeline.convert_one` |
| 失败的文件会不会每轮都重试 | **同一版本**（mtime+size 都没变）连续失败 1 次就不再碰；版本变了或累计到 `MAX_ATTEMPTS=3` 次也停，记在 `failures.json` 等人工看 | `pipeline.run` / `MAX_ATTEMPTS` |
| 装了 Office / Acrobat 之后老失败会不会自愈 | run() 开头探一次环境指纹（`office`/`acrobat`）存进 `state.json`，指纹一变就清掉 `no_office`/`no_reader` 这类**环境性**失败记录（`timeout` 等文件自身的失败保留） | `pipeline.env_signature` / `_drop_stale_env_failures` |
| 中文检索为什么用 trigram | jieba 分词边界对不上就零命中（查"马斯京根"vs 索引里"马斯京根法"）；trigram 做子串匹配 | `index.build_match_query` |
| 正文为什么只存一份 | `documents_fts` 是 external content 表（`content='documents'`），自己只存倒排、正文按 rowid 从 `documents` 读；三个触发器跟着 INSERT/UPDATE/DELETE 同步（`ON CONFLICT DO UPDATE` 也会触发 UPDATE 触发器）。旧库打开时自动 DROP 旧表 → `rebuild` → `VACUUM`（100 篇约 0.2 秒） | `index.SCHEMA` / `Index._migrate_fts` |
| 2 字查询（"乐平""洪水"） | 低于 trigram 最小长度，改走 `instr` 子串扫描；结果按 (查询串, 目录, 条数, 可见性) 缓存最近 64 个（`SHORT_QUERY_CACHE`），**upsert/delete/mark_withdrawn 一到就整体失效**——写路径漏一处，同事就会搜到已撤回/已删的旧结果 | `index._like_search` / `Index._cached_search` |
| 想"只共享片段、不落盘" | `--no-store-content` 只把前 `snippet_only_chars` 字（默认 20000，可调）写进索引（超出部分搜不到是刻意的）；`--no-render-md` 不落 md 副本、清掉旧副本 | `pipeline.run` / `Config` |
| 上千篇的批次怎么划 | 一轮最多 **attempt** `batch`（默认 100）篇（失败/超时也占额度）、最长 `round_seconds`（默认 600 秒）；每篇开始前问一次「用户回来了 / 暂停 / 退出」，是就收手，剩下的留给下个空闲窗口 | `app._run_round` / `pipeline.run(budget=, should_stop=)` |
| 点「立即重建索引」为什么以前没反应 | 它只 `_wake.set()`，worker 醒来仍要过 `_idle()`，正在用电脑时连一篇都转不了。现在置 `_force`：本轮门控与每篇的收工检查都放行，且**连着跑到没有新增为止**；`_abort` 是「停止建库」，`pause_share()` 也会把请求撤掉 | `app.update_now` / `app._drain` / `app._should_stop_round` |
| 建库中途断电/被关掉会白转吗 | 索引每 20 篇 `commit` 一次、`state.json` 每 60 秒整份重写一次，重启从下一篇继续（原来是整轮跑完才在 `finally` 写一次，CLI 全量 = 整库单事务） | `pipeline.COMMIT_EVERY` / `STATE_SECONDS` |
| 删掉/移走源文档，库里的副本会不会跟着没 | **不会**：库只增不减。源文件消失（删了、改名、移走、共享盘没插）只是记一笔 `source_missing`，md 与索引行都留着、照常可检索，状态窗口报「M 篇源文件已不在（内容仍在库中）」；源文档回来后记号自动撤掉。**把整个文件夹从 `config.folders` 移掉**也不再删 md/索引：文档标记 `withdrawn`，库主人（本机/本机网卡 IP）仍可检索，其他人看不到；文件夹加回后自动恢复（检索页会给这些行标一枚「已撤回共享」标签、状态窗也报「撤回共享 N 篇」）。想瘦身用 `landoc prune --gone`（只清源文件已不在的，撤回文档不会被误删；顺带清掉这些文档在 `state.json`/`failures.json` 里的失败记录，状态窗的"失败 N 篇"不会留着死条目）。早先是"这一轮没扫到就删"，拔了移动硬盘启动托盘会把整个库清空 | `pipeline.run` / `pipeline.prune_gone` / `pipeline._configured_tops` / `index.mark_withdrawn` |
| 状态窗口怎么让人信"它没死" | 首行 `建库中：已转换 300 篇 · 还剩 700 篇（约 1 小时 2 分）`，另有「待收录」一行；ETA 用实测秒/篇的滑动平均（`rate_s_per_doc`），不因某篇超时而跳 | `app.AppStatus.summary` / `panel.phase_text` |
| 保存建库设置时为什么闪一片黑框 | `office_bridge` 每篇转换前后都要跑 `tasklist`/`taskkill` 记基线、收尾——这两个是控制台程序，`subprocess.run` 不隐藏窗口就会每调一次闪一个 cmd 窗口（一篇 3~4 次，看着像程序在抽风）。两处都带 `creationflags=CREATE_NO_WINDOW` | `office_bridge._pids_of` / `_kill_office_processes` |
| 出问题去哪看 | 状态窗口只有"最后一句话"，细节在 `<库目录>\logs\app.log`（超 1 MB 滚成 `.1`）：启动、扫描失败、登记中心恢复，以及**每轮一行摘要**（收录/失败/尝试/剩余/用时/停止原因，空转轮不写） | `app.log_event` / `app._drain` |
| 网段放行判定为什么只有一份 | `netinfo.cidr_allowed()` 同时给库端服务与登记中心用：空清单 = 暂停共享 = 拒绝所有来源（绝不反向变成全网放行），写错的网段跳过不致命 | `netinfo.cidr_allowed` / `server._ip_allowed` / `registry._cidr_allowed` |
| 换网卡后管理员会不会被自己的中心挡成 403 | 不会：本机地址 60 秒缓存一次（`netinfo.cached_local_ips`，换网/插网线可 `clear_local_ips_cache()` 立即重探），没显式配 `roster_admin_ips` 时"谁算本机"随网络走 | `netinfo.cached_local_ips` / `registry._current_local_ips` |
| 每个请求都判"是不是库主人"，会不会每次都 getaddrinfo | 不会：`_is_owner` 也走 `netinfo.cached_local_ips()`，与登记中心共用同一份 60 秒缓存（以前服务端每请求现查，多网卡机器上慢得可感知） | `server._is_owner` |
| 检索页为什么能一条连接发几个请求 | 库端与中心都设 `protocol_version="HTTP/1.1"`，每个响应都带 `Content-Length`；空闲连接 60 秒超时回收（`Handler.timeout`），免得长连接把线程全占住。**别把 `_send` 里的 `Content-Length` 删掉**——删了就退回一次一连接 | `server.Handler` / `registry.RegistryHandler` |
| 多网卡/VPN 机器不想让某条链路改名单 | 在 `registry.json` 的 config 里显式写 `roster_admin_ips`（支持单 IP 或 CIDR），默认清单就不再自动取全部网卡地址 | `registry._roster_admin_ips` |
| 配置文件将来改了字段含义怎么办 | `config.json` 带 `config_version`（现为 1）；`from_dict` 只按 dataclass 字段加载、未知键静默忽略，新旧版本互读都不炸 | `config.CONFIG_VERSION` |
| `landoc registry` 的参数为什么不再列举 | 参数**原样透传**给登记中心自己的 argparse（`landoc registry --help` 就是中心的帮助）；以前逐条搬 13 个参数，中心加参数就得两边同步，`--roster` 漏过一次 | `cli.main` |
| 打包产物越堆越多 | 发布时加 `--prune-old`（只留最新版 zip + 固定下载名 `LanDocShare.zip`）；事后单独清：`python packaging/release.py --prune`（只打印）／`--prune --apply`（真删），同样只留最新版、当前说明与 `dist/LanDocShare`；**清单缺失时一个 zip 都不删**。本机已实际清理 9 项 / 951.3 MB（dist 579→174 MB、distribution 727→182 MB，仓库 1.44 GB→492 MB） | `packaging/release.py` |

## 实测基准（本机，Python 3.14 / WPS + Office 2019）

三份真实加密样本（一份 880 KB docx、一份 2.3 MB pptx、一份 389 KB pdf）：

- 端到端 `init → convert → 索引 → 检索`：**3 篇（docx+pptx+pdf）/ 27 秒**（三种格式各起一次 Office 实例，热路径 ~1.9 秒/篇）
- 同一类批量（5 份加密 docx）：**15.4 秒** = Office 启动 6 秒 + 约 1.9 秒/篇（一批共用一个 Office 会话；若不共用，每篇要重付 6 秒）
- 加密 docx：31,545 字正文 + 13 张表格，Heading 1/2/3 完整
- 加密 pptx：逐页形状文本 + 表格
- 加密 pdf：Acrobat 路线 **5.1 秒（含进程启动）/ 21 页 8,456 字**，文本干净、无需 OCR、不留明文副本；服务端 `水安全保障`/`十五五` 均命中并高亮
- 同一份加密 pdf 复制 100 份（`PDFSession` 一批共用一个 Acrobat）：**100.2 秒 / 100 篇 ≈ 1.0 秒/篇**（含落 md + 写索引）；纯取文本 0.73 秒/篇，启动 4.45 秒只付一次 → **比每篇重起 Acrobat（6.2 秒/篇）快 6 倍**
- 上千篇的估算因此从「1000 篇 ≈ 103 分钟」降到 **≈ 13 分钟**（2000 篇 3.4 小时 → 25 分钟）
- 一轮的边界（40 份加密 pdf 副本上实测）：`limit=10` → 只尝试 10 篇（15.0 秒，含首次 Acrobat 启动），`budget=3` → 转完手头那篇就停，`should_stop` 第 6 次为真 → 停在这一篇之后；第一轮结束 `state.json` 已记 10 篇（改造前只在整轮 `finally` 写一次）。索引 20 篇一提交比整轮单事务只慢约 15%，单事务时 WAL 峰值会涨到库大小且建库途中搜不到新内容
- 索引体积（正文只存一份之后）：100 份真实加密 pdf（每篇 8,456 字）**9.6 → 7.0 MB，省 27%**（8.3 字节/字）；合成 2 万字/篇的 1000 篇 **139.5 → 80.1 MB，省 43%**（4.0 字节/字）。写入速度两者都在 ~2 毫秒/篇，相对 1 秒/篇的转换可以忽略。老库第一次打开会自动迁移 + VACUUM（100 篇 0.21 秒）
- 速率的算法（ETA 的地基）：只数"真在转"的时间，且**每轮第一篇不计**——那一篇替整轮付了 Office/Acrobat 的 4.5~9 秒启动。20 份加密 pdf 的库上 `limit=8` 两轮：墙钟 17.5 秒 / 14.8 秒，但速率都稳定在 **0.74 秒/篇**（含启动的旧口径是 2.19 / 1.84 秒/篇，把剩余的 ETA 夸大成 26.3 秒 / 7.4 秒，实际 9.0 秒 / 3.0 秒）
- 规划阶段（扫目录 + stat）的开销：7,427 篇的目录（`G:\2025年工作`）热态 `_iter_docs` **0.37 秒** + stat **0.28 秒** = 0.65 秒；stat 分给 4 条线程只省 0.06 秒（1.27×），所以没有为它做"目录 mtime 缓存"——省下的那点时间买不回漏收新文件的风险

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
  pdf_conv.py      pdf → 文本（pymupdf / Acrobat / 兜底临时副本 / 扫描件 OCR）
  ocr.py           扫描件 OCR：Windows 系统 OCR（PS 5.1 + WinRT）优先，随包 Tesseract 兜底
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
  netinfo.py       内网地址探测 + 网段判定（CLI / 服务 / 登记中心共用同一份）
  registry.py      登记中心（8767）：心跳登记 + 在线清单（TTL 5/30 分钟）+ 分发入口 + 名单发布 + `--stop` 优雅停止
  watchdog.py      中心看门狗（`--watchdog`）：子进程退避重启、`/health` 卡死判定、日志
  roster.py        授权名单：本地增删/排序、与中心之间的发布与拉取
  roster_ui.py     授权名单窗口（tkinter，增删改 / 导入导出 / 与中心同步）
  discover.py      可访问库清单：拉中心 /libraries、24h 按身份缓存、逐库探活、导出离线地址清单
  discover_ui.py   可访问库窗口（tkinter，双击开主页 / 复制地址 / 导出清单）
  updater.py       自动更新：检查 / 下载续传 / 校验解压 / 换目录 / 健康检查与回滚
  tray.py          托盘入口 + 单实例互斥量（也是打包入口）
  tools/           随包 OCR 工具：ocr_win.ps1 + tesseract 完整 DLL 闭包 + tessdata fast 模型（共 31.2 MB）

packaging/
  LanDocShare.spec PyInstaller onedir 打包配置（排掉 torch/cv2 等重包）
  release.py       发布新版本：PyInstaller → 版本化 zip → sha256 → latest.json；`--prune-old` 发布时清旧包，`--prune [--apply]` 事后清

skill/lan-lib-search/
  SKILL.md             给 AI 助手的用法说明（触发词、流程、无 Python 时的 curl 降级）
  scripts/query.py     客户端查询：拉名单 → 并发 fan-out → 归一化合并 → 按库分组输出
```

## 许可证

MIT，见 [LICENSE](LICENSE)。随包分发的 Tesseract 与 tessdata 模型另按 Apache-2.0 授权，
来源与许可见 [landoc/tools/README.md](landoc/tools/README.md)。
