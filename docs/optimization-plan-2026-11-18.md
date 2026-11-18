# LanDocShare 优化方案（2026-11-18）

> 基于对全部 24 个 `landoc/` 模块、打包脚本、skill 客户端与 357 个单元测试的通读
> （测试现状：`428 passed, 9 skipped`）。按优先级排列，每条附证据位置与建议修法。
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

### 2.1 FTS5 改 external content 表，索引库体积省 27%~43%（已完成，2026-11-18）

~~当前 `documents.content`（原文）与 `documents_fts.content`（FTS 列）**各存一份全文**。~~
已落地：`documents_fts` 改成 `content='documents', content_rowid='rowid'` 的 external content
表，自己只存倒排；`AFTER INSERT/UPDATE/DELETE` 三个触发器同步，`upsert`/`delete` 只写
`documents`（旧实现一篇要 `SELECT rowid` + 删插 FTS，三次往返）。

迁移在 `Index._migrate_fts`：`sqlite_master.sql` 里没有 `content='documents'` 就是旧库 →
`DROP TABLE` → 按新 SCHEMA 重建 → `INSERT INTO documents_fts(documents_fts) VALUES('rebuild')`
回填 → `VACUUM` 把旧表占的页还给磁盘（100 篇 0.21 秒）。`DROP` 撞上别的进程占用时放弃本次迁移
（旧表配新触发器照样能写），下次打开重试。

实测：

| 场景 | 旧 | 新 | 省 |
|---|---|---|---|
| 100 份真实加密 pdf（每篇 8,456 字） | 9.61 MB（11.4 字节/字） | 7.04 MB（8.3 字节/字） | **27%** |
| 合成 1000 篇 × 2 万字 | 139.5 MB（7.0 字节/字） | 80.1 MB（4.0 字节/字） | **43%** |

写入速度两者都在 ~2 毫秒/篇（20k 字/篇），相对 1 秒/篇的转换与 0.73 秒/篇的取文本可忽略。
踩坑记两条：external content 表上**不能**直接 `DELETE FROM documents_fts WHERE rowid=?`
（报 `database disk image is malformed`），删改必须走触发器或 `('delete', rowid, ...)` 命令；
迁移后不 `VACUUM` 的话文件大小一点不变（空闲页留在文件里），"省磁盘"的收益要 VACUUM 才看得见。

测试：`tests/test_index.py` 加 3 个用例（正文只存一份的结构断言、增删改后倒排同步、旧库原地迁移且幂等），
全量 400 → **403 passed / 9 skipped**（并行 stat 之后 406）。

### 2.2 大目录的增量扫描：目录 mtime 缓存（评估后不做，2026-11-18）

~~`_iter_docs` 每轮 `os.walk` 全量遍历 + 排序，对 5-10 万文件的共享目录单次 walk 就是秒级。~~

先量了一遍再决定，结论是**不划算**。`G:\2025年工作`（5.4 万个文件 / 7,427 篇可转文档 /
5,659 个目录）热态实测：

| 阶段 | 耗时 |
|---|---|
| `_iter_docs`（walk + 过滤 + 排序） | 0.37 s |
| stat 7,427 篇（串行） | 0.28 s |
| stat 改 4 条线程 | 0.22 s（1.27×） |
| 规划阶段合计 | 0.65 s → 0.59 s |

对照一轮的规模：`batch=100`、`budget=600 s`，转换本身要几分钟到十几分钟。规划阶段的
0.65 秒占不到千分之一，且**只在每轮开头付一次**（冷态首扫 19.9 秒是进程启动那一次，与
"每轮"无关）。

而目录 mtime 缓存的性价比很差：NTFS 的目录 mtime 只在**直接子项增删改名**时变，**文件内容
被改不会变**，所以缓存只能省掉 readdir，仍要为每篇 stat（否则改过的文件永远不收）。省下的
就是那 0.37 秒里的一部分，却要背上"漏收新文件/漏掉改名/删目录"的正确性风险，以及把
`state.json` 撑大（5 万篇 ≈6 MB，每 60 秒整份重写）。

已经做的是零风险的那半：`_stat_many`（`pipeline.py:198`）把 stat 分给 4 条线程
（`STAT_WORKERS=4`，少于 200 篇直接串行），省 0.06 秒且结果与串行逐条一致（有单测）。

真正需要做这块的场景是**网络盘**（SMB 上 readdir/stat 都是往返，热态也许要几十秒）：
到那时再上"彻底版"`ReadDirectoryChangesW`，或者干脆按目录分区串行扫描 + 每轮只扫一个分区。

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

### 2.7 加密 pdf 一批共用一个 Acrobat（已完成，2026-11-18）

原实现 `_acrobat_text` 每篇都 `Dispatch("AcroExch.App")` 并在 `finally` 杀掉自己拉起的
进程。实测（21 页真实加密 pdf）：**单篇 6.2 秒，其中 5.5 秒（88%）是进程起停**；
App 冷启动 4.45 秒，纯取文本 0.73 秒/篇。

改法：仿 `OfficeSession` 新增 `pdf_conv.PDFSession`（专用线程 + 一次 `Dispatch` + 每篇
新建 `PDDoc`，复用一个 PDDoc 实测无额外收益；超时只杀「本会话新起的 pid」；COM 断连
`PDFSessionGone` 后重建重试一次；每 500 篇换新会话，内存约 +0.44 MB/篇）。
`pipeline.convert_one` 增加 `pdf_session` 参数，`run()` 每轮建一次并在 `finally` 关闭；
加密 PDF 不再白起 Word 会话（`needs_com` 判定修正）；`app.py` 默认 `batch` 20 → 100。

实测：100 篇副本 **100.2 秒 ≈ 1.0 秒/篇**（含落 md + 写索引；纯取文本 0.73 秒/篇），
比原来 6.2 秒/篇快 6 倍；1000 篇建库从约 103 分钟降到约 13 分钟。测试 388 passed。

### 2.8 一轮的边界与进度显示（已完成，2026-11-18）

吞吐修好之后，暴露出来的是**"这一轮什么时候该停"**和**"用户看不看得见进展"**：

1. `run(limit=)` 原来数的是**成功**篇数。一批 100 篇里全是带口令/损坏文件时，limit 永远
   到不了 —— 单次 `run()` 会一直转下去（每篇付一次 120 秒超时），库端「用户回来就收工」
   的空闲判定只在轮与轮之间，等于停不下来。
2. `state.json` / `failures.json` 只在整轮 `finally` 写一次；CLI 全量 `convert` 还是整库
   单事务 —— 断电或强退就全丢，建库途中其他连接也看不到新内容。
3. 状态窗口只有「建库中：已转换 N 篇」，没有总数/剩余/ETA，上千篇时看起来就是卡死。

改法（`pipeline.run` + `app._run_round` + `panel`）：

- `run()` 先做一遍**规划**（walk + stat + 跳过判定），得出 `total_docs` 与 `pending`，
  再逐篇转 —— 于是 limit/budget 有了明确的单位：`limit` 数**尝试**（失败也占额度），
  `budget` 是整轮墙钟上限，`should_stop()` 每篇开始前问一次（用户回来/暂停/退出）。
  `report.stop_reason` 说明停在哪（`limit` / `budget` / `stop` / `done`）。
- 落盘：索引每 `COMMIT_EVERY=20` 篇 `commit`，`state.json`/`failures.json` 每
  `STATE_SECONDS=60` 秒整份重写，收尾 `force` 一次；`report.rate` 用真在转换的时间
  （不含规划阶段）滚动平均成 `state["rate_s_per_doc"]`，供 ETA 与下一轮复用。
- 库端：`app._run_round()` 传 `limit=batch(100)`、`budget=round_seconds(600)`、
  `should_stop=_should_stop_round`、`on_progress=_on_round_progress`；状态窗口首行变成
  `建库中：已转换 300 篇 · 还剩 700 篇（约 1 小时 2 分）`，另多一行「待收录」。
- CLI `convert --limit` 的 help 改成「最多**尝试**转换的篇数（失败/超时也算一篇）」。

实测（40 份加密 pdf 副本）：`limit=10` → 只尝试 10 篇（15.0 秒，含首次 Acrobat 启动）、
`budget=3` → 转完手头 1 篇即停、`should_stop` 第 6 次为真 → 停在这一篇之后；第一轮结束
`state.json` 已记 10 篇。测试 388 → **400 passed / 9 skipped**。

### 2.9 速率与 ETA 的量法：甩掉每轮第一篇的启动开销（已完成，2026-11-18）

原先 `rate = (整轮耗时 - 规划耗时) / attempted`，而每一轮的第一篇都替整轮付一次 Office/
Acrobat 启动（4.5~9 秒）。轮越大误差越小（100 篇 ≈ +0.05 秒/篇），但库端 `batch=8~20` 的
短轮里会把它抬成 1.3~2.2 秒/篇，ETA 直接虚高两三倍。

改成"逐篇结算、第一篇不计"（`run()` 里的 `_settle_doc`：每篇的实际耗时在下一圈开头结算，
`continue`/异常路径都覆盖；`steady = attempted - 1`，只有 `steady > 0` 才采样，否则沿用
`state["rate_s_per_doc"]` 的历史值；第一轮就只转了一篇时才退回整轮时间）。

实测（20 份加密 pdf 副本，`limit=8`，连跑两轮）：

| | 墙钟 | 旧口径 | 新速率 |
|---|---|---|---|
| 第 1 轮 | 17.5 s | 2.19 秒/篇 | **0.75 秒/篇** |
| 第 2 轮 | 14.8 s | 1.84 秒/篇 | **0.74 秒/篇** |

剩余 12 篇的 ETA 从 26.3 秒修正到 **9.0 秒**（实际就是 12×0.74）。新速率与纯取文本的
0.73 秒/篇吻合，说明这一项已经量准了。

同一批改动顺带把规划阶段的 stat 并行化（`_stat_many`，见 2.2）。测试 403 →
**406 passed / 9 skipped**。

---

## 三、安全加固（P2，多数是低风险但便宜的收口）

| # | 事项 | 位置 | 建议 |
|---|---|---|---|
| 3.1 | roster 管理员判定基于**启动时**的本机地址快照 | `registry.py:479-489, 1327` | DHCP 续租/换网卡后编辑页会 403。`_is_local_request` 改为现查 `netinfo.local_ips()`（该函数已存在且注释明说是同一口径），或每 5 分钟刷新一次 |
| 3.2 | `_local_ips()`（registry）与 `local_ips()`（netinfo）重复实现 | `registry.py:479` vs `netinfo.py:8` | 删前者，统一引 netinfo。同理 `_cidr_allowed`（registry.py:448）与 `_ip_allowed`（server.py:109）几乎相同，合并到一处 |
| 3.3 | 多网卡机器上，「本机地址清单」含 VPN/虚拟网卡地址，经这些网络进来的请求可编辑 roster | `registry.py:1327` | 风险极低（内网）；文档里注明可用 `roster_admin_ips` 收窄即可，不必改代码 |
| 3.4 | HTTP 明文 + sha256 由同一中心下发 | `updater.py` | 内网设计取舍，已知即可；若未来跨网段分发，再考虑对 zip 附带离线签名 |
| 3.5 | `Config` 无 schema 版本号 | `config.py` | `to_dict` 里加 `"config_version": 1`，为将来字段语义变更留迁移钩子（成本一行） |

### 三的落地情况（2026-11-18 复核）

- 3.1 **早已落地**（`3d0dbfe`）：`registry._current_local_ips()` 每 60 秒刷新缓存、`_roster_admin_ips()`
  在"没显式配 `roster_admin_ips`"时走现查版本——换网卡/DHCP 续租后管理员不会再被自己的中心挡成 403。
- 3.2 **本轮收口**：网段判定原来真有两份（`registry._cidr_allowed` 与 `server._ip_allowed`，
  逐行相同），现在只有 `netinfo.cidr_allowed()` 一份实现，两边各留一个同名薄封装（调用点不动，
  "空清单=拒绝"的语义只在一个地方维护）。`_local_ips` 那边早前已改成转发 `netinfo.local_ips()`。
- 3.3 **文档收口，不改代码**：`roster_admin_ips` 本来就支持显式收窄；机器上如果有 VPN/虚拟网卡
  又不想让那条链路能改名单，在 `registry.json` 的 config 里写死两三台管理机的地址即可。
  多网卡默认清单包含虚拟网卡这件事，内网风险等级下不值得为它加复杂度。
- 3.4 **维持现状**：中心与安装包同在一条内网链路上，https 的证书运维成本远大于收益；
  真跨网段分发时再谈离线签名（那时 `latest.json` 里已经带了 sha256，签名只需再加一个字段）。
- 3.5 **本轮落地**：`config.CONFIG_VERSION = 1`，`to_dict()` 末尾写 `config_version`；
  `from_dict()` 按 dataclass 字段过滤，未知键（含将来更高的版本号）静默忽略，新旧互读不炸。

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

### 四的落地情况（2026-11-18 复核）

| # | 结论 | 依据 |
|---|---|---|
| 4.1 | **早已删掉** | `pyproject.toml` 的 `dependencies` 已无 jieba（`5e74a7c`）；`packaging/LanDocShare.spec:37` 还把 `jieba` 放进 `EXCLUDES`，全仓库只剩 `index.py:69` 一句注释提到它 |
| 4.2 | **早已移出** | `pytest>=8.0` 在 `[project.optional-dependencies] dev`，运行时依赖里没有它；spec 的 EXCLUDES 也排除 |
| 4.3 | **早已收口** | `cli.ACROBAT_TOOL_DIR_ENV` + "程序同级 `工具/`"是正式来源；作者本机的两个历史目录放在 `_DEV_ACROBAT_TOOL_CANDIDATES`，**只在源码运行（`sys.frozen` 为假）时才探**，打包版不会再带到同事机器上（`fd83103`） |
| 4.4 | **本轮收口** | 不再维护 13 行参数搬运表：`cli.main()` 见到首个子命令是 `registry` 就把后面所有 token 原样交给 `registry.main()`（`--help` 也直接给中心自己的帮助）。原先的逐条搬运正是 `--roster` 漏掉的根源 |
| 4.5 | **本轮加子命令，磁盘待清** | `packaging/release.py --prune [--apply]`：只留 `latest.json`、当前版本 zip、固定下载名 `LanDocShare.zip`、当前版本说明，外加 `dist/LanDocShare`；清掉更早的 zip/说明与 `dist/LanDocShare.old-*`、`dist/_old-*`。**清单缺失时一个 zip 都不删**（不知道最新是哪版就别猜）。本机**已实际清理 9 项 / 951.3 MB**（7 个历史 zip 各 90.9 MB + 两个旧构建目录 232.3 / 173.5 MB）：`dist` 579.3 → 173.5 MB、`distribution` 727.4 → 181.9 MB，仓库（不含 .git）1.44 GB → **492 MB**；保留的 `LanDocShare-0.2.6.zip` 与固定名 `LanDocShare.zip` sha256 与 `latest.json` 一致、1,148 个条目可读，再跑一次 `--prune` 报 0 项 |
| 4.6 | **部分早已落地，本轮补摘要行** | `log_event()`（`app.py:41`，>1 MB 滚成 `.1`）早就在写：启动、扫描失败、登记中心恢复都在记；本轮补上 `_drain` 的**每轮一行摘要**（收录/失败/尝试/剩余/用时/停止原因，只在 `report.attempted` 非零时写，空转不刷屏） |

---

三、四两批复核完毕后全量测试：**428 passed / 9 skipped**（新增 22 个用例：网段判定 6、配置版本 2、registry 透传 2、发布清理 2、每轮日志 1，其余为各文件补齐）。

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

- 每批改动跑 `python -m pytest tests -q`（当前基线 428 passed / 9 skipped）；
- 涉及索引迁移（2.1）与增量扫描（2.2）的批次，用 `LANSHARE_IT=1` 追加真机用例，
  并拿一个 >2 万文件的共享目录实测一轮「空转扫描」耗时对比；
- 涉及中心（1.3/2.3/2.6）的批次，用 `tests/test_registry.py` 的现有 fake store 模式
  补并发用例（`threading` + `Barrier` 同时打 `/libraries`）。
