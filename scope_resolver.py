#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scope_resolver.py — 作用域可见成员解析与错误报告（纯 Python 标准库，单文件）

输入格式
--------
每行定义一个作用域（空行与 # 注释被忽略）：

    <作用域名> [members=<m1,m2,...>] [imports=<来源.成员|来源.*,...>]

- 作用域名用点号表示嵌套层级，如 app.db.models 层级为 2，
  其外层作用域依次为 app.db、app（取已定义的最近祖先）。
- members= 与 imports= 均可省略；imports 中 “来源.*” 表示导入来源的全部本地成员。
- 导入来源按“最长已定义作用域名前缀”匹配，因此可以引用嵌套作用域的成员，
  例如 app.db.get_conn 会被解析为 作用域 app.db 的成员 get_conn。

可见性规则
----------
1. 本地成员可见；本地成员遮蔽同名导入成员与继承成员（遮蔽会给出提示）。
2. 显式导入的成员可见；导入遮蔽继承自外层的同名成员。
3. 外层作用域的全部可见成员（含其导入）对内层可见，就近遮蔽（词法作用域）。
4. 导入不跨级传递：只能导入来源作用域【本地定义】的成员，
   不能导入来源自身导入来的成员。理由：
   - 依赖关系显式化，看定义即可知道每个名字的真正出处；
   - 避免隐藏的传递依赖链（改动中间作用域不会波及下游）；
   - 解析与顺序无关，导入环只影响报告、不会造成解析死循环。

错误报告
--------
- 导入来源作用域不存在
- 导入的成员在来源中不存在（含“来源只有同名导入成员”的跨级传递情形）
- 同一作用域内成员重复定义
- 导入环（报告环上全部作用域名，含自导入）
- 多个来源导入同名成员造成冲突
- 作用域重复定义、无法识别的字段

用法
----
    python3 scope_resolver.py 定义文件.txt           # 从文件读取
    cat 定义文件.txt | python3 scope_resolver.py     # 从标准输入读取
    python3 scope_resolver.py --demo                 # 运行内置示例

退出码：0 无错误；1 有错误；2 输入读取失败。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field


DEMO_INPUT = """\
# ---- 正常：嵌套 / 导入 / 遮蔽 ----
util members=log,helper
app members=main,log imports=util.helper,util.log
app.db members=get_conn,query imports=util.log
app.db.models members=User,Order
# ---- 导入环 ----
a members=x imports=b.y
b members=y imports=a.x
# ---- 各类错误 ----
c members=m imports=ghost.thing,util.nope
d members=p,p,q
e members=z imports=util.log,other.log
other members=log
f members=w imports=app.helper,app.db.*
"""


@dataclass
class ImportSpec:
    raw: str
    lineno: int
    src: str = ""
    member: str = ""


@dataclass
class Scope:
    name: str
    lineno: int
    level: int
    members: list = field(default_factory=list)
    imports: list = field(default_factory=list)
    local: dict = field(default_factory=dict)     # 去重后的本地成员: name -> None
    imported: dict = field(default_factory=dict)  # 导入成员: name -> 来源作用域


# ---------------------------------------------------------------- 解析

def parse(text):
    """解析定义流，返回 (scopes: {name: Scope}, order: [name], errors: [str])。"""
    scopes, order, errors = {}, [], []
    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        name = parts[0]
        members, imports = [], []
        for tok in parts[1:]:
            if tok.startswith("members="):
                members.extend(m for m in tok[len("members="):].split(",") if m)
            elif tok.startswith("imports="):
                imports.extend(m for m in tok[len("imports="):].split(",") if m)
            else:
                errors.append(
                    "第%d行：无法识别的字段 %r（应为 members=... 或 imports=...）"
                    % (lineno, tok))
        if name in scopes:
            errors.append("第%d行：作用域 %r 重复定义（首次定义在第%d行）"
                          % (lineno, name, scopes[name].lineno))
            continue
        sc = Scope(name=name, lineno=lineno, level=name.count("."))
        sc.members = members
        sc.imports = [ImportSpec(raw=s, lineno=lineno) for s in imports]
        scopes[name] = sc
        order.append(name)
    return scopes, order, errors


def split_spec(raw, scopes):
    """把 'a.b.c.member' 拆成 (最长匹配的已定义作用域名, 成员名)。
    返回 (None, None) 表示来源作用域不存在；(src, "") 表示缺少成员名。"""
    best = None
    for name in scopes:
        if raw == name or raw.startswith(name + "."):
            if best is None or len(name) > len(best):
                best = name
    if best is None:
        return None, None
    if raw == best:
        return best, ""
    return best, raw[len(best) + 1:]


# ---------------------------------------------------------------- 解析语义

def resolve(scopes, order):
    """返回 (errors, notes, visible)。visible: {scope: {member: (kind, origin)}}，
    kind ∈ {"local", "import", "inherit"}。"""
    errors, notes = [], []

    # 1) 本地成员去重，报告重复定义
    for name in order:
        sc = scopes[name]
        for m in sc.members:
            if m in sc.local:
                errors.append("第%d行：作用域 %s 中成员 %r 重复定义（保留首次定义）"
                              % (sc.lineno, name, m))
            else:
                sc.local[m] = None

    # 2) 解析导入来源（最长前缀匹配），构建作用域级导入边
    edges = {n: set() for n in order}
    for name in order:
        sc = scopes[name]
        for spec in sc.imports:
            src, member = split_spec(spec.raw, scopes)
            if src is None:
                errors.append("第%d行：作用域 %s 的导入 %r 无效：来源作用域不存在"
                              % (spec.lineno, name, spec.raw))
                continue
            if member == "":
                errors.append("第%d行：作用域 %s 的导入 %r 缺少成员名"
                              "（应为 作用域.成员 或 作用域.*）"
                              % (spec.lineno, name, spec.raw))
                continue
            spec.src, spec.member = src, member
            edges[name].add(src)

    # 3) 展开导入并校验成员存在性（只允许来源的本地成员：不跨级传递）
    missing = []  # (spec, 导入方, 来源, 成员)
    for name in order:
        sc = scopes[name]
        for spec in sc.imports:
            if not spec.src:
                continue
            src_scope = scopes[spec.src]
            targets = list(src_scope.local) if spec.member == "*" else [spec.member]
            for m in targets:
                if m not in src_scope.local:
                    if spec.member != "*":
                        missing.append((spec, name, spec.src, m))
                    continue
                if m in sc.imported and sc.imported[m] != spec.src:
                    errors.append(
                        "第%d行：作用域 %s 导入冲突：成员 %r 同时来自 %s 和 %s"
                        "（可用同名本地成员遮蔽来消除歧义）"
                        % (spec.lineno, name, m, sc.imported[m], spec.src))
                    continue
                sc.imported.setdefault(m, spec.src)

    # 3b) 区分“成员不存在”与“跨级传递导入”
    for spec, importer, src, m in missing:
        if m in scopes[src].imported:
            errors.append(
                "第%d行：作用域 %s 的导入 %r 无效：%r 不是 %s 的本地成员"
                "（%s 自身从 %s 导入），不允许跨级传递导入，请直接写作 %s.%s"
                % (spec.lineno, importer, spec.raw, m, src, src,
                   scopes[src].imported[m], scopes[src].imported[m], m))
        else:
            errors.append("第%d行：作用域 %s 的导入 %r 无效：成员 %r 在作用域 %s 中不存在"
                          % (spec.lineno, importer, spec.raw, m, src))

    # 4) 导入环检测（Tarjan SCC，含自环）
    for scc in find_sccs(order, edges):
        if len(scc) > 1:
            names = sorted(scc, key=order.index)
            errors.append("导入环：%s（这些作用域相互直接或间接导入，请拆解依赖）"
                          % " -> ".join(names + [names[0]]))
        elif scc[0] in edges[scc[0]]:
            errors.append("导入环：作用域 %s 导入了自身" % scc[0])

    # 5) 计算可见成员：继承（就近祖先优先）< 导入 < 本地
    visible = {}
    for name in sorted(order, key=lambda n: scopes[n].level):
        sc = scopes[name]
        vis = {}
        for anc in ancestors(name, scopes):
            for m, origin in visible[anc].items():
                vis.setdefault(m, ("inherit", anc))
        for m, src in sc.imported.items():
            if m in vis:
                notes.append("作用域 %s：导入成员 %r（来自 %s）遮蔽了继承自 %s 的同名成员"
                             % (name, m, src, vis[m][1]))
            vis[m] = ("import", src)
        for m in sc.local:
            if m in sc.imported:
                notes.append("作用域 %s：本地成员 %r 遮蔽了导入自 %s 的同名成员"
                             % (name, m, sc.imported[m]))
            elif m in vis:
                notes.append("作用域 %s：本地成员 %r 遮蔽了继承自 %s 的同名成员"
                             % (name, m, vis[m][1]))
            vis[m] = ("local", name)
        visible[name] = vis

    return errors, notes, visible


def ancestors(name, scopes):
    """已定义的祖先作用域，就近在前。app.db.models -> [app.db, app]（跳过未定义的）。"""
    parts = name.split(".")
    result = []
    for i in range(len(parts) - 1, 0, -1):
        cand = ".".join(parts[:i])
        if cand in scopes:
            result.append(cand)
    return result


def find_sccs(nodes, edges):
    """迭代版 Tarjan 强连通分量。"""
    index_of, low, on_stack, stack = {}, {}, set(), []
    counter, sccs = [0], []
    for root in nodes:
        if root in index_of:
            continue
        index_of[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on_stack.add(root)
        work = [(root, iter(sorted(edges[root])))]
        while work:
            node, it = work[-1]
            descended = False
            for nxt in it:
                if nxt not in index_of:
                    index_of[nxt] = low[nxt] = counter[0]
                    counter[0] += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(sorted(edges[nxt]))))
                    descended = True
                    break
                elif nxt in on_stack:
                    low[node] = min(low[node], index_of[nxt])
            if descended:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index_of[node]:
                scc = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    scc.append(w)
                    if w == node:
                        break
                sccs.append(scc)
    return sccs


# ---------------------------------------------------------------- 输出

KIND_LABEL = {"local": "本地", "import": "导入", "inherit": "继承"}


def render(scopes, order, visible, errors, notes):
    out = ["=" * 56, "可见成员", "=" * 56]
    for name in order:
        sc = scopes[name]
        out.append("")
        out.append("作用域 %s（层级 %d，定义于第 %d 行）" % (name, sc.level, sc.lineno))
        vis = visible[name]
        if not vis:
            out.append("  （无可见成员）")
            continue
        width = max(len(m) for m in vis)
        for kind in ("local", "import", "inherit"):
            group = sorted(m for m, (k, _) in vis.items() if k == kind)
            for m in group:
                origin = vis[m][1]
                if kind == "local":
                    tag = "[本地]"
                elif kind == "import":
                    tag = "[导入自 %s]" % origin
                else:
                    tag = "[继承自 %s]" % origin
                out.append("  %-*s  %s" % (width, m, tag))
    out.append("")
    out.append("=" * 56)
    out.append("错误报告（%d 条）" % len(errors))
    out.append("=" * 56)
    if errors:
        out.extend("  %d. %s" % (i, e) for i, e in enumerate(errors, 1))
    else:
        out.append("  无错误")
    if notes:
        out.append("")
        out.append("-" * 56)
        out.append("遮蔽提示（%d 条，非错误）" % len(notes))
        out.append("-" * 56)
        out.extend("  * %s" % n for n in notes)
    return "\n".join(out)


# ---------------------------------------------------------------- 入口

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="作用域可见成员解析与错误报告（纯标准库单文件工具）",
        epilog="输入示例：\n"
               "  util members=log,helper\n"
               "  app members=main imports=util.helper\n"
               "  app.db members=query imports=util.*\n\n"
               "规则要点：本地 > 导入 > 继承；导入不跨级传递（详见脚本 docstring）。",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", help="作用域定义文件；缺省从标准输入读取")
    ap.add_argument("--demo", action="store_true", help="运行内置示例")
    args = ap.parse_args(argv)

    if args.demo:
        text = DEMO_INPUT
    elif args.file:
        try:
            with open(args.file, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            print("读取输入失败：%s" % exc, file=sys.stderr)
            return 2
    else:
        text = sys.stdin.read()

    scopes, order, errors = parse(text)
    sem_errors, notes, visible = resolve(scopes, order)
    errors.extend(sem_errors)
    print(render(scopes, order, visible, errors, notes))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
