#!/usr/bin/env python3
"""B 트랙 수치 평가기 (정답 기록 이후에만 실행).

  python evaluate.py 2026-11-09            # 하루 평가 -> 05_평가결과/B/<날짜>.json
  python evaluate.py --summary             # 누적 성과 (정시 표본과 late 분리)

주 지표 (사전 고정, 00_통합기획안 §5):
  10년 국채선물, 비중립 예측의 방향 적중률 + 신호당 평균 획득 틱 — 기준선과 비교.
보조 타깃(3Y·KOSPI200·USD/KRW)은 기술 통계로만 기록한다.

기준선
  momentum  : 전일 종가 -> T 움직임이 마감까지 지속
  reversal  : 전일 종가 -> T 움직임이 반전
  neutral   : 항상 중립 (신호 없음)
  report    : 리포트 방향 전망 추종 — 02_시장데이터/리포트방향/B/<날짜>.json 이 있을 때만
              ({"daol": "up|neutral|down", "eugene": "...", "note": "..."}, 예측 전에 수동 태깅)
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
CODE = ROOT / "06_코드"
TRACK = "B"
KST = timezone(timedelta(hours=9))
EVAL_VERSION = "0.1"
SIGN = {"up": 1, "neutral": 0, "down": -1}


def fail(msg):
    print(f"[중단] {msg}", file=sys.stderr)
    sys.exit(2)


def load_json(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def classify(ticks, band):
    if abs(ticks) <= band:
        return "neutral"
    return "up" if ticks > 0 else "down"


def score_signal(direction, actual_ticks, actual_dir):
    """비중립 신호만 적중/획득 틱을 계산한다. 중립 신호는 None(신호 없음)."""
    if direction == "neutral":
        return {"direction": direction, "signal": False, "hit": None, "ticks_gained": 0.0}
    return {"direction": direction, "signal": True, "hit": direction == actual_dir,
            "ticks_gained": round(SIGN[direction] * actual_ticks, 2)}


def find_prediction(date):
    base = ROOT / "04_예측결과" / TRACK
    for d, late in ((base, False), (base / "late", True)):
        if (d / f"{date}.json").exists():
            return load_json(d / f"{date}.json"), late
    fail(f"예측 파일 없음: {base}/<late>/{date}.json")


def dryrun_paths(date):
    """시험 실행: 최신 dry-run 예측, 06_코드/dryrun 스냅샷, 99_로그/dryrun 정답. 결과는 05_평가결과/B/dryrun/."""
    preds = sorted((ROOT / "99_로그" / "dryrun").glob(f"{date}_*.prediction.json"))
    if not preds:
        fail(f"dry-run 예측 없음: 99_로그/dryrun/{date}_*.prediction.json")
    return (load_json(preds[-1]), False, CODE / "dryrun" / f"{date}.snapshot.json",
            ROOT / "99_로그" / "dryrun" / f"{date}.answer.json",
            ROOT / "05_평가결과" / TRACK / "dryrun" / f"{date}.json")


def evaluate_day(date, force=False, dry=False):
    cfg = load_json(CODE / "experiment_config.json")
    band = cfg["neutral_band_ticks"]
    if band is None:
        fail("neutral_band_ticks 미확정")
    tick10, tick3 = cfg["tick_size"]["ktb10y_fut"], cfg["tick_size"]["ktb3y_fut"]

    if dry:
        pred, is_late, snap_p, ans_p, out = dryrun_paths(date)
    else:
        out = ROOT / "05_평가결과" / TRACK / f"{date}.json"
        snap_p = ROOT / "02_시장데이터" / "스냅샷" / TRACK / f"{date}.json"
        ans_p = ROOT / "02_시장데이터" / "정답" / TRACK / f"{date}.json"
    if out.exists() and not force:
        fail(f"이미 평가 결과가 있음: {out} (재평가는 --force)")
    if not dry:
        pred, is_late = find_prediction(date)
    for p in (snap_p, ans_p):
        if not p.exists():
            fail(f"파일 없음: {p}")
    snap, ans = load_json(snap_p), load_json(ans_p)

    # 누수 점검: 예측 실행 시각 < 정답 기록 시각
    if datetime.fromisoformat(pred["executed_at"]) >= datetime.fromisoformat(ans["recorded_at"]):
        fail("예측 실행 시각이 정답 기록 시각보다 늦음 — 정식 표본 불가")

    kr, close = snap["kr"], ans["close"]

    def resid(name):
        return close[name] - kr[name]["value_at_t"]

    def morning(name):
        return kr[name]["value_at_t"] - kr[name]["prev_close"]

    act10 = round(resid("ktb10y_fut") / tick10, 2)
    act10_dir = classify(act10, band)
    morn10 = round(morning("ktb10y_fut") / tick10, 2)
    morn_dir = classify(morn10, band)
    opposite = {"up": "down", "down": "up", "neutral": "neutral"}

    p = pred["primary"]
    signals = {
        "ai": score_signal(p["direction"], act10, act10_dir),
        "momentum": score_signal(morn_dir, act10, act10_dir),
        "reversal": score_signal(opposite[morn_dir], act10, act10_dir),
        "neutral": score_signal("neutral", act10, act10_dir),
    }
    tag_p = ROOT / "02_시장데이터" / "리포트방향" / TRACK / f"{date}.json"
    if tag_p.exists():
        tag = load_json(tag_p)
        for src in ("daol", "eugene"):
            if tag.get(src) in SIGN:
                signals[f"report_{src}"] = score_signal(tag[src], act10, act10_dir)

    # 보조 타깃: 방향 부호 일치와 오차만 기록 (중립 기준 미정이라 적중률은 내지 않음)
    act3 = round(resid("ktb3y_fut") / tick3, 2)
    k200_ret = round(resid("kospi200") / kr["kospi200"]["value_at_t"] * 100, 3)
    fx_chg = round(resid("usdkrw"), 2)
    s = pred["secondary"]

    def sec(pred_dir, pred_val, actual):
        return {"pred_direction": pred_dir, "pred": pred_val, "actual": actual,
                "error": round(actual - pred_val, 3),
                "sign_match": None if pred_dir == "neutral" or actual == 0 else (SIGN[pred_dir] > 0) == (actual > 0)}

    result = {
        "eval_version": EVAL_VERSION, "track": TRACK, "trade_date": date, "late": is_late, "dry_run": dry,
        "evaluated_at": datetime.now(KST).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        "model_id": pred["model_id"], "prompt_version": pred["prompt_version"],
        "neutral_band_ticks": band,
        "primary": {
            "instrument": "ktb10y_fut", "morning_ticks": morn10, "actual_ticks": act10, "actual_direction": act10_dir,
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
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    pr = result["primary"]
    print(f"[평가] {date}{' (late)' if is_late else ''}  10Y선물 오전 {morn10:+} 틱 / 잔여장 실제 {act10:+} 틱 ({act10_dir})")
    print(f"  AI: {pr['pred_direction']} {pr['pred_ticks']:+} 틱 ({pr['confidence']}) -> "
          f"{'신호 없음' if not signals['ai']['signal'] else ('적중' if signals['ai']['hit'] else '실패')}, "
          f"획득 {signals['ai']['ticks_gained']:+} 틱, 실제 방향에 준 확률 {pr['prob_of_actual']}")
    for k, v in signals.items():
        if k != "ai":
            print(f"  기준선 {k:<14} {v['direction']:<8} 획득 {v['ticks_gained']:+} 틱")
    print(f"[저장] {out.relative_to(ROOT)}")
    return result


def summary():
    d = ROOT / "05_평가결과" / TRACK
    rows = [load_json(p) for p in sorted(d.glob("*.json"))] if d.exists() else []
    if not rows:
        fail("평가 결과 없음")
    for label, group in (("정시", [r for r in rows if not r["late"]]), ("late", [r for r in rows if r["late"]])):
        if not group:
            continue
        print(f"\n== {label} 표본 {len(group)}일 ({group[0]['trade_date']} ~ {group[-1]['trade_date']}) ==")
        names = sorted({k for r in group for k in r["signals"]}, key=lambda k: (k != "ai", k))
        print(f"  {'전략':<16}{'신호':>4}{'적중':>6}{'적중률':>8}{'신호당 틱':>10}{'누적 틱':>9}")
        for n in names:
            sig = [r["signals"][n] for r in group if n in r["signals"]]
            fired = [x for x in sig if x["signal"]]
            hits = sum(1 for x in fired if x["hit"])
            total = sum(x["ticks_gained"] for x in sig)
            rate = f"{hits / len(fired):.0%}" if fired else "-"
            per = f"{total / len(fired):+.1f}" if fired else "-"
            print(f"  {n:<16}{len(fired):>4}{hits:>6}{rate:>8}{per:>10}{total:>+9.1f}")
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
    ap.add_argument("--summary", action="store_true", help="누적 성과 출력")
    ap.add_argument("--force", action="store_true", help="기존 평가 결과 덮어쓰기 (평가 코드 수정 시)")
    ap.add_argument("--dry-run", action="store_true",
                    help="시험 실행 평가 (정답: 99_로그/dryrun/<날짜>.answer.json, 결과: 05_평가결과/B/dryrun/)")
    args = ap.parse_args()
    if args.summary:
        summary()
    elif args.date:
        evaluate_day(args.date, args.force, args.dry_run)
    else:
        ap.error("날짜 또는 --summary 필요")


if __name__ == "__main__":
    main()
