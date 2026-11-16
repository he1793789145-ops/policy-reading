#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read text out of a PDF, with a working fallback when no PDF library exists.

Why this file exists: policy attachments (draft rules, application guides) are
often only available as PDF, and the interpreter an agent runtime ships with
frequently has no PDF library at all. Writing a one-off extractor inline every
run is where that path usually fails.

Usage:
    python read_pdf.py FILE.pdf                # all pages, plain text
    python read_pdf.py FILE.pdf --pages 1,3-5  # selected pages
    python read_pdf.py FILE.pdf --json         # {"backend":..., "pages":[...]}
    python read_pdf.py FILE.pdf --quiet        # no stderr status line

Backends, tried in order:
    1. pypdf        (best: handles compressed xref, object streams, encryption)
    2. pdfminer.six (if pypdf absent but pdfminer present)
    3. builtin      (stdlib only: regex object scan + zlib + ToUnicode CMap)

The builtin backend is deliberately narrow: it does not follow object streams
(`/ObjStm`), cross-reference streams, or encrypted files. When it cannot get
text it says so instead of returning something plausible-looking.

Exit codes: 0 = text found, 1 = no text extracted, 2 = file unreadable/usage.
"""

import sys
import os
import re
import zlib
import json

STATUS = (lambda msg: print(msg, file=sys.stderr))


# ---------------------------------------------------------------- page ranges

def parse_pages(spec, n_pages):
    """'1,3-5' -> [0, 2, 3, 4] (0-based). None means all pages."""
    if not spec:
        return list(range(n_pages))
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            lo = int(a) if a.strip() else 1
            hi = int(b) if b.strip() else n_pages
            out.extend(range(lo - 1, hi))
        else:
            out.append(int(part) - 1)
    return [i for i in out if 0 <= i < n_pages]


# ---------------------------------------------------------------- backend: pypdf

def try_pypdf(path):
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        reader = PdfReader(path)
        if getattr(reader, "is_encrypted", False):
            try:
                reader.decrypt("")
            except Exception:
                STATUS("pypdf: encrypted PDF, cannot decrypt")
                return None
        pages = [(p.extract_text() or "") for p in reader.pages]
        return {"backend": "pypdf", "pages": pages}
    except Exception as exc:
        STATUS("pypdf: failed (%s: %s)" % (type(exc).__name__, exc))
        return None


# ------------------------------------------------------------ backend: pdfminer

def try_pdfminer(path):
    try:
        from pdfminer.high_level import extract_text
        from pdfminer.pdfpage import PDFPage
    except ImportError:
        return None
    try:
        with open(path, "rb") as fh:
            n = len(list(PDFPage.get_pages(fh)))
        pages = []
        for i in range(n):
            try:
                pages.append(extract_text(path, page_numbers=[i]) or "")
            except Exception:
                pages.append("")
        return {"backend": "pdfminer", "pages": pages}
    except Exception as exc:
        STATUS("pdfminer: failed (%s: %s)" % (type(exc).__name__, exc))
        return None


# -------------------------------------------------------------- backend: builtin
#
# Object scan + zlib + ToUnicode. Narrow by design; see module docstring.

OBJ_RE = re.compile(rb"(\d+)\s+(\d+)\s+obj\b(.*?)\bendobj", re.S)
STREAM_RE = re.compile(rb"stream\r?\n?(.*?)\r?\n?endstream", re.S)
REF_RE = re.compile(rb"(\d+)\s+\d+\s+R")


def _deref(objs, token):
    """Return object number from a 'N 0 R' token, else None."""
    m = REF_RE.search(token or b"")
    return int(m.group(1)) if m else None


def _dict_of(objs, num):
    entry = objs.get(num)
    if not entry:
        return b""
    return entry[0]


def _filters_of(head):
    """Ordered filter names from /Filter (a name or an array of names)."""
    m = re.search(rb"/Filter\s*(\[[^\]]*\]|/[A-Za-z0-9]+)", head)
    if not m:
        return []
    return [n.decode("latin-1") for n in re.findall(rb"/([A-Za-z0-9]+)", m.group(1))]


def _ascii85(raw):
    import base64
    data = re.sub(rb"\s", b"", raw)
    if data.endswith(b"~>"):
        data = data[:-2]
    try:
        return base64.a85decode(data, adobe=False)
    except Exception:
        return None


def _asciihex(raw):
    data = re.sub(rb"[^0-9A-Fa-f]", b"", raw.split(b">")[0])
    if len(data) % 2:
        data += b"0"
    try:
        return bytes.fromhex(data.decode())
    except ValueError:
        return None


def _runlength(raw):
    out = bytearray()
    i = 0
    while i < len(raw):
        n = raw[i]
        i += 1
        if n == 128:
            break
        if n < 128:
            out += raw[i:i + n + 1]
            i += n + 1
        else:
            if i < len(raw):
                out += bytes([raw[i]]) * (257 - n)
            i += 1
    return bytes(out)


def _lzw(raw):
    """LZWDecode with the PDF defaults (EarlyChange=1)."""
    out = bytearray()
    table = [bytes([i]) for i in range(256)] + [b"", b""]
    bits, buf, nbits = 9, 0, 0
    prev = None
    for byte in raw:
        buf = (buf << 8) | byte
        nbits += 8
        while nbits >= bits:
            nbits -= bits
            code = (buf >> nbits) & ((1 << bits) - 1)
            if code == 256:
                table = table[:258]
                bits, prev = 9, None
                continue
            if code == 257:
                return bytes(out)
            if prev is None:
                entry = table[code] if code < len(table) else b""
            elif code < len(table):
                entry = table[code]
                table.append(prev + entry[:1])
            else:
                entry = prev + prev[:1]
                table.append(entry)
            out += entry
            prev = entry
            if len(table) + 1 >= (1 << bits) and bits < 12:
                bits += 1
    return bytes(out)


FILTERS = {
    "FlateDecode": lambda raw: _inflate(raw),
    "Fl": lambda raw: _inflate(raw),
    "ASCII85Decode": _ascii85,
    "A85": _ascii85,
    "ASCIIHexDecode": _asciihex,
    "AHx": _asciihex,
    "RunLengthDecode": _runlength,
    "RL": _runlength,
    "LZWDecode": _lzw,
    "LZW": _lzw,
}


def _inflate(raw):
    try:
        return zlib.decompress(raw)
    except zlib.error:
        try:
            return zlib.decompressobj().decompress(raw)
        except Exception:
            return None


def _stream_of(objs, num):
    """Decode an object's stream through its /Filter chain, in order."""
    entry = objs.get(num)
    if not entry:
        return None
    head, raw = entry[0], entry[1]
    if raw is None:
        return None
    data = raw
    for name in _filters_of(head):
        fn = FILTERS.get(name)
        if fn is None:
            STATUS("builtin: unsupported filter /%s" % name)
            return None
        data = fn(data)
        if data is None:
            STATUS("builtin: /%s decode failed" % name)
            return None
    return data


def _collect_fonts(objs, res_head):
    """Walk /Resources -> font dicts, following one level of indirection.

    Two shapes occur in the wild:
        /Resources << /Font /F1 2 0 R ... >>   Font points straight at a dict
        /Resources << /Font << /F1 2 0 R >> >>  inline dict of refs
    so a candidate with no /ToUnicode of its own may still hold the real ones.
    """
    cmap = {}
    two_byte = False

    seen = set()
    queue = [int(m.group(1)) for m in REF_RE.finditer(res_head)]
    for _depth in range(2):
        nxt = []
        for num in queue:
            if num in seen:
                continue
            seen.add(num)
            head = _dict_of(objs, num)
            if not head:
                continue
            tu = re.search(rb"/ToUnicode\s+(\d+)\s+\d+\s+R", head)
            if tu:
                cmap.update(_parse_bf(_stream_of(objs, int(tu.group(1)))))
            if b"/Type0" in head or b"Identity-H" in head or b"UCS2" in head:
                two_byte = True
            if not tu:
                nxt.extend(int(m.group(1)) for m in REF_RE.finditer(head))
        queue = nxt
    return cmap, two_byte


def _parse_bf(stream):
    """Parse a ToUnicode CMap into {code_int: str}."""
    table = {}
    if not stream:
        return table
    text = stream.decode("latin-1", "ignore")

    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            table[int(src, 16)] = _hex_to_str(dst)

    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        for m in re.finditer(
            r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(?:<([0-9A-Fa-f]+)>|\[(.*?)\])",
            block,
            re.S,
        ):
            lo, hi, dst, arr = m.groups()
            lo_i, hi_i = int(lo, 16), int(hi, 16)
            if hi_i - lo_i > 65535:
                continue
            if arr is not None:
                items = re.findall(r"<([0-9A-Fa-f]+)>", arr)
                for off, item in enumerate(items):
                    if lo_i + off <= hi_i:
                        table[lo_i + off] = _hex_to_str(item)
            else:
                base = int(dst, 16)
                width = max(len(dst) // 4, 1)
                for off in range(hi_i - lo_i + 1):
                    table[lo_i + off] = _codeunit(base + off, width)
    return table


def _hex_to_str(hexstr):
    if len(hexstr) % 4:
        hexstr = hexstr.zfill((len(hexstr) + 3) // 4 * 4)
    out = []
    for i in range(0, len(hexstr), 4):
        out.append(chr(int(hexstr[i:i + 4], 16)))
    return "".join(out)


def _codeunit(value, width):
    if width <= 1 and value <= 0xFF:
        return chr(value)
    try:
        return chr(value)
    except ValueError:
        return ""


def _unescape_literal(raw):
    """Undo PDF string escapes in a (...) literal."""
    out = bytearray()
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == 0x5C and i + 1 < len(raw):  # backslash
            nxt = raw[i + 1]
            simple = {0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12,
                      0x28: 0x28, 0x29: 0x29, 0x5C: 0x5C}
            if nxt in simple:
                out.append(simple[nxt])
                i += 2
                continue
            if 0x30 <= nxt <= 0x37:
                octal = bytearray()
                j = i + 1
                while j < len(raw) and len(octal) < 3 and 0x30 <= raw[j] <= 0x37:
                    octal.append(raw[j])
                    j += 1
                out.append(int(octal.decode(), 8) & 0xFF)
                i = j
                continue
            i += 2
            continue
        out.append(ch)
        i += 1
    return bytes(out)


def _decode_with(table, raw, two_byte):
    if not raw:
        return ""
    if not table:
        if two_byte and len(raw) % 2 == 0:
            try:
                return raw.decode("utf-16-be")
            except UnicodeDecodeError:
                pass
        return raw.decode("latin-1", "replace")
    out = []
    step = 2 if two_byte else 1
    for i in range(0, len(raw) - step + 1, step):
        code = int.from_bytes(raw[i:i + step], "big")
        out.append(table.get(code, ""))
    return "".join(out)


TEXT_OP_RE = re.compile(rb"(?:\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]*>)\s*(?:Tj|TJ|'|\")|\[(?:[^\[\]\\]|\\.)*\]\s*TJ|(?:Td|TD|T\*|ET)\b", re.S)
SHOW_RE = re.compile(rb"(\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]*>)\s*Tj", re.S)


def _extract_from_content(stream, cmap, two_byte):
    if not stream:
        return ""
    chunks = []
    for tok in TEXT_OP_RE.finditer(stream):
        s = tok.group(0)
        if s.rstrip().endswith(b"Tj") or s.rstrip().endswith(b"'"):
            for lit, hexs in re.findall(rb"\(((?:\\.|[^\\()])*)\)|<([0-9A-Fa-f\s]*)>", s):
                if lit:
                    chunks.append(_decode_with(cmap, _unescape_literal(lit), two_byte))
                elif hexs:
                    clean = re.sub(rb"\s", b"", hexs)
                    if len(clean) % 2:
                        clean += b"0"
                    chunks.append(_decode_with(cmap, bytes.fromhex(clean.decode()), two_byte))
        elif s.rstrip().endswith(b"TJ"):
            for lit, hexs in re.findall(rb"\(((?:\\.|[^\\()])*)\)|<([0-9A-Fa-f\s]*)>", s):
                if lit:
                    chunks.append(_decode_with(cmap, _unescape_literal(lit), two_byte))
                elif hexs:
                    clean = re.sub(rb"\s", b"", hexs)
                    if len(clean) % 2:
                        clean += b"0"
                    chunks.append(_decode_with(cmap, bytes.fromhex(clean.decode()), two_byte))
        else:
            chunks.append("\n")
    text = "".join(chunks)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def try_builtin(path):
    with open(path, "rb") as fh:
        data = fh.read()
    if not data.startswith(b"%PDF"):
        STATUS("builtin: not a PDF (missing %PDF header)")
        return None

    objs = {}
    for m in OBJ_RE.finditer(data):
        num = int(m.group(1))
        body = m.group(3)
        sm = STREAM_RE.search(body)
        raw = sm.group(1) if sm else None
        head = body[:sm.start()] if sm else body
        objs[num] = (head, raw)

    page_nums = [n for n, (head, _s) in objs.items()
                 if re.search(rb"/Type\s*/Page\b", head) and not re.search(rb"/Type\s*/Pages\b", head)]
    page_nums.sort()
    if not page_nums:
        STATUS("builtin: no /Type /Page objects found "
               "(object streams or xref streams are not supported)")
        return None

    pages = []
    for num in page_nums:
        head = objs[num][0]
        res = re.search(rb"/Resources\s*(\d+)\s+\d+\s+R", head)
        res_head = _dict_of(objs, int(res.group(1))) if res else head
        cmap, two_byte = _collect_fonts(objs, res_head)

        contents = b""
        cm = re.search(rb"/Contents\s+(\d+)\s+\d+\s+R", head)
        if cm:
            contents = _stream_of(objs, int(cm.group(1))) or b""
        else:
            arr = re.search(rb"/Contents\s*\[(.*?)\]", head, re.S)
            if arr:
                for ref in REF_RE.finditer(arr.group(1)):
                    contents += (_stream_of(objs, int(ref.group(1))) or b"") + b"\n"

        pages.append(_extract_from_content(contents, cmap, two_byte))

    return {"backend": "builtin", "pages": pages}


# ---------------------------------------------------------------------- driver

def read_pdf(path, quiet=False):
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    for name, fn in (("pypdf", try_pypdf), ("pdfminer", try_pdfminer), ("builtin", try_builtin)):
        got = fn(path)
        if got and any(p.strip() for p in got["pages"]):
            if not quiet:
                STATUS("%s: %d page(s)" % (got["backend"], len(got["pages"])))
            return got
        if not quiet and got is not None:
            STATUS("%s: parsed but extracted no text" % name)
    return None


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    flags = {a for a in argv[1:] if a.startswith("--")}
    quiet = "--quiet" in flags
    as_json = "--json" in flags
    page_spec = None
    for i, a in enumerate(argv[1:]):
        if a == "--pages" and i + 2 <= len(argv[1:]):
            page_spec = argv[i + 2]
    args = [a for a in args if a != page_spec] if page_spec else args

    if not args:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print("usage: read_pdf.py FILE.pdf [--pages 1,3-5] [--json] [--quiet]", file=sys.stderr)
        return 2

    path = args[0]
    got = read_pdf(path, quiet=quiet)
    if not got:
        print("no text extracted: %s" % path, file=sys.stderr)
        return 1

    sel = parse_pages(page_spec, len(got["pages"]))
    pages = [got["pages"][i] for i in sel]

    if as_json:
        print(json.dumps({"backend": got["backend"], "page_count": len(got["pages"]),
                          "pages": [{"page": i + 1, "text": pages[k]}
                                    for k, i in enumerate(sel)]},
                         ensure_ascii=False, indent=2))
    else:
        for k, i in enumerate(sel):
            print("--- page %d ---" % (i + 1))
            print(pages[k])
            print()

    return 0 if any(p.strip() for p in pages) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
