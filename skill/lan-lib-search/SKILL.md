---
name: lan-lib-search
description: 检索院内同事们共享的个人资料库（LanDocShare）。当用户要查/搜/找院里的资料、历年报告、方案、规程、批复，问"谁那儿有…""院里有没有…的资料"，或提到共享资料库、同事的资料库、lan-lib-search 时使用。典型说法："查一下院里有没有马斯京根的资料""找找玉田水库的批复""全院搜一下水安全规划"。返回命中片段与表格数据，**不提供原文全文**。不适用：查实时水情用 realtime-hydro-query；查水文数值用 hydro-data-query；查本地 md 库用 md-knowledge-search；查数智水院内部资料用 design-query。
---

# 查院里的共享资料库（lan-lib-search）

院里每个同事的电脑上可能跑着一个 LanDocShare 资料库（只读检索服务），
统一在登记中心登记。本 skill 负责：**发现有哪些库在线** → **并发去每个库查** → **合并成一份结果**。

## 快速开始

```bash
python scripts/query.py libs            # 谁对你开放、各多少篇
python scripts/query.py search 马斯京根   # 对你开放的库一起查
python scripts/query.py search 玉田水库 --lib 陈鋆
python scripts/query.py tables --lib 陈鋆 --path "2026年项目/玉田水库除险加固初步设计.docx.md"
```

脚本只用标准库（Python 3.8+ 可直接跑），不需要 pip 装任何东西。

## 怎么用（给 AI 的流程）

1. **先看名单**（可选，但库多时有用）：`query.py libs`，知道**谁对你开放**、各多少篇。
   清单已经按你的来源 IP（和 `identity.txt` 里的姓名）过滤过，看不到没给你权限的库。
2. **查**：`query.py search "<关键词>"`。查询词 **2–4 个、越具体越好**；
   命中太少就换更短的核心词（如"马斯京根"而不是"马斯京根洪水演算方法"）。
3. **读结果**：输出按库分组，每条带 `库主人` + `文档标题` + `相对路径` + `命中片段`（片段里 `<<词>>` 是命中处）。
4. **要表格**：用户追问某篇里的数据表时，用第 4 步的 `--path` 值调 `tables`；
   返回的是表格的 Markdown，不是原文。
5. **溯源**：回答里**必须**带上"来自谁的库 + 文档路径"。不提供原文、不提供下载——
   需要原文让用户线下找那位同事要。

## 姓名与授权

授权只按 **IP** 认人（库主人维护名单），姓名只用于审计/分组，**不能当作口令**。
`/api/meta` 的 `name_required` 恒为 false（旧字段，仅为兼容保留）。
只有当某个库返回下列 401 时才需要处理：

- `name has no IP registered`：你报的名字在名单里但没登记 IP——姓名不能换访问权，
  让库主人把你的 IP 补进名单；不要换别人的名字重试。
- `ip not registered`：这台机器的 IP 不在任何库主人的名单里 → 告诉用户"需要库主人把你这台机器的 IP 加进名单"，
  不要反复重试、不要猜别人的名字。
- `ip mismatch for name` / `name not registered`：报的名字与 IP 对不上 → 让用户确认登记的姓名/IP，
  或干脆不要报姓名（授权只看 IP）。

查询时脚本会自动带上 `identity.txt` 里的名字（如果登记过），但姓名不参与放行。

## 结果不满意的处理

- 0 条：先换更短的词重试一次（2 个字也能查，走的是子串兜底）；
  还不行就说明库里确实没有，如实说"在线 N 个库都没命中"，不要编。
- 部分库"未响应"：输出脚注会写"在线 X/Y 个库返回结果"，原样转述，不要当成"没有"。
- 输出里有 `（某某 未响应：timeout）`：那个库关机/休眠了，提一句即可。

## 没有 Python 时的降级（给 AI 手工执行）

```bash
# 1) 名单（带了 name 就按姓名+IP 过滤；不带只按 IP）
curl -s "http://10.91.1.197:8767/libraries?name=<你的姓名，URL 编码>"
# 2) 逐库查（把 <ip>:<port> 换成分名单里的 url；q 用 UTF-8 URL 编码）
curl -s "http://<ip>:<port>/api/search?q=%E9%A9%AC%E6%96%AF%E4%BA%AC%E6%A0%B9&limit=8&name=<你的姓名，URL 编码>"
# 3) 表格
curl -s "http://<ip>:<port>/api/tables?path=<URL 编码后的相对路径>&name=<你的姓名，URL 编码>"
```

拿到几个库的 JSON 后，AI 自己按"库内相关度归一化"合并、按库分组输出即可。

## 配置（一般不用改）

- 登记中心默认 `http://10.91.1.197:8767`；换地址有三种方式（优先级从高到低）：
  环境变量 `LANLIB_REGISTRY`、`%LOCALAPPDATA%\lan-lib-search\config.json` 里的 `"registry"`、脚本里的默认值。
- `config.json` 还可以给某个库加权重：`{"weights": {"陈鋆": 1.3}}`（同分时让某人的库排前面）。
- 缓存 `cache_libraries.json` 24 h 有效，只在中心连不上时启用（并逐库探活）。
  **带身份查询时缓存按姓名分成 `cache_libraries_<hash>.json`**，换了姓名不会拿别人的清单。
- 数据目录默认 `%LOCALAPPDATA%\lan-lib-search`，可用 `LANLIB_HOME` 改。

## 安全边界（别越界）

- 只读：没有写接口、没有全文接口、没有下载接口。
- 每个库只返回**命中片段**（约 2×snippet_tokens 字）和**表格**；不要试图拼出全文。
- 不要遍历、不要扫库、不要猜路径：`/api/tables` 只对索引里的路径生效。
