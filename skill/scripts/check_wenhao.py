#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Check the 文号 (document numbers) cited in Markdown against gov.cn's index.

Why this file exists: an invented 文号 is the easiest thing for a model to
produce and the hardest thing for a reader to catch. `财债〔2026〕14号` reads
perfectly, sits in a table of genuine siblings, and nobody ever looks it up.
This script pulls every 文号 out of the files you name and asks gov.cn whether a
document with that exact number exists.

How to read the result:

    OK          gov.cn has a document whose 文号 is exactly this. Quote it.
    NO-RECORD   the index has nothing for this 文号 at all. Either it does not
                exist, or it was published only on a ministry's own site —
                gov.cn's 政策文件库 does not carry every 部委文件. Go to the
                issuing body's site before you either cite it or delete it.
    NEAR-MISS   results came back, but none with this 文号; the numbers that did
                come back are the ones it resembles. This is the exact shape of
                a fabricated number: searching an invented 文号 returns the
                real document with a neighbouring number.
    ERROR       the request failed. Not a verdict on the 文号.

Only OK means verified. The other three all mean "do not cite this yet".

Usage:
    python check_wenhao.py skill/references/*.md
    python check_wenhao.py README.md --json
    python check_wenhao.py skill/references/*.md --offline     # extract only

Options:
    --n N        results per query (default 100, the server maximum)
    --delay S    seconds between requests (default 0.4)
    --json       machine-readable output
    --quiet      only non-OK rows and the summary
    --offline    list extracted 文号 without any network access

A line containing "check_wenhao:ignore" is skipped. Put that marker on lines
that quote a bad number on purpose (the manual's near-miss examples), so a
self-check of the skill's own docs stays actionable.

Exit codes: 0 = every 文号 verified, 1 = something not verified, 2 = usage or
network failure.
"""

import sys
import os
import re
import json
import time
import urllib.parse
import urllib.request

API = "https://sousuo.www.gov.cn/search-gov/data"
TYPES = ("zhengcelibrary_gw", "zhengcelibrary_bm")

# Leading characters that belong to the surrounding sentence, not to a body name.
NOISE = ("根据", "依据", "按照", "参照", "落实", "印发", "关于", "见", "如", "按", "据",
         "及", "和", "与", "或", "由", "经", "在", "对", "以", "并", "的", "了", "为",
         "从", "于", "该", "本", "此", "各", "共", "计", "含", "即", "是", "有",
         "年", "月", "日", "号", "至", "起", "前", "底")

PAT_YEAR_ORDER = re.compile(r"([\u4e00-\u9fff]{2,16})\s*((?:19|20)\d{2}年第\s*\d+号)")
PAT_BRACKET = re.compile(
    r"(?:([\u4e00-\u9fff]{2,16})\s*)?[〔﹝\[]\s*((?:19|20)\d{2})\s*[〕﹞\]]\s*(\d+)\s*号")
PAT_ORDER_ONLY = re.compile(r"((?:国令|国务院令|部令|署令)第\s*\d+号)")


def normalize(s):
    s = (s or "").replace("﹝", "〔").replace("﹞", "〕")
    s = s.replace("[", "〔").replace("]", "〕")
    return re.sub(r"\s+", "", s)


def clean_prefix(raw):
    prefix = normalize(raw or "")
    changed = True
    while changed:
        changed = False
        for word in NOISE:
            if prefix.startswith(word) and len(prefix) > len(word) + 1:
                prefix = prefix[len(word):]
                changed = True
    if len(prefix) < 2 or re.match(r"^\d", prefix):
        return ""
    return prefix[:16]


IGNORE_MARK = "check_wenhao:ignore"


def find_wenhao(text):
    """Yield (token, line_no). A token may be a bare ◯◯〔YYYY〕N号 when the body
    name is not adjacent (enumerations like 「2025 年〔2025〕6号」).

    A line containing IGNORE_MARK is skipped. Use it on lines that deliberately
    quote a bad number as an example (e.g. the near-miss trap in sources.md),
    so the self-check stays actionable instead of always flagging the manual."""
    seen = set()
    for lineno, line in enumerate(text.splitlines(), 1):
        if IGNORE_MARK in line:
            continue
        for m in PAT_YEAR_ORDER.finditer(line):
            token = normalize(clean_prefix(m.group(1)) + m.group(2))
            if token not in seen:
                seen.add(token)
                yield token, lineno
        for m in PAT_BRACKET.finditer(line):
            prefix = clean_prefix(m.group(1))
            core = "〔%s〕%s号" % (m.group(2), m.group(3))
            token = normalize(prefix + core)
            if token not in seen:
                seen.add(token)
                yield token, lineno
        for m in PAT_ORDER_ONLY.finditer(line):
            token = normalize(m.group(1))
            if token not in seen:
                seen.add(token)
                yield token, lineno


def fetch(t, q, n, retries=2):
    """Return (total, rows, error). total 0 means the server found nothing."""
    url = "%s?%s" % (API, urllib.parse.urlencode({"t": t, "q": q, "p": 1, "n": n}))
    err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; wenhao-check)"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            code = data.get("code")
            sv = data.get("searchVO")
            if code == 1001 or sv is None:      # 「抱歉，没有找到相关结果」
                return 0, [], None
            if code != 200:
                return 0, [], "code %s: %s" % (code, data.get("msg"))
            return sv.get("totalCount") or 0, sv.get("listVO") or [], None
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            err = "%s: %s" % (type(exc).__name__, exc)
            time.sleep(0.8 + attempt)
    return 0, [], err


def query_forms(token):
    """Full token first, then progressively shorter prefixes (prose glue)."""
    forms = [token]
    m = re.match(r"^([\u4e00-\u9fff]+)((?:19|20)\d{2}年第\d+号|〔(?:19|20)\d{2}〕\d+号)$", token)
    if not m:
        return forms
    prefix, tail = m.group(1), m.group(2)
    for cut in (10, 8, 4):
        if len(prefix) > cut:
            cand = prefix[-cut:] + tail
            if cand not in forms:
                forms.append(cand)
    return forms


def verify(token, n, delay):
    want = normalize(token)
    bare = re.match(r"^(?:〔(?:19|20)\d{2}〕|(?:19|20)\d{2}年第)\d+号$", want) is not None
    inner = re.sub(r"^[\u4e00-\u9fff]+", "", want)     # 「〔2025〕6号」
    tried, near = [], []
    bare_hits = {}
    last_total = 0
    err = None

    for cand in query_forms(token):
        for t in TYPES:
            total, rows, e = fetch(t, cand, n)
            time.sleep(delay)
            if e:
                err = e
                continue
            last_total = total
            marks = [normalize(r.get("pcode")) for r in rows]
            tried.append({"query": cand, "t": t, "total": total})

            if bare:
                # No body name given: every 文号 ending with this core is a candidate.
                # Collect across both indexes before deciding anything.
                for r in rows:
                    if normalize(r.get("pcode")).endswith(inner):
                        bare_hits[normalize(r.get("pcode"))] = r
                continue

            for r in rows:
                if normalize(r.get("pcode")) == want:
                    return {"result": "ok", "total": total, "query": cand,
                            "title": re.sub(r"<[^>]+>", "", r.get("title") or ""),
                            "date": r.get("pubtimeStr") or "",
                            "org": r.get("puborg") or "",
                            "url": r.get("url") or "",
                            "tried": tried}
            for mk in marks:
                if mk and mk not in near:
                    near.append(mk)

    if bare:
        cands = sorted(bare_hits)
        if len(cands) == 1:
            r = bare_hits[cands[0]]
            return {"result": "ok", "total": last_total, "candidates": cands,
                    "resolved": cands[0],
                    "title": re.sub(r"<[^>]+>", "", r.get("title") or ""),
                    "date": r.get("pubtimeStr") or "", "tried": tried}
        if cands:
            return {"result": "ambiguous", "total": last_total,
                    "candidates": cands, "tried": tried}
        return {"result": "no_record", "total": last_total, "tried": tried}

    if err and not tried:
        return {"result": "error", "detail": err, "total": 0}
    if last_total == 0:
        return {"result": "no_record", "total": 0, "tried": tried}
    return {"result": "near_miss", "total": last_total,
            "near": near[:4], "tried": tried}


LABEL = {"ok": "OK       ", "ambiguous": "AMBIGUOUS", "no_record": "NO-RECORD",
         "near_miss": "NEAR-MISS", "error": "ERROR    ", "unchecked": "UNCHECKED"}


def main(argv):
    args = argv[1:]
    flags = {a for a in args if a.startswith("--")}
    as_json = "--json" in flags
    quiet = "--quiet" in flags
    offline = "--offline" in flags
    n, delay, files = 100, 0.4, []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--n" and i + 1 < len(args):
            n = max(1, min(100, int(args[i + 1]))); i += 2; continue
        if a == "--delay" and i + 1 < len(args):
            delay = float(args[i + 1]); i += 2; continue
        if not a.startswith("--"):
            files.append(a)
        i += 1

    if not files:
        print("usage: check_wenhao.py FILE.md [...] [--n 100] [--json] [--quiet] [--offline]",
              file=sys.stderr)
        return 2

    findings = []
    for path in files:
        if not os.path.isfile(path):
            print("no such file: %s" % path, file=sys.stderr)
            return 2
        text = open(path, encoding="utf-8", errors="replace").read()
        for token, lineno in find_wenhao(text):
            item = {"wenhao": token, "file": path, "line": lineno}
            item.update({"result": "unchecked"} if offline else verify(token, n, delay))
            findings.append(item)
            if not as_json and not (quiet and item["result"] == "ok"):
                extra = ""
                if item["result"] == "ok":
                    extra = "%s  %s" % (item.get("date", ""), (item.get("title") or "")[:44])
                elif item["result"] == "ambiguous":
                    extra = "同号多份: %s" % ", ".join(item.get("candidates") or [])
                elif item["result"] == "near_miss":
                    extra = "近似: %s" % ", ".join(item.get("near") or [])
                elif item["result"] == "error":
                    extra = item.get("detail") or ""
                print("%s %-26s %s:%d  %s" % (LABEL[item["result"]], token,
                                              os.path.basename(path), lineno, extra))

    if as_json:
        print(json.dumps(findings, ensure_ascii=False, indent=2))

    counts = {}
    for f in findings:
        counts[f["result"]] = counts.get(f["result"], 0) + 1
    bad = [f for f in findings if f["result"] not in ("ok", "unchecked")]

    if not as_json:
        print()
        print("%d 个文号 — 核实 %d / 有疑问 %d" % (
            len(findings), counts.get("ok", 0), len(bad)))
        if bad:
            print("有疑问的不要引用，先回原文核对：")
            for f in bad:
                print("  [%s] %s  (%s:%d)" % (f["result"], f["wenhao"],
                                              os.path.basename(f["file"]), f["line"]))
    if counts.get("error"):
        return 2
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
