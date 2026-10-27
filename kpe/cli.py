"""kpe 命令行：chunks / extract / review / stats。"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import __version__
from .chunk import (Chunk, markdown_sections, merge_split, window_sections)
from .llm import LLMClient, LLMConfig, key_from_env
from .pdf import (build_sections, clean_pages, detect_offset, parse_toc,
                  read_pages)
from .prompt import (DEFAULT_MAX_POINTS, SYSTEM, build_system, build_user,
                     clamp_max_points)
from .review import render_review
from .store import append_jsonl, read_json, read_jsonl, sha1, write_json, write_jsonl
from .validate import dedupe, gate_reasons, parse_points, quote_ok


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def cmd_chunks(args: argparse.Namespace) -> int:
    src = Path(args.input)
    if not src.exists():
        print(f"输入不存在：{src}", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    suffix = src.suffix.lower()
    info: dict = {"source": str(src.resolve()), "kind": suffix.lstrip("."),
                  "toc_entries": 0, "pages": 0, "offset": 0}
    if suffix == ".pdf":
        raw = read_pages(src)
        entries, toc_end = parse_toc(raw)
        pages = clean_pages(raw, toc_end)
        info.update(pages=len(pages), toc_entries=len(entries), toc_end=toc_end)
        if entries:
            offset = detect_offset(entries, pages, toc_end)
            sections = build_sections(entries, pages, offset)
            info["offset"] = offset
        else:
            sections = window_sections("\n\n".join(pages), src.stem)
    elif suffix in (".md", ".markdown", ".txt"):
        text = src.read_text(encoding="utf-8")
        sections = (markdown_sections(text) if suffix != ".txt"
                    else window_sections(text, src.stem))
    else:
        print(f"暂不支持的类型：{suffix}", file=sys.stderr)
        return 2
    chunks = merge_split(sections, args.target_chars, args.overlap,
                         args.max_chars, args.keep_front)
    if not chunks:
        print("没有分出任何块：检查目录/标题解析", file=sys.stderr)
        return 1
    write_jsonl(out / "chunks.jsonl", [c.to_dict() for c in chunks])
    manifest = {"book": (args.book_title or src.stem).strip(), **info,
                "params": {"target_chars": args.target_chars,
                           "overlap": args.overlap,
                           "max_chars": args.max_chars,
                           "keep_front": args.keep_front},
                "chunk_count": len(chunks),
                "chunks_chars": sum(c.chars for c in chunks),
                "created_at": _now()}
    write_json(out / "manifest.json", manifest)
    print(f"源：{src.name}")
    print(f"页数={info.get('pages')} 目录条={info['toc_entries']} "
          f"偏移={info['offset']} 块数={len(chunks)} "
          f"总字符={manifest['chunks_chars']:,}")
    for c in chunks[:3] + chunks[-2:]:
        loc = (f"p{c.page_start}-{c.page_end}" if c.page_start else "-")
        print(f"  #{c.index:>3} {loc:>9} {c.chars:>5}字 | "
              f"{c.section_path[:60]}")
    print(f"已写：{out / 'chunks.jsonl'}")
    return 0


def _extract_one(client: LLMClient, chunk: dict, book: str, total: int,
                 cache_dir: Path, retry_failed: bool,
                 system: str = SYSTEM) -> dict:
    cid = chunk["id"]
    cache_path = cache_dir / f"{cid}.json"
    cached = read_json(cache_path)
    if cached and not (retry_failed and cached.get("error")):
        return {"chunk": chunk, "content": cached.get("content", ""),
                "usage": cached.get("usage", {}), "error": cached.get("error"),
                "cached": True}
    user = build_user(book, chunk["section_path"], chunk["page_start"],
                      chunk["page_end"], chunk["index"], total, chunk["text"])
    try:
        content, usage = client.chat(system, user)
        row = {"chunk_id": cid, "content": content, "usage": usage,
               "error": "", "model": client.cfg.model, "ts": _now()}
    except Exception as exc:  # noqa: BLE001
        row = {"chunk_id": cid, "content": "", "usage": {},
               "error": f"{type(exc).__name__}: {exc}",
               "model": client.cfg.model, "ts": _now()}
    write_json(cache_path, row)
    return {"chunk": chunk, "content": row["content"], "usage": row["usage"],
            "error": row["error"], "cached": False}


def _repair(client: LLMClient, chunk: dict, book: str, total: int,
            prev_user: str, err: str, cache_dir: Path,
            system: str = SYSTEM) -> tuple[str, dict]:
    user = (f"{prev_user}\n\n上次输出无法解析为 JSON（{err}）。"
            "请重新输出，只输出合法 JSON，不要解释。")
    content, usage = client.chat(system, user)
    write_json(cache_dir / f"{chunk['id']}.repair.json",
               {"chunk_id": chunk["id"], "content": content, "usage": usage,
                "ts": _now()})
    return content, usage


def cmd_extract(args: argparse.Namespace) -> int:
    out = Path(args.out)
    manifest = read_json(out / "manifest.json")
    if not manifest:
        print(f"找不到 {out / 'manifest.json'}：先跑 chunks", file=sys.stderr)
        return 2
    chunks = read_jsonl(out / "chunks.jsonl")
    if args.chapter:
        chunks = [c for c in chunks
                  if any(s in c["section_path"] for s in args.chapter)]
    if args.limit:
        chunks = chunks[:args.limit]
    done_path = out / "done.jsonl"
    done = set() if args.redo else {r["chunk_id"] for r in read_jsonl(done_path)}
    chunks = [c for c in chunks if c["id"] not in done]
    if not chunks:
        print("没有待处理的块（都已 done；要重跑加 --redo）")
        return 0
    cache_dir = out / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    client = LLMClient(LLMConfig(
        base_url=args.base_url, model=args.model,
        api_key=key_from_env(args.api_key_env),
        temperature=args.temperature, timeout=args.timeout,
        thinking=args.thinking, max_tokens=args.max_tokens))
    points_path = out / "points.jsonl"
    rejected_path = out / "rejected.jsonl"
    max_points = clamp_max_points(getattr(args, "max_points", DEFAULT_MAX_POINTS))
    system = build_system(max_points)
    stats = {"chunks": len(chunks), "done": 0, "cached": 0, "failed": 0,
             "points": 0, "rejected": 0, "dup": 0, "quote_ok": 0,
             "tokens_in": 0, "tokens_out": 0, "model": args.model,
             "max_points": max_points, "started_at": _now()}
    seen: set[str] = set()
    for p in read_jsonl(points_path):
        seen.add(sha1("".join(str(p.get("text") or "").split())))
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_extract_one, client, c, manifest["book"],
                               len(chunks), cache_dir, args.retry_failed,
                               system): c
                   for c in chunks}
        for fut in as_completed(futures):
            res = fut.result()
            chunk = res["chunk"]
            stats["cached"] += 1 if res["cached"] else 0
            usage = res.get("usage") or {}
            stats["tokens_in"] += int(usage.get("prompt_tokens") or 0)
            stats["tokens_out"] += int(usage.get("completion_tokens") or 0)
            if res.get("error"):
                stats["failed"] += 1
                append_jsonl(out / "failed.jsonl",
                             {"chunk_id": chunk["id"], "error": res["error"]})
                continue
            pts, err = parse_points(res["content"])
            if err and not res["cached"]:
                try:
                    user = build_user(manifest["book"], chunk["section_path"],
                                      chunk["page_start"], chunk["page_end"],
                                      chunk["index"], len(chunks), chunk["text"])
                    content, usage2 = _repair(client, chunk, manifest["book"],
                                              len(chunks), user, err, cache_dir,
                                              system)
                    stats["tokens_in"] += int(usage2.get("prompt_tokens") or 0)
                    stats["tokens_out"] += int(usage2.get("completion_tokens") or 0)
                    pts, err = parse_points(content)
                except Exception as exc:  # noqa: BLE001
                    err = f"repair failed: {exc}"
            stats["done"] += 1
            kept = []
            for p in dedupe(pts):
                text = str(p.get("text") or "").strip()
                quote = str(p.get("quote") or "").strip()
                reasons = gate_reasons(text)
                if reasons:
                    stats["rejected"] += 1
                    append_jsonl(rejected_path,
                                 {"chunk_id": chunk["id"], "text": text,
                                  "reasons": reasons})
                    continue
                qok = quote_ok(quote, chunk["text"])
                key = sha1("".join(text.split()))
                if key in seen:
                    stats["dup"] += 1
                    continue
                seen.add(key)
                kept.append({"chunk_id": chunk["id"],
                             "chunk_index": chunk["index"],
                             "section_path": chunk["section_path"],
                             "page_start": chunk["page_start"],
                             "page_end": chunk["page_end"],
                             "text": text, "quote": quote,
                             "quote_ok": qok, "model": args.model})
                stats["quote_ok"] += 1 if qok else 0
            for p in kept[:max_points]:
                append_jsonl(points_path, p)
                stats["points"] += 1
            append_jsonl(done_path, {"chunk_id": chunk["id"], "ts": _now()})
            if stats["done"] % 5 == 0 or stats["done"] == len(chunks):
                print(f"[{stats['done']}/{len(chunks)}] 点={stats['points']} "
                      f"拒={stats['rejected']} 失败={stats['failed']} "
                      f"缓存={stats['cached']}", flush=True)
    stats["elapsed_s"] = round(time.time() - t0, 1)
    stats["finished_at"] = _now()
    write_json(out / "stats.json", stats)
    cost = ""
    if args.price_in or args.price_out:
        cny = (stats["tokens_in"] / 1e6 * args.price_in
               + stats["tokens_out"] / 1e6 * args.price_out)
        cost = f" 成本≈¥{cny:.2f}"
    print(f"完成：{stats['done']}/{len(chunks)} 块，点={stats['points']}，"
          f"拒={stats['rejected']}，重复={stats['dup']}，失败={stats['failed']}，"
          f"quote命中={stats['quote_ok']}/{stats['points']}，"
          f"token={stats['tokens_in']}+{stats['tokens_out']}，"
          f"{stats['elapsed_s']}s{cost}")
    return 0 if stats["failed"] == 0 else 1


def cmd_review(args: argparse.Namespace) -> int:
    out = Path(args.out)
    manifest = read_json(out / "manifest.json")
    if not manifest:
        print(f"找不到 {out / 'manifest.json'}", file=sys.stderr)
        return 2
    points = read_jsonl(out / "points.jsonl")
    if args.limit:
        points = points[:args.limit]
    text = render_review(manifest, points, args.with_sections)
    dest = Path(args.dest) if args.dest else out / "review.md"
    dest.write_text(text, encoding="utf-8", newline="\n")
    print(f"已写：{dest}（{len(points)} 个点）")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    out = Path(args.out)
    manifest = read_json(out / "manifest.json") or {}
    last = read_json(out / "stats.json") or {}
    chunks = read_jsonl(out / "chunks.jsonl")
    points = read_jsonl(out / "points.jsonl")
    done = read_jsonl(out / "done.jsonl")
    models: dict[str, int] = {}
    for p in points:
        m = p.get("model") or "?"
        models[m] = models.get(m, 0) + 1
    tin = tout = calls = 0
    cache_dir = out / "cache"
    if cache_dir.is_dir():
        for f in cache_dir.glob("*.json"):
            usage = (read_json(f) or {}).get("usage") or {}
            tin += int(usage.get("prompt_tokens") or 0)
            tout += int(usage.get("completion_tokens") or 0)
            calls += 1
    current = {
        "chunks_total": len(chunks),
        "chunks_done": len({r.get("chunk_id") for r in done}),
        "points": len(points),
        "quote_ok": sum(1 for p in points if p.get("quote_ok")),
        "models": models,
        "tokens_in": tin,
        "tokens_out": tout,
        "llm_calls_cached": calls,
        "rejected": len(read_jsonl(out / "rejected.jsonl")),
        "failed": len(read_jsonl(out / "failed.jsonl")),
    }
    print(json.dumps({"manifest": manifest, "current": current,
                      "last_run": last}, ensure_ascii=False, indent=1))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kpe", description="大文件知识点抽取")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("chunks", help="只分块（不调模型）")
    c.add_argument("input")
    c.add_argument("--out", required=True)
    c.add_argument("--book-title", default="")
    c.add_argument("--target-chars", type=int, default=6000)
    c.add_argument("--overlap", type=int, default=600)
    c.add_argument("--max-chars", type=int, default=8000)
    c.add_argument("--keep-front", action="store_true",
                   help="保留前言/引言等前置内容")
    c.set_defaults(func=cmd_chunks)

    e = sub.add_parser("extract", help="逐块抽取知识点")
    e.add_argument("out")
    e.add_argument("--base-url", required=True)
    e.add_argument("--model", required=True)
    e.add_argument("--api-key-env", default="KNOWATLAS_LLM_API_KEY")
    e.add_argument("--thinking", choices=("omit", "on", "off"), default="off")
    e.add_argument("--temperature", type=float, default=0.2)
    e.add_argument("--timeout", type=float, default=180)
    e.add_argument("--max-tokens", type=int, default=0)
    e.add_argument("--workers", type=int, default=4)
    e.add_argument("--limit", type=int, default=0)
    e.add_argument("--max-points", type=int, default=DEFAULT_MAX_POINTS,
                   help="每块最多抽几个点（默认 6，上限 20；同时改写 prompt 与截断）")
    e.add_argument("--chapter", action="append", default=[],
                   help="只跑 section_path 含该子串的块，可多次")
    e.add_argument("--retry-failed", action="store_true")
    e.add_argument("--redo", action="store_true", help="忽略 done，重跑全部块")
    e.add_argument("--price-in", type=float, default=0.0,
                   help="输入价（元/百万 token）")
    e.add_argument("--price-out", type=float, default=0.0)
    e.set_defaults(func=cmd_extract)

    r = sub.add_parser("review", help="生成 Markdown 复核稿")
    r.add_argument("out")
    r.add_argument("--dest", default="")
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--with-sections", action="store_true",
                   help="保留每节的路径/页码小标题（默认只按章分组）")
    r.set_defaults(func=cmd_review)

    s = sub.add_parser("stats", help="打印 manifest/stats")
    s.add_argument("out")
    s.set_defaults(func=cmd_stats)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())