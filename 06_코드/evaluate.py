#!/usr/bin/env python3
"""수치 평가기 (정답 기록 이후에만 실행). 기본 트랙 A.

  python evaluate.py 2026-11-10                 # 하루 평가 -> 05_평가결과/A/<날짜>.json
  python evaluate.py 2026-11-06 --dry-run       # 시험 -> 05_평가결과/A/dryrun/<날짜>.json
  python evaluate.py --summary                  # 누적 성과 (정시 표본과 late 분리)
  python evaluate.py 2026-11-09 --track B --dry-run

주 지표 (사전 고정, 00_통합기획안 v0.3 §5):
  10년 국채선물, 비중립 예측의 방향 적중률 + 신호당 평균 획득 틱 — 기준선과 비교.
보조 타깃(3Y·KOSPI200·USD/KRW)은 기술 통계로만 기록한다.

실제 변동
  A: D 종가 - D-1 종가(스냅샷 prev_close)
  B: D 종가 - T 시점 값(value_at_t)

기준선 (같은 중립 구간을 적용)
  momentum  : 직전 움직임 지속 (A: D-1 하루 변동 prev_change, B: 전일 종가->T)
  reversal  : 직전 움직임 반전
  neutral   : 항상 중립 (신호 없음)
  us_rates  : A만. 간밤 미국채 10Y 변화 방향 추종 (금리 하락 -> up). 임계값은 설정 ust_baseline_min_bp
  report_*  : 리포트 방향 전망 추종 — 02_시장데이터/리포트방향/<트랙>/<날짜>.json 이 있을 때만
              ({"daol": "up|neutral|down|none", "eugene": "...", "note": "..."}, 장 마감 전 수동 태깅)
"""
import argparse
import json
import sys
from datetime import datetime

import common as cm
from common import ROOT, fail, load_json

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

EVAL_VERSION = "0.2"
SIGN = {"up": 1, "neutral": 0, "down": -1}
OPPOSITE = {"up": "down", "down": "up", "neutral": "neutral"}


def classify(ticks, band):
    if abs(ticks) <= band:
        return "neutral"
    return "up" if ticks > 0 else "down"


def score_signal(direction, actual_ticks, actual_dir):
    """비중립 신호만 적중/획득 틱을 계산한다. 중립은 신호 없음."""
    if direction == "neutral":
        return {"direction": direction, "signal": False, "hit": None, "ticks_gained": 0.0}
    return {"direction": direction, "signal": True, "hit": direction == actual_dir,
            "ticks_gained": round(SIGN[direction] * actual_ticks, 2)}


def find_prediction(track, date):
    base = cm.prediction_dir(track)
    for d, late in ((base, False), (base / "late", True)):
        if (d / f"{date}.json").exists():
            return load_json(d / f"{date}.json"), late
    fail(f"예측 파일 없음: {base}/<late>/{date}.json")


def dryrun_paths(track, date):
    """(최신 시험 예측, late, 스냅샷 경로, 정답 경로, 평가 결과 경로)"""
    preds = sorted(cm.dryrun_log_dir(track).glob(f"{date}_*.prediction.json"))
    if not preds:
        fail(f"시험 예측 없음: {cm.dryrun_log_dir(track)}/{date}_*.prediction.json")
    return (load_json(preds[-1]), False, cm.snapshot_path(track, date, True),
            cm.answer_path(track, date, True), cm.eval_dir(track) / "dryrun" / f"{date}.json")


def official_paths(track, date):
    pred, late = find_prediction(track, date)
    return (pred, late, cm.snapshot_path(track, date), cm.answer_path(track, date),
            cm.eval_dir(track) / f"{date}.json")


def evaluate_day(track, date, force=False, dry=False):
    cfg = cm.config()
    tcfg = cm.track_cfg(cfg, track)
    band = cm.neutral_band(tcfg, dry)
    tick10, tick3 = cfg["tick_size"]["ktb10y_fut"], cfg["tick_size"]["ktb3y_fut"]

    pred, is_late, snap_p, ans_p, out = (dryrun_paths if dry else official_paths)(track, date)
    if out.exists() and not force:
        fail(f"이미 평가 결과가 있음: {out} (재평가는 --force)")
    for p in (snap_p, ans_p):
        if not p.exists():
            fail(f"파일 없음: {p}")
    snap, ans = load_json(snap_p), load_json(ans_p)
    if pred["track"] != track or snap["track"] != track:
        fail("예측·스냅샷의 트랙이 인자와 다름")

    # 누수 점검: 예측 실행 시각 < 정답 기록 시각
    if datetime.fromisoformat(pred["executed_at"]) >= datetime.fromisoformat(ans["recorded_at"]):
        fail("예측 실행 시각이 정답 기록 시각보다 늦음 — 정식 표본 불가")
    if snap.get("ktb_contract") and ans.get("ktb_contract") and snap["ktb_contract"] != ans["ktb_contract"]:
        fail(f"월물 불일치: 스냅샷 {snap['ktb_contract']} / 정답 {ans['ktb_contract']}")

    kr, close = snap["kr"], ans["close"]
    base_key = "prev_close" if track == "A" else "value_at_t"

    def start(name):
        return kr[name][base_key]

    def pre_move(name):
        """예측 시점 직전의 움직임 (가격 단위). 없으면 None."""
        if track == "A":
            return kr[name].get("prev_change")
        return kr[name]["value_at_t"] - kr[name]["prev_close"]

    act10 = round((close["ktb10y_fut"] - start("ktb10y_fut")) / tick10, 2)
    act10_dir = classify(act10, band)
    pm = pre_move("ktb10y_fut")
    pre10 = None if pm is None else round(pm / tick10, 2)

    p = pred["primary"]
    signals = {"ai": score_signal(p["direction"], act10, act10_dir)}
    notes = []
    if pre10 is not None:
        pre_dir = classify(pre10, band)
        signals["momentum"] = score_signal(pre_dir, act10, act10_dir)
        signals["reversal"] = score_signal(OPPOSITE[pre_dir], act10, act10_dir)
    else:
        notes.append("직전 변동 없음 -> momentum/reversal 미계산")
    signals["neutral"] = score_signal("neutral", act10, act10_dir)
    if track == "A":
        bp = snap["us_close"]["ust10y"]["change_bp"]
        th = tcfg.get("ust_baseline_min_bp", 2.0)
        us_dir = "up" if bp <= -th else "down" if bp >= th else "neutral"
        signals["us_rates"] = score_signal(us_dir, act10, act10_dir)
    tag_p = cm.report_tag_path(track, date)
    if tag_p.exists():
        tag = load_json(tag_p)
        for src in ("daol", "eugene"):
            if tag.get(src) in SIGN:
                signals[f"report_{src}"] = score_signal(tag[src], act10, act10_dir)
    else:
        notes.append("리포트 방향 태깅 없음 -> report_* 미계산")

    # 보조 타깃: 방향 부호 일치와 오차만 기록
    act3 = round((close["ktb3y_fut"] - start("ktb3y_fut")) / tick3, 2)
    k200_ret = round((close["kospi200"] / start("kospi200") - 1) * 100, 3)
    fx_chg = round(close["usdkrw"] - start("usdkrw"), 2)
    s = pred["secondary"]

    def sec(pred_dir, pred_val, actual):
        return {"pred_direction": pred_dir, "pred": pred_val, "actual": actual,
                "error": round(actual - pred_val, 3),
                "sign_match": None if pred_dir == "neutral" or actual == 0 else (SIGN[pred_dir] > 0) == (actual > 0)}

    result = {
        "eval_version": EVAL_VERSION, "track": track, "trade_date": date, "late": is_late, "dry_run": dry,
        "evaluated_at": cm.kst_str(cm.kst_now()),
        "model_id": pred["model_id"], "prompt_version": pred["prompt_version"],
        "neutral_band_ticks": band,
        "primary": {
            "instrument": "ktb10y_fut",
            "window": "D-1 종가 -> D 종가" if track == "A" else "T -> D 종가",
            "pre_ticks": pre10, "actual_ticks": act10, "actual_direction": act10_dir,
            "pred_direction": p["direction"], "pred_ticks": p["expected_ticks"], "confidence": p["confidence"],
            "prob": p["prob"], "tick_error": round(act10 - p["expected_ticks"], 2),
            "prob_of_actual": p["prob"][act10_dir],
        },
        "signals": signals,
        "secondary": {
            "ktb3y_fut_ticks": sec(s["ktb3y_fut"]["direction"], s["ktb3y_fut"]["expected_ticks"], act3),
            "kospi200_return_pct": sec(s["kospi200"]["direction"], s["kospi200"]["expected_return_pct"], k200_ret),
            "usdkrw_change_krw": sec(s["usdkrw"]["direction"], s["usdkrw"]["expected_change_krw"], fx_chg),
        },
        "notes": notes,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    pr = result["primary"]
    pre_txt = "없음" if pre10 is None else f"{pre10:+}"
    print(f"[평가] {track} {date}{' (late)' if is_late else ''}{' (시험)' if dry else ''}  10Y선물 직전 {pre_txt} 틱 / "
          f"실제 {act10:+} 틱 ({act10_dir}, 중립 ±{band})")
    ai = signals["ai"]
    print(f"  AI: {pr['pred_direction']} {pr['pred_ticks']:+} 틱 ({pr['confidence']}) -> "
          f"{'신호 없음' if not ai['signal'] else ('적중' if ai['hit'] else '실패')}, "
          f"획득 {ai['ticks_gained']:+} 틱, 실제 방향에 준 확률 {pr['prob_of_actual']}")
    for k, v in signals.items():
        if k != "ai":
            print(f"  기준선 {k:<14} {v['direction']:<8} 획득 {v['ticks_gained']:+} 틱")
    for n in notes:
        print(f"  ※ {n}")
    print(f"[저장] {out.relative_to(ROOT)}")
    return result


def summary(track):
    d = cm.eval_dir(track)
    rows = [load_json(p) for p in sorted(d.glob("*.json"))] if d.exists() else []
    if not rows:
        fail(f"{track} 평가 결과 없음")
    for label, group in (("정시", [r for r in rows if not r["late"]]), ("late", [r for r in rows if r["late"]])):
        if not group:
            continue
        print(f"\n== {track} {label} 표본 {len(group)}일 ({group[0]['trade_date']} ~ {group[-1]['trade_date']}) ==")
        names = sorted({k for r in group for k in r["signals"]}, key=lambda k: (k != "ai", k))
        print(f"  {'전략':<16}{'일수':>4}{'신호':>4}{'적중':>6}{'적중률':>8}{'신호당 틱':>10}{'누적 틱':>9}")
        for n in names:
            sig = [r["signals"][n] for r in group if n in r["signals"]]
            fired = [x for x in sig if x["signal"]]
            hits = sum(1 for x in fired if x["hit"])
            total = sum(x["ticks_gained"] for x in sig)
            rate = f"{hits / len(fired):.0%}" if fired else "-"
            per = f"{total / len(fired):+.1f}" if fired else "-"
            print(f"  {n:<16}{len(sig):>4}{len(fired):>4}{hits:>6}{rate:>8}{per:>10}{total:>+9.1f}")
        by_conf = {}
        for r in group:
            if r["signals"]["ai"]["signal"]:
                by_conf.setdefault(r["primary"]["confidence"], []).append(r["signals"]["ai"]["hit"])
        if by_conf:
            print("  AI 확신도별: " + ", ".join(f"{c} {sum(v)}/{len(v)}" for c, v in sorted(by_conf.items())))
        if len(group) < 30:
            print(f"  ※ 표본 {len(group)}일 — 시험운영(V0) 단계로 성능 결론을 내지 않음")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("date", nargs="?", help="거래일 YYYY-MM-DD")
    ap.add_argument("--track", choices=cm.TRACKS, default=cm.config().get("default_track", "A"))
    ap.add_argument("--summary", action="store_true", help="누적 성과 출력")
    ap.add_argument("--force", action="store_true", help="기존 평가 결과 덮어쓰기 (평가 코드 수정 시)")
    ap.add_argument("--dry-run", action="store_true", help="시험 실행 평가 (결과: 05_평가결과/<트랙>/dryrun/)")
    args = ap.parse_args()
    if args.summary:
        summary(args.track)
    elif args.date:
        evaluate_day(args.track, args.date, args.force, args.dry_run)
    else:
        ap.error("날짜 또는 --summary 필요")


if __name__ == "__main__":
    main()
