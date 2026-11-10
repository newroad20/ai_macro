#!/usr/bin/env python3
"""수치 평가기 (정답 기록 이후에만 실행). 기본 트랙 A.

  python evaluate.py 2026-11-10                 # 하루 평가 -> 05_평가결과/A/<날짜>.json
  python evaluate.py 2026-11-06 --dry-run       # 시험 -> 05_평가결과/A/dryrun/<날짜>.json
  python evaluate.py --summary                  # 누적 성과 (정시 표본과 late 분리)
  python evaluate.py --sources [--dry-run]      # 근거 출처 분포 (daol/eugene/미국 등, 정답 전에도 가능)
  python evaluate.py 2026-11-09 --track B --dry-run
  python evaluate.py 2026-11-11 --arm news      # 비교군 평가 -> 05_평가결과/A/arms/news/<날짜>.json
  python evaluate.py --summary --arm news       # 비교군 누적 성과
  python evaluate.py --compare [--dry-run]      # 기본 예측 vs 비교군, 둘 다 평가된 날짜만

주 지표 (사전 고정, 00_통합기획안 v0.3 §5, 설정 scoring_rule):
  10년 국채선물 단일 타깃.
  ① 신호당 평균 획득 틱: 비중립 신호 전체(실제 중립일 포함)의 부호 x 실제 변동
  ② 방향 적중률: 비중립 신호 중 실제 변동이 중립 구간 밖인 날만 (실제 중립일은 적중 판정 없음)
보조 타깃(3Y·KOSPI200·USD/KRW)과 시가->종가 분석은 기술 통계로만 기록한다.

실제 변동
  A: D 종가 - D-1 종가(스냅샷 prev_close)
  B: D 종가 - T 시점 값(value_at_t)

기준선 (같은 중립 구간을 적용)
  momentum  : 직전 움직임 지속 (A: D-1 하루 변동 prev_change, B: 전일 종가->T)
  reversal  : 직전 움직임 반전
  neutral   : 항상 중립 (신호 없음)
  us_rates  : A만. 간밤 미국채 10Y 변화 방향 추종 (금리 하락 -> up). 임계값은 설정 ust_baseline_min_bp
  always_up / always_down : 요약(--summary)에서만. 기간 중 항상 한 방향을 찍는 가장 기본 기준선
              (더 많이 나온 방향 = 사후 다수 방향은 표시만, 사후 정보이므로 AI에게 유리하지 않은 보수적 비교)
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
    """비중립 신호만 획득 틱을 계산한다. 실제가 중립 구간이면 적중 판정 없음(hit=None)."""
    if direction == "neutral":
        return {"direction": direction, "signal": False, "hit": None, "ticks_gained": 0.0}
    return {"direction": direction, "signal": True,
            "hit": None if actual_dir == "neutral" else direction == actual_dir,
            "ticks_gained": round(SIGN[direction] * actual_ticks, 2)}


def rationale_sources(pred):
    """근거(up/down_drivers)의 출처별 개수. 정답과 무관한 기술 통계."""
    out = {}
    for side in ("up_drivers", "down_drivers"):
        for d in pred.get("rationale", {}).get(side, []):
            src = d.get("source", "unknown")
            out.setdefault(src, {"up": 0, "down": 0})
            out[src][side.split("_")[0]] += 1
    return out


def find_prediction(track, date, arm=None):
    base = cm.prediction_dir(track, arm)
    for d, late in ((base, False), (base / "late", True)):
        if (d / f"{date}.json").exists():
            return load_json(d / f"{date}.json"), late
    fail(f"예측 파일 없음: {base}/<late>/{date}.json")


def dryrun_paths(track, date, arm=None):
    """(최신 시험 예측, late, 스냅샷 경로, 정답 경로, 평가 결과 경로). 스냅샷·정답은 비교군과 공유."""
    d = cm.dryrun_log_dir(track, arm)
    preds = sorted(d.glob(f"{date}_*.prediction.json"))
    if not preds:
        fail(f"시험 예측 없음: {d}/{date}_*.prediction.json")
    return (load_json(preds[-1]), False, cm.snapshot_path(track, date, True),
            cm.answer_path(track, date, True), cm.eval_dir(track, arm) / "dryrun" / f"{date}.json")


def official_paths(track, date, arm=None):
    pred, late = find_prediction(track, date, arm)
    return (pred, late, cm.snapshot_path(track, date), cm.answer_path(track, date),
            cm.eval_dir(track, arm) / f"{date}.json")


def evaluate_day(track, date, force=False, dry=False, arm=None):
    cfg = cm.config()
    tcfg = cm.track_cfg(cfg, track)
    band = cm.neutral_band(tcfg, dry)
    tick10, tick3 = cfg["tick_size"]["ktb10y_fut"], cfg["tick_size"]["ktb3y_fut"]

    pred, is_late, snap_p, ans_p, out = (dryrun_paths if dry else official_paths)(track, date, arm)
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

    # 시가 갭 분석 (정답에 10년 선물 시가가 있을 때만, 기술 통계)
    gap = None
    o10 = ans.get("open", {}).get("ktb10y_fut")
    if o10 is not None:
        gap_t = round((o10 - start("ktb10y_fut")) / tick10, 2)
        oc_t = round((close["ktb10y_fut"] - o10) / tick10, 2)
        gap = {"gap_ticks": gap_t, "open_to_close_ticks": oc_t, "open_to_close_direction": classify(oc_t, band),
               "ai_vs_open_to_close": None if p["direction"] == "neutral" or oc_t == 0
               else (SIGN[p["direction"]] > 0) == (oc_t > 0)}
    else:
        notes.append("시가 없음 -> 시가 갭 분석 생략")

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
        "eval_version": EVAL_VERSION, "track": track, "arm": arm or cm.BASE_ARM, "trade_date": date,
        "late": is_late, "dry_run": dry,
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
            "open_gap": gap,
        },
        "signals": signals,
        "rationale_sources": rationale_sources(pred),
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
    print(f"[평가] {track}{'' if arm in (None, cm.BASE_ARM) else ' arm=' + arm} {date}{' (late)' if is_late else ''}{' (시험)' if dry else ''}  10Y선물 직전 {pre_txt} 틱 / "
          f"실제 {act10:+} 틱 ({act10_dir}, 중립 ±{band})")
    ai = signals["ai"]
    print(f"  AI: {pr['pred_direction']} {pr['pred_ticks']:+} 틱 ({pr['confidence']}) -> "
          f"{'신호 없음' if not ai['signal'] else ('판정 없음(실제 중립)' if ai['hit'] is None else '적중' if ai['hit'] else '실패')}, "
          f"획득 {ai['ticks_gained']:+} 틱, 실제 방향에 준 확률 {pr['prob_of_actual']}")
    for k, v in signals.items():
        if k != "ai":
            print(f"  기준선 {k:<14} {v['direction']:<8} 획득 {v['ticks_gained']:+} 틱")
    if gap:
        print(f"  시가 갭 {gap['gap_ticks']:+} 틱 / 시가->종가 {gap['open_to_close_ticks']:+} 틱 ({gap['open_to_close_direction']})")
    for n in notes:
        print(f"  ※ {n}")
    print(f"[저장] {out.relative_to(ROOT)}")
    return result


def summary(track, arm=None):
    d = cm.eval_dir(track, arm)
    rows = [load_json(p) for p in sorted(d.glob("????-??-??.json"))] if d.exists() else []
    name = track if arm in (None, cm.BASE_ARM) else f"{track} arm={arm}"
    if not rows:
        fail(f"{name} 평가 결과 없음")
    for label, group in (("정시", [r for r in rows if not r["late"]]), ("late", [r for r in rows if r["late"]])):
        if not group:
            continue
        print(f"\n== {name} {label} 표본 {len(group)}일 ({group[0]['trade_date']} ~ {group[-1]['trade_date']}) ==")
        table = {n: [r["signals"][n] for r in group if n in r["signals"]]
                 for n in sorted({k for r in group for k in r["signals"]}, key=lambda k: (k != "ai", k))}
        for name, dirn in (("always_up", "up"), ("always_down", "down")):
            table[name] = [score_signal(dirn, r["primary"]["actual_ticks"], r["primary"]["actual_direction"]) for r in group]
        ups = sum(r["primary"]["actual_direction"] == "up" for r in group)
        downs = sum(r["primary"]["actual_direction"] == "down" for r in group)
        print(f"  실제 방향: up {ups} / down {downs} / 중립 {len(group) - ups - downs}  "
              f"(사후 다수 방향: {'always_up' if ups >= downs else 'always_down'})")
        print(f"  {'전략':<14}{'일수':>4}{'신호':>4}{'판정':>4}{'적중':>5}{'적중률':>7}{'신호당 틱':>10}{'누적 틱':>9}")
        for n, sig in table.items():
            fired = [x for x in sig if x["signal"]]
            judged = [x for x in fired if x["hit"] is not None]
            hits = sum(1 for x in judged if x["hit"])
            total = sum(x["ticks_gained"] for x in sig)
            rate = f"{hits / len(judged):.0%}" if judged else "-"
            per = f"{total / len(fired):+.1f}" if fired else "-"
            print(f"  {n:<14}{len(sig):>4}{len(fired):>4}{len(judged):>4}{hits:>5}{rate:>7}{per:>10}{total:>+9.1f}")
        by_conf = {}
        for r in group:
            a = r["signals"]["ai"]
            if a["signal"] and a["hit"] is not None:
                by_conf.setdefault(r["primary"]["confidence"], []).append(a["hit"])
        if by_conf:
            print("  AI 확신도별 적중: " + ", ".join(f"{c} {sum(v)}/{len(v)}" for c, v in sorted(by_conf.items())))
        gaps = [r["primary"]["open_gap"] for r in group if r["primary"].get("open_gap")]
        oc = [g["ai_vs_open_to_close"] for g in gaps if g["ai_vs_open_to_close"] is not None]
        if oc:
            print(f"  (기술 통계) AI 방향 vs 시가->종가 부호 일치 {sum(oc)}/{len(oc)}")
        print("  ※ 적중률은 실제 변동이 중립 구간 밖인 날만 판정. 신호당 틱은 모든 신호 포함.")
        if len(group) < 30:
            print(f"  ※ 표본 {len(group)}일 — 시험운영(V0) 단계로 성능 결론을 내지 않음 "
                  "(20일 중 14일 적중도 동전 던지기로 나올 확률 약 6%)")


def sources(track, dry, arm=None):
    """날짜별·누적 근거 출처 분포. 예측 파일만 읽으므로 정답 전에도 실행 가능."""
    preds = {}
    if dry:
        for p in sorted(cm.dryrun_log_dir(track, arm).glob("????-??-??_*.prediction.json")):
            preds[p.name[:10]] = (load_json(p), False)  # 날짜별 최신 시험 예측
    else:
        base = cm.prediction_dir(track, arm)
        for d, late in ((base, False), (base / "late", True)):
            for p in sorted(d.glob("????-??-??.json")) if d.exists() else []:
                preds[p.stem] = (load_json(p), late)
    if not preds:
        fail(f"{track} 예측 파일 없음{' (시험)' if dry else ''}")

    srcs = sorted({s for pr, _ in preds.values() for s in rationale_sources(pr)})
    total = {s: {"up": 0, "down": 0} for s in srcs}
    print(f"== {track} 근거 출처 분포{' (시험)' if dry else ''} {len(preds)}일 ==  (칸: 상승근거/하락근거)")
    print(f"  {'날짜':<12}{'방향':<9}" + "".join(f"{s:>12}" for s in srcs))
    for date, (pr, late) in sorted(preds.items()):
        rs = rationale_sources(pr)
        for s, c in rs.items():
            total[s]["up"] += c["up"]
            total[s]["down"] += c["down"]
        cells = "".join(f"{(str(rs[s]['up']) + '/' + str(rs[s]['down'])) if s in rs else '-':>12}" for s in srcs)
        print(f"  {date + ('*' if late else ''):<12}{pr['primary']['direction']:<9}{cells}")
    n_all = sum(c["up"] + c["down"] for c in total.values())
    print(f"  {'누적':<21}" + "".join(f"{str(total[s]['up']) + '/' + str(total[s]['down']):>12}" for s in srcs))
    print(f"  {'비중':<21}" + "".join(f"{(total[s]['up'] + total[s]['down']) / n_all:>12.0%}" for s in srcs))
    rep = {s: total[s]["up"] + total[s]["down"] for s in ("daol", "eugene") if s in total}
    if sum(rep.values()):
        print("  리포트 간 비중: " + " / ".join(f"{s} {v / sum(rep.values()):.0%}" for s, v in rep.items()))
    print("  * = late. 출처는 예측 세션이 스스로 표기한 값 (채점에서 오독 판정된 근거도 포함).")


def compare(track, dry):
    """기본 예측과 각 비교군을 같은 날짜끼리 비교한다 (둘 다 평가된 날짜만, 정시 표본만)."""
    tcfg = cm.track_cfg(cm.config(), track)
    arms = [a for a in cm.arm_names(tcfg) if a != cm.BASE_ARM]
    if not arms:
        fail(f"{track} 트랙에 비교군이 없음")
    sub = "dryrun" if dry else ""

    def load(arm):
        d = cm.eval_dir(track, arm) / sub
        rows = {p.stem: load_json(p) for p in sorted(d.glob("????-??-??.json"))} if d.exists() else {}
        return {k: v for k, v in rows.items() if not v["late"]}

    base = load(cm.BASE_ARM)
    for arm in arms:
        other = load(arm)
        dates = sorted(set(base) & set(other))
        print(f"\n== {track} 기본(base) vs {arm}{' (시험)' if dry else ''}: 공통 {len(dates)}일 "
              f"(base만 {len(set(base) - set(other))}일, {arm}만 {len(set(other) - set(base))}일 제외) ==")
        if not dates:
            continue
        print(f"  {'날짜':<12}{'실제 틱':>8}  {'base':<16}{arm:<16}")
        tot = {cm.BASE_ARM: [], arm: []}
        same = 0
        for dt in dates:
            b, o = base[dt], other[dt]
            act = b["primary"]["actual_ticks"]
            cells = []
            for r, key in ((b, cm.BASE_ARM), (o, arm)):
                sig = r["signals"]["ai"]
                tot[key].append(sig)
                mark = "-" if not sig["signal"] else ("·" if sig["hit"] is None else "O" if sig["hit"] else "X")
                cells.append(f"{r['primary']['pred_direction']} {mark} {sig['ticks_gained']:+.0f}")
            same += b["primary"]["pred_direction"] == o["primary"]["pred_direction"]
            print(f"  {dt:<12}{act:>+8.0f}  {cells[0]:<16}{cells[1]:<16}")
        for key, sig in tot.items():
            fired = [x for x in sig if x["signal"]]
            judged = [x for x in fired if x["hit"] is not None]
            hits = sum(1 for x in judged if x["hit"])
            total = sum(x["ticks_gained"] for x in sig)
            rate = f"{hits / len(judged):.0%}" if judged else "-"
            per = f"{total / len(fired):+.1f}" if fired else "-"
            print(f"  {key:<8} 신호 {len(fired)} / 판정 {len(judged)} / 적중 {hits} ({rate}) / 신호당 {per} 틱 / 누적 {total:+.1f} 틱")
        print(f"  방향 일치 {same}/{len(dates)}일  (O 적중, X 실패, · 실제 중립, - 신호 없음)")
        if len(dates) < 30:
            print("  ※ 공통 표본이 적어 차이에 대한 결론을 내지 않음")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("date", nargs="?", help="거래일 YYYY-MM-DD")
    ap.add_argument("--track", choices=cm.TRACKS, default=cm.config().get("default_track", "A"))
    ap.add_argument("--summary", action="store_true", help="누적 성과 출력")
    ap.add_argument("--force", action="store_true", help="기존 평가 결과 덮어쓰기 (평가 코드 수정 시)")
    ap.add_argument("--dry-run", action="store_true", help="시험 실행 평가 (결과: 05_평가결과/<트랙>/dryrun/)")
    ap.add_argument("--sources", action="store_true", help="근거 출처 분포 (예측 파일만 사용, 정답 전에도 가능)")
    ap.add_argument("--arm", default=cm.BASE_ARM, help="비교군 (기본 base, 예: news)")
    ap.add_argument("--compare", action="store_true", help="기본 예측 vs 비교군 (같은 날짜끼리)")
    args = ap.parse_args()
    cm.arm_cfg(cm.track_cfg(cm.config(), args.track), args.arm)  # 이름 검사
    if args.compare:
        compare(args.track, args.dry_run)
    elif args.sources:
        sources(args.track, args.dry_run, args.arm)
    elif args.summary:
        summary(args.track, args.arm)
    elif args.date:
        evaluate_day(args.track, args.date, args.force, args.dry_run, args.arm)
    else:
        ap.error("날짜 또는 --summary 필요")


if __name__ == "__main__":
    main()
