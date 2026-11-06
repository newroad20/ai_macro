#!/usr/bin/env python3
"""B 트랙 예측 실행기.

입력 패키지 생성 -> 프로젝트 밖 임시 폴더에서 격리된 `claude -p` 실행 -> 스키마·의미 검증 -> 저장.

정식 실행:  python run_prediction.py 2026-11-05
시험 실행:  python run_prediction.py 2026-11-05 --dry-run --snapshot dryrun/2026-11-05.snapshot.json

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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
CODE = ROOT / "06_코드"
SCHEMAS = CODE / "schemas"
TRACK = "B"
KST = timezone(timedelta(hours=9))  # 한국은 서머타임이 없으므로 고정 오프셋 사용
META_FIELDS = ["schema_version", "track", "trade_date", "prompt_version", "model_id",
               "executed_at", "input_manifest_sha256"]


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def kst_now():
    return datetime.now(KST).replace(microsecond=0)


def kst_str(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S+09:00")


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fail(msg):
    print(f"[중단] {msg}", file=sys.stderr)
    sys.exit(2)


def schema_errors(schema_name, instance):
    schema = load_json(SCHEMAS / schema_name)
    return [f"{'/'.join(map(str, e.path)) or '(root)'}: {e.message}"
            for e in Draft202012Validator(schema).iter_errors(instance)]


def timing_problems(snap):
    """late 사유 목록. 비어 있으면 정시."""
    cfg = load_json(CODE / "experiment_config.json")
    sched = datetime.fromisoformat(snap["scheduled_at"])
    problems = []
    if datetime.fromisoformat(snap["captured_at"]) > sched + timedelta(minutes=cfg["late_tolerance_minutes"]):
        problems.append(f"스냅샷 기록({snap['captured_at']})이 허용 범위를 넘음")
    for r in snap["reports"]:
        for key in ("pdf_created_at", "obtained_at"):
            if datetime.fromisoformat(r[key]) > sched:
                problems.append(f"{r['source']} {key}({r[key]})가 T 이후")
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
    ap.add_argument("--dry-run", action="store_true", help="결과를 99_로그/dryrun 에만 저장 (late 검사는 경고만)")
    ap.add_argument("--snapshot", help="스냅샷 경로 (기본: 02_시장데이터/스냅샷/B/<date>.json)")
    ap.add_argument("--allow-late", action="store_true", help="late 상태로 실행하고 04_예측결과/B/late/ 에 저장")
    args = ap.parse_args()
    date = args.date

    cfg = load_json(CODE / "experiment_config.json")
    if args.snapshot:
        snap_path = Path(args.snapshot)
        if not snap_path.exists():  # 06_코드 기준 상대경로도 허용
            snap_path = CODE / args.snapshot
    else:
        snap_path = ROOT / "02_시장데이터" / "스냅샷" / TRACK / f"{date}.json"
    if not snap_path.exists():
        fail(f"스냅샷 없음: {snap_path}")
    snap = load_json(snap_path)
    errs = schema_errors("snapshot.schema.json", snap)
    if errs:
        fail("스냅샷이 스키마에 맞지 않음:\n  " + "\n  ".join(errs))
    if snap["trade_date"] != date:
        fail(f"스냅샷 trade_date({snap['trade_date']})와 인자({date})가 다름")

    out_dir = ROOT / "04_예측결과" / TRACK
    late_problems = timing_problems(snap)
    is_late = bool(late_problems)
    if not args.dry_run:
        neutral_band = cfg["neutral_band_ticks"]
        if neutral_band is None:
            fail("experiment_config.json 의 neutral_band_ticks 가 미확정(null). 정식 실행 전 확정 필요.")
        if (ROOT / "02_시장데이터" / "정답" / TRACK / f"{date}.json").exists():
            fail("정답 파일이 이미 존재함. 정답 기록 이후의 예측은 정식 표본이 될 수 없음.")
        for d in (out_dir, out_dir / "late"):
            if (d / f"{date}.json").exists():
                fail(f"이미 예측 파일이 있음: {d / (date + '.json')} (덮어쓰기 금지)")
        if is_late and not args.allow_late:
            fail("late 상태:\n  " + "\n  ".join(late_problems) + "\n  --allow-late 로 late 분리 저장 가능")
    else:
        neutral_band = cfg["neutral_band_ticks"] if cfg["neutral_band_ticks"] is not None else cfg["runner"]["dryrun_neutral_band_ticks"]
        if cfg["neutral_band_ticks"] is None:
            print(f"[경고] neutral_band_ticks 미확정 -> 시험용 임시값 {neutral_band} 사용")
    for p in late_problems:
        print(f"[late] {p}")
    if not cfg["tick_size"]["verified"]:
        print("[경고] tick_size 미검증 (0.01 가정)")

    prompt_path = CODE / cfg["runner"]["prompt_file"]
    prompt_version = prompt_path.stem.split("_", 1)[1]
    system_prompt = prompt_path.read_text(encoding="utf-8").format(
        neutral_band_ticks=neutral_band, tick_size=cfg["tick_size"]["ktb10y_fut"])

    started = kst_now()
    stamp = started.strftime("%Y%m%dT%H%M%S")
    log_dir = ROOT / "99_로그"
    (log_dir / "runs").mkdir(parents=True, exist_ok=True)
    pdf_dir = ROOT / "01_리서치_원본" / date

    pkg_record = ROOT / "03_가공데이터" / TRACK / date
    with tempfile.TemporaryDirectory(prefix="ai_macro_run_") as tmp:
        work = Path(tmp)
        manifest_hash = build_package(snap, pdf_dir, work)
        if not args.dry_run:
            shutil.copytree(work, pkg_record, dirs_exist_ok=True)
        print(f"[실행] model={cfg['runner']['model']} prompt={prompt_version} manifest={manifest_hash[:12]}…")
        try:
            proc = run_claude(work, system_prompt, cfg["runner"]["model"], cfg["runner"]["timeout_seconds"])
        except subprocess.TimeoutExpired:
            fail("claude 실행 시간 초과")

    raw_log = log_dir / "runs" / f"{date}_{TRACK}_{stamp}.jsonl"
    raw_log.write_text(proc.stdout, encoding="utf-8")
    init, result = parse_events(proc.stdout)
    status, detail = "ok", ""
    pred = None
    if proc.returncode != 0 or result.get("is_error") or not result:
        status, detail = "error", (proc.stderr.strip() or str(result.get("result", "")))[:500]
    elif not isinstance(result.get("structured_output"), dict):
        status, detail = "error", "structured_output 없음: " + str(result.get("result", ""))[:300]
    else:
        pred = {"schema_version": "0.1", "track": TRACK, "trade_date": date,
                "prompt_version": prompt_version, "model_id": init.get("model", "unknown"),
                "executed_at": kst_str(started), "input_manifest_sha256": manifest_hash,
                **result["structured_output"]}
        problems = schema_errors("prediction.schema.json", pred) + semantic_errors(pred, neutral_band)
        if problems:
            status, detail = "invalid", "; ".join(problems)

    usage = result.get("usage", {})
    summary = {"date": date, "track": TRACK, "dry_run": args.dry_run, "late": is_late, "status": status,
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
        dest = log_dir / "dryrun" / f"{date}_{stamp}.prediction.json"
    else:
        dest = (out_dir / "late" if is_late else out_dir) / f"{date}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(pred, ensure_ascii=False, indent=2), encoding="utf-8")
    p = pred["primary"]
    print(f"[완료] {dest.relative_to(ROOT)}\n  10Y선물: {p['direction']} / 틱 {p['expected_ticks']:+} / 확신 {p['confidence']}"
          f" / 확률 {p['prob']}\n  비용(표시값) ${summary['cost_usd']}, {summary['duration_ms']}ms")


if __name__ == "__main__":
    main()
