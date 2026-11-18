# LanDocShare 优化方案（2026-11-18）

> 基于对全部 24 个 `landoc/` 模块、打包脚本、skill 客户端与 357 个单元测试的通读
> （测试现状：`357 passed, 9 skipped, 125s`）。按优先级排列，每条附证据位置与建议修法。
> 总体判断：**架构成熟、边界处理扎实、测试覆盖真实场景**（透明加密 COM 链路、更新回滚、
> 看门狗、IP 授权口径一致性等都做对了），下面的优化不推翻任何既有设计，只在现状上做减法
> 与修补。

---

## 一、先做：正确性缺陷（P0，改动小、收益确定）

### 1.1 失败重试上限的判断写反了（`landoc/pipeline.py:267-275`）

```python
if (
    failed_before
    and failed_before.get("mtime") == stat.st_mtime
    and failed_before.get("size") == stat.st_size
    and failed_before.get("attempts", 0) < MAX_ATTEMPTS   # ← 反了
):
    report.skip("known_failure")
```

注释与常量名都说「连续失败超过 3 次就不再自动重试」，实际效果是：

- attempts < 3 且文件未变 → 跳过（符合测试 `test_known_failure_is_not_retried_every_run`）；
- **attempts ≥ 3 之后 → 条件恒假 → 每个空闲周期都重试**。带口令/损坏的文件在第 3 次
  修改后，每个空闲窗口重付一次最长 120 秒的 COM 超时，且 `attempts` 无限增长。

建议修法（语义：文件没变就跳过本轮；累计失败达到上限则永久放弃）：

```python
if failed_before and (
    failed_before.get("attempts", 0) >= MAX_ATTEMPTS
    or (
        failed_before.get("mtime") == stat.st_mtime
        and failed_before.get("size") == stat.st_size
    )
):
    report.skip("known_failure")
    continue
```

补两个测试：① attempts=3 且文件未变 → skip；② attempts=3 且文件已变 → 仍然 skip（放弃）。
工作量：半小时内（含测试）。

### 1.2 环境恢复后失败文件永远不会重试（`landoc/pipeline.py:308-318`）

`no_office` / `no_reader`（`pdf_conv` 兜底）类失败记录在 `failures.json` 里，只有文件
mtime 变化才会重试。同事机器后来装了 Office/Acrobat，旧失败**永远不会自愈**，只能手工
删 failures.json。建议：`pipeline.run` 开头探测一次 `probe.has_office()` /
`probe.has_acrobat()`（注册表查询，毫秒级），与上次运行记录的环境签名（存进 `state.json`）
不一致时，清掉 `reason in {"no_office", "no_reader"}` 的失败记录再扫。

### 1.3 登记中心每次读清单都写盘（`landoc/registry.py:414-419`）

`RegistryStore.libraries()` 里 `prune=True` 分支**无条件** `_save()`：每个
`GET /libraries`、`GET /health`、首页请求都会全量序列化 + 临时文件 + `os.replace`
（在锁内串行执行）。几十人 + discover 窗口每 60 秒刷新，写盘频率远超必要。

修法：`_prune_locked()` 返回实际删除的条目数，`libraries()` 仅在删除数 > 0 时落盘：

```python
def _prune_locked(self) -> int:
    now = time.time()
    stale = [k for k, e in self._entries.items() if now - e["last_seen"] > self.forget_ttl]
    for key in stale:
        self._entries.pop(key, None)
    return len(stale)
```

`register()` 里的 `_save()` 保留（心跳本来就该持久化）。

### 1.4 `/api/info` 一次请求解析名单两次（`landoc/registry.py:838-839`）

```python
"roster_available": self._roster_state() is not None,
"roster_count": (self._roster_state() or {}).get("count", 0),
```

`_roster_state()` 每次都读盘 + `parse_roster`。同文件里 `_landing_html` 也会再调一次。
建议给 roster 增加与 manifest 相同的 **mtime 缓存**（`_MANIFEST_CACHE` 的现成模式，
`registry.py:143-205`），三处调用共用。

---

## 二、性能优化（P1）

### 2.1 FTS5 改 external content 表，索引库体积约减三分之一（`landoc/index.py:25-45`）

当前 `documents.content`（原文）与 `documents_fts.content`（FTS 列）**各存一份全文**。
检索路径其实只用 FTS 索引 + `JOIN documents` 取原文（`_run_query` 里 `documents.content
AS raw`），FTS 表里的正文副本纯属浪费。`store_content=True`（默认、召回优先）时 1000 篇
×3 万字 ≈ 180MB 的 DB，改后约省 30-40%（trigram 倒排索引仍在）。

改法：

```sql
CREATE VIRTUAL TABLE documents_fts USING fts5(
    title, content,
    content='documents', content_rowid='rowid',
    tokenize='trigram'
);
```

配 AFTER INSERT/UPDATE/DELETE 三个触发器同步（外部内容表的标准做法），`upsert`/`delete`
改为只写 `documents`。迁移放在 `Index.__init__`：检测到旧结构（fts 表无 `content=` 属性，
可用 `pragma table_info` / 尝试重建）时，新建表 + `INSERT INTO documents_fts(documents_fts)
VALUES('rebuild');` 一次性回填，`meta` 里记 `schema=2`。**注意**：`rebuild` 与触发器
二选一（迁移时先建表用 rebuild，运行期靠触发器）。

收益对照测试：`tests/test_index.py` 现有用例全部应保持绿；加一个「迁移后可检索」用例。

### 2.2 大目录的增量扫描：目录 mtime 缓存（`landoc/pipeline.py:133-156`）

`_iter_docs` 每轮 `os.walk` 全量遍历 + 排序。空闲调度里每轮 `run()`（batch=20）都重新
walk；对 5-10 万文件的共享目录（设计目标「每人一个库」，实测单位网盘挂载目录很大），
单次 walk 就是秒级到分钟级，而大多数轮次只转换 0 篇。

分两步走：

1. **低成本版（建议先做）**：`state.json` 里记 `{目录路径: 目录mtime}` 缓存。Windows/
   NTFS 目录的 mtime 在**直接子项**增删改时更新；每轮 walk 时对 mtime 未变的子目录跳过
   递归，直接复用缓存里的文件清单（缓存失效安全兜底：顶层目录 mtime 变了就整树重扫）。
   失误代价可控——最坏情况是漏收一个新文件，下一轮顶层变化或定期（如每 30 分钟）全量
   校准一次就能补上。
2. **彻底版（P3 再说）**：后台线程 `ReadDirectoryChangesW` 监听，变更队列驱动增量。
   pywin32 有现成接口，但要处理网络盘/挂载盘不支持通知的情况，复杂度不低。

### 2.3 登记中心补限流（`landoc/registry.py:778+, 1232+`）

库端服务有 `RateLimiter`（`server.py:66-86`，认证前），中心**没有任何限流**：
`POST /register`（64KB body 上限）可被局域网内脚本打满 CPU/磁盘（每心跳一次
`_save()`）；`/download` 大文件流式发送无并发上限。内网 + CIDR 白名单下风险偏低，但
修法便宜：把 `RateLimiter` 挪到公共位置（如 `landoc/ratelimit.py`），中心对
`/register` 与页面路由套用（/download 放宽或只限并发数）。同时复用 1.3 的成果，写盘
频率自然下降。

### 2.4 审计日志轮转（`landoc/server.py:89-106`）

`access_log.jsonl` 无限增长（每请求一行，60 req/min/IP 常年累月）。`updater.log` 已有
1MB 轮转先例（`updater.py:104-115`），照抄：超过 20MB 改名 `.1`，只保留一代。顺带把
「每请求 open/close 文件」改成常开句柄 + 轮转时重开（可选，收益小）。

### 2.5 2 字查询的子串扫描缓存（`landoc/index.py:221-225, 266-298`）

低于 trigram 最小长度的查询走 `instr` 全表扫描（几千篇 × 每篇至多 2 万字 ≈ 每次查询
扫数亿字符，秒级）。个人库规模下可接受，但水文常用词（「乐平」「洪水」）恰恰是 2 字词、
重复率高。最便宜的缓解：`Index` 内加一个 `functools.lru_cache` 风格的
`(q, dir_filter, limit) → rows` 缓存（容量 64，写操作时清空），命中即返回。更彻底的
bigram 辅助表方案收益/成本比不高，不建议现在做。

### 2.6 中心 `/skill.zip` 的 mtime 缓存（`landoc/registry.py:208-228`）

每个下载请求都 `rglob` 模板目录 + 重新 zip。按 1.4 的模式缓存 `(registry_url,
模板树最新mtime) → bytes` 即可。同事批量装 skill 时收益明显。

---

## 三、安全加固（P2，多数是低风险但便宜的收口）

| # | 事项 | 位置 | 建议 |
|---|---|---|---|
| 3.1 | roster 管理员判定基于**启动时**的本机地址快照 | `registry.py:479-489, 1327` | DHCP 续租/换网卡后编辑页会 403。`_is_local_request` 改为现查 `netinfo.local_ips()`（该函数已存在且注释明说是同一口径），或每 5 分钟刷新一次 |
| 3.2 | `_local_ips()`（registry）与 `local_ips()`（netinfo）重复实现 | `registry.py:479` vs `netinfo.py:8` | 删前者，统一引 netinfo。同理 `_cidr_allowed`（registry.py:448）与 `_ip_allowed`（server.py:109）几乎相同，合并到一处 |
| 3.3 | 多网卡机器上，「本机地址清单」含 VPN/虚拟网卡地址，经这些网络进来的请求可编辑 roster | `registry.py:1327` | 风险极低（内网）；文档里注明可用 `roster_admin_ips` 收窄即可，不必改代码 |
| 3.4 | HTTP 明文 + sha256 由同一中心下发 | `updater.py` | 内网设计取舍，已知即可；若未来跨网段分发，再考虑对 zip 附带离线签名 |
| 3.5 | `Config` 无 schema 版本号 | `config.py` | `to_dict` 里加 `"config_version": 1`，为将来字段语义变更留迁移钩子（成本一行） |

---

## 四、工程卫生（P2）

### 4.1 砍掉 jieba 死依赖（`pyproject.toml:9`、`packaging/LanDocShare.spec:27`）

全仓库对 jieba 的唯一引用是 `index.py:52` **注释里的一句话**（「这正是纯 jieba 分词
方案的死穴」）。检索实现从头到尾用 FTS5 trigram。但 jieba 还躺在 `dependencies` 里，
spec 还专门 `collect_data_files("jieba")` 打包它的词典——onedir 包体白带约 10-15MB，
且给「为什么不用分词」的后来者制造困惑。删两处引用即可（同事机器上已装的无妨，
import 不存在也不影响运行）。

### 4.2 `pytest` 移出运行时依赖（`pyproject.toml:13`）

放进 `[project.optional-dependencies] dev = [...]`。spec 的 EXCLUDES 已排除 pytest，
不影响打包，只是元数据更诚实。

### 4.3 去掉兜底工具的私人路径探测（`landoc/cli.py:47-50`）

```python
ACROBAT_TOOL_CANDIDATES = (
    Path(r"D:\应用软件\右键解密"),
    Path(r"Claude Code\解密平台\工具\单个解密"),
)
```

这是作者本机目录，却会随发布版分发给全院（每次 `init` 都探测、还写进同事的
config.json）。改为：只认 `--acrobat-tool-dir` 显式指定 + `LANDOC_ACROBAT_TOOL_DIR`
环境变量；作者本机用环境变量或 init 参数保留现状。

### 4.4 cli → registry 的参数转发改透传（`landoc/cli.py:510-540`）

`_cmd_registry` 手工把 13 个参数逐个搬进 argv，中心每加一个参数要同步改两处（已经发生
过：`--roster` 就没接）。改为 `cli registry` 之后的 token 原样透传给 `registry.main()`
（argparse 那端天然支持），删掉 30 行搬运代码。

### 4.5 本地磁盘清理（不入库，顺手做）

`dist/LanDocShare.old-20261118-095304`（176MB）、`dist/_old-0.1.0`（235MB）、
`distribution/`（728MB，多版本包累积）共 1GB+。`.gitignore` 已覆盖，仅是磁盘问题。
可加 `packaging/release.py --prune` 子命令：只保留最新版本 zip + `LanDocShare.zip`
+ `latest.json`。

### 4.6 库端统一运行日志（`landoc/app.py`）

现在散落着 `logs/access_log.jsonl`（审计）、`update.log`、`tray.log`，但 **app/pipeline
自身没有日志**——`_worker` 里轮次失败只写进内存 `AppStatus.message`，重启即丢，远程
排障只能让同事复现。加一个 `%LOCALAPPDATA%\LanDocShare\logs\app.log`（带轮转），
`_drain_while_idle` 每轮记一行摘要（converted/failed/skip 统计），异常记完整堆栈。
状态窗口「查看日志」菜单已有目录入口，零 UI 改动。

---

## 五、产品 / 体验（P2-P3，按需求排）

1. **检索页缺浏览入口**（`landoc/web/index.html`）：不输关键词只能逐级点目录。
   建议加 `GET /api/recent`（`ORDER BY indexed_at DESC LIMIT 50`，一行 SQL）+ 首页
   默认渲染「最近收录」，让第一次打开的同事立刻看到库里有东西。
2. **搜索无 loading 态 / 无空结果建议**：请求期间按钮置灰 +「搜索中…」；空结果时
   提示尝试 2 字词（已有文案，加「查看该目录」快捷动作更顺）。
3. **xlsx/et 支持**：`SKIP_EXTS`（`pipeline.py:32`）一期刻意跳过。单位里表格类资料
   占比通常不低，`openpyxl` 直读明文 xlsx 成本很小（加密的仍走 COM：Excel.Application
   取 UsedRange 值拼 GFM）。建议列为 0.3 的主打功能。
4. **扫描件 OCR**：`no_text_layer` 目前只能失败记账。内网若有现成 OCR 服务再接，
   不建议本地引重型依赖（包体、准确性都不划算）。
5. **失败文件的自助入口**：状态窗口显示「失败 N 篇」并一键打开 `failures.json` 所在
   目录——配合 1.2 的环境自愈，同事侧基本不用找管理员。

---

## 六、建议实施顺序

| 批次 | 内容 | 预估 |
|---|---|---|
| 第 1 批（本周可完） | 1.1 重试上限、1.3 registry 写盘、1.4/2.6 mtime 缓存、4.1 jieba、4.2 pytest、4.3 私人路径、4.4 参数透传 | 各 0.5h 内，互不依赖，可一次发 0.2.8 |
| 第 2 批 | 1.2 环境自愈、2.4 审计轮转、2.3 中心限流、4.6 运行日志、3.1/3.2 去重与现查 | 各 ≤ 半天 |
| 第 3 批 | 2.1 FTS external content（含迁移与测试）、2.2 目录 mtime 增量扫描 | 各 1-2 天，需要拿真实大库实测 |
| 第 4 批 | 5.1/5.2 检索页打磨、5.3 xlsx、4.5 发布清理脚本 | 按产品节奏排 0.3 |

**不建议动的**（现状是对的，别「顺手优化」）：`list_dir` 的两步子查询（注释里实测过
单层查询会 30s 超时）；`OfficeSession` 单线程 COM 亲和；`_make_snippet` 的 `<<>>` 标记
协议；updater 的「不能删自己、两次改名、健康标记回滚」链条；watchdog 对退出码 0/3 的
特判；`authorize_person` 的 IP 优先语义（中心与库端共用同一份）。

---

## 附：验证方式

- 每批改动跑 `python -m pytest tests -q`（当前基线 357 passed）；
- 涉及索引迁移（2.1）与增量扫描（2.2）的批次，用 `LANSHARE_IT=1` 追加真机用例，
  并拿一个 >2 万文件的共享目录实测一轮「空转扫描」耗时对比；
- 涉及中心（1.3/2.3/2.6）的批次，用 `tests/test_registry.py` 的现有 fake store 模式
  补并发用例（`threading` + `Barrier` 同时打 `/libraries`）。
