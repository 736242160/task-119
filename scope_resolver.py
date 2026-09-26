#!/usr/bin/env python3
"""
scope_resolver.py — 跨作用域导入解析器（纯标准库，单文件）

输入格式（每行定义一个作用域，'#' 后为注释，空行忽略）：

    <作用域名> : <成员1> <成员2> ... ; <导入1> <导入2> ...

  - 作用域名可用点号表示嵌套，如 `app.db` 表示 `db` 嵌套在 `app` 内。
  - 成员列表与导入列表用分号 `;` 分隔；导入列表可省略（连同分号）。
  - 导入项形如 `来源作用域.成员名` 导入单个成员，或 `来源作用域.*` 导入其全部可见成员。

语义规则（含自定规则说明）：

  1. 导入的成员在导入作用域内可见。
  2. 本作用域成员遮蔽（shadow）同名的导入成员；导入成员又遮蔽外层作用域的同名成员。
     优先级：本地成员 > 导入成员 > 外层（父）作用域可见成员。
  3. 跨级传递规则（自定）：导入是【可传递】的——若 b 从 c 导入了 x，
     则 a 从 b 导入 x 是合法的（可见性即接口，与 Python 模块语义一致：
     `from c import x` 后 x 就是 b 模块的可用名字）。
     理由：以"可见性"而非"定义点"作为导入依据，规则统一、无需区分
     本地成员与再导出成员，且与主流语言（Python）行为一致，用户预期成本最低。
  4. 导入环（a 导入 b、b 又直接或间接导入 a）会被检测并报告环上全部作用域名；
     环上能解析的成员仍正常解析，解析不了的成员按"成员不存在"报错。
  5. 嵌套作用域可看到其所有祖先作用域的可见成员（词法作用域链）。
  6. 错误报告：导入来源不存在 / 导入成员不存在 / 同作用域成员重复定义 /
     作用域重复定义 / 导入环。

用法：

    python3 scope_resolver.py 输入文件          # 从文件读取
    cat 输入文件 | python3 scope_resolver.py    # 从标准输入读取
    python3 scope_resolver.py --demo            # 运行内置示例
"""

import argparse
import sys
from collections import defaultdict


class Scope:
    def __init__(self, name, lineno):
        self.name = name
        self.lineno = lineno
        self.members = []          # 本地成员（保序，去重后）
        self.imports = []          # [(来源作用域名, 成员名 或 '*', 行号)]
        self.visible = set()       # 解析后的可见成员集合
        self.origin = {}           # 成员名 -> 来源描述


def parse(text):
    """解析输入文本，返回 (scopes: dict, errors: list)。"""
    scopes = {}
    errors = []
    order = []

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue

        # 拆分 成员段 / 导入段
        if ";" in line:
            head, imp_part = line.split(";", 1)
        else:
            head, imp_part = line, ""

        if ":" in head:
            name, mem_part = head.split(":", 1)
        else:
            # 兼容无成员写法： "scope" 或 "scope ; imports"
            name, mem_part = head, ""
        name = name.strip()
        if not name:
            errors.append(f"第{lineno}行: 缺少作用域名")
            continue

        if name in scopes:
            errors.append(f"第{lineno}行: 作用域 '{name}' 重复定义"
                          f"（首次定义在第{scopes[name].lineno}行）")
            continue
        scope = Scope(name, lineno)
        scopes[name] = scope
        order.append(name)

        # 本地成员，查重
        seen = set()
        for m in mem_part.split():
            if m in seen:
                errors.append(f"第{lineno}行: 作用域 '{name}' 中成员 '{m}' 重复定义")
                continue
            seen.add(m)
            scope.members.append(m)

        # 导入项
        for tok in imp_part.split():
            if "." not in tok:
                errors.append(f"第{lineno}行: 作用域 '{name}' 的导入项 '{tok}' "
                              f"格式非法（应为 来源作用域.成员 或 来源作用域.*）")
                continue
            src, member = tok.rsplit(".", 1)
            if not src or not member:
                errors.append(f"第{lineno}行: 作用域 '{name}' 的导入项 '{tok}' 格式非法")
                continue
            scope.imports.append((src, member, lineno))

    return scopes, order, errors


def parent_of(name, scopes):
    """返回 name 的最近已定义祖先作用域名，无则 None。"""
    parts = name.split(".")
    for i in range(len(parts) - 1, 0, -1):
        cand = ".".join(parts[:i])
        if cand in scopes:
            return cand
    return None


def find_cycles(scopes):
    """对作用域级导入图做 Tarjan SCC，返回环列表（每个环是作用域名列表）。"""
    graph = {n: [src for src, _, _ in s.imports if src in scopes]
             for n, s in scopes.items()}
    index_of, lowlink, on_stack = {}, {}, set()
    stack, sccs, counter = [], [], [0]

    def strongconnect(v):
        # 迭代版 Tarjan，避免深图递归溢出
        work = [(v, iter(graph[v]))]
        index_of[v] = lowlink[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        while work:
            node, it = work[-1]
            advanced = False
            for w in it:
                if w not in index_of:
                    index_of[w] = lowlink[w] = counter[0]
                    counter[0] += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(graph[w])))
                    advanced = True
                    break
                elif w in on_stack:
                    lowlink[node] = min(lowlink[node], index_of[w])
            if advanced:
                continue
            work.pop()
            if work:
                lowlink[work[-1][0]] = min(lowlink[work[-1][0]], lowlink[node])
            if lowlink[node] == index_of[node]:
                scc = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    scc.append(w)
                    if w == node:
                        break
                sccs.append(scc)

    for v in graph:
        if v not in index_of:
            strongconnect(v)

    cycles = []
    for scc in sccs:
        if len(scc) > 1:
            cycles.append(sorted(scc))
        elif scc[0] in graph[scc[0]]:  # 自环
            cycles.append(scc)
    return cycles


def resolve(scopes, order, errors):
    """不动点迭代求每个作用域的可见成员集合。"""
    for name in order:
        scopes[name].visible = set(scopes[name].members)

    parents = {n: parent_of(n, scopes) for n in order}

    changed = True
    while changed:  # 单调递增且有限，必终止
        changed = False
        for name in order:
            scope = scopes[name]
            before = len(scope.visible)
            for src, member, _ in scope.imports:
                if src not in scopes:
                    continue  # 来源不存在，稍后统一报错
                if member == "*":
                    scope.visible |= scopes[src].visible
                elif member in scopes[src].visible:
                    scope.visible.add(member)
            p = parents[name]
            if p is not None:
                scope.visible |= scopes[p].visible
            if len(scope.visible) != before:
                changed = True

    # 来源不存在 / 成员不存在 报错
    for name in order:
        for src, member, lineno in scopes[name].imports:
            if src not in scopes:
                errors.append(f"第{lineno}行: 作用域 '{name}' 导入的来源作用域 "
                              f"'{src}' 不存在")
            elif member != "*" and member not in scopes[src].visible:
                errors.append(f"第{lineno}行: 作用域 '{name}' 从 '{src}' 导入的成员 "
                              f"'{member}' 不存在")

    # 计算每个可见成员的来源（本地 > 导入 > 外层）
    for name in order:
        scope = scopes[name]
        local = set(scope.members)
        imported = {}
        for src, member, _ in scope.imports:
            if src not in scopes:
                continue
            names = scopes[src].visible if member == "*" else {member}
            for m in names:
                if m in scopes[src].visible:
                    imported.setdefault(m, src)
        p = parents[name]
        outer = scopes[p].visible if p else set()

        for m in sorted(scope.visible):
            if m in local:
                note = "本地"
                if m in imported:
                    note += f"（遮蔽了来自 '{imported[m]}' 的同名导入）"
                scope.origin[m] = note
            elif m in imported:
                scope.origin[m] = f"导入自 '{imported[m]}'"
            else:
                scope.origin[m] = f"继承自外层 '{p}'"


def render(scopes, order, cycles, errors):
    out = []
    out.append("=" * 60)
    out.append("可见成员")
    out.append("=" * 60)
    for name in order:
        scope = scopes[name]
        out.append(f"\n作用域 {name}（定义于第{scope.lineno}行）:")
        if not scope.visible:
            out.append("  （无可见成员）")
        for m in sorted(scope.visible):
            out.append(f"  - {m}    [{scope.origin[m]}]")

    out.append("")
    out.append("=" * 60)
    out.append("错误报告")
    out.append("=" * 60)
    if cycles:
        for cyc in cycles:
            out.append(f"[环] 检测到导入环，环上作用域: {' -> '.join(cyc)}")
    if errors:
        for e in errors:
            out.append(f"[错误] {e}")
    if not cycles and not errors:
        out.append("（无错误）")
    return "\n".join(out)


DEMO_INPUT = """\
# 示例：嵌套、遮蔽、传递、环、各类错误
app : config logging
app.db : connect query ; app.logging
app.web : handler ; app.db.* app.db.connect
util : helper fmt
mathutil : helper ; util.helper
a : x ; b.y
b : y ; a.x
ghost_user : g ; nowhere.thing
dup : m1 m2 m1
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description="跨作用域导入解析器")
    ap.add_argument("input", nargs="?", help="输入文件（缺省读标准输入）")
    ap.add_argument("--demo", action="store_true", help="运行内置示例")
    args = ap.parse_args(argv)

    if args.demo:
        text = DEMO_INPUT
        print("---- 示例输入 ----")
        print(text)
    elif args.input:
        with open(args.input, encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()

    scopes, order, errors = parse(text)
    cycles = find_cycles(scopes)
    resolve(scopes, order, errors)
    print(render(scopes, order, cycles, errors))
    return 1 if (errors or cycles) else 0


if __name__ == "__main__":
    sys.exit(main())
