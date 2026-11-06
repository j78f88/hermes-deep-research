"""a page gets one source kind, the one its checks gave most often."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fast_research import FastResearch  # noqa: E402


def _seed(fr, url, kinds):
    conn = fr.store.conn()
    for i, kind in enumerate(kinds):
        cur = conn.execute("INSERT INTO claims (run_id, job_id, text, quote, page_url, check_status) VALUES (?,?,?,?,?,?)",
                           (fr.run_id, "j", f"claim {url} {i}", f"quote number {i}", url, "pass"))
        conn.execute("INSERT INTO claim_support (claim_id, run_id, verdict, checked_at, source_kind) VALUES (?,?,?,?,?)",
                     (cur.lastrowid, fr.run_id, "supported", 0, kind))
    conn.commit()


def test_majority_kind_and_cautious_tie(tmp_path):
    fr = FastResearch(tmp_path, "q", run_id="kinds", skip_preflight=True)
    _seed(fr, "https://a.example/x", ["self", "self", "discovery"])
    _seed(fr, "https://b.example/y", ["self", "discovery"])
    _seed(fr, "https://c.example/z", ["garbage"])
    kinds = fr._page_kinds({"https://a.example/x", "https://b.example/y", "https://c.example/z", "https://none.example"})
    assert kinds == {"https://a.example/x": "self", "https://b.example/y": "discovery", "https://c.example/z": "unknown"}
