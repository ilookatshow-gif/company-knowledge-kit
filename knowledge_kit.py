#!/usr/bin/env python3
"""Original offline lexical evidence retrieval. Document contents are data only."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
import unicodedata

VERSION = "0.1.0"
MAX_FILES = 200
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 5 * 1024 * 1024
MAX_INDEX_BYTES = 8 * 1024 * 1024
INDEX_KIND = "company-knowledge-kit-index"


class KitError(Exception):
    def __init__(self, code, message, exit_code=2):
        self.code, self.message, self.exit_code = code, message, exit_code
        super().__init__(code)


def fail(code, message, exit_code=2):
    raise KitError(code, message, exit_code)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def reject_symlinks(path):
    path = Path(os.path.abspath(str(path)))
    for part in (path,) + tuple(path.parents):
        if part.is_symlink():
            fail("SYMLINK_REFUSED", "Symlink paths are not accepted.")
    return path


def read_bytes(path, maximum):
    reject_symlinks(path)
    if not path.is_file():
        fail("INPUT_NOT_FILE", "An input must be a regular file.")
    with path.open("rb") as handle:
        data = handle.read(maximum + 1)
    if len(data) > maximum:
        fail("INPUT_TOO_LARGE", "An input exceeds the documented size limit.")
    return data


def load_json(path, maximum):
    try:
        return json.loads(read_bytes(Path(path), maximum).decode("utf-8"),
                          object_pairs_hook=unique_keys)
    except (UnicodeError, ValueError):
        fail("INVALID_JSON", "Input must be UTF-8 JSON with unique keys.")


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def relative_path(value):
    if (not isinstance(value, str) or not value or "\\" in value or ":" in value
            or value.startswith("/") or any(x in ("", ".", "..") for x in value.split("/"))):
        fail("INVALID_PATH", "Document paths must be simple relative POSIX paths.")
    path = PurePosixPath(value)
    if path.suffix.lower() not in (".md", ".txt"):
        fail("UNSUPPORTED_FILE", "Only UTF-8 .md and .txt documents are supported.")
    return value


def corpus_root(value):
    path = Path(value).resolve(strict=True)
    if not path.is_dir():
        fail("INVALID_ROOT", "The corpus root must be an existing directory.")
    return path


def source_path(root, value):
    relative_path(value)
    path = root.joinpath(*value.split("/"))
    reject_symlinks(path)
    if root not in path.resolve().parents:
        fail("INVALID_PATH", "A source must stay within the corpus root.")
    return path


def metadata(doc):
    if not isinstance(doc, dict):
        fail("INVALID_MANIFEST", "Every document entry must be an object.")
    ident, title = doc.get("id"), doc.get("title")
    if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", ident):
        fail("INVALID_ID", "Document IDs must be short stable identifiers.")
    if not isinstance(title, str) or not title.strip() or len(title) > 300:
        fail("INVALID_TITLE", "Each document needs a title of at most 300 characters.")
    status = doc.get("status", "active")
    if status not in ("active", "archived"):
        fail("INVALID_STATUS", "Status must be active or archived.")
    version = doc.get("version")
    if version is not None and (not isinstance(version, str) or len(version) > 100):
        fail("INVALID_VERSION", "Version must be a short string or null.")
    return {"id": ident, "path": relative_path(doc.get("path")), "title": title,
            "status": status, "version": version}


def check_unique(documents):
    ids, paths = set(), set()
    for doc in documents:
        if doc["id"] in ids or doc["path"] in paths:
            fail("DUPLICATE_DOCUMENT", "IDs and source paths must be unique.")
        ids.add(doc["id"])
        paths.add(doc["path"])


def write_index(output, value, replace=False):
    output = reject_symlinks(Path(output))
    if not output.parent.is_dir():
        fail("OUTPUT_PARENT_MISSING", "Create the output parent directory explicitly first.")
    if output.exists() and not replace:
        fail("OUTPUT_EXISTS", "Index exists; use --replace only for an intentional rebuild.")
    if output.exists() and not output.is_file():
        fail("OUTPUT_NOT_FILE", "Index output must be a regular file.")
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if len(payload) > MAX_INDEX_BYTES:
        fail("INDEX_TOO_LARGE", "Serialized index exceeds its size limit.")
    fd, temporary = tempfile.mkstemp(prefix=".knowledge-index-", dir=str(output.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        reject_symlinks(output)
        if replace:
            os.replace(temporary, str(output))
        else:
            # Hard-link creates the destination atomically without overwriting a racing writer.
            os.link(temporary, str(output))
        return sha(payload)
    except FileExistsError:
        fail("OUTPUT_EXISTS", "Index exists; no overwrite was performed.")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def build(manifest, output, replace=False):
    manifest = reject_symlinks(Path(manifest))
    manifest_digest = sha(read_bytes(manifest, MAX_FILE_BYTES))
    value = load_json(manifest, MAX_FILE_BYTES)
    if (not isinstance(value, dict) or set(value) != {"documents"}
            or not isinstance(value["documents"], list)
            or not 1 <= len(value["documents"]) <= MAX_FILES):
        fail("INVALID_MANIFEST", "Manifest must contain a documents list with 1 to 200 entries.")
    root = corpus_root(manifest.parent)
    documents = []
    for doc in value["documents"]:
        if not isinstance(doc, dict) or set(doc) - {"id", "path", "title", "status", "version"}:
            fail("INVALID_MANIFEST", "Manifest entries contain unsupported fields.")
        documents.append(metadata(doc))
    check_unique(documents)
    output = reject_symlinks(Path(output))
    inputs = [manifest] + [source_path(root, doc["path"]) for doc in documents]
    for input_path in inputs:
        if output == input_path or (output.exists() and input_path.exists()
                                    and os.path.samefile(output, input_path)):
            fail("OUTPUT_IS_SOURCE", "Index output must not replace the manifest or a source document.")
    total = 0
    for doc in documents:
        data = read_bytes(source_path(root, doc["path"]), MAX_FILE_BYTES)
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            fail("CORPUS_TOO_LARGE", "The corpus exceeds the 5 MiB total limit.")
        try:
            text = data.decode("utf-8")
        except UnicodeError:
            fail("INVALID_UTF8", "Documents must contain valid UTF-8 text.")
        if "\x00" in text:
            fail("BINARY_DOCUMENT", "Documents must not contain NUL bytes.")
        doc.update({"text": text, "sha256": sha(data), "bytes": len(data),
                    "line_count": len(text.splitlines())})
    if sha(read_bytes(manifest, MAX_FILE_BYTES)) != manifest_digest:
        fail("SOURCE_CHANGED_DURING_BUILD", "Manifest changed during the build; no index was written.")
    index = {"schema_version": 1, "kind": INDEX_KIND,
             "manifest": {"path": manifest.name, "sha256": manifest_digest}, "documents": documents}
    digest = write_index(output, index, replace)
    return {"ok": True, "command": "index build", "documents": len(documents),
            "active_documents": sum(d["status"] == "active" for d in documents),
            "source_bytes": total, "index_sha256": digest, "network_used": False}


def load_index(index_path):
    value = load_json(index_path, MAX_INDEX_BYTES)
    if (not isinstance(value, dict) or set(value) != {"schema_version", "kind", "manifest", "documents"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["kind"] != INDEX_KIND or not isinstance(value["documents"], list)
            or not 1 <= len(value["documents"]) <= MAX_FILES):
        fail("INVALID_INDEX", "Unsupported or malformed index schema.")
    manifest = value["manifest"]
    if (not isinstance(manifest, dict) or set(manifest) != {"path", "sha256"}
            or not isinstance(manifest["path"], str) or not manifest["path"]
            or manifest["path"] in (".", "..")
            or any(character in manifest["path"] for character in ("/", "\\", ":"))
            or not isinstance(manifest["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", manifest["sha256"])):
        fail("INVALID_INDEX", "Malformed manifest reference in index.")
    total = 0
    for doc in value["documents"]:
        if (not isinstance(doc, dict) or set(doc) != {"id", "path", "title", "status", "version",
                                                   "text", "sha256", "bytes", "line_count"}):
            fail("INVALID_INDEX", "Malformed indexed document.")
        metadata(doc)
        text = doc["text"]
        if not isinstance(text, str) or "\x00" in text:
            fail("INVALID_INDEX", "Indexed text must be plain UTF-8.")
        data = text.encode("utf-8")
        total += len(data)
        if (type(doc["bytes"]) is not int or type(doc["line_count"]) is not int
                or doc["bytes"] != len(data) or doc["line_count"] != len(text.splitlines())
                or doc["sha256"] != sha(data) or len(data) > MAX_FILE_BYTES
                or total > MAX_TOTAL_BYTES):
            fail("INVALID_INDEX", "Indexed hashes, sizes or line metadata are inconsistent.")
    check_unique(value["documents"])
    return value


def verify_sources(index, root):
    try:
        root = corpus_root(root)
        manifest_path = root / index["manifest"]["path"]
        if sha(read_bytes(manifest_path, MAX_FILE_BYTES)) != index["manifest"]["sha256"]:
            fail("STALE_INDEX", "The source manifest changed; rebuild the index.", 3)
        manifest = load_json(manifest_path, MAX_FILE_BYTES)
        if (not isinstance(manifest, dict) or not isinstance(manifest.get("documents"), list)
                or [metadata(d) for d in manifest["documents"]]
                != [metadata(d) for d in index["documents"]]):
            fail("STALE_INDEX", "Indexed source metadata no longer matches the manifest.", 3)
        for doc in index["documents"]:
            data = read_bytes(source_path(root, doc["path"]), MAX_FILE_BYTES)
            if sha(data) != doc["sha256"]:
                fail("STALE_INDEX", "A source changed; rebuild the index before using citations.", 3)
    except (KitError, OSError):
        fail("STALE_INDEX", "A source changed, is missing or unsafe; rebuild the index.", 3)


def tokens(text):
    return re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold(), flags=re.UNICODE)


def query_terms(query):
    if not isinstance(query, str) or len(query) > 1000:
        fail("INVALID_QUERY", "Queries must be at most 1000 characters.")
    result = list(dict.fromkeys(tokens(query)))
    if not 1 <= len(result) <= 16:
        fail("INVALID_QUERY", "Use 1 to 16 distinct lexical query terms.")
    return result


def chunks(text, lines_per_chunk=12):
    lines = text.splitlines()
    for start in range(0, len(lines), lines_per_chunk):
        end = min(start + lines_per_chunk, len(lines))
        yield start + 1, end, "\n".join(lines[start:end])


def citation(doc, start, end, text):
    return {key: doc[key] for key in ("id", "path", "title", "status", "version", "sha256")} | {
        "start_line": start, "end_line": end, "text": text}


def search(index_path, root, query, limit=5):
    if type(limit) is not int or not 1 <= limit <= 20:
        fail("INVALID_LIMIT", "Limit must be between 1 and 20.")
    terms = query_terms(query)
    index = load_index(index_path)
    verify_sources(index, root)
    candidates = []
    for doc in index["documents"]:
        if doc["status"] != "active":
            continue
        for start, end, text in chunks(doc["text"]):
            found = tokens(text)
            if all(term in found for term in terms):
                hit = citation(doc, start, end, text)
                hit["matched_terms"] = terms
                candidates.append((sum(found.count(t) for t in terms), doc["id"], start, hit))
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    return {"ok": True, "command": "search", "status": "found" if candidates else "not_found",
            "query_terms": terms, "matching_chunks": len(candidates),
            "results": [x[3] for x in candidates[:limit]], "limit": limit,
            "truncated": len(candidates) > limit, "source_freshness": "verified",
            "retrieval": "exact_normalized_words_all_terms_per_12_line_chunk",
            "generated_answer": False, "network_used": False}


def source_show(index_path, root, ident, start=1, end=None):
    index = load_index(index_path)
    verify_sources(index, root)
    doc = next((d for d in index["documents"] if d["id"] == ident), None)
    if doc is None:
        fail("SOURCE_NOT_FOUND", "No source with that ID is recorded in this index.")
    lines = doc["text"].splitlines()
    if not lines:
        if start != 1 or end is not None:
            fail("INVALID_RANGE", "An empty source has no line range.")
        return {"ok": True, "command": "source show", "source": citation(doc, 0, 0, ""),
                "source_freshness": "verified", "network_used": False}
    end = min(start + 11, len(lines)) if end is None else end
    if not 1 <= start <= end <= len(lines) or end - start + 1 > 60:
        fail("INVALID_RANGE", "Choose a valid inclusive range of at most 60 lines.")
    return {"ok": True, "command": "source show",
            "source": citation(doc, start, end, "\n".join(lines[start - 1:end])),
            "source_freshness": "verified", "network_used": False}


def demo():
    source = Path(__file__).resolve().parent / "examples"
    with tempfile.TemporaryDirectory(prefix="company-knowledge-demo-") as temp:
        index = Path(temp).resolve() / "index.json"
        build(source / "corpus.json", index)
        result = search(index, source, "возврат")
        result.update({"command": "demo", "synthetic": True})
        return result


def onboard():
    # Explicit, once-per-install onboarding; ordinary search output never contains advertising.
    marker = Path(__file__).resolve().parent / ".onboarded"
    try:
        with marker.open("x", encoding="utf-8") as handle:
            handle.write(VERSION + "\n")
    except FileExistsError:
        return {"ok": True, "command": "onboard", "already_shown": True}
    return {"ok": True, "command": "onboard", "already_shown": False,
            "author": "Дмитрий Лукашов", "channel": "https://t.me/lookatsoul",
            "next_step": "https://lookatshow.ru/executive?utm_source=github&utm_medium=open_module&utm_campaign=company-knowledge-kit&utm_content=onboard"}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        fail("CLI_USAGE", "Invalid command arguments; use --help for the command contract.")


def main(argv=None):
    try:
        parser = Parser(description="Offline lexical source search with exact line citations; no LLM or network.")
        parser.add_argument("--json", action="store_true", help="Output is always stable JSON.")
        parser.add_argument("--version", action="version", version=VERSION)
        commands = parser.add_subparsers(dest="command", required=True)
        commands.add_parser("doctor", help="Check runtime and bundled synthetic fixtures; no auth required.")
        commands.add_parser("demo", help="Search the bundled synthetic corpus without persistent output.")
        commands.add_parser("onboard", help="Show optional author links once; writes .onboarded beside CLI.")
        index = commands.add_parser("index", help="Build an explicit allowlist index.").add_subparsers(dest="verb", required=True)
        build_parser = index.add_parser("build", help="Read manifest files and create a local snapshot.")
        build_parser.add_argument("--manifest", required=True)
        build_parser.add_argument("--output", required=True)
        build_parser.add_argument("--replace", action="store_true")
        find = commands.add_parser("search", help="Find active evidence; all words must match one 12-line chunk.")
        find.add_argument("--index", required=True)
        find.add_argument("--root", required=True)
        find.add_argument("--query", required=True)
        find.add_argument("--limit", type=int, default=5)
        show = commands.add_parser("source", help="Read exact source context.").add_subparsers(dest="verb", required=True)
        show_parser = show.add_parser("show", help="Read at most 60 lines, including archived sources by explicit ID.")
        show_parser.add_argument("--index", required=True)
        show_parser.add_argument("--root", required=True)
        show_parser.add_argument("--id", required=True)
        show_parser.add_argument("--start", type=int, default=1)
        show_parser.add_argument("--end", type=int)
        args = parser.parse_args(argv)
        if args.command == "doctor":
            fixtures = (Path(__file__).resolve().parent / "examples" / "corpus.json").is_file()
            result = {"ok": sys.version_info >= (3, 9) and fixtures, "command": "doctor",
                      "version": VERSION, "python_supported": sys.version_info >= (3, 9),
                      "synthetic_fixtures_found": fixtures, "auth": "not_required",
                      "mode": "offline", "network_used": False}
        elif args.command == "demo":
            result = demo()
        elif args.command == "onboard":
            result = onboard()
        elif args.command == "index":
            result = build(Path(args.manifest), Path(args.output), args.replace)
        elif args.command == "search":
            result = search(args.index, args.root, args.query, args.limit)
        else:
            result = source_show(args.index, args.root, args.id, args.start, args.end)
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0 if result["ok"] else 2
    except KitError as error:
        print(json.dumps({"ok": False, "error": {"code": error.code, "message": error.message}}, ensure_ascii=True, sort_keys=True))
        return error.exit_code
    except (OSError, ValueError, TypeError, UnicodeError, RuntimeError):
        print(json.dumps({"ok": False, "error": {"code": "LOCAL_IO_ERROR", "message": "Local input or output could not be processed; no source content or path is exposed."}}, sort_keys=True))
        return 2


if __name__ == "__main__":
    sys.exit(main())
