# 第 4 批改动清单：日志补全（退出/崩溃/更新/失败明细/被拒可见/查询记录保留）

> 依据：2026-11-24 日志审计（`D:\MyLib\logs\app.log`、`access_log.jsonl`、`updates\logs\update.log` 三份实测）。
> 本文件只写改动方案，**不动代码**；拍板后再按节实施。
> 目标：出了故障能在日志里查到"谁、什么时候、因为什么"，而不是只有一行"启动了"。

## 0. 现状（实测，非推测）

| 日志 | 位置 | 体量 | 内容 |
|---|---|---|---|
| `app.log` | `<库>/logs/app.log`（`app.py:44 log_event`，1 MB 滚 `.1`） | 97 行 / 9.6 KB，覆盖 0.2.8→0.2.22 六天 | 启动 49、登记中心 29、轮次 19，**全库只有 5 个调用点** |
| `access_log.jsonl` | `<库>/logs/access_log.jsonl`（`server.py:65 Audit`，4 MB 滚 `.1`） | 231 行 / 26 KB | 回环 154（67%）、`10.91.1.197` 75、`10.91.1.178` 2 次 401；200×222 / 404×6 / 401×2 / 403×1 |
| `update.log` | `%LOCALAPPDATA%\LanDocShare\updates\logs\update.log`（`updater.log`） | 152 行 / 19.7 KB | 只有下载失败（sha 校验不符）、升级成功、回滚；**没有"发现新版本""用户点了升级"** |

先纠正一条我口头说过的判断：**「更新全链路无日志」不准确**——更新器自己有 `update.log`（`updater.py` 的 `log()`，1 MB 滚 `.1`），
缺的是 **app 侧视角**（发现新版、下载好、点了升级、升级失败）和**可发现性**（没人知道要去 `%LOCALAPPDATA%` 看）。

## 1. 设计约定（本批不引入 logging 模块）

- 沿用 `[YYYY-MM-DD HH:MM:SS] 文本` 格式，**不加级别前缀**（那是 P2）；一行一件事，便于 `Select-String` 捞。
- 三类日志各管一段，不混写：
  - `app.log`：人读的运行史（启动/停止/崩溃/每轮摘要/失败明细/更新/被拒）。
  - `access_log.jsonl`：机器读的一请求一行（**格式不动**；只补 `fallback`/`dir` 两个字段，轮转改成带时间戳归档、不覆盖旧记录）。
  - `update.log`：更新器自己的细节（格式不动，只把路径写进 README）。
- 所有新日志点都走 `log_event`（写失败静默，绝不拖垮服务）。

## 2. P0-1　退出/停止要留一行

**问题**：`App.stop()`（`landoc/app.py:236-249`）全程无日志；今天托盘是被 `Kill()` 干掉的，日志里连"服务停过"都看不出来。

**改法**

1. `landoc/app.py:236` 签名加 `reason`，并加幂等守卫（托盘退出会连续调到）：

```python
def stop(self, reason: str = "") -> None:
    if self._stop.is_set():      # 已经停过：别重复记一行
        return
    self._stop.set()
    ...（原逻辑不变）...
    self._update(phase="stopped")
    log_event(self.cfg.lib_dir, f"库服务停止：{reason or '正常退出'}（本次收录 {self.converted} 篇）")
```

2. 调用点各给一句 reason：

| 位置 | 改成 |
|---|---|
| `landoc/tray.py:290`（右键退出） | `self.app.stop("托盘退出")` |
| `landoc/tray.py:462`（无托盘模式 Ctrl+C） | `app.stop("命令行退出")` |
| `landoc/tray.py:482`（`--edit-settings` 收尾） | `tray_app.app.stop("改完建库设置退出")` |
| `landoc/app.py:422`（升级前停服） | `self.stop("安装更新")` |

其余 `.stop()` 不是服务停止，别动：`app.py:240` 是 `_updates.stop()`、`tray.py:199` 与 `292` 是 `icon.stop()`。

3. 副作用检查：`stop()` 在 `start()` 之前被调也不会炸（`httpd` 为 `None`、`_threads` 空）；
   但幂等守卫会让"第二次 stop"不再 join 线程——`tests/test_app.py` 里大量 `finally: app.stop()`，必须整文件回归。

**测试**：`test_stop_writes_app_log`（start→stop→日志含「库服务停止：托盘退出」）、
`test_stop_twice_logs_once`（连调两次只有 1 行）。

## 3. P0-2　崩溃要留痕（excepthook）

**问题**：既没有 `sys.excepthook` 也没有 `threading.excepthook`。`App._worker`（`app.py:490-523`）里
`_drain` 之外的任何异常都会让那条线程**静默死掉**：界面还写"共享中"，实际不建库了。

**改法**

1. `landoc/app.py` 紧跟 `log_event`（:57 之后）新增：

```python
_CRASH_TRACE_LINES = 6          # 栈尾巴留 6 帧够定位，整段不超 2000 字符
_hooks_installed = False

def install_crash_hooks(lib_dir) -> None:
    """主线程与后台线程的未捕获异常都落 app.log：一条线程死了不能没人知道。"""
```

2. 两个 hook 都记一行 + 栈尾，再交回默认行为：

```
[2026-11-24 09:58:11] 未捕获异常（landoc-worker 线程）：RuntimeError: COM 挂了
    File "landoc\app.py", line 503, in _worker
    File "landoc\pipeline.py", line 512, in run
```

- `sys.excepthook`：记完调原 hook（保留默认 stderr/崩溃语义）。
- `threading.excepthook`：记完**不重抛**（原行为就是打 stderr）。
- 用模块级 `_hooks_installed` 保证只包一次（`start()` 会在升级失败后被再次调用）。
- 截断：`traceback.format_exception` 取末 6 行，整体 `[:2000]`。

3. 安装点在 `App.start()` 开头附近（`app.py:192` 之后）：`install_crash_hooks(self.cfg.lib_dir)`。

**测试**：`test_crash_hook_records_uncaught_exception`（直接调 `sys.excepthook(RuntimeError, RuntimeError("x"), tb)` 后断言）、
`test_thread_crash_is_logged`（直接调 `threading.excepthook(arg)`；**不要真起线程抛错**，pytest 会当失败）。
每个用例结束把 `sys.excepthook`/`threading.excepthook` 还原，避免污染后续用例。

## 4. P0-3　更新链路在 app.log 上留三行

**问题**：`_on_update_change`（`app.py:348-382`）只把 phase/version/message 塞进内存状态，重启即失；
`update.log` 里有细节但没人知道路径。

**改法**

1. `app.py:348` 的 `_on_update_change` 里，按 **phase 变化去重**后记一行（新字段 `self._last_update_phase = ""`）：

```python
note = {"available": "发现新版本", "downloading": "开始下载新版本",
        "ready": "新版本已下载好，等待升级", "manual": "版本跨度过大，需手动重装",
        "failed": "新版本下载失败"}.get(phase)
if note and phase != self._last_update_phase:
    self._last_update_phase = phase
    detail = str(fields.get("message") or "")[:200]
    log_event(self.cfg.lib_dir, f"更新：{note} v{fields.get('version') or '?'}" + (f"：{detail}" if detail else ""))
```

```
[..] 更新：发现新版本 v0.3.1：发现新版本 v0.3.1
[..] 更新：新版本已下载好，等待升级 v0.3.1
[..] 更新：新版本下载失败 v0.3.1：更新下载失败：校验失败：sha256 是 19369d48b7ece4d2…
```

2. 升级落地处（`app.py:422` `_update(update_phase="installing")` 之前）补一行：

```python
log_event(self.cfg.lib_dir, f"更新：开始安装 v{self.status.update_version}（先停服务）")
```

`spawn_update` 抛异常的既有分支（:425-427）再加：`log_event(..., f"更新：启动更新器失败：{exc}")`。

3. `README.md` 排障段补一句：更新细节在 `%LOCALAPPDATA%\LanDocShare\updates\logs\update.log`。

**测试**：`test_update_phase_changes_are_logged`（同 phase 连来两次只 1 行）、
`test_install_is_logged_before_stopping`（monkeypatch `updater.spawn_update` 抛错 → 两条日志都在）。

**手工清理（不写代码）**：`%LOCALAPPDATA%\LanDocShare\logs\update.log`（11-18 的旧路径遗留 10.4 KB）删掉，
避免以后有人看错文件。

## 5. P1-4　失败明细落一行

**问题**：`_drain`（`app.py:564-569`）只记「失败 N 篇」；`RunReport.failed`（`pipeline.py`）里其实有
`list[tuple[str, str]]`（rel + reason），白扔了。现在 `10.91.1.178` 之类问题只能靠 `failures.json` 反查。

**改法**：在现有摘要行之后追加（最多 3 条）：

```python
failed = report.failed or []
if failed:
    shown = "、".join(f"{rel}（{reason}）" for rel, reason in failed[:3])
    tail = f" 等 {len(failed)} 篇" if len(failed) > 3 else ""
    log_event(self.cfg.lib_dir, f"失败明细：{shown}{tail}")
```

reason 已是短码：`unreadable` / `no_office` / `timeout` / `password_protected` / `parse_failed` /
`empty_text` / `write_failed` / 异常类名小写。

```
[..] 一轮结束：收录 0 篇、失败 2 篇、尝试 2 篇、剩余 0 篇，用时 3 秒（done）
[..] 失败明细：资料/坏.docx（unreadable）、资料/带口令.docx（password_protected）
```

**测试**：`test_failure_details_are_logged`、`test_failure_details_are_capped_at_three`（5 条 → 3 条 + 「等 5 篇」）。

## 6. P1-5　被拒访问要能看见

**问题**：401/403/429 只进 `access_log.jsonl`；`10.91.1.178` 被拒两次，面板、托盘、`app.log` 全无痕迹。
同事问「我怎么搜不了」时，库主人没有任何线索。

**改法**

1. `landoc/server.py:71` `Audit.__init__(self, path, max_bytes=0, on_denied=None)`；
   `log()` 里状态命中 `(401, 403, 429)` 时回调一次（包 `try/except`，回调炸了也不能影响响应）：

```python
DENIED_STATUSES = (401, 403, 429)
```

2. `server.py:360` `make_server(cfg, index=None, port=None, *, on_denied=None)` → `Audit(..., on_denied=on_denied)`。
3. `app.py:203` 建服务时（`make_server`）传 `on_denied=self._note_denied`。
4. `AppStatus`（`app.py:60`）加两个字段：`denied: int = 0`、`denied_note: str = ""`。
5. `App._note_denied(self, *, ip, name, api, status)`（新方法，挨着 `_refresh_counts`）：
   - 计数并 `self._update(denied=..., denied_note=f"{ip} {time.strftime('%H:%M')}")`；
   - 落盘**节流**：类常量 `_DENIED_LOG_GAP = 600.0`，10 分钟内只记第一行，
     文案 `访问被拒：401 /api/search，来源 10.91.1.178（不在名单）`——不节流会被扫描器刷满 1 MB。
6. 可见化（两处，都不改布局）：
   - `panel.py` 的 `status_rows`：`denied > 0` 时在「共享范围」值末尾追加 `" · 最近有 N 次访问被拒（IP 时间）"`；
     现有 `tests/test_panel.py:38/48/63/73` 断言的都是 `denied == 0` 的情况，不受影响。
   - `AppStatus.summary()`：追加 `" · N 次访问被拒"`（托盘悬停可见）。
7. 不做：不给被拒事件弹气泡（会打断主人做事），也不写 `access_log` 以外的地方。

**测试**：`test_denied_visits_show_in_shared_scope_row`（`status_rows`）、
`test_denied_visits_are_logged_once_per_window`（连 5 次只 1 行）、
`test_make_server_passes_denied_callback`（`make_server(on_denied=spy)` + 名单外 IP 请求 → spy 收到 1 条）。

## 7. P1-6　查询记录留住，而且更好用（原方案「收敛明文」作废）

**你的决定**（2026-11-24）：同事的查询记录要保留。所以**不改 `q` 的落法**：
`server.py:302` 继续 `q=q` 全量明文，一份同事的检索史（现库 26 条：乐平、玉田水库、水安全……）长期可查。

这条从"少记"改成"记好"，补三件事：

1. **补两个有用字段**，都在 `server.py:299-302` 现成变量里：

```python
rows, fell_back = self.index.search(q, params.get("dir") or "", limit_int, include_withdrawn=owner)
self._record(ip, name, route, 200, count=len(rows), fallback=fell_back,
             q=q, dir=params.get("dir") or "")
```

- `fallback`：这一查是不是退化成了 `LIKE`（两字词、分词失败时）。事后看 "为什么慢/为什么命中怪" 一眼定位。
- `dir`：在哪个目录下查的（空串 = 全库）。判断"同事只会在某个资料夹里翻"很有用。

2. **轮转不再覆盖旧记录**（现在 `Audit._rotate`：`access_log.jsonl` → `.1`，第二次轮转把上一代删掉，
   等于只留最近 ~3.4 万次请求，更早的查询记录会被吃掉）：

```python
# server.py:85 _rotate：改名带时间戳，永不覆盖、永不删除
rotated = self.path.with_name(f"{self.path.stem}-{time.strftime('%Y%m%d-%H%M%S')}{self.path.suffix}")
```

体量估算：26 KB / 231 请求 ≈ 113 B/请求，一个月按 2000 次请求算约 0.2 MB，一年 2~3 MB——
不设保留上限，需要清理时手工删 `logs/access_log-*.jsonl`。README 的「日志」段写一句。

3. **0 命中的查询要能单独捞出来**（不改格式，靠字段组合就行）：
   `count: 0` 的行就是"同事查了、库里没有"，是资料缺口线索。README 给一条现成命令：

```powershell
Select-String -Path logs\access_log-*.jsonl,logs\access_log.jsonl -Pattern '"count": 0' |
  ForEach-Object { $_.Line }
```

**不做**：不加 `q_len`、不缩短保留期、不加配置开关——查询记录本身就是你要的数据，
真要脱敏也该在"给别人看日志"那一步做，而不是在写入时丢。

**测试**：`test_search_record_carries_query_fallback_and_dir`（有明文 `q`、`fallback`、`dir`）、
`test_access_log_rotates_without_overwriting`（连滚两次，两个归档文件都在）；
既有 `test_audit_written_per_request`（`tests/test_server.py:346`）只查 `/api/stats`，不受影响；
`test_audit_rotates_instead_of_growing_forever`（:368）断言要跟着改成"归档文件存在 + 原文件变小"。

## 8. 测试汇总

| # | 文件 | 用例 | 断言要点 |
|---|---|---|---|
| 1 | `tests/test_app.py` | `test_stop_writes_app_log` | 「库服务停止：托盘退出」 |
| 2 | `tests/test_app.py` | `test_stop_twice_logs_once` | 1 行 |
| 3 | `tests/test_app.py` | `test_crash_hook_records_uncaught_exception` | 「未捕获异常（主线程）」 |
| 4 | `tests/test_app.py` | `test_thread_crash_is_logged` | 线程名出现在行内 |
| 5 | `tests/test_app.py` | `test_update_phase_changes_are_logged` | 同 phase 不重复 |
| 6 | `tests/test_app.py` | `test_install_is_logged_before_stopping` | 「开始安装」+「启动更新器失败」 |
| 7 | `tests/test_app.py` | `test_failure_details_are_logged` | rel 与 reason 都在 |
| 8 | `tests/test_app.py` | `test_failure_details_are_capped_at_three` | 3 条 +「等 5 篇」 |
| 9 | `tests/test_app.py` | `test_denied_visits_are_logged_once_per_window` | 5 次 → 1 行 |
| 10 | `tests/test_panel.py` | `test_denied_visits_show_in_shared_scope_row` | 值里含「访问被拒」 |
| 11 | `tests/test_server.py` | `test_make_server_passes_denied_callback` | spy 收到 403 |
| 12 | `tests/test_server.py` | `test_search_record_carries_query_fallback_and_dir` | 明文 `q` + `fallback` + `dir` |
| 13 | `tests/test_server.py` | `test_access_log_rotates_without_overwriting` | 连滚两次，两代归档都在 |

合计新增 13 条 → 期望 **576 passed / 17 skipped**（当前基线 563）。

## 9. 落地后 `app.log` 长什么样（样例）

```
[2026-11-24 09:00:02] 库服务启动：端口 8767，版本 0.2.22
[2026-11-24 09:00:41] 一轮结束：收录 0 篇、失败 2 篇、尝试 2 篇、剩余 0 篇，用时 3 秒（done）
[2026-11-24 09:00:41] 失败明细：资料/坏.docx（unreadable）、资料/带口令.docx（password_protected）
[2026-11-24 09:03:12] 更新：发现新版本 v0.3.1：发现新版本 v0.3.1
[2026-11-24 09:03:14] 更新：新版本已下载好，等待升级 v0.3.1
[2026-11-24 09:07:55] 访问被拒：401 /api/search，来源 10.91.1.178（不在名单）
[2026-11-24 09:31:00] 更新：开始安装 v0.3.1（先停服务）
[2026-11-24 09:31:01] 库服务停止：安装更新（本次收录 3 篇）
[2026-11-24 09:58:11] 未捕获异常（landoc-worker 线程）：RuntimeError: COM 挂了
[2026-11-24 09:58:11] 库服务停止：托盘退出（本次收录 0 篇）
```

## 10. 明确不做（P2，留到以后再议）

- 级别前缀（INFO/WARN）、ISO 带时区、毫秒、`duration`/UA/request id：要同时动两份日志格式和 skill 侧解析，收益低。
- 启动环境行（库名/文件夹数/网段/OCR/库大小/WAL 大小）：排障时 `landoc status` 现查更快。
- WAL 104 MB 健康提示（现在 `library.db-wal` 确实这么大）：属于库维护/3.10，单独开。
- 本机回环请求占 67% 的抑制：主人自己调试也要看，不抑制；只是面板上不显示。
- 查询记录的脱敏/裁剪：你要的是"留住记录"，所以写入侧一律不裁剪；真要给别人看日志，另做导出。
- 旧 `%LOCALAPPDATA%\LanDocShare\logs\update.log`：手工删，不写代码。

## 11. 验收

- `python -m pytest tests -q` → 期望 **576 passed / 17 skipped**。
- 手测四件：
  1. 托盘右键退出 → `app.log` 有「库服务停止：托盘退出」；`taskkill /F` 则**没有**这一行（正好用来区分正常退出与被杀）。
  2. 临时让 `pipeline.run` 抛异常（或用 `--debug` 版改一行）→ 有「未捕获异常（landoc-worker 线程）」。
  3. 用名单外的机器访问 → 面板「共享范围」行出现「最近有 1 次访问被拒（IP 时间）」，`app.log` 有「访问被拒」。
  4. 搜一个命中词 → `access_log.jsonl` 该行有明文 `q`、`count`、`fallback: false`；把 `access_log.jsonl` 改到超过 4 MB（或临时调小 `Audit(max_bytes=…)`）验证轮转出带时间戳的归档文件、旧记录没被覆盖。

## 12. 待你拍板（一处已定）

1. **「最近 N 次被拒」的计数口径**：建议"本次启动以来"（重启归零、明细在日志里），
   还是改成"最近 24 小时"（更贴近"今天有人一直试"的直觉，但要读 jsonl 汇总）。
2. **崩溃 hook 要不要也写一条 `access_log`**：建议不写——两份日志职责分开，混着查更慢。

已定：**查询记录全量保留明文 `q`**（你 2026-11-24 的决定），第 7 节改为"记好"而不是"少记"；
轮转改为带时间戳归档、不覆盖旧档。

## 13. 落实情况（2026-11-24 实施完毕）

### 13.1 改动的文件

| 文件 | 改了什么 |
|---|---|
| `landoc/app.py` | `install_crash_hooks`（`sys.excepthook` + `threading.excepthook`）；`AppStatus.denied/denied_note` 与 `summary()` 追加「N 次访问被拒」；`stop(reason)` 幂等 + 停止日志；升级先记「开始安装」再停服务、失败记「启动更新器失败」；`_log_update_phase` / `_log_failure_details` / `_note_denied` 三个新方法；`make_server(..., on_denied=self._note_denied)` |
| `landoc/tray.py` | 三处 `app.stop()` 带原因：托盘退出 / 命令行退出 / 改完建库设置退出 |
| `landoc/server.py` | `Audit.__init__(..., on_denied=None)`、`DENIED_STATUSES=(401,403,429)`；`_rotate()` 改成带时间戳归档（同名加序号，不删旧档）；`/api/search` 记录补 `fallback`/`dir`；`make_server(..., *, on_denied=None)` |
| `landoc/panel.py` | `status_rows` 在「共享范围」行追加「· 最近有 N 次访问被拒（IP 时间）」 |
| `README.md` | 「日志与失败文件」整节重写：五类新记录、归档式轮转、`count: 0` 的 PowerShell 捞法；回滚那段的 `update.log` 路径改正 |
| 测试 | 13 条新增 + 1 条改写（见 13.2） |

### 13.2 测试（实际命名，与第 8 节略有出入）

`tests/test_app.py`（9 条）：`test_stop_is_logged_with_a_reason`、`test_stop_twice_logs_one_line`、`test_crash_in_main_thread_is_logged`、`test_crash_in_a_worker_thread_is_logged`、`test_update_phase_changes_are_logged_once_each`、`test_install_is_logged_before_the_service_stops`、`test_failure_details_are_logged`、`test_failure_details_stop_at_three`、`test_denied_visits_are_logged_once_per_window`。

`tests/test_panel.py`（1 条）：`test_status_rows_show_denied_visits_in_scope`。

`tests/test_server.py`（3 条新增 + 1 条改写）：`test_make_server_passes_the_denied_callback`、`test_denied_request_notifies_the_app`、`test_search_record_carries_query_fallback_and_dir`；原 `test_audit_rotates_instead_of_growing_forever` 改写为 `test_access_log_rotates_without_overwriting`（现在断言「归档不覆盖、50 条一条不少」）。

### 13.3 与清单的偏离

1. 第 8 节第 11 条原写「spy 收到 403」：拆成两条——`make_server` 只断言回调确实挂到了 handler 的 `Audit` 上；真正的 401/403 通知由 `test_denied_request_notifies_the_app` 直调 `Audit.log` 验证（200 不惊动状态窗口）。
2. `Audit(on_denied=seen.append)` 没法用 `is` 比：每次取 `seen.append` 都是新的 bound method 对象，测试里改成普通函数。
3. 失败明细两条用例的 runner 必须**第二轮返回空 `RunReport()`**：`_drain()` 会一直跑到"没有新增"才收工，`runner` 恒定返回同一份带 `attempted` 的报告就是死循环（踩过，测试挂起 10 分钟）。
4. `_UPDATE_PING_GAP` 原来写在 `__init__` 附近，与 `_DENIED_LOG_GAP` 一起提到 `LibraryApp` 类头做类常量（只保留一份）。
5. 第一版把 `denied_note` 的注释示例写成真实网段 `10.91.1.178`，被 `tests/test_site_networks.py::test_no_real_network_is_baked_into_the_source` 当场抓到（该用例扫 `landoc/**/*.py`、禁 `10.91.`）→ 改成文档示例地址 `192.0.2.9`。

### 13.4 仍是默认值、等你改主意的两处

- 被拒计数口径：**本次启动以来**（`_denied_count` 进程内累加，重启归零），明细在 `app.log`；要「最近 24 小时」得改成读 `access_log` 汇总。
- 崩溃 hook **不写** `access_log`：两份日志职责分开（一处看运行、一处看谁查了什么）。

### 13.5 验收

- `python -m pytest tests -q` → **576 passed / 17 skipped in 214.36s**（第 3 批后 563 + 本批 13 条，一次通过；期间只修了自己写的两处测试与一条被脱敏用例抓到的注释）。
- 手测四件（第 11 节）尚未在打包版上跑：托盘退出 / 崩溃 / 名单外访问 / 轮转归档，等下一次发布前连同第 3 批的 dist 一起验。
