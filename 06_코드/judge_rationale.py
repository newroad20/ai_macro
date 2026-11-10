#!/usr/bin/env python3
"""근거 채점 Agent. 기본 트랙 A (2단계 채점).

A 트랙 — 2단계 (설정 judge_mode = two_stage)
  1단계 blind  : 정답 없이 예측·스냅샷·리포트 원문만 보고 근거 수치·추론·무효화 조건 형식을 채점하고 저장.
                 정답 파일을 작업 폴더에 넣지 않으므로 구조적으로 결과를 볼 수 없다. 예측 직후(아침)에 돌려도 된다.
  2단계 outcome: 1단계 결과를 고정한 뒤 정답·수치평가를 보여주고 무효화 발동·이미 반영·추론-결과 분류·교훈을 판정.

  python judge_rationale.py 2026-11-10                    # 1단계(없으면) + 2단계
  python judge_rationale.py 2026-11-10 --stage blind      # 1단계만 (예측 직후 권장)
  python judge_rationale.py 2026-11-10 --stage outcome    # 2단계만 (정답 기록·evaluate.py 이후)
  python judge_rationale.py 2026-11-06 --dry-run          # 시험 -> 05_평가결과/A/dryrun/
  결과: 05_평가결과/A/rationale/<날짜>.blind.json, <날짜>.outcome.json

비교군 (예: 뉴스): --arm news  -> 05_평가결과/A/arms/news/rationale/ (뉴스 원문도 채점 폴더에 넣음)

채점 신뢰도 점검
  python judge_rationale.py 2026-11-10 --stage blind --variant r2   # 같은 예측 재채점 -> <날짜>.blind.r2.json
  사람 채점: 05_평가결과/A/human/<날짜>.json  {"scorer": "...", "scores": {"evidence_fidelity": 4, ...}, "note": "..."}
  python judge_rationale.py --agreement                    # 재채점·사람 점수와의 차이 요약

B 트랙 — 단일 단계 (보관용): python judge_rationale.py 2026-11-09 --track B --dry-run

모든 채점 세션은 예측과 같은 격리 방식(--safe-mode, Read 도구만, 프로젝트 밖 임시 폴더)으로 실행한다.
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

import common as cm
import evaluate as ev
import run_prediction as rp
from common import ROOT, CODE, fail, load_json

DEFAULT_JUDGE = {"model": "claude-opus-5", "timeout_seconds": 900}
LEGACY_META = ["schema_version", "track", "trade_date", "judge_prompt_version", "judge_model_id", "judged_at",
               "prediction_executed_at"]
SCORE_KEYS = ["evidence_fidelity", "invalidation_quality", "reasoning_quality", "overall"]


def strip_schema(name, meta=()):
    s = copy.deepcopy(load_json(cm.SCHEMAS / name))
    s.pop("$schema", None)
    for k in meta:
        s["properties"].pop(k)
        s["required"].remove(k)
    return s


def schema_errors(schema, obj):
    return [f"{'/'.join(map(str, e.path)) or '(root)'}: {e.message}" for e in Draft202012Validator(schema).iter_errors(obj)]


def evidence_stats(checks):
    counted = [c for c in checks if c["verdict"] != "not_checkable"]
    bad = [c for c in counted if c["verdict"] in ("misread", "unsupported")]
    return {"checked": len(counted), "errors": len(bad),
            "error_rate": round(len(bad) / len(counted), 3) if counted else None}


def run_session(files, prompt_path, schema, user_msg, jcfg, track, date, label, dry, arm=None):
    """files: {작업폴더 파일명: (원본 경로 | dict)} -> (structured_output | None, 실행 요약)"""
    started = cm.kst_now()
    stamp = started.strftime("%Y%m%dT%H%M%S")
    prompt_version = prompt_path.stem.split("_", 1)[1]
    with tempfile.TemporaryDirectory(prefix="ai_macro_judge_") as tmp:
        work = Path(tmp)
        for name, src in files.items():
            if isinstance(src, dict):
                (work / name).write_text(json.dumps(src, ensure_ascii=False, indent=2), encoding="utf-8")
            else:
                if not Path(src).exists():
                    fail(f"파일 없음: {src}")
                (work / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, work / name)
        cmd = ["claude", "-p", "--safe-mode", "--no-session-persistence", "--tools", "Read",
               "--model", jcfg["model"], "--output-format", "stream-json", "--verbose",
               "--system-prompt", prompt_path.read_text(encoding="utf-8"), "--json-schema", json.dumps(schema)]
        print(f"[채점:{label}] track={track}{'' if arm in (None, cm.BASE_ARM) else ' arm=' + arm} model={jcfg['model']} prompt={prompt_version} date={date}"
              f"{' (시험)' if dry else ''}  입력: {', '.join(files)}")
        for attempt in (1, 2):  # 채점은 예측이 아니므로, 출력 형식 실패(구조화 출력 없음)에 한해 1회 재시도
            try:
                proc = subprocess.run(cmd, input=user_msg, cwd=work, capture_output=True, text=True,
                                      encoding="utf-8", timeout=jcfg["timeout_seconds"])
            except subprocess.TimeoutExpired:
                fail("claude 실행 시간 초과")
            _, res = rp.parse_events(proc.stdout)
            if proc.returncode == 0 and res and not res.get("is_error") and not isinstance(res.get("structured_output"), dict)                     and attempt == 1:
                print("  [재시도] 구조화 출력 없음 -> 1회 재실행")
                continue
            break

    log_dir = ROOT / "99_로그" / "runs"
    log_dir.mkdir(parents=True, exist_ok=True)
    arm_part = "" if arm in (None, cm.BASE_ARM) else f"_{arm}"
    raw_log = log_dir / f"{date}_{track}{arm_part}_judge-{label}_{stamp}.jsonl"
    raw_log.write_text(proc.stdout, encoding="utf-8")
    init, result = rp.parse_events(proc.stdout)
    status, detail, out = "ok", "", None
    if proc.returncode != 0 or result.get("is_error") or not result:
        status, detail = "error", (proc.stderr.strip() or str(result.get("result", "")))[:500]
    elif not isinstance(result.get("structured_output"), dict):
        status, detail = "error", "structured_output 없음: " + str(result.get("result", ""))[:300]
    else:
        out = result["structured_output"]
        errs = schema_errors(schema, out)
        if errs:
            status, detail, out = "invalid", "; ".join(errs), None
    run = {"date": date, "track": track, "arm": arm or cm.BASE_ARM, "kind": f"judge-{label}", "dry_run": dry, "status": status,
           "model_id": init.get("model"), "prompt_version": prompt_version, "executed_at": cm.kst_str(started),
           "cost_usd": result.get("total_cost_usd"), "duration_ms": result.get("duration_ms"), "detail": detail,
           "raw_log": str(raw_log.relative_to(ROOT))}
    with open(ROOT / "99_로그" / "runs.log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(run, ensure_ascii=False) + "\n")
    if status != "ok":
        print(f"[실패:{status}] {detail}\n원본 로그: {raw_log}", file=sys.stderr)
        sys.exit(1)
    return out, run


def save(obj, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[저장] {path.relative_to(ROOT)}")


# ---------------- A 트랙: 2단계 ----------------

def stage_paths(track, date, dry, variant, arm=None):
    d = cm.eval_dir(track, arm) / ("dryrun" if dry else "rationale")
    suf = f".{variant}" if variant else ""
    return d / f"{date}.blind{suf}.json", d / f"{date}.outcome{suf}.json"


def prediction_for(track, date, dry, arm=None):
    if dry:
        return ev.dryrun_paths(track, date, arm)[0]
    return ev.find_prediction(track, date, arm)[0]


def meta(track, date, stage, run, pred, variant, arm=None):
    return {"schema_version": "0.2", "track": track, "arm": arm or cm.BASE_ARM, "trade_date": date, "stage": stage,
            "variant": variant or "main", "judge_prompt_version": run["prompt_version"],
            "judge_model_id": run["model_id"], "judged_at": run["executed_at"],
            "prediction_executed_at": pred["executed_at"], "cost_usd": run["cost_usd"]}


def run_blind(track, date, dry, variant, force, jcfg, files_cfg, arm=None, news_profiles=None, cutoff=None):
    blind_p, _ = stage_paths(track, date, dry, variant, arm)
    if blind_p.exists() and not force:
        print(f"[1단계] 이미 있음 -> 재사용: {blind_p.relative_to(ROOT)}")
        return load_json(blind_p)
    pred = prediction_for(track, date, dry, arm)
    snap_p = cm.snapshot_path(track, date, dry)
    snap = load_json(snap_p)
    files = {"prediction.json": pred, "snapshot.json": snap_p}
    for r in snap["reports"]:
        files[f"report_{r['source']}.pdf"] = cm.research_dir(date) / r["file"]
    answer_existed = cm.answer_path(track, date, dry).exists()
    msg = "작업 폴더의 prediction.json, snapshot.json, report_daol.pdf, report_eugene.pdf를 읽고 "
    with tempfile.TemporaryDirectory(prefix="ai_macro_news_") as news_tmp:
        if news_profiles:  # 예측 때와 같은 규칙으로 뉴스 패키지를 다시 만든다 (같은 기사 ID bond_NN 등)
            info = rp.build_news(date, news_profiles, cutoff, Path(news_tmp))
            for p in sorted((Path(news_tmp) / "news").glob("*")):
                files[f"news/{p.name}"] = p
            msg = (f"작업 폴더의 prediction.json, snapshot.json, report_daol.pdf, report_eugene.pdf, news/INDEX.md "
                   f"(뉴스 {info['included']}건), news/ 의 관점별 원문 파일을 읽고 ")
        out, run = run_session(files, CODE / files_cfg["blind"], strip_schema("judgment_blind.schema.json"),
                               msg + "지침에 따라 1단계 채점 결과를 JSON으로 출력하세요.",
                               jcfg, track, date, "blind", dry, arm)
    obj = {**meta(track, date, "blind", run, pred, variant, arm), "answer_existed_at_judging": answer_existed,
           "answer_shown": False, **out}
    save(obj, blind_p)
    st, sc = evidence_stats(out["evidence_checks"]), out["scores"]
    print(f"  1단계 점수: 수치충실 {sc['evidence_fidelity']} / 무효화 형식 {sc['invalidation_quality']}"
          f" / 추론(사전) {sc['reasoning_quality']}  |  근거 수치 오류 {st['errors']}/{st['checked']}"
          f"  |  애널리스트 의존 {out['analyst_dependence']['level']}")
    for c in out["evidence_checks"]:
        if c["verdict"] in ("misread", "unsupported"):
            print(f"   ✗ [{c['verdict']}] {c['claimed']}  ->  원문: {c['source_value']}")
    bad_inv = [i["condition"] for i in out["invalidation_observability"] if not i["observable"]]
    if bad_inv:
        print(f"   판정 불가 무효화 조건 {len(bad_inv)}개: " + " / ".join(bad_inv))
    print(f"  비용(표시값) ${run['cost_usd']}, {run['duration_ms']}ms")
    return obj


def run_outcome(track, date, dry, variant, force, jcfg, files_cfg, arm=None):
    blind_p, out_p = stage_paths(track, date, dry, variant, arm)
    if out_p.exists() and not force:
        fail(f"이미 2단계 결과가 있음: {out_p} (재판정은 --force)")
    if not blind_p.exists():
        fail(f"1단계 결과 없음: {blind_p}. 먼저 --stage blind")
    ans_p = cm.answer_path(track, date, dry)
    num_p = cm.eval_dir(track, arm) / ("dryrun" if dry else "") / f"{date}.json"
    for p in (ans_p, num_p):
        if not p.exists():
            fail(f"파일 없음: {p}  (정답 기록과 evaluate.py 실행이 먼저 필요)")
    pred = prediction_for(track, date, dry, arm)
    blind = load_json(blind_p)
    blind_core = {k: v for k, v in blind.items() if k in
                  ("evidence_checks", "invalidation_observability", "reasoning_review", "analyst_dependence", "scores")}
    files = {"prediction.json": pred, "snapshot.json": cm.snapshot_path(track, date, dry), "answer.json": ans_p,
             "numeric_eval.json": num_p, "blind_judgment.json": blind_core}
    out, run = run_session(files, CODE / files_cfg["outcome"], strip_schema("judgment_outcome.schema.json"),
                           "작업 폴더의 prediction.json, snapshot.json, answer.json, numeric_eval.json, blind_judgment.json을 "
                           "읽고 지침에 따라 2단계 판정 결과를 JSON으로 출력하세요.",
                           jcfg, track, date, "outcome", dry, arm)
    obj = {**meta(track, date, "outcome", run, pred, variant, arm), "answer_shown": True,
           "blind_file": blind_p.name, **out}
    save(obj, out_p)
    trig = {t["triggered"] for t in out["invalidation_triggered"]}
    print(f"  2단계: 분류 {out['reasoning_outcome']['category']} (주도: {out['reasoning_outcome']['dominant_side_in_hindsight']})"
          f" / 이미 반영 {out['priced_in']['score']} / 종합 {out['scores']['overall']}"
          f" / 무효화 발동 {'있음' if 'yes' in trig else '없음'}")
    print(f"  교훈: {out['lesson']}")
    print(f"  비용(표시값) ${run['cost_usd']}, {run['duration_ms']}ms")


def agreement(track, dry, arm=None):
    """같은 날짜의 기본 채점과 재채점(variant)·사람 점수의 점수 차이를 요약한다."""
    d = cm.eval_dir(track, arm) / ("dryrun" if dry else "rationale")
    human_d = cm.eval_dir(track, arm) / "human"
    rows = []
    for main in sorted(d.glob("????-??-??.blind.json")):
        date = main.name[:10]
        base = scores_of(d, date, None)
        others = {p.name.split(".")[2]: scores_of(d, date, p.name.split(".")[2])
                  for p in sorted(d.glob(f"{date}.blind.*.json"))}
        if (human_d / f"{date}.json").exists():
            others["human"] = load_json(human_d / f"{date}.json")["scores"]
        for who, sc in others.items():
            diffs = {k: sc[k] - base[k] for k in SCORE_KEYS if k in sc and k in base}
            rows.append((date, who, diffs))
    if not rows:
        print("비교할 재채점·사람 점수가 없음 (--variant 로 재채점, human/<날짜>.json 에 사람 점수)")
        return
    print(f"{'날짜':<12}{'비교':<8}" + "".join(f"{k[:12]:>14}" for k in SCORE_KEYS))
    for date, who, diffs in rows:
        print(f"{date:<12}{who:<8}" + "".join(f"{diffs[k]:>+14}" if k in diffs else f"{'-':>14}" for k in SCORE_KEYS))
    for who in sorted({w for _, w, _ in rows}):
        vals = [abs(v) for _, w, df in rows if w == who for v in df.values()]
        print(f"  {who}: 점수 차이 절댓값 평균 {sum(vals) / len(vals):.2f} (항목 {len(vals)}개), 2점 이상 차이 {sum(v >= 2 for v in vals)}개")


def scores_of(d, date, variant):
    suf = f".{variant}" if variant else ""
    sc = dict(load_json(d / f"{date}.blind{suf}.json")["scores"])
    o = d / f"{date}.outcome{suf}.json"
    if o.exists():
        sc["overall"] = load_json(o)["scores"]["overall"]
    return sc


# ---------------- B 트랙: 단일 단계 (보관) ----------------

def run_legacy(track, date, dry, force, jcfg, prompt_file):
    pdf_dir = ROOT / "01_리서치_원본" / date
    pred, _, snap_p, ans_p, num_p = (ev.dryrun_paths if dry else ev.official_paths)(track, date)
    out_p = cm.eval_dir(track) / ("dryrun" if dry else "rationale") / (f"{date}.rationale.json" if dry else f"{date}.json")
    for p in (snap_p, ans_p, num_p):
        if not Path(p).exists():
            fail(f"파일 없음: {p}  (정답 기록과 evaluate.py 실행이 먼저 필요)")
    if out_p.exists() and not force:
        fail(f"이미 채점 결과가 있음: {out_p} (재채점은 --force)")
    snap = load_json(snap_p)
    files = {"prediction.json": pred, "snapshot.json": snap_p, "answer.json": ans_p, "numeric_eval.json": num_p}
    for r in snap["reports"]:
        files[f"report_{r['source']}.pdf"] = pdf_dir / r["file"]
    schema = strip_schema("judgment.schema.json", LEGACY_META)
    out, run = run_session(files, CODE / prompt_file, schema,
                           "작업 폴더의 prediction.json, snapshot.json, report_daol.pdf, report_eugene.pdf, answer.json, "
                           "numeric_eval.json을 읽고 지침에 따라 채점 결과를 JSON으로 출력하세요.",
                           jcfg, track, date, "single", dry)
    judgment = {"schema_version": "0.1", "track": track, "trade_date": date,
                "judge_prompt_version": run["prompt_version"], "judge_model_id": run["model_id"] or "unknown",
                "judged_at": run["executed_at"], "prediction_executed_at": pred["executed_at"], **out}
    errs = schema_errors(load_json(cm.SCHEMAS / "judgment.schema.json"), judgment)
    if errs:
        fail("채점 결과 검증 실패: " + "; ".join(errs))
    save(judgment, out_p)
    st, sc = evidence_stats(out["evidence_checks"]), out["scores"]
    print(f"  점수: 수치충실 {sc['evidence_fidelity']} / 반영판단 {sc['priced_in']} / 무효화 {sc['invalidation_quality']}"
          f" / 추론 {sc['reasoning_quality']} / 종합 {sc['overall']}  |  근거 수치 오류 {st['errors']}/{st['checked']}"
          f"  |  분류 {out['reasoning_outcome']['category']}")
    print(f"  교훈: {out['lesson']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("date", nargs="?")
    ap.add_argument("--track", choices=cm.TRACKS, default=cm.config().get("default_track", "A"))
    ap.add_argument("--stage", choices=["all", "blind", "outcome"], default="all", help="2단계 채점의 단계 (A 트랙)")
    ap.add_argument("--variant", help="재채점 이름 (예: r2). 기본 채점과 별도 파일에 저장")
    ap.add_argument("--agreement", action="store_true", help="재채점·사람 점수와의 차이 요약")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="기존 채점 결과 덮어쓰기")
    ap.add_argument("--arm", default=cm.BASE_ARM, help="비교군 (기본 base, 예: news)")
    args = ap.parse_args()
    cfg = cm.config()
    tcfg = cm.track_cfg(cfg, args.track)
    acfg = cm.arm_cfg(tcfg, args.arm)
    if args.agreement:
        return agreement(args.track, args.dry_run, args.arm)
    if not args.date:
        ap.error("날짜 또는 --agreement 필요")

    jcfg = {**DEFAULT_JUDGE, **cfg.get("judge", {})}
    if tcfg.get("judge_mode") != "two_stage":
        if acfg is not None:
            fail("비교군 채점은 2단계 채점 트랙(A)에서만 지원")
        return run_legacy(args.track, args.date, args.dry_run, args.force, jcfg, tcfg["judge_prompt_file"])
    files_cfg = (acfg or {}).get("judge_prompt_files") or tcfg["judge_prompt_files"]
    news_profiles = (acfg or {}).get("news_profiles")
    cutoff = cm.at_time(args.date, tcfg["cutoff_time_kst"])
    if args.stage in ("all", "blind"):
        run_blind(args.track, args.date, args.dry_run, args.variant, args.force and args.stage == "blind", jcfg,
                  files_cfg, args.arm, news_profiles, cutoff)
    if args.stage in ("all", "outcome"):
        run_outcome(args.track, args.date, args.dry_run, args.variant, args.force, jcfg, files_cfg, args.arm)


if __name__ == "__main__":
    main()
