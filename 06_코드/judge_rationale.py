#!/usr/bin/env python3
"""근거 채점 Agent (B 트랙). 수치 평가(evaluate.py) 이후에 실행한다.

  python judge_rationale.py 2026-11-09             # 정식 -> 05_평가결과/B/rationale/<날짜>.json
  python judge_rationale.py 2026-11-09 --dry-run   # 시험 -> 05_평가결과/B/dryrun/<날짜>.rationale.json

예측 세션과 같은 격리 방식(--safe-mode, Read 도구만, 프로젝트 밖 임시 폴더)으로 별도 세션을 띄운다.
이 세션만 정답을 본다. 예측 파일은 읽기만 하고 수정하지 않는다.
"""
import argparse
import copy
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from jsonschema import Draft202012Validator

import evaluate as ev
import run_prediction as rp

ROOT, CODE, TRACK = rp.ROOT, rp.CODE, rp.TRACK
META = ["schema_version", "track", "trade_date", "judge_prompt_version", "judge_model_id", "judged_at",
        "prediction_executed_at"]
DEFAULT_JUDGE = {"model": "claude-opus-5", "prompt_file": "prompts/judge_v1.0.md", "timeout_seconds": 900}


def judge_output_schema():
    s = copy.deepcopy(rp.load_json(rp.SCHEMAS / "judgment.schema.json"))
    s.pop("$schema", None)
    for k in META:
        s["properties"].pop(k)
        s["required"].remove(k)
    return s


def inputs(date, dry):
    """(예측, 스냅샷 경로, 정답 경로, 수치평가 경로, PDF 폴더, 출력 경로)"""
    pdf_dir = ROOT / "01_리서치_원본" / date
    if dry:
        pred, _, snap_p, ans_p, num_p = ev.dryrun_paths(date)
        out = ROOT / "05_평가결과" / TRACK / "dryrun" / f"{date}.rationale.json"
    else:
        pred, _ = ev.find_prediction(date)
        snap_p = ROOT / "02_시장데이터" / "스냅샷" / TRACK / f"{date}.json"
        ans_p = ROOT / "02_시장데이터" / "정답" / TRACK / f"{date}.json"
        num_p = ROOT / "05_평가결과" / TRACK / f"{date}.json"
        out = ROOT / "05_평가결과" / TRACK / "rationale" / f"{date}.json"
    for p in (snap_p, ans_p, num_p):
        if not Path(p).exists():
            rp.fail(f"파일 없음: {p}  (정답 기록과 evaluate.py 실행이 먼저 필요)")
    return pred, Path(snap_p), Path(ans_p), Path(num_p), pdf_dir, out


def evidence_stats(checks):
    counted = [c for c in checks if c["verdict"] != "not_checkable"]
    bad = [c for c in counted if c["verdict"] in ("misread", "unsupported")]
    return {"checked": len(counted), "errors": len(bad),
            "error_rate": round(len(bad) / len(counted), 3) if counted else None}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("date")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="기존 채점 결과 덮어쓰기")
    args = ap.parse_args()

    cfg = rp.load_json(CODE / "experiment_config.json")
    jcfg = {**DEFAULT_JUDGE, **cfg.get("judge", {})}
    pred, snap_p, ans_p, num_p, pdf_dir, out = inputs(args.date, args.dry_run)
    if out.exists() and not args.force:
        rp.fail(f"이미 채점 결과가 있음: {out} (재채점은 --force)")
    snap = rp.load_json(snap_p)

    prompt_path = CODE / jcfg["prompt_file"]
    prompt_version = prompt_path.stem.split("_", 1)[1]
    started = rp.kst_now()
    stamp = started.strftime("%Y%m%dT%H%M%S")

    with tempfile.TemporaryDirectory(prefix="ai_macro_judge_") as tmp:
        work = Path(tmp)
        (work / "prediction.json").write_text(json.dumps(pred, ensure_ascii=False, indent=2), encoding="utf-8")
        for src, name in ((snap_p, "snapshot.json"), (ans_p, "answer.json"), (num_p, "numeric_eval.json")):
            shutil.copyfile(src, work / name)
        for r in snap["reports"]:
            src = pdf_dir / r["file"]
            if not src.exists():
                rp.fail(f"리포트 파일 없음: {src}")
            shutil.copyfile(src, work / f"report_{r['source']}.pdf")
        cmd = ["claude", "-p", "--safe-mode", "--no-session-persistence", "--tools", "Read",
               "--model", jcfg["model"], "--output-format", "stream-json", "--verbose",
               "--system-prompt", prompt_path.read_text(encoding="utf-8"),
               "--json-schema", json.dumps(judge_output_schema())]
        user = ("작업 폴더의 prediction.json, snapshot.json, report_daol.pdf, report_eugene.pdf, answer.json, "
                "numeric_eval.json을 읽고 지침에 따라 채점 결과를 JSON으로 출력하세요.")
        print(f"[채점] model={jcfg['model']} prompt={prompt_version} date={args.date}{' (dry-run)' if args.dry_run else ''}")
        try:
            proc = subprocess.run(cmd, input=user, cwd=work, capture_output=True, text=True,
                                  encoding="utf-8", timeout=jcfg["timeout_seconds"])
        except subprocess.TimeoutExpired:
            rp.fail("claude 실행 시간 초과")

    log_dir = ROOT / "99_로그" / "runs"
    log_dir.mkdir(parents=True, exist_ok=True)
    raw_log = log_dir / f"{args.date}_{TRACK}_judge_{stamp}.jsonl"
    raw_log.write_text(proc.stdout, encoding="utf-8")
    init, result = rp.parse_events(proc.stdout)

    status, detail, judgment = "ok", "", None
    if proc.returncode != 0 or result.get("is_error") or not result:
        status, detail = "error", (proc.stderr.strip() or str(result.get("result", "")))[:500]
    elif not isinstance(result.get("structured_output"), dict):
        status, detail = "error", "structured_output 없음: " + str(result.get("result", ""))[:300]
    else:
        judgment = {"schema_version": "0.1", "track": TRACK, "trade_date": args.date,
                    "judge_prompt_version": prompt_version, "judge_model_id": init.get("model", "unknown"),
                    "judged_at": rp.kst_str(started), "prediction_executed_at": pred["executed_at"],
                    **result["structured_output"]}
        errs = [f"{'/'.join(map(str, e.path)) or '(root)'}: {e.message}" for e in
                Draft202012Validator(rp.load_json(rp.SCHEMAS / "judgment.schema.json")).iter_errors(judgment)]
        if errs:
            status, detail = "invalid", "; ".join(errs)

    with open(ROOT / "99_로그" / "runs.log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"date": args.date, "track": TRACK, "kind": "judge", "dry_run": args.dry_run,
                            "status": status, "model_id": init.get("model"), "prompt_version": prompt_version,
                            "executed_at": rp.kst_str(started), "cost_usd": result.get("total_cost_usd"),
                            "duration_ms": result.get("duration_ms"), "detail": detail,
                            "raw_log": str(raw_log.relative_to(ROOT))}, ensure_ascii=False) + "\n")
    if status != "ok":
        print(f"[실패:{status}] {detail}\n원본 로그: {raw_log}", file=sys.stderr)
        sys.exit(1)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(judgment, ensure_ascii=False, indent=2), encoding="utf-8")
    st, sc = evidence_stats(judgment["evidence_checks"]), judgment["scores"]
    print(f"[완료] {out.relative_to(ROOT)}")
    print(f"  점수: 수치충실 {sc['evidence_fidelity']} / 반영판단 {sc['priced_in']} / 무효화 {sc['invalidation_quality']}"
          f" / 추론 {sc['reasoning_quality']} / 종합 {sc['overall']}")
    print(f"  근거 수치 오류 {st['errors']}/{st['checked']}  |  분류: {judgment['reasoning_outcome']['category']}"
          f"  |  애널리스트 의존: {judgment['analyst_dependence']['level']}")
    for c in judgment["evidence_checks"]:
        if c["verdict"] in ("misread", "unsupported"):
            print(f"   ✗ [{c['verdict']}] {c['claimed']}  ->  원문: {c['source_value']}")
    print(f"  교훈: {judgment['lesson']}")
    print(f"  비용(표시값) ${result.get('total_cost_usd')}, {result.get('duration_ms')}ms")


if __name__ == "__main__":
    main()
