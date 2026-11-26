"""库配置：`<lib_dir>/config.json` 的读写（原子写）。"""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

#: 占位值：源码里不留任何单位的具体网段。真正的默认值由向导/CLI 现算
#: （`wizard.default_lan_cidrs()` = 本机所在网段），见 README「谁能访问」。
DEFAULT_ALLOW_CIDRS = ["192.168.0.0/16"]
CONFIG_NAME = "config.json"
# 配置文件的语义版本：只用于"这个字段在新版里含义变了"时的迁移钩子，不参与鉴权/比对。
# 加字段不用升；只有当同一个键的语义变了（比如 allow_cidrs 从"额外放行"改成"唯一口径"）才升。
CONFIG_VERSION = 1


@dataclass
class Config:
    """一个资料库的配置。

    folders 是库主人手动多选的共享目录；allow_cidrs 是允许访问的来源网段，
    默认只对规划院内网开放；deny_cidrs 是显式拒绝的来源网段（优先于 allow_cidrs，
    用来"整个网段放行、只挡掉其中几台"）；allow_people 是可选叠加的"姓名 → IP"名单；
    people_departments 是同一批人的"姓名 → 部门/备注"，只用于分组查看，不参与授权。

    store_content / render_md 是"收紧共享"的两个隐私开关（设计 §6.6，默认都开）：
    - store_content=True 全文进索引；False 只存前 `snippet_only_chars` 字，检索与片段都只在这段里命中；
    - render_md=True 在 `md/` 留一份 Markdown 副本（`/api/tables` 优先读它）；
      False 不落副本，表格改从索引里的正文抽（正文被上面那个开关截断时表格也可能被截断）。

    snippet_only_chars 是 store_content=False 时的前缀长度：越大越保检索召回，越小越防
    "整库明文被拷走"。本机的取舍是**召回优先**，所以默认给到 20000 字（多数简报/汇报/规程
    可整篇保留），而不是一个抠搜的小数字。改开关后重跑 `convert` 就会按新策略收紧/补齐已有文档。

    registry_url 是登记中心地址（如 `http://172.16.1.197:8767`）：非空时库软件每 60 s
    心跳一次，中心据此下发"谁在线、谁的库有多少篇"；留空则只在本机局域网里被发现。

    check_updates / auto_download 是自动更新开关（设计 §9，默认都开）：启动后约半分钟
    问一次中心有没有新版本，有就在后台下好；**安装始终由用户点"升级"触发**，不静默替换。
    关掉 check_updates 后完全不去问中心，只在托盘菜单里手动"检查更新"。

    ocr_scanned_pdfs / ocr_dpi / ocr_max_pages 控制扫描件 OCR（没有文字层的 PDF）：
    默认开启，按 300 dpi 渲染、最多识别前 60 页。识别优先用 Windows 系统 OCR（随中文
    语言包提供，零体积），系统缺中文识别包时用随包带的 Tesseract 兜底；两条都没有时
    这类文件仍按"没有文字层"记失败。关掉开关就不会把任何页面渲染成图片，适合只收
    电子版文档、或不想让建库机多做 OCR 的机器。
    """

    name: str
    lib_dir: Path
    folders: list[Path] = field(default_factory=list)
    allow_cidrs: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOW_CIDRS))
    deny_cidrs: list[str] = field(default_factory=list)
    allow_people: dict[str, str] = field(default_factory=dict)
    people_departments: dict[str, str] = field(default_factory=dict)
    # 只有名单里的人能查（名单在托盘「授权名单…」里维护）。名单还空着时 =
    # 谁都查不到，不能悄悄退回"按网段放行"。
    roster_only: bool = False
    port: int = 8766
    registry_url: str = ""
    check_updates: bool = True
    auto_download: bool = True
    snippet_tokens: int = 48
    store_content: bool = True
    snippet_only_chars: int = 20000
    render_md: bool = True
    acrobat_tool_dir: Path | None = None
    ocr_scanned_pdfs: bool = True
    ocr_dpi: int = 300
    ocr_max_pages: int = 60

    # ---- 派生路径 ----
    @property
    def md_dir(self) -> Path:
        return Path(self.lib_dir) / "md"

    @property
    def db_path(self) -> Path:
        return Path(self.lib_dir) / "library.db"

    @property
    def config_path(self) -> Path:
        return Path(self.lib_dir) / CONFIG_NAME

    # ---- 序列化 ----
    def to_dict(self) -> dict:
        data = dataclasses.asdict(self)
        data["lib_dir"] = str(self.lib_dir)
        data["folders"] = [str(p) for p in self.folders]
        data["acrobat_tool_dir"] = str(self.acrobat_tool_dir) if self.acrobat_tool_dir else None
        # 版本号写在关键字后面，旧版 `from_dict` 会把它当未知键丢掉，不影响互认
        data["config_version"] = CONFIG_VERSION
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        known = {f.name for f in dataclasses.fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}
        kwargs["lib_dir"] = Path(kwargs["lib_dir"])
        kwargs["folders"] = [Path(p) for p in kwargs.get("folders", [])]
        tool = kwargs.get("acrobat_tool_dir")
        kwargs["acrobat_tool_dir"] = Path(tool) if tool else None
        # 目前只有 v1：读到更高版本不做任何猜测，原样按已知字段加载（迁移钩子留给将来）
        return cls(**kwargs)

    def save(self) -> None:
        """原子写：同目录临时文件 + os.replace，避免写坏配置。"""
        target = self.config_path
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".config", suffix=".tmp", dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    @classmethod
    def load(cls, lib_dir: Path) -> "Config":
        path = Path(lib_dir) / CONFIG_NAME
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))
