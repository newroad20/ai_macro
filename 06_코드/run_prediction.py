#!/usr/bin/env python3
"""예측 실행기 (기본 트랙 A).

입력 패키지 생성 -> 프로젝트 밖 임시 폴더에서 격리된 `claude -p` 실행 -> 스키마·의미 검증 -> 저장.

정식 실행:  python run_prediction.py 2026-11-10                 # A: 08:45 전에 끝나야 정시
시험 실행:  python run_prediction.py 2026-11-06 --dry-run       # 스냅샷: 06_코드/dryrun/A/<날짜>.snapshot.json
B 트랙:     python run_prediction.py 2026-11-09 --track B --dry-run
뉴스 비교군: python run_prediction.py 2026-11-11 --arm news    # 기본 예측과 별도 세션·별도 저장
            (뉴스: 01_리서치_원본/<날짜>/news/{bond,equity}/articles/*.txt, 결과: 04_예측결과/A/arms/news/)

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
import re
import tempfile
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
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


# ---------------- 뉴스 (비교군 news) ----------------

def _article_header(path):
    head = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[:12]:
        if line.startswith("====="):
            break
        if ":" in line:
            k, v = line.split(":", 1)
            head[k.strip()] = v.strip()
    return head


def _collected_at(profile_dir):
    """article_list.txt 의 '생성: YYYY-MM-DD HH:MM' (KST) -> datetime | None"""
    lst = profile_dir / "article_list.txt"
    if not lst.exists():
        return None
    m = re.search(r"생성:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", lst.read_text(encoding="utf-8", errors="replace"))
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=cm.KST) if m else None


def build_news(date, profiles, cutoff, dest):
    """기사 원문을 관점별로 묶어 dest/news/<profile>.txt (기사마다 '### [<profile>_NN]' 머리) 와 INDEX.md 를 만든다.

    기사를 파일 하나씩 두면 모델이 목록만 보고 원문을 열지 않는 경우가 있어(11/10 시험) 관점별 한 파일로 묶는다.

    반환: {"problems": [late 사유], "included": n, "excluded": [(파일, 사유)], "profiles": {...}}
    - 수집 생성 시각이 컷오프 이후면 late 사유
    - 발행 시각이 컷오프 이후이거나 읽을 수 없는 기사는 제외 (누수 방지)
    - 수집 파이프라인의 LLM 요약(briefing/state/xlsx)은 넣지 않는다
    """
    src_root = cm.news_dir(date)
    info = {"problems": [], "included": 0, "excluded": [], "profiles": {}}
    rows = []
    out = dest / "news"
    out.mkdir(parents=True, exist_ok=True)
    for prof in profiles:
        pdir = src_root / prof
        arts = sorted((pdir / "articles").glob("*.txt")) if (pdir / "articles").exists() else []
        collected = _collected_at(pdir)
        info["profiles"][prof] = {"collected_at": cm.kst_str(collected) if collected else None, "articles": len(arts)}
        if not arts:
            info["problems"].append(f"뉴스 {prof} 기사 없음: {pdir / 'articles'}")
            continue
        if collected is None:
            info["problems"].append(f"뉴스 {prof} 수집 시각을 확인할 수 없음 (article_list.txt '생성:')")
        elif collected > cutoff:
            info["problems"].append(f"뉴스 {prof} 수집({collected:%H:%M})이 컷오프({cutoff:%H:%M}) 이후")
        chunks = []
        for i, a in enumerate(arts, 1):
            h = _article_header(a)
            try:
                pub = parsedate_to_datetime(h["발행"]).astimezone(cm.KST)
            except (KeyError, TypeError, ValueError):
                info["excluded"].append((a.name, "발행 시각 없음/형식 오류"))
                continue
            if pub > cutoff:
                info["excluded"].append((a.name, f"발행 {pub:%m-%d %H:%M}이 컷오프 이후"))
                continue
            name = f"{prof}_{i:02d}"
            body = a.read_text(encoding="utf-8", errors="replace").strip()
            chunks.append(f"### [{name}] 발행 {pub:%m-%d %H:%M} KST\n\n{body}\n")
            rows.append((pub, name, prof, h.get("매체", "?"), h.get("관련도", "?"), h.get("제목", a.stem)))
        if chunks:
            (out / f"{prof}.txt").write_text("\n\n".join(chunks), encoding="utf-8")
    info["included"] = len(rows)
    if rows:
        lines = [f"# 뉴스 목록 ({date} 개장 전 수집, 발행 시각 순)", "",
                 "원문은 관점별 파일 news/<관점>.txt 안에 '### [기사 ID]' 머리로 들어 있다.",
                 "발행 시각은 KST. 국내 D-1 정규장 마감은 D-1 15:45 KST. 같은 기사가 두 관점에 모두 있을 수 있다.", "",
                 "| 기사 ID | 관점 | 매체 | 발행(KST) | 관련도 | 제목 |", "|---|---|---|---|---|---|"]
        for pub, name, prof, media, score, title in sorted(rows):
            lines.append(f"| {name} | {prof} | {media} | {pub:%m-%d %H:%M} | {score} | {title.replace('|', '/')} |")
        (out / "INDEX.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return info


def build_package(snap, pdf_dir, dest, news_profiles=None, cutoff=None):
    """snapshot.json + report_<source>.pdf (+ news/) + MANIFEST.json 을 만든다. (manifest 해시, 뉴스 정보) 반환."""
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "snapshot.json").write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    for r in snap["reports"]:
        src = Path(pdf_dir) / r["file"]
        if not src.exists():
            fail(f"리포트 파일 없음: {src}")
        shutil.copyfile(src, dest / f"report_{r['source']}.pdf")
    news = build_news(snap["trade_date"], news_profiles, cutoff, dest) if news_profiles else None
    files = {p.relative_to(dest).as_posix(): sha256_file(p) for p in sorted(dest.rglob("*"))
             if p.is_file() and p.name != "MANIFEST.json"}
    manifest_hash = hashlib.sha256("\n".join(f"{k}:{v}" for k, v in sorted(files.items())).encode()).hexdigest()
    (dest / "MANIFEST.json").write_text(
        json.dumps({"files": files, "manifest_sha256": manifest_hash}, indent=2), encoding="utf-8")
    return manifest_hash, news


def model_output_schema():
    """모델이 채울 부분만 남긴 스키마 (메타 필드는 실행기가 채운다). 원본은 prediction.schema.json 하나."""
    s = copy.deepcopy(load_json(SCHEMAS / "prediction.schema.json"))
    s.pop("$schema", None)
    for k in META_FIELDS:
        s["properties"].pop(k)
        s["required"].remove(k)
    return s


def run_claude(workdir, system_prompt, model, timeout, with_news=False):
    cmd = ["claude", "-p", "--safe-mode", "--no-session-persistence", "--tools", "Read",
           "--model", model, "--output-format", "stream-json", "--verbose",
           "--system-prompt", system_prompt, "--json-schema", json.dumps(model_output_schema())]
    user_prompt = "작업 폴더의 snapshot.json, report_daol.pdf, report_eugene.pdf를 읽고, 지침에 따라 예측을 JSON으로 출력하세요."
    if with_news:
        user_prompt = ("작업 폴더의 snapshot.json, report_daol.pdf, report_eugene.pdf, news/INDEX.md, news/bond.txt, "
                       "news/equity.txt를 모두 읽고, 지침에 따라 예측을 JSON으로 출력하세요.")
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
    ap.add_argument("--arm", default=cm.BASE_ARM, help="비교군 (기본 base = 리포트만, news = 리포트+뉴스)")
    args = ap.parse_args()
    date, track, arm = args.date, args.track, args.arm

    cfg = cm.config()
    tcfg = cm.track_cfg(cfg, track)
    acfg = cm.arm_cfg(tcfg, arm)
    arm_tag = "" if acfg is None else f" arm={arm}"
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

    out_dir = cm.prediction_dir(track, arm)
    started = kst_now()
    late_problems = timing_problems(snap, tcfg, started)
    cutoff = cm.at_time(date, tcfg["cutoff_time_kst"])
    news_profiles = acfg.get("news_profiles") if acfg else None
    if news_profiles and not cm.news_dir(date).exists():
        fail(f"뉴스 폴더 없음: {cm.news_dir(date)}  (news/<profile>/articles/*.txt 형태로 넣기)")
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

    prompt_path = CODE / (acfg or tcfg)["prompt_file"]
    prompt_version = prompt_path.stem.split("_", 1)[1]
    freeze = cfg.get("v0_freeze") if acfg is None else acfg.get("freeze")
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
    pdf_dir = cm.research_dir(date)

    with tempfile.TemporaryDirectory(prefix="ai_macro_run_") as tmp:
        work = Path(tmp)
        manifest_hash, news = build_package(snap, pdf_dir, work, news_profiles, cutoff)
        if news:
            print(f"[뉴스] 포함 {news['included']}건, 제외 {len(news['excluded'])}건  "
                  + ", ".join(f"{k} 수집 {v['collected_at'][11:16] if v['collected_at'] else '?'}"
                              for k, v in news["profiles"].items()))
            for name, why in news["excluded"]:
                print(f"  제외: {name} ({why})")
            if news["included"] == 0:
                fail("포함할 뉴스 기사가 없음")
            late_problems += news["problems"]
            is_late = bool(late_problems)
            for p in news["problems"]:
                print(f"[late{' (시험이라 경고만)' if args.dry_run else ''}] {p}")
            if is_late and not args.dry_run and not args.allow_late:
                fail("late 상태:\n  " + "\n  ".join(late_problems) + "\n  --allow-late 로 late 분리 저장 가능")
        if not args.dry_run:
            pkg_dir = ROOT / "03_가공데이터" / track / ("" if acfg is None else f"arms/{arm}") / date
            shutil.copytree(work, pkg_dir, dirs_exist_ok=True)
        print(f"[실행] track={track}{arm_tag} model={cfg['runner']['model']} prompt={prompt_version} manifest={manifest_hash[:12]}…")
        try:
            proc = run_claude(work, system_prompt, cfg["runner"]["model"], cfg["runner"]["timeout_seconds"],
                              with_news=bool(news_profiles))
        except subprocess.TimeoutExpired:
            fail("claude 실행 시간 초과")

    raw_log = log_dir / "runs" / f"{date}_{track}{'' if acfg is None else '_' + arm}_{stamp}.jsonl"
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
    summary = {"date": date, "track": track, "arm": arm, "dry_run": args.dry_run, "late": is_late, "status": status,
               "model_id": init.get("model"), "prompt_version": prompt_version,
               "executed_at": kst_str(started), "cost_usd": result.get("total_cost_usd"),
               "duration_ms": result.get("duration_ms"), "output_tokens": usage.get("output_tokens"),
               "detail": detail, "raw_log": str(raw_log.relative_to(ROOT))}
    if news:
        summary["news"] = {"included": news["included"], "excluded": len(news["excluded"]),
                           "collected_at": {k: v["collected_at"] for k, v in news["profiles"].items()}}
    with open(log_dir / "runs.log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(summary, ensure_ascii=False) + "\n")

    if status != "ok":
        print(f"[실패:{status}] {detail}\n원본 로그: {raw_log}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        dest = cm.dryrun_log_dir(track, arm) / f"{date}_{stamp}.prediction.json"
    else:
        dest = (out_dir / "late" if is_late else out_dir) / f"{date}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(pred, ensure_ascii=False, indent=2), encoding="utf-8")
    p = pred["primary"]
    print(f"[완료]{arm_tag} {dest.relative_to(ROOT)}\n  10Y선물: {p['direction']} / 틱 {p['expected_ticks']:+} / 확신 {p['confidence']}"
          f" / 확률 {p['prob']}\n  비용(표시값) ${summary['cost_usd']}, {summary['duration_ms']}ms")


if __name__ == "__main__":
    main()
