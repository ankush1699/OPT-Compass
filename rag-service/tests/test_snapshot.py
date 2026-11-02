import gzip
import json
import sys

import numpy as np
import pytest

from app import snapshot
from eval import run_eval


def test_vector_round_trip_is_exact():
    vec = np.random.default_rng(1).normal(size=768).astype("float32")
    assert np.array_equal(snapshot.decode_vector(snapshot.encode_vector(vec)), vec)


def header_lines(**overrides):
    header = {"format": 1, "embed_model": "BAAI/bge-base-en-v1.5", "dim": 768, "count": 3, **overrides}
    return iter([json.dumps(header) + "\n"])


def test_header_of_a_matching_snapshot_is_accepted():
    assert snapshot.read_header(header_lines())["count"] == 3


@pytest.mark.parametrize("overrides", [{"embed_model": "other/model"}, {"dim": 384}, {"format": 99}])
def test_a_snapshot_from_another_model_or_format_is_refused(overrides):
    with pytest.raises(SystemExit):
        snapshot.read_header(header_lines(**overrides))


class FakeConn:
    def __init__(self, existing):
        self.existing, self.inserted = existing, []

    def execute(self, sql, params=()):
        conn = self

        class Cursor:
            rowcount = 1

            def fetchone(self):
                return (conn.existing + len(conn.inserted),)

        if sql.lstrip().startswith("INSERT"):
            conn.inserted.append(params)
        return Cursor()

    def commit(self):
        pass


def write_snapshot(path, n=2):
    vec = snapshot.encode_vector(np.zeros(768, dtype="float32"))
    with gzip.open(path, "wt") as f:
        f.write(json.dumps({"format": 1, "embed_model": "BAAI/bge-base-en-v1.5", "dim": 768, "count": n}) + "\n")
        for i in range(n):
            f.write(json.dumps({"source": "eCFR", "source_url": "u", "citation": f"c{i}", "content": f"text {i}", "content_hash": f"h{i}", "embedding": vec}) + "\n")


def test_restore_loads_every_chunk(tmp_path, monkeypatch):
    conn = FakeConn(existing=0)
    monkeypatch.setattr(snapshot, "connect", lambda: conn)
    monkeypatch.setattr(snapshot, "init_schema", lambda c: None)
    write_snapshot(tmp_path / "s.gz", n=2)
    assert snapshot.restore(tmp_path / "s.gz") == 2 and len(conn.inserted) == 2


def test_restore_if_empty_leaves_a_filled_table_alone(tmp_path, monkeypatch):
    conn = FakeConn(existing=550)
    monkeypatch.setattr(snapshot, "connect", lambda: conn)
    monkeypatch.setattr(snapshot, "init_schema", lambda c: None)
    write_snapshot(tmp_path / "s.gz")
    assert snapshot.restore(tmp_path / "s.gz", if_empty=True) == 0 and conn.inserted == []


def test_the_committed_snapshot_matches_the_configured_embedding_model():
    with gzip.open(snapshot.DEFAULT_PATH, "rt") as f:
        header = snapshot.read_header(iter(f))
    assert header["count"] == 550


def test_eval_gate_threshold_is_read_from_the_command_line(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run_eval", "--min-hit6", "0.9"])
    assert run_eval.gate_threshold() == 0.9
    monkeypatch.setattr(sys, "argv", ["run_eval"])
    assert run_eval.gate_threshold() is None
