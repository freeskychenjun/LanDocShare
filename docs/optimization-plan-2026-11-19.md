# LanDocShare 优化方案 · 第二轮（2026-11-19）

> 评审范围：0.2.7 → 0.2.13 的全部增量（约 5000 行：deny_cidrs、库只增不减、
> PDFSession、调度器重写、Tk 单解释器、向导重写），加上一轮未读过的模块
> （wizard / roster_ui / discover_ui / roster / people / pdf_conv / query.py）。
> 测试基线：`482 passed, 9 skipped, 165s`（上轮 357）。

## 〇、上一轮（2026-11-18）方案的落实情况

逐条核对 0.2.8–0.2.13 的提交：**P0/P1/P2 全部落地，且多处做得比建议更彻底**——

| 上轮条目 | 落实情况 |
|---|---|
| 1.1 重试上限判断写反 | ✅ `555de34`，含回归注释 |
| 1.2 环境自愈 | ✅ 同上，做成 `env_signature()` 指纹存 state |
| 1.3/1.4 中心写盘/名单双解析 | ✅ `3d0dbfe`（prune 有删除才落盘 + roster mtime 缓存） |
| 2.1 FTS external content | ✅ 超出建议：`_migrate_fts` + 触发器 + `rebuild` + `VACUUM` 一步到位 |
| 2.3 中心限流 | ✅ `7dec0a0`（抽 `ratelimit.py`，120/min） |
| 2.4 审计轮转 | ✅ 4 MB 滚 `.1` |
| 2.6 skill.zip 缓存 | ✅ 模板签名缓存 |
| 4.1/4.2/4.3/4.4 jieba/pytest/私人路径/参数透传 | ✅ `5e74a7c`、`fd83103` |
| 4.5 发布清理 | ✅ `release.py --prune [--apply]`（实测清出 951 MB） |
| 4.6 app.log | ✅ `c75d6e4`，含轮转与心跳恢复日志 |
| 5.1/5.2 最近收录 + loading | ✅ `6204485`（`/api/recent` + 前端） |
| 3.5 config 版本号 | ✅ `CONFIG_VERSION = 1` |
| 未做（顺延到本轮） | 2.2 目录级增量扫描、2.5 两字查询缓存、5.3 xlsx |

新增的超预期亮点（值得点名，避免将来被"优化"掉）：**PDFSession 复用把加密 PDF
从 6.2 秒/篇压到 0.73 秒/篇（8.5×）**；并行 stat（7468 篇 471→220ms）；调度器
改成「尝试篇数 + 600 秒预算 + 每篇收工检查」，带 EWMA 速率与 ETA；
`HostedWindow` 把全部窗口挂到一条 Tk 线程（修 tcl86t.dll 崩溃）；名单默认仅中心
本机可下载（`--roster-public` 才放开）。这些都是对的，别动。

本轮建议因此变少、也更聚焦：**两个打包版功能缺口（P0）、三处新旧行为脱节（P1）、
两条性能顺延项（P2）**。

---

## 一、P0：打包版用户的功能缺口

### 1.1 `prune` 进不了打包 exe，而它是"库只增不减"的唯一出口

- CLI 已有 `landoc prune --gone`（`cli.py:529-534`，README 也这么教），但托盘入口
  `tray.py:359-361` 的转发白名单没收录它：

  ```python
  CLI_COMMANDS = frozenset(
      {"init", "probe", "convert", "serve", "status", "people", "registry", "update"}
  )
  ```

  打包版里 `LanDocShare.exe prune --lib-dir X --gone` 会掉进托盘流程，被
  argparse 当成不认识的参数拒掉。同事拿到的是 exe，不是源码——**瘦身命令对
  全部打包用户不可达**。
- 这不只是"少了条命令"：「库只增不减」的新语义下（`pipeline.py` 注释与 README
  §250 行），**误共享的文档删掉源文件后仍对全院可检索**，唯一收回手段就是
  `prune --gone`。出口堵死 = 隐私兜底失效。

建议（两步，合一次发版）：
1. `CLI_COMMANDS` 加 `"prune"`（一词改动）+ 一条测试：`tray.main(["prune", …])`
   确认路由到 cli；
2. 给 GUI 一个入口：状态窗口已有「M 篇源已不在」的行（`AppStatus.gone`），
   在它旁边加「清理…」按钮（`messagebox.askyesno` 确认后调
   `pipeline.prune_gone(cfg)`），比让同事敲命令行现实得多。

### 1.2 `_is_owner` 每个请求做一次 getaddrinfo（`server.py:225-227`）

```python
def _is_owner(self, ip: str) -> bool:
    return ip in local_ips()      # netinfo.local_ips() 无缓存
```

`_dispatch` 的**每个** API 请求都走这里（`include_withdrawn=owner` 决定可见性）。
`local_ips()` 里是 `socket.getaddrinfo(gethostname(), None)`——内网机器 DNS/主机名
配置不干净时（很常见），一次解析可以挂几百毫秒到几秒，**每个检索请求都被拖住**，
且完全看不出原因。中心侧同一个问题已经在 `3d0dbfe` 修过
（`registry._current_local_ips()`，60 秒 TTL），库端漏掉了对称的这份。

建议：把 TTL 缓存下沉到 `netinfo`（`cached_local_ips(ttl=60.0)`），
`server._is_owner` 与 `registry._current_local_ips` 共用一份实现，测试照
`tests/test_netinfo.py` 的样式补缓存命中/过期两条。

---

## 二、P1：新旧行为脱节（都是 0.2.10「姓名不再授权」的余波）

### 2.1 检索页 401 流程还在教用户"填姓名"（`web/index.html:118-131`）

`ip not registered` 时页面提示"可以试试点『换个人』后填姓名"，其余 401 走
`prompt` 问姓名后**重试**。但新语义下（`server.authorize_person`，`server.py:132+`）
姓名不授权：不在名单 IP 里的人填什么名字都是 401，prompt→重试 是个死循环。
建议：401 一律改为"这台电脑的 IP 不在授权名单里，请联系库主人登记 IP"，
删掉重试分支；页脚「换个名字」保留（只改审计署名，文案已写对）。

### 2.2 `deny_cidrs` 后端全链路、前端零入口

grep 证实 `deny_cidrs` 只出现在 cli.py / server.py / registry.py / app.py——
向导没有输入框、名单窗口没有列、状态窗口"共享范围"不显示。而向导的提示文案
（`wizard.py` `sync_scope_state`）已经写着"网段内、**且不在拒绝网段里**的机器都能查"，
功能看得见摸不着。建议：
- 向导「允许的网段」下一行加可选的「拒绝网段」输入（`parse_cidrs` 现成可复用，
  空即无），`build_config` / `apply_config` 带上该字段（`apply_config` 现在不碰
  deny_cidrs，改建库设置时也不会冲掉，这两点都要保住）；
- `panel.status_rows` 的「共享范围」补一句"· 拒绝 N 段"；
- 心跳已上报、中心已判定，无需动 registry。

### 2.3 `prune_gone` 不清 `failures.json` 的对应条目（`pipeline.py:702-735`）

`prune_gone` 只 `files.pop(rel)`，`state["failures"]` 里同名条目留着（文档都删了，
失败记录成了死数据，`failures.json` 会随年月越长越多）。顺手在循环里
`failures.pop(rel, None)`；state 整份已在 `_write_state` 落盘，无额外成本。

---

## 三、P2：性能（上轮顺延项，仍然成立）

### 3.1 目录级增量扫描（`pipeline._iter_docs`）

并行 stat 只覆盖了规划阶段的 stat 部分（471→220ms），**walk + 排序本身仍每轮全量**：
空闲调度里每 30 秒一轮、手动「立即重建索引」一轮接一轮，目录树越大越吃亏
（G:\ 7468 篇只是 stat 就 220ms，walk 本身还要另算；十万级目录是秒级/轮）。
方案不变：`state.json` 记 `{目录: mtime}`，子目录 mtime 未变就复用缓存的文件清单，
顶层目录变了才重扫该子树，另加每 30 分钟一次全量校准兜底（防网络盘 mtime 不可靠）。
失误代价是"晚一轮收录"，可接受。

### 3.2 HTTP/1.1 keep-alive（`server.py` / `registry.py` 一行配置）

两个服务都跑在默认 `HTTP/1.0`：每个请求新建 TCP 连接 + 新线程，而 `Index.conn`
是 `threading.local` ——等于**每个请求新开一个 sqlite 连接**（WAL pragma、schema
检查），线程死后靠 GC 回收。检索页一次打开就是 3 个请求（meta/tree/recent），
skill 是 12 路并发。所有响应路径（`_send`/`_error`/`_send_file`/416 分支）都已带
`Content-Length`，把 `Handler.protocol_version = "HTTP/1.1"` 设上即可让浏览器与
skill 复用连接；线程数与 sqlite 连接数随之收敛到"并发客户端数"。
回归点：`_drain_request_body` 已处理 body 读取（不受影响）；需补一条
"同连接连续两个请求"的测试。`Index.__init__` 每连接跑一次 `executescript(SCHEMA)`
的成本也会随之摊薄。

### 3.3 两字查询 LRU 缓存（`index.search`）

水文高频词恰是两字词，每次都走 `_like_search` 全表 `instr` 扫描。建议在
`Index` 内加 `(q, dir, limit, include_withdrawn) → rows` 的小缓存（容量 64），
**写路径（`upsert` / `delete` / `mark_withdrawn`）必须整体失效**——尤其
`mark_withdrawn`，否则撤回共享后旧缓存还会把文档搜出来给同事。
这一失效语义是本条的全部风险所在，测试要覆盖"撤回后立即检索不可见"。

---

## 四、P3：小项（顺手清单）

| # | 事项 | 位置 | 说明 |
|---|---|---|---|
| 4.1 | `build_config(acrobat_tool_dir=...)` 传 `str`，`Config` 声明 `Path \| None` | `wizard.py:243` | 运行时靠 `Path(tool_dir)` 兜着；统一成 `Path` 消除隐患 |
| 4.2 | `validate_lib_dir` 校验时 `mkdir` 父目录 | `wizard.py:149` | 校验函数带副作用；改成保存时建目录，或至少注释声明 |
| 4.3 | 仓库根残留 `.release-notes-0.2.13.txt`（未跟踪） | `packaging/release.py --notes-file` | 临时文件应写进 `distribution/` 或用完即删；`.gitignore` 补 `.release-notes-*.txt` |
| 4.4 | `AppStatus` 缺"失败 N 篇"常驻字段 | `app.py` | `_drain` 里 `failed` 只用于一次性通知；状态窗口加一行只读计数，配合 1.1 的清理按钮正好成对 |
| 4.5 | xlsx/et 仍整类跳过 | `pipeline.SKIP_EXTS` | 上轮 5.3 顺延：明文走 openpyxl、加密走 Excel COM 取 UsedRange，可作 0.3 主打功能 |
| 4.6 | skill `/libraries` 的 `name` 参数只剩审计作用 | `query.py` | 无害；SKILL.md 措辞已部分更新，把"按姓名匹配授权"的说法再顺一遍即可 |

---

## 五、建议实施顺序

| 批次 | 内容 | 预估 |
|---|---|---|
| 第 1 批（合 0.2.14） | 1.1 CLI_COMMANDS + 清理按钮、1.2 `cached_local_ips`、2.1 401 文案、2.3 prune 清 failures | 各 ≤ 1h，全部带测试 |
| 第 2 批 | 2.2 deny_cidrs GUI、4.1/4.2/4.3/4.4 | 半天内 |
| 第 3 批 | 3.2 HTTP/1.1（两个服务）、3.3 检索缓存 | 各半天，回归面大要多跑测试 |
| 第 4 批 | 3.1 目录增量扫描 | 1-2 天，拿真实大目录实测"空转一轮"耗时对比 |
| 按产品节奏 | 4.5 xlsx | 0.3 主打 |

**这轮确认不要动的**（正确性已验证，改动风险大于收益）：FTS external content
迁移链（`_migrate_fts`/`rebuild`/`VACUUM`）；`PDFSession` 的 500 篇换会话；
调度器的 attempt-based limit + budget + `should_stop` 语义；`HostedWindow`
单解释器模型；`withdrawn` 在 search/tree/list/recent/tables 五个读路径的过滤
（本轮逐一核过，`tables()` 也过滤了，没有漏网）；roster 默认私享。

## 附：验证方式

- 基线 `python -m pytest tests -q`：482 passed（第 1/2 批改动后不得低于此数）；
- 1.1 的 exe 路径要用打包版真跑一次 `LanDocShare.exe prune --lib-dir … --gone`；
- 3.2 改 HTTP/1.1 后：浏览器开检索页（3 请求同连接）、skill 12 路并发、
  `/download` 断点续传（Range）三处手测；
- 3.3 的缓存必须带"撤回→立搜不可见""upsert→立搜可见"两条时序测试。

---

## 六、采纳情况（2026-11-19 落地，0.2.14）

| 条目 | 决定 | 落地位置 | 测试 |
|---|---|---|---|
| 1.1 prune 可达 + 清理按钮 | **采纳** | `tray.CLI_COMMANDS` 加 `prune`；`tray._prune_library` → `app.prune_gone()`；状态窗 actions 行「清理…」按 `status.gone` 亮起/写「清理 N 篇」，`messagebox` 确认后执行 | `test_tray.test_prune_is_a_reachable_cli_command` / `test_main_dispatches_prune_to_cli` / `test_panel_cleanup_button_calls_back_into_the_app` |
| 1.2 `cached_local_ips` | **采纳**（下沉到 netinfo，60 s TTL，与中心共用） | `netinfo.cached_local_ips` / `clear_local_ips_cache`；`server._is_owner`、`registry._current_local_ips` 都改用它，删掉 registry 自己那份缓存 | `test_netinfo` 3 例（复用/清缓存重探/ttl=0）、`test_registry` 换网用例 |
| 2.1 401 文案 | **采纳**：删掉"填姓名重试"，只留「IP 不在授权名单，请联系库主人登记 IP」；带名字被 401 时**自动丢掉名字按 IP 重试一次**（避免旧 localStorage 里的名字把已授权机器挡在门外） | `web/index.html` `api(path, params, retried)` | `test_server.test_page_sends_unregistered_visitors_to_the_owner_not_to_a_name_form` |
| 2.2 deny_cidrs GUI | **采纳** | 向导行 8「拒绝的网段」（留空=不拒绝，`parse_deny_cidrs` 空返回 `[]`，**不回落默认网段**）；`build_config(deny_cidrs=)`、`apply_config` 复制 deny；`status_rows` 共享范围追加「· 拒绝 N 段」 | `test_wizard` 5 例、`test_panel` 2 例 |
| 2.3 prune 清 failures | **采纳**（且同时重写 `failures.json` 镜像，否则死条目留在镜像里） | `pipeline._write_failures` + `prune_gone` 里 `failures.pop(rel)` | `test_prune` 双清 + 不动的文档保留 |
| 3.1 目录级增量扫描 | **不采纳** | 仓库 `README.md`「实测基准」已量过：7,427 篇的目录热态 `_iter_docs` 0.37 s + stat 0.28 s，为此做目录 mtime 缓存"省下的时间买不回漏收新文件的风险"；且按目录跳过会把"原地改文件"的收录推迟到 30 分钟校准 | — |
| 3.2 HTTP/1.1 | **采纳** | `server.Handler` / `registry.RegistryHandler` 设 `protocol_version="HTTP/1.1"` + `timeout=60`（空闲连接回收） | `test_server.test_two_requests_can_share_one_connection`、`test_registry.test_registry_keeps_a_connection_alive_for_heartbeats` |
| 3.3 两字查询 LRU(64) | **采纳** | `index.SHORT_QUERY_CACHE`；键 `(查询串, 目录, 条数, 可见性)`；`upsert/delete/mark_withdrawn` 整体失效；命中返回副本 | `test_index` 4 例（缓存命中/写后失效/可见性分键/副本不串改） |
| 4.1 acrobat_tool_dir 类型 | **采纳** | `build_config(acrobat_tool_dir: str \| Path \| None)` → `Path(...)` 或 `None` | `test_wizard.test_build_config_normalises_acrobat_tool_dir_to_path` |
| 4.2 validate_lib_dir 副作用 | **采纳**：校验只检查最近已存在祖先可写，建目录留到保存 | `wizard.validate_lib_dir` | 原用例改为断言 `not target.parent.exists()` |
| 4.3 `.release-notes-*.txt` | **采纳** | `.gitignore` 补一条（文件保留，只是不再污染 `git status`） | — |
| 4.4 失败篇数常驻 | **采纳** | `AppStatus.failed`；`_refresh_counts` 从 `state["failures"]` 数；状态窗文档行「N 篇 · M 篇转换失败」 | `test_panel.test_status_rows_surface_failed_conversions` |
| 4.5 xlsx/et | 顺延 0.3 | — | — |
| 4.6 skill `name` 仅审计 | 不改代码（无害）；措辞已在 README/授权名单段落写明"按 IP 认人、姓名只作日志" | — | — |

全量测试：**498 passed, 9 skipped**（基线 482，新增 16 例；`python -m pytest tests -q`，约 3 分钟）。

顺带发现并修正：替换 `test_prune.py` 时曾误删原用例 `test_prune_keeps_docs_whose_source_is_still_there`（已补回，`--gone` 不许动源文件还在的文档）。

