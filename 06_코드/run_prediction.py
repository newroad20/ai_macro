#!/usr/bin/env python3
"""예측 실행기 (기본 트랙 A).

입력 패키지 생성 -> 프로젝트 밖 임시 폴더에서 격리된 `claude -p` 실행 -> 스키마·의미 검증 -> 저장.

정식 실행:  python run_prediction.py 2026-11-10                 # A: 08:45 전에 끝나야 정시
시험 실행:  python run_prediction.py 2026-11-06 --dry-run       # 스냅샷: 06_코드/dryrun/A/<날짜>.snapshot.json
B 트랙:     python run_prediction.py 2026-11-09 --track B --dry-run

설계 원칙
- 예측 세션은 임시 폴더의 입력 파일만 본다 (--safe-mode, Read 도구만, 폴더 밖 읽기는 권한 거부).
- 검증을 통과한 첫 결과만 저장한다. 같은 날짜의 기존 예측은 덮어쓰지 않으며 자동 재시도는 없다.
- 정답 파일이 이미 있으면 정식 실행을 거부한다.
"""
import argparse
import copy
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from jsonschema import Draft202012Validator

import common as cm
from common import ROOT, CODE, SCHEMAS, fail, kst_now, kst_str, load_json

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

META_FIELDS = ["schema_version", "track", "trade_date", "prompt_version", "model_id",
               "executed_at", "input_manifest_sha256"]


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def schema_errors(schema_name, instance):
    schema = load_json(SCHEMAS / schema_name)
    return [f"{'/'.join(map(str, e.path)) or '(root)'}: {e.message}"
            for e in Draft202012Validator(schema).iter_errors(instance)]


def timing_problems(snap, tcfg, run_at=None):
    """late 사유 목록. 비어 있으면 정시. A는 예측 실행도 컷오프 전이어야 한다."""
    cutoff = cm.at_time(snap["trade_date"], tcfg["cutoff_time_kst"])
    problems = []
    cap_limit = cutoff + timedelta(minutes=tcfg["capture_tolerance_minutes"])
    if datetime.fromisoformat(snap["captured_at"]) > cap_limit:
        problems.append(f"스냅샷 기록({snap['captured_at'][11:16]})이 기준({cap_limit:%H:%M}) 이후")
    for r in snap["reports"]:
        for key in ("pdf_created_at", "obtained_at"):
            if datetime.fromisoformat(r[key]) > cutoff:
                problems.append(f"{r['source']} {key}({r[key][11:16]})가 컷오프({cutoff:%H:%M}) 이후")
    if tcfg.get("execute_before_cutoff") and run_at and run_at > cutoff:
        problems.append(f"예측 실행({run_at:%H:%M})이 컷오프({cutoff:%H:%M}) 이후")
    return problems


def build_package(snap, pdf_dir, dest):
    """snapshot.json + report_<source>.pdf + MANIFEST.json 을 만들고 manifest 해시를 반환."""
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "snapshot.json").write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    for r in snap["reports"]:
        src = Path(pdf_dir) / r["file"]
        if not src.exists():
            fail(f"리포트 파일 없음: {src}")
        shutil.copyfile(src, dest / f"report_{r['source']}.pdf")
    files = {p.name: sha256_file(p) for p in sorted(dest.iterdir()) if p.name != "MANIFEST.json"}
    manifest_hash = hashlib.sha256("\n".join(f"{k}:{v}" for k, v in sorted(files.items())).encode()).hexdigest()
    (dest / "MANIFEST.json").write_text(
        json.dumps({"files": files, "manifest_sha256": manifest_hash}, indent=2), encoding="utf-8")
    return manifest_hash


def model_output_schema():
    """모델이 채울 부분만 남긴 스키마 (메타 필드는 실행기가 채운다). 원본은 prediction.schema.json 하나."""
    s = copy.deepcopy(load_json(SCHEMAS / "prediction.schema.json"))
    s.pop("$schema", None)
    for k in META_FIELDS:
        s["properties"].pop(k)
        s["required"].remove(k)
    return s


def run_claude(workdir, system_prompt, model, timeout):
    cmd = ["claude", "-p", "--safe-mode", "--no-session-persistence", "--tools", "Read",
           "--model", model, "--output-format", "stream-json", "--verbose",
           "--system-prompt", system_prompt, "--json-schema", json.dumps(model_output_schema())]
    user_prompt = "작업 폴더의 snapshot.json, report_daol.pdf, report_eugene.pdf를 읽고, 지침에 따라 예측을 JSON으로 출력하세요."
    return subprocess.run(cmd, input=user_prompt, cwd=workdir, capture_output=True, text=True,
                          encoding="utf-8", timeout=timeout)


def parse_events(stdout):
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
    result = next((e for e in reversed(events) if e.get("type") == "result"), {})
    return init, result


def semantic_errors(pred, neutral_band):
    errs = []
    p = pred["primary"]
    probs = p["prob"]
    if abs(sum(probs.values()) - 1) > 0.01:
        errs.append(f"확률 합계가 1이 아님: {sum(probs.values()):.3f}")
    top = max(probs, key=probs.get)
    if p["direction"] != top:
        errs.append(f"direction({p['direction']})이 확률 최댓값({top})과 다름")
    expected_view = {"up": "long", "neutral": "neutral", "down": "short"}[p["direction"]]
    if p["duration_view"] != expected_view:
        errs.append(f"duration_view({p['duration_view']})가 direction({p['direction']})과 불일치")
    if p["direction"] == "up" and p["expected_ticks"] <= 0:
        errs.append("direction=up인데 expected_ticks <= 0")
    if p["direction"] == "down" and p["expected_ticks"] >= 0:
        errs.append("direction=down인데 expected_ticks >= 0")
    if p["direction"] == "neutral" and abs(p["expected_ticks"]) > neutral_band:
        errs.append(f"direction=neutral인데 |expected_ticks|가 중립 구간({neutral_band}) 초과")
    return errs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("date", help="거래일 YYYY-MM-DD")
    ap.add_argument("--track", choices=cm.TRACKS, default=cm.config().get("default_track", "A"))
    ap.add_argument("--dry-run", action="store_true", help="결과를 시험 폴더에만 저장 (late 검사는 경고만)")
    ap.add_argument("--snapshot", help="스냅샷 경로 (기본: 정식 02_시장데이터/스냅샷/<트랙>/, 시험 06_코드/dryrun/...)")
    ap.add_argument("--allow-late", action="store_true", help="late 상태로 실행하고 04_예측결과/<트랙>/late/ 에 저장")
    ap.add_argument("--allow-unfrozen", action="store_true", help="v0_freeze 와 다른 모델·프롬프트로 정식 실행 (의도한 변경일 때만)")
    args = ap.parse_args()
    date, track = args.date, args.track

    cfg = cm.config()
    tcfg = cm.track_cfg(cfg, track)
    if args.snapshot:
        snap_path = Path(args.snapshot)
        if not snap_path.exists():  # 06_코드 기준 상대경로도 허용
            snap_path = CODE / args.snapshot
    else:
        snap_path = cm.snapshot_path(track, date, args.dry_run)
    if not snap_path.exists():
        fail(f"스냅샷 없음: {snap_path}")
    snap = load_json(snap_path)
    errs = schema_errors("snapshot.schema.json", snap)
    if errs:
        fail("스냅샷이 스키마에 맞지 않음:\n  " + "\n  ".join(errs))
    if snap["trade_date"] != date or snap["track"] != track:
        fail(f"스냅샷({snap['track']} {snap['trade_date']})과 인자({track} {date})가 다름")

    out_dir = cm.prediction_dir(track)
    started = kst_now()
    late_problems = timing_problems(snap, tcfg, started)
    is_late = bool(late_problems)
    neutral_band = cm.neutral_band(tcfg, args.dry_run)
    if not args.dry_run:
        if tcfg["status"] != "active":
            fail(f"{track} 트랙은 현재 {tcfg['status']} 상태. 정식 실행 불가 (시험은 --dry-run).")
        if cm.answer_path(track, date).exists():
            fail("정답 파일이 이미 존재함. 정답 기록 이후의 예측은 정식 표본이 될 수 없음.")
        for d in (out_dir, out_dir / "late"):
            if (d / f"{date}.json").exists():
                fail(f"이미 예측 파일이 있음: {d / (date + '.json')} (덮어쓰기 금지)")
        if is_late and not args.allow_late:
            fail("late 상태:\n  " + "\n  ".join(late_problems) + "\n  --allow-late 로 late 분리 저장 가능")
    elif tcfg["neutral_band_ticks"] is None:
        print(f"[경고] {track} neutral_band_ticks 미확정 -> 시험용 임시값 {neutral_band} 사용")
    for p in late_problems:
        print(f"[late{' (시험이라 경고만)' if args.dry_run else ''}] {p}")

    prompt_path = CODE / tcfg["prompt_file"]
    prompt_version = prompt_path.stem.split("_", 1)[1]
    freeze = cfg.get("v0_freeze")
    if freeze and track == "A" and date >= freeze["from"]:
        drift = [f"{k}: 설정 {v} / 고정 {freeze[k]}" for k, v in
                 (("model", cfg["runner"]["model"]), ("prompt_version", prompt_version)) if v != freeze[k]]
        if drift and not args.dry_run and not args.allow_unfrozen:
            fail("점검 기간 고정값과 다름 (v0_freeze):\n  " + "\n  ".join(drift)
                 + "\n  의도한 변경이면 새 기간으로 기록하고 --allow-unfrozen")
        for d in drift:
            print(f"[경고] 고정값과 다름 — {d}")
    system_prompt = prompt_path.read_text(encoding="utf-8").format(
        neutral_band_ticks=neutral_band, tick_size=cfg["tick_size"]["ktb10y_fut"])

    stamp = started.strftime("%Y%m%dT%H%M%S")
    log_dir = ROOT / "99_로그"
    (log_dir / "runs").mkdir(parents=True, exist_ok=True)
    pdf_dir = ROOT / "01_리서치_원본" / date

    with tempfile.TemporaryDirectory(prefix="ai_macro_run_") as tmp:
        work = Path(tmp)
        manifest_hash = build_package(snap, pdf_dir, work)
        if not args.dry_run:
            shutil.copytree(work, ROOT / "03_가공데이터" / track / date, dirs_exist_ok=True)
        print(f"[실행] track={track} model={cfg['runner']['model']} prompt={prompt_version} manifest={manifest_hash[:12]}…")
        try:
            proc = run_claude(work, system_prompt, cfg["runner"]["model"], cfg["runner"]["timeout_seconds"])
        except subprocess.TimeoutExpired:
            fail("claude 실행 시간 초과")

    raw_log = log_dir / "runs" / f"{date}_{track}_{stamp}.jsonl"
    raw_log.write_text(proc.stdout, encoding="utf-8")
    init, result = parse_events(proc.stdout)
    status, detail = "ok", ""
    pred = None
    if proc.returncode != 0 or result.get("is_error") or not result:
        status, detail = "error", (proc.stderr.strip() or str(result.get("result", "")))[:500]
    elif not isinstance(result.get("structured_output"), dict):
        status, detail = "error", "structured_output 없음: " + str(result.get("result", ""))[:300]
    else:
        pred = {"schema_version": "0.1", "track": track, "trade_date": date,
                "prompt_version": prompt_version, "model_id": init.get("model", "unknown"),
                "executed_at": kst_str(started), "input_manifest_sha256": manifest_hash,
                **result["structured_output"]}
        problems = schema_errors("prediction.schema.json", pred) + semantic_errors(pred, neutral_band)
        if problems:
            status, detail = "invalid", "; ".join(problems)

    usage = result.get("usage", {})
    summary = {"date": date, "track": track, "dry_run": args.dry_run, "late": is_late, "status": status,
               "model_id": init.get("model"), "prompt_version": prompt_version,
               "executed_at": kst_str(started), "cost_usd": result.get("total_cost_usd"),
               "duration_ms": result.get("duration_ms"), "output_tokens": usage.get("output_tokens"),
               "detail": detail, "raw_log": str(raw_log.relative_to(ROOT))}
    with open(log_dir / "runs.log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(summary, ensure_ascii=False) + "\n")

    if status != "ok":
        print(f"[실패:{status}] {detail}\n원본 로그: {raw_log}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        dest = cm.dryrun_log_dir(track) / f"{date}_{stamp}.prediction.json"
    else:
        dest = (out_dir / "late" if is_late else out_dir) / f"{date}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(pred, ensure_ascii=False, indent=2), encoding="utf-8")
    p = pred["primary"]
    print(f"[완료] {dest.relative_to(ROOT)}\n  10Y선물: {p['direction']} / 틱 {p['expected_ticks']:+} / 확신 {p['confidence']}"
          f" / 확률 {p['prob']}\n  비용(표시값) ${summary['cost_usd']}, {summary['duration_ms']}ms")


if __name__ == "__main__":
    main()
