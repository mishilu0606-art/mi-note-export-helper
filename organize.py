#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mi-note-vault / organize.py
把 mi-note-export 导出的原始 Markdown，整理成可直接作为 Obsidian vault 打开的目录。

设计原则：
  1. 默认只"体检 + 出报告"，绝不擅自改动文件。确认无误后加 --apply 才落地。
  2. 所有"删除"一律降级为"移动到 _trash/"，随时可撤回。
  3. 优先复用已有分类文件夹，不擅自新建。

用法：
  python organize.py <input_dir>                                  # 体检，只出报告
  python organize.py <input_dir> --out <vault_dir> --apply        # 确认后真正整理
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import shutil
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------- 常量

DEFAULT_MIN_CHARS = 20          # 低于此字数且无图 → 视为碎片
SENSITIVE_DIR = "_sensitive"    # 疑似含隐私 → 隔离待人工确认
TODO_DIR = "_todo"              # 待办类
FRAGMENT_DIR = "_fragments"     # 碎片
INBOX_DIR = "_inbox"            # 无法归类的根目录笔记
TRASH_DIR = "_trash"            # 重复件回收站（可恢复）
ASSET_DIR = "_assets"
REPORT_DIR = "_reports"
CORRUPT_DIR = "_corrupted"      # 0 字节 / 文件头损坏的附件（原文件仍在 output/assets，可重导）

# 敏感信息正则：命中即隔离，宁可多抓不可放过
# 注意：不能用 \b —— 中文也是 \w 字符，"手机号13800138000" 中数字前没有边界，\b 会漏判。
# 统一改用 (?<!\d) / (?!\d) 只约束数字边界。
SENSITIVE_PATTERNS: list[tuple[str, str]] = [
    ("身份证号", r"(?<!\d)\d{17}[\dXx](?!\d)"),
    ("手机号", r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    ("银行卡号", r"(?<!\d)\d{16,19}(?!\d)"),
    ("邮箱", r"[\w.+-]+@[\w-]+\.[\w.]{2,}"),
    ("密码/密钥", r"(密码|passwd|password|pwd|验证码|token|密钥|私钥|api[_\- ]?key)\s*[:：=]?\s*\S{4,}"),
    ("详细地址", r"[\u4e00-\u9fa5]{2,7}(省|市|自治区)[\u4e00-\u9fa5\d]{2,12}(区|县|市)[\u4e00-\u9fa5\d]{2,20}(路|街|道|号|栋|幢|单元|室)"),
]

TODO_RE = re.compile(r"^\s*[-*+]\s*\[([ xX])\]", re.M)
IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
FOLDER_NUM_PREFIX = re.compile(r"^\d+[\s._\-]*")   # "1读书笔记" -> "读书笔记"


# ---------------------------------------------------------------- 数据模型

@dataclass
class Note:
    path: Path
    rel: str
    title: str
    folder: str
    text: str
    images: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    hits: list[str] = field(default_factory=list)
    target: str = ""

    @property
    def n_chars(self) -> int:
        return len(re.sub(r"\s+", "", self.text))

    @property
    def todo_open(self) -> int:
        return len([m for m in TODO_RE.finditer(self.text) if m.group(1).strip() == ""])

    @property
    def todo_done(self) -> int:
        return len([m for m in TODO_RE.finditer(self.text) if m.group(1).strip() != ""])

    def norm(self) -> str:
        """规范化文本，用于判重：忽略空白与标点差异"""
        t = IMG_RE.sub("", self.text)
        t = re.sub(r"\s+", "", t)
        t = re.sub(r"[，。,.;；:：!！?？、\"'`~—\-_*#>]", "", t)
        return t.lower()

    def digest(self) -> str:
        return hashlib.sha256(self.norm().encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 扫描

def scan(root: Path) -> list[Note]:
    notes: list[Note] = []
    for p in sorted(root.rglob("*.md")):
        rel = p.relative_to(root).as_posix()
        if rel.startswith("_"):          # 跳过上一轮产生的 _trash / _reports 等
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            print(f"[warn] 读取失败 {p}: {e}", file=sys.stderr)
            continue
        folder = p.parent.relative_to(root).as_posix()
        folder = "" if folder == "." else folder
        images = [m.group(2) for m in IMG_RE.finditer(text)]
        notes.append(
            Note(path=p, rel=rel, title=p.stem, folder=folder, text=text, images=images)
        )
    return notes


def existing_folders(root: Path) -> list[str]:
    """已有分类文件夹。assets 是附件目录不是分类，必须排除"""
    skip = {"assets", ".obsidian", ".git", ".trash"}
    return sorted(
        d.name for d in root.iterdir()
        if d.is_dir() and not d.name.startswith("_") and d.name.lower() not in skip
    )


def resolve_asset(root: Path, note_path: Path, url: str) -> Path | None:
    """
    定位图片真实位置。mi-note-export 把附件统一放在根目录 assets/，
    但子文件夹里的笔记引用可能写成相对自身路径，因此这里做多重兜底。
    """
    if url.startswith(("http://", "https://", "data:")):
        return None
    name = Path(url).name
    for cand in (
        note_path.parent / url,
        note_path.parent / name,
        root / url,
        root / "assets" / name,
        root / ASSET_DIR / name,
    ):
        if cand.exists():
            return cand
    for p in root.rglob(name):          # 最后兜底：全目录搜文件名
        return p
    return None


# ---- 自动归类：字符 bigram + IDF 加权 ----

def bigrams(s: str) -> set[str]:
    s = re.sub(r"\s+", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)}


def build_profiles(notes: list[Note], folders: list[str], top: int = 60) -> dict[str, set[str]]:
    """为每个已有分类抽取特征 bigram，作为归类的"指纹" """
    tf: dict[str, dict[str, int]] = {f: defaultdict(int) for f in folders}
    df: dict[str, int] = defaultdict(int)
    for n in notes:
        if n.folder in tf:
            for b in bigrams(n.text):
                tf[n.folder][b] += 1
                df[b] += 1
    profiles: dict[str, set[str]] = {}
    for f, counts in tf.items():
        if not counts:
            continue
        scored = sorted(
            ((c * (1 + math.log((len(notes) + 1) / (1 + df[b]))), b) for b, c in counts.items()),
            reverse=True,
        )
        profiles[f] = {b for _, b in scored[:top]}
    return profiles


def match_score(profile: set[str], text: str) -> float:
    bs = bigrams(text)
    return len(bs & profile) / len(bs) if bs else 0.0


# ---------------------------------------------------------------- 各阶段判定

def stage_sensitive(notes: Iterable[Note], extra: list[str]) -> None:
    pats = SENSITIVE_PATTERNS + [(f"自定义{i}", p) for i, p in enumerate(extra)]
    for n in notes:
        for label, pat in pats:
            if re.search(pat, n.text, re.I):
                n.flags.append("sensitive")
                n.hits.append(label)
                break


def load_modify_dates(root: Path) -> dict[str, float]:
    """读 mi-note-export 的 .sync-state.json，建 相对路径 → modifyDate 映射。
    文件不存在或结构对不上就返回空表（去重退化为按字数）。"""
    sf = root / ".sync-state.json"
    try:
        data = json.loads(sf.read_text(encoding="utf-8"))
        out: dict[str, float] = {}
        for v in (data.get("notes") or {}).values():
            fp, md = v.get("filePath"), v.get("modifyDate")
            if not fp or md is None:
                continue
            p = Path(fp)
            try:
                rel = str(p.relative_to(root))
            except ValueError:
                rel = str(p)
            out[rel] = float(md)
        return out
    except Exception:
        return {}


def stage_dedupe(notes: list[Note], mtimes: dict[str, float] | None = None) -> None:
    """同内容组保留一件：优先「云端修改时间最新」（sync-state.modifyDate），
    拿不到时间再退回「字数最多」。其余判重复进回收站。"""
    mtimes = mtimes or {}

    def rank(n: Note):
        return (-mtimes.get(n.rel, 0.0), -n.n_chars, n.rel)

    seen: dict[str, Note] = {}
    for n in sorted(notes, key=rank):
        d = n.digest()
        if d in seen:
            n.flags.append("duplicate")
            n.hits.append(f"同 {seen[d].rel}")
        else:
            seen[d] = n


def stage_todo(notes: Iterable[Note], drop_done: bool) -> None:
    for n in notes:
        if n.todo_open == 0 and n.todo_done == 0:
            continue
        if n.todo_open == 0 and n.todo_done > 0 and drop_done:
            n.flags.append("todo-done")
        elif n.todo_open > 0:
            n.flags.append("todo")


def stage_fragment(notes: Iterable[Note], min_chars: int) -> None:
    for n in notes:
        if n.n_chars < min_chars and not n.images:
            n.flags.append("fragment")


def core_name(folder: str) -> str:
    """去掉数字前缀，便于 '1读书笔记' 与正文里的 '读书笔记' 匹配"""
    return FOLDER_NUM_PREFIX.sub("", folder).strip()


def stage_merge(notes: list[Note], folders: list[str], keyword_map: dict[str, str],
                threshold: float) -> None:
    """给根目录（无文件夹）的笔记找一个归宿；优先复用已有分类"""
    if not folders:
        return
    profiles = build_profiles(notes, folders)
    for n in notes:
        if n.folder:                      # 已在分类里，不动
            continue
        hit, reason = None, ""
        # 1) 用户显式关键词映射，最准
        for kw, target in keyword_map.items():
            if kw in n.title or kw in n.text:
                hit, reason = target, f"关键词「{kw}」"
                break
        # 2) 分类名字面重合
        if not hit:
            for f in folders:
                c = core_name(f)
                if len(c) >= 2 and (c in n.title or c in n.text):
                    hit, reason = f, "分类名命中"
                    break
        # 3) bigram 指纹相似度
        if not hit:
            best, bs = "", 0.0
            for f, prof in profiles.items():
                s = match_score(prof, n.text)
                if s > bs:
                    best, bs = f, s
            if best and bs >= threshold:
                hit, reason = best, f"相似度 {bs:.2f}"
        if hit:
            n.target = hit
            n.hits.append(f"归入 {hit}（{reason}）")
        else:
            n.target = INBOX_DIR
            n.hits.append("未能归类")


def decide(notes: list[Note]) -> None:
    """按优先级决定最终去向：敏感 > 重复 > 待办 > 碎片 > 归类"""
    priority = {
        "sensitive": SENSITIVE_DIR,
        "duplicate": TRASH_DIR,
        "todo": TODO_DIR,
        "todo-done": TODO_DIR,
        "fragment": FRAGMENT_DIR,
    }
    for n in notes:
        for flag, dest in priority.items():
            if flag in n.flags:
                n.target = dest
                break
        if not n.target:                  # 没被标记：有分类的留原地，否则进 _inbox
            n.target = n.folder or INBOX_DIR
        # 清理与最终去向矛盾的提示
        if n.target == INBOX_DIR:
            n.hits = [h for h in n.hits if not h.startswith("归入")] or ["未能归类"]
        else:
            n.hits = [h for h in n.hits if h != "未能归类"]


# ---------------------------------------------------------------- 落地

def fix_links(text: str, link_style: str) -> str:
    def repl(m: re.Match) -> str:
        alt, url = m.group(1), m.group(2)
        if url.startswith(("http://", "https://", "data:")):
            return m.group(0)
        name = Path(url).name
        if link_style == "wiki":
            return f"![[{name}]]" + (f" {alt}" if alt else "")
        return f"![{alt}]({ASSET_DIR}/{name})"

    return IMG_RE.sub(repl, text)


# 常见图片格式的文件头魔数；下载中断的坏文件往往 0 字节或头部不对
_IMG_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF8", b"RIFF")
_IMG_SUFFIX = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def is_broken_image(p: Path) -> bool:
    """附件健康检查：0 字节，或图片后缀与文件头对不上 → 视为损坏。"""
    try:
        if p.stat().st_size == 0:
            return True
        if p.suffix.lower() not in _IMG_SUFFIX:
            return False
        with open(p, "rb") as f:
            head = f.read(16)
        return not head.startswith(_IMG_MAGIC)
    except OSError:
        return True


def apply_all(root: Path, out: Path, notes: list[Note], link_style: str) -> dict:
    stats = defaultdict(int)
    out.mkdir(parents=True, exist_ok=True)

    for n in notes:
        dest_dir = out / n.target if n.target else out
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / n.path.name
        # 同名冲突自动加后缀
        i = 1
        while dest.exists():
            dest = dest_dir / f"{n.path.stem}_{i}{n.path.suffix}"
            i += 1

        text = fix_links(n.text, link_style)
        # 补一段 YAML frontmatter，方便 Obsidian 检索
        fm = [
            "---",
            f"title: {n.title}",
            f"source: {n.rel}",
            f"folder: {n.folder or '（根目录）'}",
            f"chars: {n.n_chars}",
            f"organized_at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        ]
        if n.hits:
            fm.append(f"hits: {'; '.join(n.hits)}")
        fm.append("---")
        dest.write_text("\n".join(fm) + "\n\n" + text, encoding="utf-8")
        stats[n.target or "（根目录）"] += 1

        # 图片统一收拢到 _assets；坏文件（0 字节 / 魔数不符）挪 _corrupted
        for img in n.images:
            src = resolve_asset(root, n.path, img)
            if src is None:
                stats["（图片缺失）"] += 1
                continue
            if is_broken_image(src):
                cdir = out / CORRUPT_DIR
                cdir.mkdir(parents=True, exist_ok=True)
                tgt = cdir / src.name
                i = 1
                while tgt.exists():
                    tgt = cdir / f"{src.stem}_{i}{src.suffix}"
                    i += 1
                shutil.copy2(src, tgt)
                stats["（图片损坏）"] += 1
                continue
            adir = out / ASSET_DIR
            adir.mkdir(parents=True, exist_ok=True)
            tgt = adir / src.name
            if not tgt.exists():
                shutil.copy2(src, tgt)
            stats["（图片）"] += 1

    # 敏感目录防裸奔：同步进 git / iCloud 前先拦一道
    if any(n.target == SENSITIVE_DIR for n in notes):
        sdir = out / SENSITIVE_DIR
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / ".gitignore").write_text("*\n!/.gitignore\n", encoding="utf-8")

    return dict(stats)


# ---------------------------------------------------------------- 报告

def write_reports(out: Path, notes: list[Note], stats: dict, folders: list[str]) -> None:
    rdir = out / REPORT_DIR
    rdir.mkdir(parents=True, exist_ok=True)

    counts = defaultdict(int)
    for n in notes:
        for f in n.flags:
            counts[f] += 1

    lines = [
        "# 便签整理报告",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 扫描笔记：**{len(notes)}** 条",
        f"- 已有分类：{('、'.join(folders)) if folders else '（无）'}",
        "",
        "## 处置统计",
        "",
        "| 判定 | 数量 | 去向 |",
        "| --- | --- | --- |",
    ]
    label = {
        "sensitive": ("疑似含隐私", SENSITIVE_DIR),
        "duplicate": ("内容重复", TRASH_DIR),
        "todo": ("待办事项", TODO_DIR),
        "todo-done": ("已完成待办", TODO_DIR),
        "fragment": ("内容碎片", FRAGMENT_DIR),
    }
    for k, (zh, dest) in label.items():
        if counts.get(k):
            lines.append(f"| {zh} | {counts[k]} | `{dest}/` |")
    inbox = sum(1 for n in notes if n.target == INBOX_DIR)
    if inbox:
        lines.append(f"| 未能归类 | {inbox} | `{INBOX_DIR}/` |")
    lines += ["", "## 落地统计", "", "| 目标目录 | 文件数 |", "| --- | --- |"]
    for k, v in sorted(stats.items()):
        lines.append(f"| `{k}` | {v} |")

    lines += [
        "",
        "## 重要提醒",
        "",
        f"- 所有「删除」都只是移动到 `{TRASH_DIR}/`，**可以随时拖回来**，没有真正删文件。",
        f"- `{SENSITIVE_DIR}/` 里的内容只是**疑似**敏感，请人工过一遍再决定。",
        f"- 已在 `{SENSITIVE_DIR}/.gitignore` 写入屏蔽规则，但 **Obsidian 同步到 iCloud / 网盘时 .gitignore 不生效**——"
        "含隐私内容建议开启加密插件或加密存储后再同步。",
        f"- `{CORRUPT_DIR}/` 里是 0 字节或文件头损坏的附件（原文件仍在 output/assets/），"
        "重跑一次导出即可尝试补全。",
        f"- `{INBOX_DIR}/` 是需要你手动归类的，建议先看这里。",
    ]
    (rdir / "report.md").write_text("\n".join(lines), encoding="utf-8")

    with (rdir / "actions.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["原路径", "标题", "字数", "标记", "命中", "目标目录"])
        for n in notes:
            w.writerow([n.rel, n.title, n.n_chars, "|".join(n.flags) or "-",
                        "; ".join(n.hits) or "-", n.target or "（根目录）"])


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="整理 mi-note-export 的输出为 Obsidian vault")
    ap.add_argument("input", help="mi-note-export 的输出目录")
    ap.add_argument("--out", help="整理后的 vault 目录，默认 <input>_vault")
    ap.add_argument("--apply", action="store_true", help="真正写盘（默认只出报告）")
    ap.add_argument("--rules", help="自定义规则 JSON")
    ap.add_argument("--min-chars", type=int, default=DEFAULT_MIN_CHARS, help="碎片字数阈值")
    ap.add_argument("--classify-threshold", type=float, default=0.05,
                    help="自动归类相似度阈值，调高更保守（默认 0.05）")
    ap.add_argument("--link-style", choices=["wiki", "relative"], default="wiki", help="图片链接风格")
    ap.add_argument("--keep-done-todo", action="store_true", help="保留已完成的待办")
    ap.add_argument("--verbose", "-v", action="store_true", help="逐条打印判定结果与去向")
    args = ap.parse_args()

    root = Path(args.input).expanduser().resolve()
    if not root.is_dir():
        print(f"[error] 目录不存在：{root}", file=sys.stderr)
        return 1
    out = Path(args.out).expanduser().resolve() if args.out else root.parent / f"{root.name}_vault"

    rules = {}
    if args.rules and Path(args.rules).exists():
        rules = json.loads(Path(args.rules).read_text(encoding="utf-8"))

    notes = scan(root)
    if not notes:
        print("[error] 没扫到任何 .md 文件", file=sys.stderr)
        return 1
    folders = existing_folders(root)

    # 判定顺序即优先级
    stage_sensitive(notes, rules.get("sensitive_patterns", []))
    stage_dedupe(notes, load_modify_dates(root))
    stage_todo(notes, drop_done=not args.keep_done_todo)
    stage_fragment(notes, args.min_chars)
    stage_merge(notes, folders, rules.get("keyword_map", {}), args.classify_threshold)
    decide(notes)

    LABEL = {"sensitive": "敏感", "duplicate": "重复", "todo": "待办",
             "todo-done": "已完成待办", "fragment": "碎片"}
    if args.verbose:
        print(f"[1/6] 扫描       {len(notes)} 条 .md")
        print(f"[2/6] 已有分类   {('、'.join(folders)) if folders else '（无）'}")
        for k in ("sensitive", "duplicate", "todo", "todo-done", "fragment"):
            c = sum(1 for n in notes if k in n.flags)
            if c:
                print(f"[3/6] {LABEL[k]:<8} {c} 条")
        inbox = sum(1 for n in notes if n.target == INBOX_DIR)
        if inbox:
            print(f"[4/6] 待人工归类 {inbox} 条 → {INBOX_DIR}/")
        print("[5/6] 图片链接   wiki → ![[...]]")
        print("[6/6] 逐条判定：")
        print("-" * 62)
        for n in notes:
            marks = "+".join(LABEL.get(f, f) for f in n.flags) or "—"
            dest = n.target or "（根目录）"
            print(f"  {n.title or n.rel}  [{n.n_chars}字]  {marks}  →  {dest}")
        print("-" * 62)

    stats = apply_all(root, out, notes, args.link_style) if args.apply else {}
    write_reports(out, notes, stats, folders)

    n_keep = sum(1 for n in notes if n.target not in (TRASH_DIR, SENSITIVE_DIR, TODO_DIR, FRAGMENT_DIR))
    print(f"扫描 {len(notes)} 条 → 保留 {n_keep} 条")
    for k, v in sorted(stats.items()) if stats else []:
        print(f"  {k}: {v}")
    print(f"报告：{out / REPORT_DIR / 'report.md'}")
    if not args.apply:
        print("\n[预览模式] 未改动任何文件。确认无误后加 --apply 执行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
