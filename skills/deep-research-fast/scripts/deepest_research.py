#!/usr/bin/env python3
"""Deepest research: a base fast run, parallel follow-ups and one checked answer."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fast_research import FastResearch, PhaseError, load_config
import fast_prompts as P
import fast_worker as fw


def pick_questions(raw, main_question: str, max_questions: int = 4, *, earlier=()) -> list[dict]:
    """Keep distinct, highest-impact questions. Fewer than two is a valid result."""
    def words(text):
        return re.sub(r"[^\w\s]", "", text.lower()).split()
    main = words(main_question)
    rows = []
    for row in raw if isinstance(raw, list) else []:
        if not isinstance(row, dict):
            continue
        question, impact = row.get("question"), row.get("impact")
        if not isinstance(question, str) or not question.strip() or type(impact) is not int or not 1 <= impact <= 5:
            continue
        key = words(question)
        if not key or key == main:
            continue
        rows.append({"question": question.strip(), "gap": row.get("gap") if isinstance(row.get("gap"), str) else "",
                     "impact": impact})
    chosen, keys = [], [set(words(question)) for question in earlier]
    for row in sorted(rows, key=lambda r: -r["impact"]):
        key = set(words(row["question"]))
        if any(len(key & other) / len(key | other) >= 0.8 for other in keys):
            continue
        chosen.append(row)
        keys.append(key)
        if len(chosen) >= max(0, min(int(max_questions), 10)):
            break
    return chosen if max_questions > 0 else []


class DeepestResearch(FastResearch):
    ID_PREFIX = "deepest"

    def __init__(self, root, question, **kwargs):
        super().__init__(root, question, **kwargs)
        # Child ids append '-q' and ten hex characters. Never silently alter a supplied id.
        if len(self.run_id) > 116:
            raise ValueError("deepest run id must leave room for child ids (at most 116 characters)")
        self.dcfg = copy.deepcopy(self.cfg.get("deepest", {}))
        self.dcfg["max_questions"] = max(0, min(int(self.dcfg.get("max_questions", 4)), 10))
        self.dcfg["parallel_branches"] = max(1, min(int(self.dcfg.get("parallel_branches", 4)), 4))
        self.rcfg = self.dcfg.get("review_round", {})
        self.rcfg["max_questions"] = max(0, min(int(self.rcfg.get("max_questions", 4)), 4))

    def _run(self, stop_after):
        # FastResearch.run holds the controller lock before entering here.
        failed = [phase for name, phase in (("branches.json", "branches"), ("review_branches.json", "review_round_branches"))
                  if any(row["status"] == "failed" for row in (self._read_json(name) or []))]
        for phase in failed:
            self.store.start_phase(self.run_id, phase)
        if failed:
            archive = self.store.phase(self.run_id, "review_round_archive")
            decision = self._read_json("review_questions.json")
            stamp = time.time_ns()
            prefix = None  # the round has started but its answer is not written: nothing to redo yet
            if archive and archive["status"] == "done":
                prefix = "review_round_"
            elif not (decision and decision["ran"]):
                # A rebuilt first review can change whether another wave is useful.
                prefix = ""
                if self.store.phase(self.run_id, "review_round_questions"):
                    self.store.start_phase(self.run_id, "review_round_questions")
                for name in ("review_questions.json", "review-questions"):
                    if self.path(name).exists():
                        self.path(name).replace(self.path(f"{name}.legacy-{stamp}"))
            if prefix is not None:
                for name in ("answer", "check", "review", "finish"):
                    self.store.start_phase(self.run_id, prefix + name)
                self._archive_answer(suffix=f".legacy-{stamp}")
        return super()._run(stop_after)

    def answer_stages(self, stop_after=None, *, prefix=""):
        # Once archive starts, top-level files belong to round two. Never run round one again.
        if not self.store.phase(self.run_id, "review_round_archive"):
            super().answer_stages(stop_after)
            if stop_after in ("answer", "check", "review", "finish"):
                return
        decision = self._phase("review_round_questions", self.review_questions)
        if not decision["ran"]:
            return
        self._phase("review_round_branches", lambda: self.branches("review_questions.json", "review_branches.json"))
        self._phase("review_round_archive", lambda: self._archive_answer(folder="round1"))
        super().answer_stages(stop_after, prefix="review_round_")

    def _archive_answer(self, *, folder=None, suffix=""):
        if folder:
            self.path(folder).mkdir(exist_ok=True)
        # Move cached replies too, so adopted workers cannot reuse the first answer or review.
        for name in ("answer", "sentence-check", "review", "answer.md", "answer_draft.md", "evidence.md",
                     "citations.json", "quote_check.json", "answer.pre-check.md", "citations.pre-check.json",
                     "sentence_check.json", "grades.json", "not_verified.json", "review.json"):
            source = self.path(name)
            target = self.path(folder, name) if folder else self.path(name + suffix)
            if source.exists() and not target.exists():
                source.replace(target)
        return {"folder": folder}

    def review_questions(self):
        review = self._usable_review()
        missing = review.get("missing_parts")
        gaps = [item.strip() for item in missing if isinstance(item, str) and item.strip()] if isinstance(missing, list) else []
        enabled = str(self.rcfg.get("enabled", True)).strip().lower() not in ("false", "0", "no", "none", "")
        reason = ("disabled" if not enabled or self.rcfg["max_questions"] == 0 else
                  "accepted" if review.get("verdict") == "accept" else
                  "no_missing_parts" if not gaps else None)
        data = {"ran": False, "skipped": reason, "questions": [],
                "first_readiness": self.readiness(), "first_review_issue_counts": self._review_issues()}
        if not reason:
            earlier = [row["question"] for row in self._read_json("questions.json")["questions"]]
            claims = self._admitted()
            rejected = [c["text"] for rid in self.source_run_ids() for c in self.store.claims(rid)
                        if c["check_status"] != "pass"]
            res = self.call("writer", P.FOLLOWUPS.format(question=self.question, admitted=self._findings(claims),
                           gaps=json.dumps({"missing_parts": gaps, "already_asked_do_not_repeat": earlier}),
                           rejected=json.dumps(rejected), max_questions=self.rcfg["max_questions"]),
                           self.path("review-questions"), budget=self.cfg["budgets_seconds"]["plan"])
            parsed = fw.parse_json_reply(res.stdout) or {}
            data["questions"] = pick_questions(parsed.get("questions"), self.question, self.rcfg["max_questions"], earlier=earlier)
            data["ran"] = bool(data["questions"])
            data["skipped"] = None if data["ran"] else "no_questions"
        self.write_json("review_questions.json", data)
        return data

    def research_steps(self):
        return [("base", self.base), ("questions", self.questions), ("branches", self.branches)]

    def source_run_ids(self):
        return [r["id"] for r in self.store.child_runs(self.run_id)]

    def child_id(self, question, *, base=False):
        return f"{self.run_id}-base" if base else f"{self.run_id}-q{hashlib.sha1(question.encode()).hexdigest()[:10]}"

    def child(self, question, *, base=False):
        config = copy.deepcopy(self.cfg)
        if not base:
            limits = self.dcfg.get("branch_limits", {})
            config["limits"].update({"concurrency": limits.get("concurrency", 4),
                                     "max_search_fetch_jobs": limits.get("max_search_fetch_jobs", 16),
                                     "research_deadline_seconds": limits.get("research_deadline_seconds", 720)})
            for key in ("max_search_jobs", "max_fetch_jobs"):
                config["limits"][key] = min(int(limits.get(key, config["limits"][key])),
                                             int(config["limits"]["max_search_fetch_jobs"]))
        child = FastResearch(self.root, question, run_id=self.child_id(question, base=base), parent_run_id=self.run_id,
                             skip_preflight=True, config=config, oneshot=self.oneshot, harvest_pages=self.harvest_pages,
                             harvest_search=self.harvest_search, usage=self.usage, log=self._log_fn)
        child.lease_poll = self.lease_poll
        return child

    def base(self):
        child = self.child(self.question, base=True)
        record = child.run(stop_after="research")
        if record["status"] != "completed":
            error = RuntimeError if record["status"] == "failed" else PhaseError
            raise error(f"base research did not complete: {record['error']}")
        return {"run_id": child.run_id}


    def questions(self):
        base = self.child(self.question, base=True)
        admitted = base._admitted()
        covered = {c["topic"] for c in admitted}
        gaps = [t for t in base._topics() if t["id"] not in covered]
        rejected = [c["text"] for c in self.store.claims(base.run_id) if c["check_status"] != "pass"]
        res = self.call("writer", P.FOLLOWUPS.format(question=self.question, admitted=self._findings(admitted),
                       gaps=json.dumps(gaps), rejected=json.dumps(rejected), max_questions=self.dcfg["max_questions"]),
                       self.path("questions"), budget=self.cfg["budgets_seconds"]["plan"])
        parsed = fw.parse_json_reply(res.stdout) or {}
        questions = pick_questions(parsed.get("questions"), self.question, self.dcfg["max_questions"])
        data = {"questions": questions, "fewer_than_two": len(questions) < 2}
        self.write_json("questions.json", data)
        return data

    def _branch_counts(self, run_id, question, status, error=None):
        jobs = self.store.job_counts(run_id)
        admitted = self.store.claims(run_id, status="pass")
        pages = self.store.conn().execute("SELECT COUNT(*) FROM pages WHERE run_id=?", (run_id,)).fetchone()[0]
        return {"run_id": run_id, "question": question, "status": status, "error": error,
                "jobs": sum(sum(counts.values()) for counts in jobs.values()),
                "admitted_findings": len(admitted), "new_pages": pages}

    def branches(self, questions_file="questions.json", branches_file="branches.json"):
        questions = self._read_json(questions_file)["questions"]
        saved = {row["run_id"]: row for row in (self._read_json(branches_file) or [])}
        pending = [row for row in questions if self.child_id(row["question"]) not in saved
                   or saved[self.child_id(row["question"])]["status"] == "failed"]
        def run(row):
            run_id = self.child_id(row["question"])
            try:
                child = self.child(row["question"])
                rec = child.run(stop_after="research")
                return self._branch_counts(child.run_id, row["question"], rec["status"], rec["error"])
            except Exception as exc:
                self.log(f"branch {run_id}: failed {exc!r}")
                return self._branch_counts(run_id, row["question"], "failed", repr(exc))
        with ThreadPoolExecutor(max_workers=self.dcfg["parallel_branches"]) as pool:
            saved.update((row["run_id"], row) for row in pool.map(run, pending))
        results = [saved[self.child_id(row["question"])] for row in questions]
        self.write_json(branches_file, results)
        return {"branches": results}

    def answer(self):
        self._check_legacy_passes()
        self._checker_independent()
        claims = self._admitted()
        if not claims:
            raise PhaseError("no finding passed the quote and meaning checks, so there is nothing to cite")
        questions = {row["id"]: row["question"] for row in self.store.child_runs(self.run_id)}
        findings = "\n\n".join(f"Original question: {questions[c['run_id']]}\n{self._findings([c])}" for c in claims)
        return self._write_answer(P.SYNTHESIS.format(question=self.question, findings=findings), claims)

    def record(self, status="completed", error=None, end_at=None):
        rec = super().record(status=status, error=error, end_at=end_at)
        rec["questions"] = (self._read_json("questions.json") or {}).get("questions", [])
        rec["branches"] = self._read_json("branches.json") or []
        rec["review_round"] = self._read_json("review_questions.json") or {
            "ran": False, "skipped": "pending", "questions": [],
            "first_readiness": None, "first_review_issue_counts": {}}
        rec["review_round"]["branches"] = self._read_json("review_branches.json") or []
        first = self.path("round1", "answer.md")
        if status == "stopped" and first.exists() and not self.path("answer.md").exists():
            # The second round stopped. The first answer was checked and reviewed, so point at it.
            rec["answer_path"] = str(first)
            rec["review_path"] = str(self.path("round1", "review.json"))
            rec["readiness"] = f"{rec['review_round'].get('first_readiness') or self.NOT_REVIEWED} This is the first-round answer, because the second round stopped."
        self.write_json("run.json", rec)
        return rec


def main(argv=None):
    ap = argparse.ArgumentParser(description="Deepest research")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run or resume deepest research")
    run.add_argument("--question", required=True)
    run.add_argument("--root", required=True)
    run.add_argument("--run-id")
    args = ap.parse_args(argv)
    try:
        rec = DeepestResearch(args.root, args.question, run_id=args.run_id, log=print).run()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({key: rec[key] for key in ("run_id", "status", "error", "wall_seconds", "quote_check", "answer_path", "readiness", "review_path", "review_round")}, indent=2))
    return 0 if rec["status"] == "completed" and rec["quote_check"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
