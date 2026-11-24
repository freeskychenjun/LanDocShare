# 第 2 批改动（2026-11-24）

对应 `docs/optimization-plan-2026-11-23.md` 第六节的第 2 批：**1.1 清理与转换互斥**、**3.4 失败清单**。
第 3 条 3.6（心跳触发更新检查）已在第 1 批落地，本批不再动。

基线：0.2.22 + 第 1 批（HEAD `3aa2618`），`python -m pytest tests -q` → 553 passed / 17 skipped。
本批结束后：**562 passed / 17 skipped**（新增 9 条）。

---

## 1. 1.1 清理与建库互斥

### 1.1.1 一把库锁（`landoc/pipeline.py`）

```python
LIBRARY_LOCK = threading.RLock()
PRUNE_LOCK_TIMEOUT = 2.0
```

- `run()` 拆成 `run()`（持锁）+ `_run()`（实体，有「调用方必须先持锁」的文档串）：整轮转换都在锁里。
- `prune_gone()` 拆成 `prune_gone()`（`acquire(timeout=2.0)`，抢不到就抛 `PruneBusy`）+ `_prune_gone_locked()`。
- 为什么是「抢不到就报错」而不是「排队等」：等下去，Tk 线程会僵到界面无响应；报错时库一个字节都没动，
  重试成本只有一次点击。面板在建库期间把按钮置灰后，正常用户根本走不到这条路上。

`PruneBusy(RuntimeError)` 的文案：「建库正在进行，这一轮结束再清理（库没有改动）」。

### 1.1.2 `prune_gone` 不再吞错，且顺序反过来

原文（问题所在）：

```python
try:
    (Path(cfg.md_dir) / _md_rel(rel)).unlink()
except OSError:
    pass
try:
    index.delete(_md_rel(rel))
except Exception:  # 静默
    pass
files.pop(rel, None)   # 索引删失败也照样删 state
```

- 删不掉索引行时照样删 state → 索引里留下孤儿行、state 里连记录都没了，**这行永远清不掉**，
  而且还能被同事搜到（方案实测 `still searchable by colleagues: True`）。
- 现在：先 `index.delete()`（抛就往外抛，state 不动，下次还能清）→ 再 unlink md（只放过 `FileNotFoundError`）
  → `index.commit()` → 最后才删 state 条目并落盘。

### 1.1.3 面板：建库期间置灰（`landoc/panel.py`）

新增纯函数 `prune_button_state(status) -> (文字, 能不能点)`：

| 状态 | 按钮 |
|---|---|
| `gone == 0` | 「清理…」，disabled |
| `gone > 0` 且 `phase != "converting"` | 「清理 N 篇」，可点 |
| `gone > 0` 且 `phase == "converting"` | 「清理 N 篇」，**disabled** |

点下去撞上 `PruneBusy` 时用 `showinfo`（「这次没轮到」），不再报 `showerror`（不是错误）。

### 1.1.4 命令行：先要单实例互斥量（`landoc/cli.py`、`landoc/tray.py`）

- `landoc prune --gone` 先 `acquire_single_instance()`，托盘在跑就打印
  「LanDocShare 正在运行：先从托盘退出，再执行 prune --gone」并返回 **2**。
- 新增 `tray.release_single_instance()`，**用完必须放**：同进程第二次 `CreateMutexW` 会因为旧句柄
  返回 `ERROR_ALREADY_EXISTS`，被误判成「托盘在跑」（测试里连调两次就会踩到）。
- `pipeline.PruneBusy` 也在这里兜住，同样返回 2 —— 脚本里能用退出码区分「没清」和「清干净了」。

## 2. 3.4 失败清单跟文件走

`run()` 收尾原来只扫 `files`，`failures` 里的死记录一直挂着。现在补同样一轮：

| 情况 | 处理 |
|---|---|
| 文件夹还在共享列表、文件没了 | 记 `source_missing=True` + `missing_since` |
| 文件夹整个撤出共享列表 | 直接删掉这条失败记录 |

配套：

- `app._refresh_counts()` 数「失败 N 篇」时跳过 `source_missing` 的条目 —— 它既不会重试，也不该占着这句提示。
- `prune_gone` 除了 `files` 里的 `source_missing`，也认 `failures` 里的：库里本来就没有 md 副本，
  所以只忘掉记录、不删文件，也不计入返回值 `removed`（那句「已清理 N 篇」说的是真删掉的库内容）。

## 3. 新增测试（9 条）

| 文件 | 用例 |
|---|---|
| `tests/test_pipeline.py` | `test_prune_gone_refuses_while_a_round_is_in_flight`、`test_prune_gone_keeps_the_state_when_the_index_is_locked`、`test_a_round_after_the_prune_does_not_bring_entries_back`、`test_missing_failed_source_is_flagged_and_pruned`、`test_withdrawing_the_folder_forgets_its_failure_records` |
| `tests/test_prune.py` | `test_prune_gone_refuses_while_the_tray_is_running`、`test_prune_gone_reports_busy_without_touching_the_library` |
| `tests/test_panel.py` | `test_prune_button_is_disabled_while_building` |
| `tests/test_app.py` | `test_failed_count_ignores_sources_that_are_gone` |

三个「抢锁」用例都用**另一条线程**持锁：`LIBRARY_LOCK` 是 `RLock`，同线程再进一次是允许的，
在主线程里先 `acquire` 再调 `prune_gone` 会「成功」而不是超时（这条踩过，别改回去）。

## 4. 没做到 / 留给下一批

- 「命令行的跨进程互斥量」只有 monkeypatch 级单测：真正开两个进程抢互斥量没有自动化用例，
  需要手工验一次（托盘开着时跑 `landoc prune --gone`）。
- 1.1 的真机复现（方案里那三条实验输出）没有重跑；锁与置灰的语义由单测覆盖，行为上的
  「界面不卡、库账不复活」还差一次真机点击。
- 3.4 对真实库（20 条历史失败，源文件都还在）的观察：本批只保证「源文件真没了」的路径，
  那 20 条应当原样保留，不会被误清。
