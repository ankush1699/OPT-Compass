"""Save the chunk table to one file and load it back, so a server (or CI) gets the same corpus and embeddings
without fetching the sources or running the embedding model.

  python -m app.snapshot dump snapshot/chunks.jsonl.gz
  python -m app.snapshot restore [path] [--if-empty]

Line 1 of the file is a header (format, embedding model, dimension, count); each later line is one chunk with its
embedding stored as base64 float32, so a restore is bit-for-bit what was dumped. A restore refuses a file made with a
different embedding model or size, because those vectors would not match the query embeddings.
"""
import base64
import gzip
import json
import sys
from pathlib import Path

import numpy as np

from app.config import load_settings
from app.db import connect, init_schema

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "snapshot" / "chunks.jsonl.gz"
FORMAT = 1
FIELDS = ("source", "source_url", "citation", "content", "content_hash")


def encode_vector(vec) -> str:
    return base64.b64encode(np.asarray(vec, dtype="<f4").tobytes()).decode("ascii")


def decode_vector(text: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(text), dtype="<f4")


def dump(path: Path) -> int:
    settings = load_settings()
    conn = connect()
    rows = conn.execute(f"SELECT {', '.join(FIELDS)}, embedding FROM chunks ORDER BY id").fetchall()
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(json.dumps({"format": FORMAT, "embed_model": settings.embed_model, "dim": settings.embed_dim, "count": len(rows)}) + "\n")
        for row in rows:
            record = dict(zip(FIELDS, row[:-1]))
            record["embedding"] = encode_vector(row[-1])
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"dumped {len(rows)} chunks to {path}")
    return len(rows)


def read_header(lines) -> dict:
    header = json.loads(next(lines))
    settings = load_settings()
    if header.get("format") != FORMAT:
        raise SystemExit(f"Unsupported snapshot format {header.get('format')!r}.")
    if header["embed_model"] != settings.embed_model or header["dim"] != settings.embed_dim:
        raise SystemExit(
            f"Snapshot was made with {header['embed_model']} ({header['dim']} dims) but this server uses "
            f"{settings.embed_model} ({settings.embed_dim}); refusing to load vectors that would not match."
        )
    return header


def restore(path: Path, if_empty: bool = False) -> int:
    conn = connect()
    init_schema(conn)
    existing = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    if if_empty and existing:
        print(f"chunks table already has {existing} rows; nothing to restore")
        return 0
    added = 0
    with gzip.open(path, "rt", encoding="utf-8") as f:
        lines = iter(f)
        header = read_header(lines)
        for line in lines:
            r = json.loads(line)
            vec = decode_vector(r["embedding"])
            if len(vec) != header["dim"]:
                raise SystemExit(f"A chunk has {len(vec)} dimensions, expected {header['dim']}.")
            cur = conn.execute(
                "INSERT INTO chunks (source, source_url, citation, content, content_hash, embedding) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (source_url, content_hash) DO NOTHING",
                (*(r[k] for k in FIELDS), vec),
            )
            added += cur.rowcount
    conn.commit()
    total = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    if total != header["count"] and not existing:
        raise SystemExit(f"Expected {header['count']} chunks after the restore, found {total}.")
    print(f"restored {added} chunks ({total} in the database)")
    return added


def main(argv: list[str]) -> int:
    if len(argv) < 1 or argv[0] not in ("dump", "restore"):
        print(__doc__)
        return 2
    paths = [Path(a) for a in argv[1:] if not a.startswith("--")]
    path = paths[0] if paths else DEFAULT_PATH
    if argv[0] == "dump":
        dump(path)
    else:
        restore(path, if_empty="--if-empty" in argv)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
