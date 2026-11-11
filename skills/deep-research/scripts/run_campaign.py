#!/usr/bin/env python3
"""Phased run driver for a deep-research campaign.

A campaign runs as fixed phases. Plain code does every mechanical step; only synthesis, repairs and
review decisions call a model, each as a fresh one-shot Hermes session with its own budget, toolsets and
packet (templates/phases/). No session carries the whole campaign, so none needs a large step budget.

  start   new campaign from a question, target and purpose
  resume  continue from <root>/orchestration/driver-state.json (phases whose hashes match are skipped)
  status  one-line state

Phases, in order:
  setup            init_campaign.py, chosen lanes, controls that do not apply (with reasons), owned assets,
                   tool preflight, worker kit and lane briefs
  lanes            public lane workers (systemd-run --user, or --launcher direct), one blocking wait, per-lane quote check,
                   fan-in into campaign_state.json, quote_check.py --write, body-free read audit
  context_release  campaign_controller.record_context_release, logged propagation, citations.json
  synthesis        model: the reference pack and exhibit ledger; then fact_grades_report.py --write
  qa_reviews       mechanical context-handoff check, then the model evidence/privacy review
  repairs          bounded rounds: model repair of the pack, then the failed reviews again
  plain_brief      plain_brief_rewrite.py --campaign-root (its own writer and editor calls)
  final_reviews    mechanical taxonomy, librarian, visual and final-assurance checks (kit/mechanical_qa.py),
                   then any configured model reviews
  export           export_deliverables.py, scoped git commit and push when the export root is a git repo,
                   archive copy (live, dry-run or skip)
  completion       complete_campaign.py; its verdict is recorded exactly as printed

Skip rule: a phase is skipped when it finished before, the outputs of every earlier phase (its inputs)
are unchanged, and its own outputs still match the hashes the driver last recorded. A phase whose model
session stops for steps, or fails in any other way, stops the run with a clear state; `resume` reruns it.
Each driver run is one append-only run_state_machine segment.
"""
from __future__ import annotations

# Retain the actual executed module, including imports before completion starts.
__checker_module_code__ = __import__("sys")._getframe().f_code

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import string
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import run_state_machine as rsm

SCRIPTS = Path(__file__).resolve().parent
SKILL = SCRIPTS.parent
KIT = SCRIPTS / "kit"
TEMPLATES = SKILL / "templates" / "phases"
SCHEMA = "deep-research-driver-state-v1"
STATE_REL = "orchestration/driver-state.json"
DONE_REL = "orchestration/driver.done"
LOCK_REL = "orchestration/driver.lock"
PHASES = ("setup", "lanes", "context_release", "synthesis", "qa_reviews", "repairs", "plain_brief",
          "final_reviews", "export", "completion")
REVIEW_RECEIPTS = {
    "evidence_privacy": "qa/evidence-privacy-review.json",
    "context_handoff": "qa/context-handoff-review.json",
    "visual": "qa/visual-review.json",
    "taxonomy_review": "qa/taxonomy-review.json",
    "librarian_repair": "qa/librarian-repair-receipt.json",
    "final_assurance": "qa/final-assurance.json",
}
# Happy-path states each phase appends to the run segment (run_state_machine's enum and transitions).
PHASE_STATES = {
    "setup": ["accepted"],
    "lanes": ["research_running", "bundle_validated"],
    "context_release": ["bundle_frozen", "prior_work_scanning", "prior_work_validated"],
    "synthesis": ["outputs_running", "outputs_complete"],
    "qa_reviews": ["reviews_running"],
}
FAIL_STATES = {
    "setup": "blocked_invalid_input", "lanes": "blocked_missing_evidence", "context_release": "blocked_missing_evidence",
    "synthesis": "blocked_missing_canonical_output", "qa_reviews": "blocked_review_hold",
    "repairs": "blocked_review_hold", "plain_brief": "blocked_missing_canonical_output",
    "final_reviews": "blocked_review_hold", "export": "blocked_missing_canonical_output",
    "completion": "blocked_review_hold",
}
DEFAULT_HISTORY_REASON = ("External-only run: the request limits this campaign to public lanes, so no relevant-history "
                          "scan or internal retrieval lane ran.")


class PhaseFailed(Exception):
    def __init__(self, reason: str, *, step_limit: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.step_limit = step_limit


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    except OSError:
        return None


def canonical_sha(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def kit(name: str):
    """Import a kit module lazily, so importing this driver needs only the scripts folder."""
    if str(SCRIPTS / "kit") not in sys.path:
        sys.path.insert(0, str(SCRIPTS / "kit"))
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return __import__(name)


def render(template: str, **fields) -> str:
    text = (TEMPLATES / template).read_text(encoding="utf-8")
    return string.Template(text).substitute(**{k: str(v) for k, v in fields.items()})


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48] or "campaign"


# Mechanical steps that tests replace with stand-ins (they call models, the network or the export root).
def run_tool_preflight(root: Path, primary_source: str | None) -> dict:
    return kit("campaign_setup").tool_preflight(root, primary_source)


def run_owned_assets(root: Path, target: str, supplied: Path | None) -> dict:
    return kit("campaign_setup").write_owned_assets(root, target, supplied)


def run_script(args: list[str], log: Path, timeout: float) -> dict:
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log.open("wb") as fh:
            proc = subprocess.run([sys.executable, *args], stdout=fh, stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL, timeout=timeout)
        return {"rc": proc.returncode, "log": str(log)}
    except subprocess.TimeoutExpired:
        return {"rc": 124, "log": str(log), "error": "timeout"}


def run_plain_brief(root: Path, log: Path, timeout: float) -> dict:
    return run_script([str(SCRIPTS / "plain_brief_rewrite.py"), "--campaign-root", str(root)], log, timeout)


def run_verifier(root: Path) -> dict:
    proc = subprocess.run([sys.executable, str(SCRIPTS / "verify_campaign_depth.py"), "--campaign-root", str(root),
                           "--json"], capture_output=True, text=True, timeout=900)
    data = None
    try:
        data = json.loads(proc.stdout[proc.stdout.index("{"):]) if "{" in proc.stdout else None
    except ValueError:
        data = None
    return data or {"status": "UNKNOWN", "rc": proc.returncode, "stderr": proc.stderr[-1000:], "holds": [], "failures": []}


def run_export(root: Path, mode: str, reason: str | None, run_id: str) -> dict:
    return kit("closeout").export(root, mode, reason, run_id)


def run_completion(root: Path, log: Path, timeout: float) -> dict:
    """complete_campaign.py with an absolute --campaign-root. It prints one JSON report; its status is the verdict."""
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run([sys.executable, str(SCRIPTS / "complete_campaign.py"), "--campaign-root", str(root)],
                              capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"rc": 124, "verdict_line": None, "error": "timeout"}
    log.write_text(proc.stdout + ("\n--- stderr ---\n" + proc.stderr if proc.stderr else ""), encoding="utf-8")
    try:
        printed = json.loads(proc.stdout[proc.stdout.index("{"):]) if "{" in proc.stdout else {}
    except ValueError:
        printed = {}
    status, classes = printed.get("status"), printed.get("classes") or []
    line = f"{status} ({', '.join(classes)})" if status and classes else status
    return {"rc": proc.returncode, "verdict": status, "classes": classes, "verdict_line": line, "log": str(log)}


class Driver:
    def __init__(self, root: Path, state: dict):
        self.root = root
        self.state = state
        self.cfg = state["config"]
        self.ov = state.get("overrides", {})
        self.req = state["request"]
        self.run_state = rsm.RunState()
        self.segment: dict = {}

    # ----------------------------------------------------------------- state
    def save(self) -> None:
        self.state["updated_at"] = now()
        write_json(self.root / STATE_REL, self.state)

    def phase(self, name: str) -> dict:
        return self.state["phases"].setdefault(name, {"status": "pending"})

    def lanes(self) -> list[str]:
        return [lane["id"] for lane in self.req["lanes"]]

    def transition(self, to_state: str, reason: str, hashes: dict | None = None, terminal_reason: str | None = None):
        tr = rsm.Transition(correlation_id=self.state["run_id"], from_state=self.run_state.current_state(),
                            to_state=to_state, occurred_at=now(), actor="run_campaign.py", reason=reason,
                            artifact_hashes={k: v for k, v in (hashes or {}).items() if v},
                            attempt=self.segment["attempt"], retry_of=self.segment.get("retry_of"),
                            terminal_reason=terminal_reason)
        self.run_state = self.run_state.append(tr)
        self.segment["transitions"].append(tr.__dict__.copy())

    def outputs(self, name: str) -> list[str]:
        lanes = self.lanes()
        if name == "setup":
            return ([f"workers/{lane}-packet.md" for lane in lanes] +
                    ["workers/save_source.py", "workers/gh_get.py", "workers/check_my_lane.py",
                     "workers/lane-output-schema.json"])
        if name == "lanes":
            out = [f"lanes/{lane}.{ext}" for lane in lanes for ext in ("json", "md")]
            out += [f"workspace/sources/{lane}/index.jsonl" for lane in lanes]
            return out + ["lanes/08-controller-synthesis-actionregister.json", "lanes/09-board-brief-verification.json"]
        if name == "context_release":
            return ["context/context-brief.json", "context/context-brief.md", "citations.json"]
        if name == "synthesis":
            return ["internal/reference-pack.md", "synthesis/visual-exhibits.json", "synthesis/action-register.md"]
        if name in ("qa_reviews", "repairs"):
            names = list(dict.fromkeys(["context_handoff", *self.cfg["reviews"]["pre_brief"]]))
            extra = ["internal/reference-pack.md", "synthesis/visual-exhibits.json"] if name == "repairs" else []
            return [REVIEW_RECEIPTS[n] for n in names if n in REVIEW_RECEIPTS] + extra
        if name == "plain_brief":
            return ["internal/plain-brief.md", "qa/plain-brief-review.json"]
        if name == "final_reviews":
            names = list(dict.fromkeys([
                "taxonomy_review", "librarian_repair", "visual", "final_assurance",
                *self.cfg["reviews"]["post_brief"], *self.cfg["reviews"]["final"],
            ]))
            return [REVIEW_RECEIPTS[n] for n in names if n in REVIEW_RECEIPTS] + ["qa/mechanical-checks.json"]
        if name == "export":
            return ["receipts/closeout.json"]
        return []

    def current_hashes(self, rels: list[str]) -> dict:
        return {rel: sha256_file(self.root / rel) for rel in rels}

    def input_digest(self, name: str) -> str:
        earlier = PHASES[:PHASES.index(name)]
        return canonical_sha({"request": self.state["request_sha256"],
                              "upstream": {p: self.state["phases"].get(p, {}).get("outputs", {}) for p in earlier}})

    def can_skip(self, name: str) -> tuple[bool, str]:
        rec = self.state["phases"].get(name, {})
        if rec.get("status") not in ("complete", "hold"):
            return False, f"status {rec.get('status', 'pending')}"
        if rec.get("input_digest") != self.input_digest(name):
            return False, "inputs changed"
        ledger = self.state.setdefault("artifacts", {})
        for rel, recorded in (rec.get("outputs") or {}).items():
            current = sha256_file(self.root / rel)
            if current is None or current != ledger.get(rel, recorded):
                return False, f"output changed: {rel}"
        return True, "output hashes match"

    def refresh_ledger(self) -> None:
        """After a phase runs, record the current hash of every tracked output the driver may have rewritten."""
        ledger = self.state.setdefault("artifacts", {})
        for name in PHASES:
            for rel in self.state["phases"].get(name, {}).get("outputs", {}):
                current = sha256_file(self.root / rel)
                if current:
                    ledger[rel] = current

    # ------------------------------------------------------------ model phases
    def run_model(self, jobs: list[dict]) -> list[dict]:
        """Launch every job (kind, name, packet), wait once for all markers, return per-job results."""
        units = kit("units")
        cfg, launcher = self.cfg, self.ov.get("launcher") or self.cfg["launcher"]
        hermes = self.ov.get("hermes_bin") or cfg["hermes_bin"]
        markers, launched = [], []
        stamp = datetime.now(timezone.utc).strftime("%H%M%S")
        for job in jobs:
            spec = cfg["phases"][job["kind"]]
            name = job["name"]
            marker = self.root / "orchestration" / "markers" / f"{name}.done"
            record = self.root / "orchestration" / "phases" / f"{name}.json"
            logs = self.root / "orchestration" / "logs"
            earlier = [p for p in (marker, record, *logs.glob(f"{name}-run-*.log")) if p.exists()]
            if earlier:  # a rerun of the same phase name keeps the earlier record, log and marker
                keep = self.root / "orchestration" / "history" / f"{self.segment.get('tag', 'run')}-{stamp}-{name}"
                keep.mkdir(parents=True, exist_ok=True)
                for path in earlier:
                    shutil.move(str(path), str(keep / path.name))
            tmp = self.root / "workspace" / "runs" / name / "tmp"
            tmp.mkdir(parents=True, exist_ok=True)
            argv = [sys.executable, str(KIT / "phase_runner.py"), "--marker", str(marker), "--name", name,
                    "--root", str(self.root), "--packet", str(job["packet"]), "--record", str(record),
                    "--log-dir", str(self.root / "orchestration" / "logs"), "--budget", str(spec["budget"]),
                    "--toolsets", ",".join(spec["toolsets"]), "--model", job.get("model") or spec["model"],
                    "--provider", cfg["provider"], "--reasoning", spec["reasoning"],
                    "--run-budget", str(spec["run_budget_seconds"]), "--hermes-bin", hermes]
            if self.ov.get("agent_log"):
                argv += ["--agent-log", self.ov["agent_log"]]
            unit = f"dr-{self.state['run_id'][:24]}-{name}-{stamp}"[:120]
            launch = units.launch_unit(unit=unit, argv=argv, launcher=launcher, env={"TMPDIR": str(tmp)},
                                       journal=self.root / "receipts" / "worker-launch-journal.txt")
            launched.append((job, spec, marker, record, launch))
            if launch["rc"] == 0:
                markers.append(marker)
        timeout = self.ov.get("wait_timeout") or cfg["wait_timeout_seconds"]
        wait = units.wait_markers(markers, timeout=timeout, interval=self.ov.get("wait_interval", 5.0)) if markers \
            else {"status": "done"}
        results = []
        for job, spec, marker, record_path, launch in launched:
            record = read_json(record_path) or {}
            status = record.get("status") or ("launch_failed" if launch["rc"] != 0 else
                                              "timeout" if not marker.exists() else "no_record")
            sid = record.get("session_id")
            calls = units.session_tool_calls(sid, Path(self.ov["state_db"]) if self.ov.get("state_db") else None)
            res = {"name": job["name"], "kind": job["kind"], "status": status, "session_id": sid,
                   "model": job.get("model") or spec["model"], "budget": spec["budget"],
                   "resume_count": record.get("resume_count", 0), "exit_code": record.get("exit_code"),
                   "tool_calls": calls["tool_calls"], "cost_usd": calls["cost_usd"], "unit": launch["unit"],
                   "launch_rc": launch["rc"], "wait": wait.get("status"), "record": str(record_path.relative_to(self.root))}
            res["within_budget"] = None if res["tool_calls"] is None else (
                res["tool_calls"] <= spec["budget"] * (1 + int(res.get("resume_count") or 0))
            )
            results.append(res)
            self.state.setdefault("sessions", []).append({**res, "phase": self.state["current_phase"], "at": now()})
        self.save()
        return results

    @staticmethod
    def check_results(results: list[dict], what: str) -> None:
        bad = [r for r in results if r["status"] != "completed"]
        if bad:
            step = any(r["status"] in ("step_limit_reached", "step_limit_after_resume") for r in bad)
            raise PhaseFailed(f"{what}: " + "; ".join(f"{r['name']} {r['status']}" for r in bad), step_limit=step)

    def common_fields(self) -> dict:
        return {"ROOT": self.root, "TODAY": self.req["date"], "PURPOSE": self.req["purpose"],
                "QUESTION": self.req["question"], "TARGET": self.req["target"]}

    def packet_path(self, name: str) -> Path:
        return self.root / "orchestration" / "packets" / f"{name}.md"

    def producers(self) -> list[str]:
        roles = self.cfg["roles"]
        models = [self.cfg["phases"]["lanes"]["model"], self.cfg["phases"]["synthesis"]["model"],
                  self.cfg["phases"]["repair"]["model"], roles["writer"], roles["rewriting_editor"]]
        return list(dict.fromkeys(models))

    def review_packet(self, review: str, round_no: int, kind: str) -> tuple[Path, str]:
        spec = self.cfg["phases"][kind]
        producers = self.producers()
        if spec["model"] in producers:
            raise PhaseFailed(f"reviewer model {spec['model']} for {review} is also a producer")
        pack, brief = self.root / "internal/reference-pack.md", self.root / "internal/plain-brief.md"
        fields = {**self.common_fields(), "PRODUCERS_TEXT": ", ".join(producers), "PRODUCERS_JSON": json.dumps(producers),
                  "CONTROLLER": self.cfg["phases"]["synthesis"]["model"], "REVIEWER_MODEL": spec["model"],
                  "BUDGET": spec["budget"]}
        common = string.Template((TEMPLATES / "review-common.md").read_text(encoding="utf-8")).substitute(
            **{k: str(v) for k, v in fields.items()})
        name = f"review-{review}-r{round_no}" if kind == "review" else f"review-{review}"
        export_note = (f"{self.req['export_mode']} ({self.req.get('export_reason') or 'live export'})")
        text = render(f"review-{review}.md", PHASE_NAME=name, REVIEW_COMMON=common, ROUND=round_no,
                      PACK_SHA256=sha256_file(pack) or "missing", BRIEF_SHA256=sha256_file(brief) or "missing",
                      PRODUCERS_JSON=json.dumps(producers), CONTROLLER=fields["CONTROLLER"], EXPORT_NOTE=export_note)
        path = self.packet_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path, name

    def run_reviews(self, reviews: list[str], round_no: int, kind: str) -> dict:
        jobs = []
        for review in reviews:
            packet, name = self.review_packet(review, round_no, kind)
            jobs.append({"kind": kind, "name": name, "packet": packet})
        results = self.run_model(jobs)
        self.check_results(results, "review session")
        return {review: self.receipt_status(review) for review in reviews}

    def receipt_status(self, review: str) -> str:
        data = read_json(self.root / REVIEW_RECEIPTS[review]) or {}
        return str(data.get("status") or "MISSING").upper()

    # ------------------------------------------------------------------ phases
    def do_setup(self) -> dict:
        setup = kit("campaign_setup")
        root, req = self.root, self.req
        steps = [setup.run_init(root, req)]
        steps.append(setup.prune_lanes(root, self.lanes()))
        steps.append(setup.apply_factual_controls(
            root, req["purpose"],
            Path(req["preseed_dir"]) if req.get("preseed_dir") else None,
            export_mode=req.get("export_mode", "live"),
            export_reason=req.get("export_reason"),
        ))
        steps.append(setup.apply_history_mode(root, req["history"], req.get("history_reason") or DEFAULT_HISTORY_REASON))
        steps.append(run_owned_assets(root, req["target"], Path(req["owned_assets"]) if req.get("owned_assets") else None))
        steps.append(setup.install_worker_kit(root))
        shutil.copyfile(SKILL / "references" / "reader-pair-style.md", root / "qa" / "packets" / "reader-pair-style.md")
        pre = run_tool_preflight(root, req.get("primary_source"))
        steps.append(pre)
        if pre.get("status") == "failed":
            raise PhaseFailed(f"tool preflight failed: {pre.get('failed')}")
        if (self.ov.get("launcher") or self.cfg["launcher"]) == "systemd" and not shutil.which("systemd-run"):
            raise PhaseFailed("systemd-run is not on PATH; rerun with --launcher direct")
        spec = self.cfg["phases"]["lanes"]
        for lane in req["lanes"]:
            scaffold = read_json(root / "lanes" / f"{lane['id']}.json") or {}
            name = (scaffold.get("lane") or {}).get("name") or lane["id"]
            focus = lane.get("focus") or ((scaffold.get("lane") or {}).get("objective") or f"Research {name}.") + \
                " Cover the parts of the campaign question this lane can answer from public sources."
            text = render("lane.md", PHASE_NAME=f"lane-{lane['id']}", ROOT=root, LANE_ID=lane["id"], LANE_NAME=name,
                          FOCUS=focus, QUESTION=req["question"], TODAY=req["date"], PURPOSE=req["purpose"],
                          BUDGET=spec["budget"], SEARCH_CALLS=max(10, int(spec["budget"] * 0.4)),
                          CHECKPOINT_1=max(5, int(spec["budget"] * 0.4)), CHECKPOINT_2=max(8, int(spec["budget"] * 0.7)),
                          FINDINGS_TARGET=max(8, spec["budget"] // 8), SOURCES_TARGET=max(5, spec["budget"] // 15))
            (root / "workers" / f"{lane['id']}-packet.md").write_text(text, encoding="utf-8")
        return {"status": "complete", "steps": steps}

    def do_lanes(self) -> dict:
        fan, check = kit("fan_in"), kit("check_my_lane")
        root, spec = self.root, self.cfg["phases"]["lanes"]
        jobs, reused, attempts = [], [], {}
        for lane in self.lanes():
            if fan.lane_valid(root, lane):
                reused.append(lane)
                continue
            claimed = fan.claim(root, lane, spec["model"], f"driver-lane-{lane}")
            if not claimed.get("attempt_id"):
                raise PhaseFailed(f"could not claim lane {lane}: {claimed.get('reason')}")
            attempts[lane] = claimed["attempt_id"]
            jobs.append({"kind": "lanes", "name": f"lane-{lane}", "packet": root / "workers" / f"{lane}-packet.md"})
        results = self.run_model(jobs) if jobs else []
        lane_rows, failed = [], []
        for res in results:
            lane = res["name"][len("lane-"):]
            fan.ensure_lane_markdown(root, lane)
            checked = check.check_lane(root, lane)
            ok = res["status"] in ("completed", "step_limit_reached") and checked["ok"]
            reason = None if ok else (res["status"] if res["status"] != "completed" else "lane_check_failed")
            fan.ingest(root, lane, attempts[lane], ok, reason)
            lane_rows.append({"lane": lane, "session": res["status"], "check_ok": checked["ok"],
                              "findings": checked["findings"], "quoted": checked["quoted"],
                              "problems": (checked["schema_errors"] + checked["quote_problems"])[:5]})
            if not ok:
                failed.append((lane, reason, res["status"] in ("step_limit_reached", "step_limit_after_resume")))
        if failed:
            raise PhaseFailed("lanes failed: " + "; ".join(f"{l} {r}" for l, r, _ in failed),
                              step_limit=any(s for _, _, s in failed))
        closed = fan.close_controller_lanes(root)
        qc = run_script([str(SCRIPTS / "quote_check.py"), "--campaign-root", str(root), "--write"],
                        root / "orchestration" / "logs" / "quote-check.log", 600)
        sessions = {s["name"][len("lane-"):]: s["session_id"] for s in self.state.get("sessions", [])
                    if s["kind"] == "lanes" and s.get("session_id")}
        kit("read_audit").write_audit(root, sessions, set(sessions), self.lanes(),
                                      Path(self.ov["state_db"]) if self.ov.get("state_db") else None)
        quote = read_json(root / "receipts" / "quote-check.json") or {}
        verdict = str(quote.get("verdict") or quote.get("status") or ("PASS" if qc["rc"] == 0 else "FAIL")).upper()
        return {"status": "complete" if verdict == "PASS" else "hold", "reused_lanes": reused, "lanes": lane_rows,
                "controller_lanes": closed, "quote_check": verdict}

    def do_context_release(self) -> dict:
        cc, ce, fan = kit("campaign_controller"), kit("context_edit"), kit("fan_in")
        root = self.root
        rel = cc.record_context_release(root)
        if rel.get("status") != "released":
            raise PhaseFailed(f"context release refused: {rel.get('reason')}")
        at = rel["released_at"]
        reason = "propagate the first recorded context release time (campaign_controller.record_context_release)"
        no_ctx = self.req.get("history_reason") or DEFAULT_HISTORY_REASON

        def brief(data):
            data["released_at"] = at
            data["release_recorded_by"] = "campaign_controller.record_context_release"
            if not data.get("items"):
                data["status"] = "complete"
                data["no_relevant_context_reason"] = no_ctx
            elif str(data.get("status")).lower() not in ("complete", "pass", "verified"):
                data["status"] = "complete"

        edits = [ce.logged_json_edit(root, "context/context-brief.json", brief, reason)]
        md = (root / "context" / "context-brief.md").read_text(encoding="utf-8")
        if f"Released for synthesis: {at}" not in md:
            note = f"\n\nReleased for synthesis: {at} (after all research lanes finished)."
            if not (read_json(root / "context" / "context-brief.json") or {}).get("items"):
                note += f"\n\nNo internal context: {no_ctx}"
            edits.append(ce.logged_edit(root, "context/context-brief.md", md.rstrip() + note + "\n", reason))

        def history(data):
            data["context_released_at"] = at

        edits.append(ce.logged_json_edit(root, "history/relevant-history.json", history, reason))

        def manifest(data):
            data.setdefault("context", {}).setdefault("release", {})["released_at"] = at
            data["context"]["status"] = "complete"

        edits.append(ce.logged_json_edit(root, "manifest.json", manifest, reason))
        citations = fan.build_citations(root, self.lanes())
        return {"status": "complete", "released_at": at, "already_recorded": bool(rel.get("already_recorded")),
                "logged_edits": [e for e in edits if e], "citation_codes": len(citations["sources"])}

    def do_synthesis(self) -> dict:
        root, spec = self.root, self.cfg["phases"]["synthesis"]
        cites = read_json(root / "citations.json") or {}
        codes = cites.get("code_prefixes") or {}
        lane_files = "\n".join(f"- lanes/{lane}.json and lanes/{lane}.md (code prefix {codes.get(lane, '?')})"
                               for lane in self.lanes())
        ctx = read_json(root / "context" / "context-brief.json") or {}
        note = ("It has no items: " + str(ctx.get("no_relevant_context_reason"))) if not ctx.get("items") else \
            "Use its items only for fit with the owner's context, labelled as such, never as public evidence."
        example = (cites.get("sources") or [{}])[0].get("code") or "L01-S01"
        text = render("synthesis.md", PHASE_NAME="synthesis", **self.common_fields(), BUDGET=spec["budget"],
                      LANE_FILES=lane_files, CODE_MAP=", ".join(f"{v} = {k}" for k, v in codes.items()),
                      EXAMPLE_CODE=example, RELEASED_AT=ctx.get("released_at"), CONTEXT_NOTE=note)
        packet = self.packet_path("synthesis")
        packet.parent.mkdir(parents=True, exist_ok=True)
        packet.write_text(text, encoding="utf-8")
        self.check_results(self.run_model([{"kind": "synthesis", "name": "synthesis", "packet": packet}]),
                           "synthesis session")
        pack = (root / "internal" / "reference-pack.md").read_text(encoding="utf-8")
        ledger = read_json(root / "synthesis" / "visual-exhibits.json") or {}
        problems = []
        if "TBD" in pack:
            problems.append("reference pack still has TBD")
        if str(ledger.get("status")).lower() not in ("complete", "pass", "passed", "verified"):
            problems.append(f"exhibit ledger status {ledger.get('status')}")
        if problems:
            raise PhaseFailed("synthesis outputs incomplete: " + "; ".join(problems))
        return {"status": "complete"}

    def do_qa_reviews(self) -> dict:
        mqa = kit("mechanical_qa")
        pre = mqa.check_pre_brief(self.root, self.producers(), self.cfg["phases"]["synthesis"]["model"], 1)
        statuses = {"context_handoff": pre["status"]}
        if self.cfg["reviews"]["pre_brief"]:
            statuses.update(self.run_reviews(self.cfg["reviews"]["pre_brief"], 1, "review"))
        return {"status": "complete" if all(s == "PASS" for s in statuses.values()) else "hold", "receipts": statuses}

    def do_repairs(self) -> dict:
        mqa = kit("mechanical_qa")
        model_reviews = list(self.cfg["reviews"]["pre_brief"])
        reviews = list(dict.fromkeys(["context_handoff", *model_reviews]))
        statuses = {r: self.receipt_status(r) for r in reviews}
        rounds = []
        max_rounds = self.cfg["repair_rounds"]
        spec = self.cfg["phases"]["repair"]
        # The bound holds across driver runs for the same synthesised pack: rounds recorded against this
        # synthesis output count; a new synthesis starts a new bound. Round numbers never repeat.
        generation = (self.phase("synthesis").get("outputs") or {}).get("internal/reference-pack.md")
        repairs = self.phase("repairs")
        history = self.state.setdefault("repair_history", [])
        known = {r.get("round") for r in history}
        for r in (repairs.get("detail") or {}).get("rounds", []):  # rounds saved before the history existed
            if r.get("round") not in known:
                history.append(r)
                known.add(r.get("round"))
        earlier = history
        on_disk = [int(m.group(1)) for p in (self.root / "qa" / "repairs").glob("round-*-input.json")
                   if (m := re.match(r"round-(\d+)-input\.json$", p.name))]
        numbers = [r.get("round", 0) for r in earlier] + [self.state.get("repair_rounds_done", 0)] + on_disk
        used_rounds = {r["round"] for r in earlier if r.get("generation") == generation}
        # Rounds saved before generations were recorded count only if synthesis has not run since.
        synthesised_after = str(self.phase("synthesis").get("ended_at") or "") > \
            str(repairs.get("previous_ended_at") or "")
        if not synthesised_after:
            used_rounds |= {r["round"] for r in earlier if "generation" not in r}
        used = len(used_rounds)
        next_number = max(numbers) + 1
        for round_no in range(next_number, next_number + max(0, max_rounds - used)):
            failing = [r for r, s in statuses.items() if s != "PASS"]
            if not failing:
                break
            self.transition("repair_required", f"repair round {round_no}: {', '.join(failing)} not PASS")
            archive = self.root / "receipts" / "qa-history" / f"repair-round-{round_no}"
            archive.mkdir(parents=True, exist_ok=True)
            requests = {}
            for review in failing:
                src = self.root / REVIEW_RECEIPTS[review]
                if src.is_file():
                    shutil.copyfile(src, archive / src.name)
                data = read_json(src) or {}
                requests[review] = {k: data.get(k) for k in ("status", "blocking", "requested_repairs", "findings",
                                                             "sampled_claims", "check_details") if data.get(k)}
            verify = run_verifier(self.root)
            keep = ("exhibit", "reference_pack_process", "frontmatter", "sources", "duplicate_prose")
            requests["verifier_holds"] = [h for h in verify.get("holds", []) + verify.get("failures", [])
                                          if any(k in h.lower() for k in keep)]
            inputs = self.root / "qa" / "repairs" / f"round-{round_no}-input.json"
            write_json(inputs, requests)
            before = sha256_file(self.root / "internal/reference-pack.md")
            name = f"repair-r{round_no}"
            text = render("repair.md", PHASE_NAME=name, **self.common_fields(), ROUND=round_no, MAX_ROUNDS=max_rounds,
                          BUDGET=spec["budget"], REPAIR_INPUTS=f"qa/repairs/round-{round_no}-input.json (the failed "
                          f"reviews' blocking items, requested repairs and findings, and the verifier holds)")
            packet = self.packet_path(name)
            packet.write_text(text, encoding="utf-8")
            self.check_results(self.run_model([{"kind": "repair", "name": name, "packet": packet}]), "repair session")
            changed = sha256_file(self.root / "internal/reference-pack.md") != before
            pre = mqa.check_pre_brief(self.root, self.producers(), self.cfg["phases"]["synthesis"]["model"], round_no + 1)
            statuses["context_handoff"] = pre["status"]
            rerun_models = sorted(
                (set(failing) & set(model_reviews))
                | ({"evidence_privacy"} if changed and "evidence_privacy" in model_reviews else set())
            )
            if rerun_models:
                statuses.update(self.run_reviews(rerun_models, round_no + 1, "review"))
            rerun = sorted(set(rerun_models) | ({"context_handoff"} if "context_handoff" in failing or changed else set()))
            self.transition("reviews_running", f"round {round_no} re-reviewed: {statuses}")
            rounds.append({"round": round_no, "generation": generation, "failing": failing, "pack_changed": changed,
                           "rereviewed": rerun, "statuses": dict(statuses)})
            self.state["repair_rounds_done"] = round_no
            history.append({**rounds[-1], "at": now()})
            self.save()
        ok = all(s == "PASS" for s in statuses.values())
        total = used + len(rounds)
        return {"status": "complete" if ok else "hold", "rounds": rounds, "receipts": statuses,
                "rounds_for_this_pack": total, "round_numbers_for_this_pack": sorted(used_rounds | {r["round"] for r in rounds}),
                "note": None if ok else f"reviews still not PASS after {total} of {max_rounds} rounds for this pack"}

    def do_plain_brief(self) -> dict:
        cc = kit("campaign_controller")
        res = run_plain_brief(self.root, self.root / "orchestration" / "logs" / "plain-brief.log",
                              self.cfg["plain_brief_timeout_seconds"])
        if not (self.root / "internal" / "plain-brief.md").is_file():
            log = Path(res.get("log") or "")
            text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
            if res["rc"] == 1 and text.strip() and "Traceback" not in text:
                # The generator's own readiness gate refused (for example the pack review is not PASS).
                # That is a result to carry to the completion check, not a crash.
                return {"status": "hold", "rc": res["rc"], "brief_written": False,
                        "gate_refusal": text.strip().splitlines()[-1][:500]}
            raise PhaseFailed(f"plain_brief_rewrite.py wrote no brief (rc {res['rc']}); see {res.get('log')}")
        gate = cc.record_plain_brief(self.root)
        review = read_json(self.root / "qa" / "plain-brief-review.json") or {}
        ok = res["rc"] == 0 and gate.get("status") == "complete"
        return {"status": "complete" if ok else "hold", "rc": res["rc"], "gate": gate.get("status"),
                "gate_problems": gate.get("problems", [])[:10], "review_verdict": review.get("verdict") or review.get("status")}

    def write_tag_check(self) -> None:
        """Check that each reader's frontmatter tags are well-formed. No tag registry is used."""
        rows = {}
        for rel in ("internal/reference-pack.md", "internal/plain-brief.md"):
            text = (self.root / rel).read_text(encoding="utf-8") if (self.root / rel).is_file() else ""
            m = re.search(r"^tags:\s*\[([^\]]*)\]", text, re.M)
            tags = [t.strip().strip("'\"") for t in (m.group(1).split(",") if m else []) if t.strip()]
            rows[rel] = {tag: bool(re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_/-]+", tag)) for tag in tags}
        write_json(self.root / "qa" / "packets" / "tag-check.json",
                   {"checked_at": now(), "taxonomy": "tag format only (no tag registry)",
                    "taxonomy_found": False, "files": rows})

    def do_final_reviews(self) -> dict:
        if not (self.root / "internal" / "plain-brief.md").is_file():
            return {"status": "hold", "skipped": "no plain brief exists, so the post-brief reviews were not run",
                    "receipts": {r: self.receipt_status(r) for r in
                                 self.cfg["reviews"]["post_brief"] + self.cfg["reviews"]["final"]}}
        self.write_tag_check()
        mqa = kit("mechanical_qa")
        statuses = mqa.check_post_brief(self.root, self.producers(), self.cfg["phases"]["synthesis"]["model"])
        if self.cfg["reviews"]["post_brief"]:
            statuses.update(self.run_reviews(self.cfg["reviews"]["post_brief"], 1, "final_review"))
        verify = run_verifier(self.root)
        write_json(self.root / "receipts" / "verify-before-final.json", verify)
        if self.cfg["reviews"]["final"]:
            statuses.update(self.run_reviews(self.cfg["reviews"]["final"], 1, "final_review"))
        return {"status": "complete" if all(s == "PASS" for s in statuses.values()) else "hold", "receipts": statuses,
                "verifier_before_final": verify.get("status")}

    def do_export(self) -> dict:
        record = run_export(self.root, self.req["export_mode"], self.req.get("export_reason"), self.state["run_id"])
        write_json(self.root / "receipts" / "closeout.json", record)
        ok = record.get("status") in ("exported", "dry_run", "skipped")
        return {"status": "complete" if ok else "hold", "mode": self.req["export_mode"], "result": record.get("status"),
                "reason": self.req.get("export_reason")}

    def do_completion(self) -> dict:
        res = run_completion(self.root, self.root / "orchestration" / "logs" / "completion.log",
                             self.cfg["completion_timeout_seconds"])
        verdict = res.get("verdict") or ((res.get("verdict_line") or "").split() or [None])[0]
        self.state["completion"] = {"verdict": verdict, "classes": res.get("classes", []),
                                    "verdict_line": res.get("verdict_line"), "exit_code": res.get("rc"),
                                    "log": res.get("log"), "at": now()}
        if res.get("rc") not in (0, 2, 3) or verdict not in ("PASS", "HOLD", "FAIL"):
            raise PhaseFailed(f"completion check did not print a verdict (rc {res.get('rc')})")
        return {"status": "complete" if verdict == "PASS" else "hold", "verdict_line": res["verdict_line"],
                "exit_code": res["rc"]}

    # -------------------------------------------------------------------- run
    def run(self, from_phase: str | None = None, stop_after: str | None = None) -> int:
        runs = self.state.setdefault("runs", [])
        prev = runs[-1] if runs else None
        self.segment = {"tag": f"run-{len(runs) + 1}", "attempt": len(runs), "retry_of": prev["tag"] if prev else None,
                        "started_at": now(), "transitions": [], "from_phase": from_phase}
        if prev and prev.get("transitions"):
            try:
                prior = rsm.RunState()
                for t in prev["transitions"]:
                    prior = prior.append(rsm.Transition(**t))
                keys = sorted({k for t in prev["transitions"] for k in t.get("artifact_hashes", {})})
                self.segment["resume_plan"] = rsm.resume(prior, correlation_id=self.state["run_id"],
                                                         completed_stage_hashes={k: v for k, v in
                                                                                 self.current_hashes(keys).items() if v})
            except rsm.StateError as exc:
                self.segment["resume_plan"] = {"error": str(exc)}
        runs.append(self.segment)
        self.state.update(status="running", reason=None)
        force = PHASES.index(from_phase) if from_phase else len(PHASES)
        code = 0
        for index, name in enumerate(PHASES):
            self.state["current_phase"] = name
            rec = self.phase(name)
            skip, why = self.can_skip(name)
            if skip and index < force:
                rec["last_skip"] = {"at": now(), "reason": why}
                self.append_states(name, rec, reused=True)
                self.save()
            else:
                code = self.run_phase(name, rec, why if index < force else f"forced from {from_phase}")
                if code:
                    break
            if stop_after == name:
                self.state.update(status="stopped", reason=f"stopped after {name} as requested")
                break
        else:
            verdict = (self.state.get("completion") or {}).get("verdict_line")
            self.state.update(status="complete", reason=None, verdict=verdict)
        self.segment["ended_at"] = now()
        self.save()
        return code

    def append_states(self, name: str, rec: dict, reused: bool) -> None:
        reason = ("reused: " if reused else "") + f"{name} {rec.get('status')}"
        hashes = rec.get("outputs") or {}
        if name == "repairs" and not reused:
            return  # repair rounds append their own repair_required / reviews_running pairs
        for st in PHASE_STATES.get(name, []):
            self.transition(st, reason, hashes if st == PHASE_STATES[name][-1] else None)
        if name == "completion":
            done = self.state.get("completion") or {}
            verdict = done.get("verdict") or (done.get("verdict_line") or "").split(" ")[0]
            self.transition("owner_review_ready", reason)
            if verdict == "PASS":
                self.transition("research_only_closed", "completion check printed PASS")
            else:
                self.transition("blocked_review_hold", "completion check did not print PASS",
                                terminal_reason=(self.state.get("completion") or {}).get("verdict_line"))

    def run_phase(self, name: str, rec: dict, why: str) -> int:
        rec.update(status="running", started_at=now(), previous_ended_at=rec.get("ended_at"), ended_at=None,
                   reason=None, run_reason=why,
                   input_digest=self.input_digest(name))
        self.save()
        try:
            states = PHASE_STATES.get(name, [])
            if name == "setup":  # the segment's initial state comes before any setup work
                self.transition("accepted", "campaign request accepted")
                states = []
            for st in states[:-1]:  # e.g. research_running before the lanes start
                self.transition(st, f"{name} started")
            detail = getattr(self, f"do_{name}")()
            rec.update(status=detail.pop("status"), detail=detail, ended_at=now(),
                       outputs={k: v for k, v in self.current_hashes(self.outputs(name)).items() if v})
            self.refresh_ledger()
            if states:
                self.transition(states[-1], f"{name} {rec['status']}", rec["outputs"])
            elif name == "completion":
                self.append_states(name, rec, reused=False)
            self.save()
            return 0
        except PhaseFailed as exc:
            return self.fail(name, rec, exc.reason, exc.step_limit)
        except Exception as exc:  # noqa: BLE001 - any error stops the run with a recorded state
            return self.fail(name, rec, f"{type(exc).__name__}: {exc}", False)

    def fail(self, name: str, rec: dict, reason: str, step_limit: bool) -> int:
        rec.update(status="failed", reason=reason, ended_at=now())
        state = "blocked_exhausted_retry" if step_limit else FAIL_STATES[name]
        try:
            self.transition(state, f"{name} failed", terminal_reason=reason)
        except rsm.StateError as exc:
            rec["state_error"] = str(exc)
        self.state.update(status="failed", reason=f"{name}: {reason}")
        self.save()
        return 1


# ---------------------------------------------------------------------- CLI
@contextmanager
def driver_lock(root: Path):
    path = root / LOCK_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(f"another driver run holds {path}") from None
        yield


DEFAULT_PUBLIC_LANES = (
    "01-steering-source-map",
    "02-current-state-truth",
    "03-evidence-claims-map",
    "04-prior-art-alternatives",
)


def load_lanes(args) -> list[dict]:
    if args.lanes_file:
        data = json.loads(Path(args.lanes_file).read_text(encoding="utf-8"))
        lanes = data["lanes"] if isinstance(data, dict) else data
        return [{"id": str(item["id"]), "focus": item.get("focus")} for item in lanes]
    if args.lanes:
        return [{"id": lane.strip(), "focus": None} for lane in args.lanes.split(",") if lane.strip()]
    return [{"id": lane, "focus": None} for lane in DEFAULT_PUBLIC_LANES]


def resolve_config(args) -> dict:
    settings = __import__("research_settings")
    return settings.load_phases(config_dir=args.config_dir, profile=args.profile)


def execute(root: Path, from_phase: str | None, stop_after: str | None) -> int:
    state = read_json(root / STATE_REL)
    if not state:
        print(f"no driver state at {root / STATE_REL}; use start", file=sys.stderr)
        return 2
    done = root / DONE_REL
    with driver_lock(root):
        if done.exists():
            done.unlink()
        driver = Driver(root, state)
        code = 1
        try:
            code = driver.run(from_phase, stop_after)
        finally:
            write_json(done, {"status": driver.state.get("status"), "exit_code": code, "at": now(),
                              "reason": driver.state.get("reason")})
    print(status_line(driver.state))
    return code


def detach(root: Path, args) -> int:
    units = kit("units")
    argv = [sys.executable, str(Path(__file__).resolve()), "resume", "--root", str(root)]
    if getattr(args, "stop_after", None):
        argv += ["--stop-after", args.stop_after]
    if getattr(args, "from_phase", None):
        argv += ["--from", args.from_phase]
    done = root / DONE_REL
    if done.exists():
        done.unlink()
    unit = f"dr-driver-{slug(root.name)[:40]}-{datetime.now(timezone.utc).strftime('%H%M%S')}"
    rec = units.launch_unit(unit=unit, argv=argv, launcher=args.launcher or "systemd",
                            journal=root / "receipts" / "worker-launch-journal.txt")
    print(json.dumps({"unit": unit, "rc": rec["rc"], "marker": str(done),
                      "wait": f"python3 {SCRIPTS / 'wait_for_jobs.py'} --marker {done} --timeout 21600",
                      "status": f"python3 {Path(__file__).resolve()} status --root {root}"}))
    return 0 if rec["rc"] == 0 else 1


def cmd_start(args) -> int:
    root = Path(args.root).expanduser().resolve()
    if (root / STATE_REL).exists():
        print(f"{root / STATE_REL} exists; use resume", file=sys.stderr)
        return 2
    lanes = load_lanes(args)
    if not lanes:
        print("name the lanes with --lanes or --lanes-file", file=sys.stderr)
        return 2
    if args.export != "live" and not args.export_reason:
        print("--export dry-run or skip needs --export-reason", file=sys.stderr)
        return 2
    config = resolve_config(args)
    request = {"question": args.question, "target": args.target, "target_type": args.target_type,
               "purpose": args.purpose, "primary_source": args.primary_source, "audience": args.audience,
               "date": args.date or datetime.now().astimezone().date().isoformat(), "lanes": lanes,
               "history": args.history, "history_reason": args.history_reason, "export_mode": args.export,
               "export_reason": args.export_reason, "owned_assets": args.owned_assets, "preseed_dir": args.preseed_dir,
               "no_deliverable_export": args.no_deliverable_export}
    overrides = {k: v for k, v in {"hermes_bin": args.hermes_bin, "launcher": args.launcher, "agent_log": args.agent_log,
                                   "state_db": args.state_db, "wait_timeout": args.wait_timeout,
                                   "wait_interval": args.wait_interval}.items() if v is not None}
    state = {"schema_version": SCHEMA, "root": str(root), "run_id": f"{slug(args.target)}-{request['date']}",
             "created_at": now(), "request": request, "request_sha256": canonical_sha(request), "config": config,
             "overrides": overrides, "status": "pending", "phases": {}, "artifacts": {}, "runs": [], "sessions": []}
    write_json(root / STATE_REL, state)
    if args.detach:
        return detach(root, args)
    return execute(root, None, args.stop_after)


def cmd_resume(args) -> int:
    root = Path(args.root).expanduser().resolve()
    if args.from_phase and args.from_phase not in PHASES:
        print(f"--from must be one of {', '.join(PHASES)}", file=sys.stderr)
        return 2
    if args.refresh_config:
        state = read_json(root / STATE_REL)
        if not state:
            print(f"no driver state at {root / STATE_REL}; use start", file=sys.stderr)
            return 2
        new = resolve_config(args)
        if new != state["config"]:
            state.setdefault("config_history", []).append({"at": now(), "previous": state["config"]})
            state["config"] = new
        for s in state.get("sessions", []):
            if s.get("tool_calls") is not None and s.get("budget"):
                s["within_budget"] = s["tool_calls"] <= s["budget"] * (1 + int(s.get("resume_count") or 0))
        write_json(root / STATE_REL, state)
    if args.launcher:
        state = read_json(root / STATE_REL)
        if state:
            state.setdefault("overrides", {})["launcher"] = args.launcher
            write_json(root / STATE_REL, state)
    if args.detach:
        return detach(root, args)
    return execute(root, args.from_phase, args.stop_after)


def status_line(state: dict) -> str:
    phases = state.get("phases", {})
    done = sum(1 for p in PHASES if phases.get(p, {}).get("status") in ("complete", "hold"))
    sessions = state.get("sessions", [])
    calls = [s for s in sessions if s.get("tool_calls") is not None]
    over = [s["name"] for s in calls if not s.get("within_budget")]
    verdict = (state.get("completion") or {}).get("verdict_line")
    parts = [f"{state.get('run_id')}: {state.get('status')}", f"phase={state.get('current_phase')}",
             f"{done}/{len(PHASES)} phases done",
             f"holds={','.join(p for p in PHASES if phases.get(p, {}).get('status') == 'hold') or '-'}",
             f"sessions={len(sessions)}", f"over_budget={','.join(over) or '-'}",
             f"verdict={verdict or '-'}"]
    if state.get("reason"):
        parts.append(f"reason={state['reason']}")
    return " ".join(parts)


def cmd_status(args) -> int:
    root = Path(args.root).expanduser().resolve()
    state = read_json(root / STATE_REL)
    if not state:
        print(f"{root}: no driver state")
        return 2
    if args.json:
        print(json.dumps({"line": status_line(state), "status": state.get("status"),
                          "current_phase": state.get("current_phase"), "reason": state.get("reason"),
                          "completion": state.get("completion"),
                          "phases": {p: state["phases"].get(p, {}).get("status", "pending") for p in PHASES},
                          "sessions": [{k: s.get(k) for k in ("phase", "name", "session_id", "model", "tool_calls",
                                                               "budget", "within_budget", "resume_count", "status",
                                                               "cost_usd")} for s in state.get("sessions", [])]},
                         indent=2))
    else:
        print(status_line(state))
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = p.add_subparsers(dest="command", required=True)

    def runtime(sp):
        sp.add_argument("--detach", action="store_true", help="run the driver in a systemd --user unit and return")
        sp.add_argument("--stop-after", choices=PHASES, help="stop cleanly after this phase")

    s = sub.add_parser("start", help="new campaign")
    s.add_argument("--root", required=True, help="campaign root (absolute)")
    s.add_argument("--question", required=True)
    s.add_argument("--target", required=True)
    s.add_argument("--target-type", default="technical_idea")
    s.add_argument("--purpose", default="factual", choices=["factual", "decision_assessment"])
    s.add_argument("--primary-source")
    s.add_argument("--audience")
    s.add_argument("--date")
    s.add_argument("--lanes", help="comma list of scaffold lane ids")
    s.add_argument("--lanes-file", help='JSON list of {"id": ..., "focus": ...}')
    s.add_argument("--history", default="external-only", choices=["external-only"])
    s.add_argument("--history-reason")
    s.add_argument("--export", default="live", choices=["live", "dry-run", "skip"])
    s.add_argument("--export-reason")
    s.add_argument("--owned-assets", help="owned-assets markdown that lanes may read (default: none; DEEP_RESEARCH_SCAN_OWNED_ASSETS=1 runs a name-level local scan instead)")
    s.add_argument("--preseed-dir", help="the four pre-seed artifacts (required for decision_assessment)")
    s.add_argument("--no-deliverable-export", action="store_true")
    s.add_argument("--config-dir")
    s.add_argument("--profile")
    s.add_argument("--hermes-bin", help="hermes binary (tests use a fake)")
    s.add_argument("--launcher", choices=["systemd", "direct"])
    s.add_argument("--agent-log")
    s.add_argument("--state-db")
    s.add_argument("--wait-timeout", type=float)
    s.add_argument("--wait-interval", type=float)
    runtime(s)
    s.set_defaults(func=cmd_start)

    r = sub.add_parser("resume", help="continue from saved state")
    r.add_argument("--root", required=True)
    r.add_argument("--from", dest="from_phase", help="rerun this phase and every later one")
    r.add_argument("--launcher", choices=["systemd", "direct"], help="launcher for --detach")
    r.add_argument("--refresh-config", action="store_true",
                   help="re-resolve config/phases.yaml into the saved state (the change is recorded)")
    r.add_argument("--config-dir")
    r.add_argument("--profile")
    runtime(r)
    r.set_defaults(func=cmd_resume)

    st = sub.add_parser("status", help="one-line state")
    st.add_argument("--root", required=True)
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
