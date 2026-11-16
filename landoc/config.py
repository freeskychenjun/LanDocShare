"""库配置：`<lib_dir>/config.json` 的读写（原子写）。"""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ALLOW_CIDRS = ["10.91.0.0/16"]
CONFIG_NAME = "config.json"


@dataclass
class Config:
    """一个资料库的配置。

    folders 是库主人手动多选的共享目录；allow_cidrs 是允许访问的来源网段，
    默认只对规划院内网开放；allow_people 是可选叠加的"姓名 → IP"名单。
    """

    name: str
    lib_dir: Path
    folders: list[Path] = field(default_factory=list)
    allow_cidrs: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOW_CIDRS))
    allow_people: dict[str, str] = field(default_factory=dict)
    port: int = 8766
    snippet_tokens: int = 48
    store_content: bool = True
    render_md: bool = True
    acrobat_tool_dir: Path | None = None

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
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        known = {f.name for f in dataclasses.fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}
        kwargs["lib_dir"] = Path(kwargs["lib_dir"])
        kwargs["folders"] = [Path(p) for p in kwargs.get("folders", [])]
        tool = kwargs.get("acrobat_tool_dir")
        kwargs["acrobat_tool_dir"] = Path(tool) if tool else None
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
