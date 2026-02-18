#!/usr/bin/env python3
import argparse
import re
from pathlib import Path
from typing import Dict, List, Set, Tuple

from clang.cindex import Index, Cursor, CursorKind, StorageClass, TypeKind


# ---------------------- FS helpers ----------------------

def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="ignore")


def write_text(p: Path, s: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(s, encoding="utf-8")


def _is_in_test_dir(p: Path) -> bool:
    return any(part.startswith("TEST_") for part in p.parts)


# ---------------------- Discovery limited to ../pltf and ../cfg ----------------------

def list_headers(roots: List[Path]) -> List[Path]:
    out: List[Path] = []
    for r in roots:
        if r.is_dir():
            out.extend([p for p in r.rglob("*.h") if p.is_file() and not _is_in_test_dir(p)])
    return sorted(out)


def list_c_files(roots: List[Path]) -> List[Path]:
    out: List[Path] = []
    for r in roots:
        if r.is_dir():
            out.extend([p for p in r.rglob("*.c") if p.is_file() and not _is_in_test_dir(p)])
    return sorted(out)


# ---------------------- Clang helpers ----------------------

def text_from_extent(ext) -> str:
    src_path = Path(ext.start.file.name)
    src = read_text(src_path)
    lines = src.splitlines(keepends=True)

    def idx(loc):
        li = loc.line - 1
        ci = loc.column - 1
        return sum(len(lines[i]) for i in range(li)) + ci

    start = idx(ext.start)
    end = idx(ext.end)
    return src[start:end]


def function_prototype(fn: Cursor) -> str:
    ret = fn.result_type.spelling if fn.result_type else "void"
    params = []
    for p in fn.get_arguments():
        t = p.type.spelling
        name = p.spelling or "param"
        params.append(f"{t} {name}")
    if fn.type.kind == TypeKind.FUNCTIONPROTO and fn.type.is_function_variadic():
        params.append("...")
    param_str = ", ".join(params) if params else "void"
    return f"{ret} {fn.spelling}({param_str});"


def collect_tu_globals(tu_cursor: Cursor) -> Dict[str, Cursor]:
    out = {}
    for c in tu_cursor.get_children():
        if c.kind == CursorKind.VAR_DECL and c.semantic_parent.kind == CursorKind.TRANSLATION_UNIT:
            usr = c.get_usr() or f"{c.spelling}@{c.location.file}:{c.location.line}"
            out[usr] = c
    return out


def classify_var(ref: Cursor) -> Tuple[bool, bool]:
    if ref is None or ref.kind != CursorKind.VAR_DECL:
        return (False, False)
    if ref.semantic_parent and ref.semantic_parent.kind == CursorKind.TRANSLATION_UNIT:
        is_static = (ref.storage_class == StorageClass.STATIC)
        return (True, is_static)
    return (False, False)


def analyze_function(fn: Cursor, tu_globals: Dict[str, Cursor]):
    calls: Set[str] = set()
    used_globals: Set[str] = set()
    used_static: Set[str] = set()

    def walk(n: Cursor):
        if n.kind == CursorKind.CALL_EXPR:
            tgt = None
            for ch in n.get_children():
                if hasattr(ch, "referenced") and ch.referenced:
                    tgt = ch.referenced
                    break
            if tgt and tgt.kind == CursorKind.FUNCTION_DECL and tgt.spelling:
                calls.add(tgt.spelling)

        if n.kind == CursorKind.DECL_REF_EXPR and n.referenced:
            ref = n.referenced
            is_glob, is_stat = classify_var(ref)
            if is_glob:
                usr = ref.get_usr() or f"{ref.spelling}@{ref.location.file}:{ref.location.line}"
                if usr in tu_globals:
                    if is_stat:
                        used_static.add(usr)
                    else:
                        used_globals.add(usr)

        for ch in n.get_children():
            walk(ch)

    for ch in fn.get_children():
        if ch.kind == CursorKind.COMPOUND_STMT:
            walk(ch)

    return calls, used_globals, used_static


def remove_function_proto_from_header(text: str, func_name: str) -> str:
    pattern = re.compile(
        r'(^|\n)\s*([A-Za-z_][\w\s\*\(\),\[\]:]+?\s+)?'
        + re.escape(func_name)
        + r'\s*\([^;{]*\)\s*;\s*(?=\n|$)',
        re.DOTALL,
    )
    return re.sub(pattern, r"\1", text)


def is_const_qualified(t) -> bool:
    try:
        return bool(t.is_const_qualified())
    except Exception:
        return False


def is_array_type(t) -> bool:
    return t.kind in (
        TypeKind.CONSTANTARRAY,
        TypeKind.INCOMPLETEARRAY,
        TypeKind.VARIABLEARRAY,
        TypeKind.DEPENDENTSIZEDARRAY,
    )


def array_elem_type_spelling(t) -> str:
    return t.element_type.spelling


def array_count_or_none(t):
    try:
        if t.kind == TypeKind.CONSTANTARRAY:
            return int(t.element_count)
    except Exception:
        pass
    return None


# ---------------------- Main ----------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", help="workspace path (this script folder)")
    # allow overriding output dir if needed
    ap.add_argument("--out-root", default=None, help="path to /unitTest (default: sibling of root)")
    # pass-through extra clang args after '--'
    args, extra_clang = ap.parse_known_args()

    root = Path(args.root).resolve()         # e.g., /workspace
    parent = root.parent                     # common parent of /workspace, /pltf, /cfg, /unitTest
    out_root = Path(args.out_root).resolve() if args.out_root else (parent / "unitTest")

    # Scan exactly ../pltf and ../cfg
    scan_roots: List[Path] = [parent / "pltf", parent / "cfg"]

    # Clang args: std + includes for ../pltf and ../cfg (+ local) + extras
    clang_args: List[str] = ["-std=c11"]
    for inc in scan_roots:
        clang_args.append(f"-I{inc}")
    clang_args.append("-I.")                 # local
    clang_args.extend(extra_clang)           # pass-through

    index = Index.create()

    headers_all = list_headers(scan_roots)
    c_files = list_c_files(scan_roots)

    for c_path in c_files:
        tu = index.parse(str(c_path), args=clang_args)
        tu_globals = collect_tu_globals(tu.cursor)

        for fn in tu.cursor.get_children():
            if fn.kind != CursorKind.FUNCTION_DECL or not fn.is_definition():
                continue
            if Path(str(fn.location.file)).resolve() != c_path:
                continue

            fn_name = fn.spelling

            # TEST PACKAGE under ../unitTest
            test_pkg_dir = out_root / f"TEST_{fn_name}"
            src_dir = test_pkg_dir / "src"
            test_dir = test_pkg_dir / "test"

            test_exists = test_pkg_dir.exists()
            src_exists = src_dir.exists()
            src_empty = (not src_exists) or (src_exists and not any(src_dir.iterdir()))

            # CASE 3: TEST exists + src not empty -> skip
            if src_exists and not src_empty:
                print(f"[SKIP] TEST_{fn_name} exists and src/ not empty")
                continue

            # CASE 1: TEST missing → create src + test
            if not test_exists:
                test_dir.mkdir(parents=True, exist_ok=True)

            # CASE 1 and CASE 2: recreate src content
            src_dir.mkdir(parents=True, exist_ok=True)

            _calls, used_glob_usr, used_stat_usr = analyze_function(fn, tu_globals)
            fn_text = text_from_extent(fn.extent)
            proto = function_prototype(fn)

            need_stddef = False
            need_string = False

            for usr in used_stat_usr:
                v = tu_globals[usr]
                t = v.type
                if is_array_type(t):
                    need_stddef = True
                    if not is_const_qualified(t) and array_count_or_none(t) is not None:
                        need_string = True

            # ================== src/<fn>.h ==================
            header_lines = [
                f"#ifndef TEST_{fn_name.upper()}_H",
                f"#define TEST_{fn_name.upper()}_H",
                "",
            ]

            for h in headers_all:
                header_lines.append(f'#include "{h.name}"')
            header_lines.append("")

            if need_stddef:
                header_lines.append("#include <stddef.h>")
            if need_string:
                header_lines.append("#include <string.h>")
            if need_stddef or need_string:
                header_lines.append("")

            header_lines.append(proto)

            for usr in sorted(used_stat_usr):
                v = tu_globals[usr]
                t = v.type
                vname = v.spelling
                v_is_const = is_const_qualified(t)
                v_is_array = is_array_type(t)

                if v_is_array:
                    elem_t = array_elem_type_spelling(t)
                    cnt = array_count_or_none(t)

                    header_lines.append(
                        f"{'const ' if v_is_const else ''}{elem_t}* get_{vname}_ptr(void);"
                    )
                    header_lines.append(f"size_t get_{vname}_size(void);")

                    if (not v_is_const) and (cnt is not None):
                        header_lines.append(f"void set_{vname}(const {elem_t}* src, size_t n);")

                else:
                    tname = t.spelling
                    header_lines.append(f"{tname} get_{vname}(void);")
                    if not v_is_const:
                        header_lines.append(f"void set_{vname}({tname} val);")

            header_lines.append("")
            header_lines.append(f"#endif /* TEST_{fn_name.upper()}_H */\n")

            write_text(src_dir / f"{fn_name}.h", "\n".join(header_lines))

            # ================== src/<fn>.c ==================
            impl = [
                f'#include "{fn_name}.h"',
                "#include <stddef.h>",
                "#include <string.h>",
                "",
            ]

            if used_glob_usr:
                impl.append("/* globals used (real definitions) */")
                for usr in sorted(used_glob_usr):
                    v = tu_globals[usr]
                    orig = text_from_extent(v.extent).strip()
                    if not orig.endswith(";"):
                        orig += ";"
                    impl.append(orig)
                impl.append("")

            if used_stat_usr:
                impl.append("/* static globals (copied) */")
                for usr in sorted(used_stat_usr):
                    v = tu_globals[usr]
                    t = v.type
                    vname = v.spelling
                    static_src = text_from_extent(v.extent).strip()
                    if not static_src.endswith(";"):
                        static_src += ";"
                    impl.append(static_src)

                    v_is_const = is_const_qualified(t)
                    v_is_array = is_array_type(t)

                    if v_is_array:
                        elem_t = array_elem_type_spelling(t)
                        cnt = array_count_or_none(t)

                        impl.append(
                            f"{'const ' if v_is_const else ''}{elem_t}* get_{vname}_ptr(void) {{ return {vname}; }}"
                        )

                        if cnt is not None:
                            impl.append(
                                f"size_t get_{vname}_size(void) {{ return (size_t){cnt}; }}"
                            )
                        else:
                            impl.append("size_t get_{vname}_size(void) { return 0; }")

                        if (not v_is_const) and (cnt is not None):
                            impl.append(
                                f"void set_{vname}(const {elem_t}* src, size_t n) {{\n"
                                f"    size_t m = (n < (size_t){cnt}) ? n : (size_t){cnt};\n"
                                f"    memcpy({vname}, src, m * sizeof({elem_t}));\n"
                                f"}}"
                            )

                    else:
                        tname = t.spelling
                        impl.append(f"{tname} get_{vname}(void) {{ return {vname}; }}")
                        if not v_is_const:
                            impl.append(f"void set_{vname}({tname} val) {{ {vname} = val; }}")

                impl.append("")

            impl.append("/* FUNCTION TO TEST */")
            impl.append(fn_text)

            write_text(src_dir / f"{fn_name}.c", "\n".join(impl))

            # ================== copy cleaned headers ==================
            for h in headers_all:
                cleaned = remove_function_proto_from_header(read_text(h), fn_name)
                write_text(src_dir / h.name, cleaned)

            # ================== create test/<fn>.c only if test didn't exist ==================
            if not test_exists:
                test_c = [
                    f'#include <{fn_name}.h>',
                    '#include "unity.h"',
                    "",
                ]

                for h in headers_all:
                    test_c.append(f'#include "mock_{h.name}"')

                test_c += [
                    "",
                    "void setUp(void) {}",
                    "void tearDown(void) {}",
                    "",
                    f"void test_{fn_name}(void)",
                    "{",
                    '    TEST_IGNORE_MESSAGE("Auto-generated stub test");',
                    "}",
                    "",
                ]

                write_text(test_dir / f"test_{fn_name}.c", "\n".join(test_c))

            print(f"[OK] Generated TEST_{fn_name} (src regenerated) -> {test_pkg_dir}")

if __name__ == "__main__":
    main()