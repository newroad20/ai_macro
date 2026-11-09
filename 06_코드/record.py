#!/usr/bin/env python3
"""스냅샷·정답 기록기. 숫자 몇 개만 넘기면 나머지는 자동으로 채운다. 기본 트랙은 A.

A 트랙 (주 트랙: 개장 전 실행, 전일 종가 -> 당일 종가)
  # 거래일 D 아침 08:45 전 — 미국 값 3개 (국내 전일 종가는 직전 정답 파일에서 자동)
  python record.py snapshot 2026-11-10 --spx 0.31 --ndx 1.09 --ust10y -5.2
  # D 장 마감 후 — 종가 4개 (이 파일이 다음 거래일의 전일 종가가 된다)
  python record.py answer 2026-11-10 --f3 103.90 --f10 109.45 --k200 790.1 --fx 1350.2 [--o10 109.30 --o3 103.80]

  기준 가격은 KRX 정규장(15:45) 종가. 야간 종가는 --night-f10/--night-f3 (선택, 입력 정보로만 사용).
  미국 값 출처·확인 시각은 --us-source 로 남긴다.

  첫날처럼 직전 정답 파일이 없으면 --prev-f3/--prev-f10/--prev-k200/--prev-fx 로 전일 종가를,
  --prev-ticks10/--prev-ticks3 으로 전일 국채선물 변동(틱)을 준다(전일 지속·반전 기준선용, 선택).

B 트랙 (중단·보관: 10:00 값 -> 마감)
  python record.py snapshot 2026-11-06 --track B --f3 103.62 --f10 108.71 --k200 395.12 --fx 1339.8 --spx 0.41 --ndx 0.62
  python record.py answer   2026-11-06 --track B --f3 103.90 --f10 108.95 --k200 396.40 --fx 1337.5

자동으로 채우는 값: scheduled_at(설정), captured_at(지금), 리포트 생성·입수 시각(PDF 워터마크),
전일 종가(직전 정답 파일), 미국 세션 날짜(직전 평일).
--dry-run: 시험 기록. 날짜·시각 검사를 건너뛰고 시험 폴더에 저장한다
  (A: 06_코드/dryrun/A/, 99_로그/dryrun/A/ · B: 06_코드/dryrun/, 99_로그/dryrun/).
"""
import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pypdf
from jsonschema import Draft202012Validator

import common as cm
from common import ROOT, KST, fail, kst_str, load_json

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

INSTR = {"f3": "ktb3y_fut", "f10": "ktb10y_fut", "k200": "kospi200", "fx": "usdkrw"}
TICK_ARGS = {"ktb10y_fut": "prev_ticks10", "ktb3y_fut": "prev_ticks3"}
# 전일 종가 대비 허용 변화율(%). 초과하면 오입력으로 보고 거부 (--force 로 무시)
MAX_MOVE_PCT = {"ktb3y_fut": 1.0, "ktb10y_fut": 1.5, "kospi200": 6.0, "usdkrw": 2.5}
MAX_US_PCT = 8.0
MAX_UST_BP = 40.0
MARKET_CLOSE = (15, 45)


def validate(schema_name, obj):
    schema = load_json(cm.SCHEMAS / schema_name)
    errs = [f"{'/'.join(map(str, e.path)) or '(root)'}: {e.message}" for e in Draft202012Validator(schema).iter_errors(obj)]
    if errs:
        fail("스키마 검증 실패:\n  " + "\n  ".join(errs))


def previous_weekday(date):
    d = datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def pdf_times(path):
    """(생성 시각, 입수 시각, 입수 시각 출처). 입수 시각은 FnGuide 워터마크, 없으면 파일 저장 시각."""
    r = pypdf.PdfReader(path)
    created = None
    m = re.match(r"D:(\d{14})([+-]\d{2})'(\d{2})'", str((r.metadata or {}).get("/CreationDate", "")))
    if m:
        tz = timezone(timedelta(hours=int(m.group(2)), minutes=int(m.group(3)) * (1 if m.group(2)[0] != "-" else -1)))
        created = datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=tz).astimezone(KST)
    obtained, src = None, "워터마크"
    for page in r.pages:
        w = re.search(r"::(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\)", page.extract_text() or "")
        if w:
            obtained = datetime.strptime(w.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=KST)
            break
    if obtained is None:
        obtained = datetime.fromtimestamp(Path(path).stat().st_mtime, KST).replace(microsecond=0)
        src = "파일 저장 시각(워터마크 없음)"
    return created or obtained, obtained, src


def find_reports(date):
    d = ROOT / "01_리서치_원본" / date
    if not d.exists():
        fail(f"리포트 폴더 없음: {d}")
    found = {}
    for p in d.glob("*.pdf"):
        n = p.name.upper()
        src = "daol" if ("DAOL" in n or "다올" in p.name) else "eugene" if ("EUGENE" in n or "유진" in p.name) else None
        if src:
            found.setdefault(src, []).append(p)
    for src in ("daol", "eugene"):
        if len(found.get(src, [])) != 1:
            fail(f"{d} 안에 {src} 리포트 PDF가 정확히 1개 있어야 함 (현재 {len(found.get(src, []))}개)")
    out = []
    for src in ("daol", "eugene"):
        p = found[src][0]
        created, obtained, how = pdf_times(p)
        print(f"  리포트 {src}: 생성 {created:%H:%M:%S} / 입수 {obtained:%H:%M} ({how})")
        out.append({"source": src, "file": p.name, "pdf_created_at": kst_str(created), "obtained_at": kst_str(obtained)})
    return out


def check_date(args):
    now = cm.kst_now()
    if not args.dry_run and args.date != now.strftime("%Y-%m-%d"):
        fail(f"정식 기록은 거래일 당일에만 가능 (오늘 {now:%Y-%m-%d}). 연습은 --dry-run.")
    return now


def check_move(name, value, prev, force):
    move = abs(value / prev - 1) * 100
    if move > MAX_MOVE_PCT[name] and not force:
        fail(f"{name}: 전일 종가 {prev} 대비 {move:.2f}% 변화 (허용 {MAX_MOVE_PCT[name]}%). 오입력이면 수정, 맞으면 --force.")


def us_block(args):
    for n, v in (("S&P500", args.spx), ("Nasdaq", args.ndx)):
        if abs(v) > MAX_US_PCT and not args.force:
            fail(f"{n} 등락률 {v}% 는 비정상 범위. 오입력이면 수정, 맞으면 --force.")
    us = {"as_of_date": args.us_date or previous_weekday(args.date),
          "sp500": {"change_pct": args.spx}, "nasdaq": {"change_pct": args.ndx}}
    if args.us_source:
        us["source"] = args.us_source
    if args.ust10y is not None:
        if abs(args.ust10y) > MAX_UST_BP and not args.force:
            fail(f"미국채 10Y 변화 {args.ust10y}bp 는 비정상 범위. 오입력이면 수정, 맞으면 --force.")
        us["ust10y"] = {"change_bp": args.ust10y}
        if args.ust10y_yield is not None:
            us["ust10y"]["yield"] = args.ust10y_yield
    return us


def write_out(obj, out, dry, force):
    if out.exists() and not (dry and force):
        fail(f"이미 파일이 있음: {out} (덮어쓰기 금지{' — 시험 파일은 --force 로 덮어쓰기 가능' if dry else ''})")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[저장{' (시험)' if dry else ''}] {out.relative_to(ROOT)}")


def snapshot(args):
    cfg = cm.config()
    tcfg = cm.track_cfg(cfg, args.track)
    now = check_date(args)
    out = cm.snapshot_path(args.track, args.date, args.dry_run)

    need = [("--spx", args.spx), ("--ndx", args.ndx)]
    if args.track == "A":
        need.append(("--ust10y", args.ust10y))
    else:
        need += [(f"--{k}", getattr(args, k)) for k in INSTR]
    missing = [n for n, v in need if v is None]
    if missing:
        fail("필수 입력 누락: " + " ".join(missing))

    prevs = cm.previous_answers(args.track, args.date, args.dry_run, n=2)
    last = prevs[-1][1] if prevs else None
    if last and args.prev_f10 is None and prevs[-1][0] != previous_weekday(args.date):
        print(f"[주의] 직전 정답이 {prevs[-1][0]} 것임 (직전 평일 {previous_weekday(args.date)} 아님). "
              "휴장일이 아니라면 빠진 날의 정답을 먼저 기록하거나 --prev-* 로 직접 주세요.")
    if last and args.contract and last.get("ktb_contract") and last["ktb_contract"] != args.contract:
        print(f"[주의] 직전 정답 월물({last['ktb_contract']})과 입력 월물({args.contract})이 다름. "
              "월물 교체일이면 --prev-f3/--prev-f10 으로 새 월물의 전일 종가를 직접 주세요.")
    kr = {}
    for k, name in INSTR.items():
        given = getattr(args, "prev_" + k)
        p = given if given is not None else (last["close"][name] if last else None)
        if p is None:
            fail(f"전일 종가를 알 수 없음({name}). 직전 정답 파일이 없으니 --prev-{k} 로 직접 입력하세요.")
        kr[name] = {"prev_close": p}
        if args.track == "B":
            check_move(name, getattr(args, k), p, args.force)
            kr[name]["value_at_t"] = getattr(args, k)
    if args.track == "A":
        for name, val in (("ktb10y_fut", args.night_f10), ("ktb3y_fut", args.night_f3)):
            if val is not None:
                check_move(name, val, kr[name]["prev_close"], args.force)
                kr[name]["night_close"] = val
        roll = tcfg.get("contract_roll", {})
        if roll.get("next_roll_date") and args.date >= roll["next_roll_date"] and args.contract != roll.get("next"):
            print(f"[주의] 월물 교체일({roll['next_roll_date']}) 이후. 규칙상 {roll.get('next')}물을 써야 함: "
                  f"--contract {roll.get('next')} 와 새 월물의 --prev-f3/--prev-f10 을 주세요.")
    if args.track == "A":  # 전일 변동 (전일 지속·반전 기준선용)
        for name, targ in TICK_ARGS.items():
            ticks = getattr(args, targ)
            if ticks is not None:
                kr[name]["prev_change"] = round(ticks * cfg["tick_size"][name], 4)
            elif len(prevs) == 2 and getattr(args, "prev_" + {v: k for k, v in INSTR.items()}[name]) is None:
                kr[name]["prev_change"] = round(prevs[1][1]["close"][name] - prevs[0][1]["close"][name], 4)

    sched = cm.at_time(args.date, tcfg["scheduled_time_kst"])
    snap = {"schema_version": "0.1", "track": args.track, "trade_date": args.date,
            "scheduled_at": kst_str(sched), "captured_at": kst_str(now),
            "reports": find_reports(args.date), "us_close": us_block(args), "kr": kr}
    if args.contract:
        snap["ktb_contract"] = args.contract
    if args.note:
        snap["note"] = args.note
    validate("snapshot.schema.json", snap)

    print(f"  전일 종가 출처: {'직전 정답 ' + prevs[-1][0] if last and args.prev_f10 is None else '직접 입력'}"
          f" / 미국 세션 {snap['us_close']['as_of_date']}")
    if args.track == "A" and "prev_change" not in kr["ktb10y_fut"]:
        print("  [안내] 10년 선물 전일 변동 없음 -> 전일 지속·반전 기준선은 이날 계산하지 않음 (--prev-ticks10 로 입력 가능)")
    limit = cm.at_time(args.date, tcfg["cutoff_time_kst"]) + timedelta(minutes=tcfg["capture_tolerance_minutes"])
    if now > limit and not args.dry_run:
        print(f"[late 예상] 기록 시각 {now:%H:%M}이 기준({limit:%H:%M})보다 늦음. 예측 실행 시 --allow-late 필요.")
    write_out(snap, out, args.dry_run, args.force)


def answer(args):
    now = check_date(args)
    out = cm.answer_path(args.track, args.date, args.dry_run)
    if not args.dry_run:
        pred = cm.prediction_dir(args.track)
        has_pred = any((d / f"{args.date}.json").exists() for d in (pred, pred / "late"))
        if not has_pred and not args.force:
            fail("이 날짜의 예측 파일이 없음. 예측 전에 정답을 기록하면 안 됨 (--force 로 무시 가능).")
        if (now.hour, now.minute) < MARKET_CLOSE and not args.force:
            fail(f"아직 장 마감 전 ({now:%H:%M}). {MARKET_CLOSE[0]}:{MARKET_CLOSE[1]} 이후에 기록 (--force 로 무시 가능).")
    missing = [f"--{k}" for k in INSTR if getattr(args, k) is None]
    if missing:
        fail("필수 입력 누락: " + " ".join(missing))
    snap_p = cm.snapshot_path(args.track, args.date, args.dry_run)
    if snap_p.exists():  # 오입력 검사: 그날 스냅샷의 전일 종가 대비
        kr = load_json(snap_p)["kr"]
        for k, name in INSTR.items():
            check_move(name, getattr(args, k), kr[name]["prev_close"], args.force)
    ans = {"schema_version": "0.1", "track": args.track, "trade_date": args.date, "recorded_at": kst_str(now),
           "close": {name: getattr(args, k) for k, name in INSTR.items()}}
    opens = {name: v for name, v in (("ktb3y_fut", args.o3), ("ktb10y_fut", args.o10)) if v is not None}
    if opens:
        ans["open"] = opens
    if args.contract:
        ans["ktb_contract"] = args.contract
    if args.source:
        ans["source"] = args.source
    if args.note:
        ans["note"] = args.note
    validate("answer.schema.json", ans)
    write_out(ans, out, args.dry_run, args.force)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kind", choices=["snapshot", "answer"])
    ap.add_argument("date", help="거래일 YYYY-MM-DD")
    ap.add_argument("--track", choices=cm.TRACKS, default=cm.config().get("default_track", "A"))
    for k, label in (("f3", "3년 국채선물"), ("f10", "10년 국채선물"), ("k200", "KOSPI200"), ("fx", "USD/KRW")):
        ap.add_argument(f"--{k}", type=float, help=f"{label} (answer: 종가, B snapshot: T 시점 값)")
        ap.add_argument(f"--prev-{k}", type=float, help=f"{label} 전일 종가 직접 입력")
    ap.add_argument("--prev-ticks10", type=float, help="A: 10년 국채선물 전일 변동(틱) 직접 입력")
    ap.add_argument("--prev-ticks3", type=float, help="A: 3년 국채선물 전일 변동(틱) 직접 입력")
    ap.add_argument("--night-f10", type=float, help="A 선택: 10년 국채선물 야간 종가 (입력 정보, 평가 기준 아님)")
    ap.add_argument("--night-f3", type=float, help="A 선택: 3년 국채선물 야간 종가")
    ap.add_argument("--o10", type=float, help="answer 선택: 10년 국채선물 정규장 시가 (시가 갭 분석)")
    ap.add_argument("--o3", type=float, help="answer 선택: 3년 국채선물 정규장 시가")
    ap.add_argument("--us-source", help="미국 값 출처와 확인 시각 (예: 'investing.com 07:50')")
    ap.add_argument("--spx", type=float, help="S&P500 직전 미국 세션 등락률(%%)")
    ap.add_argument("--ndx", type=float, help="Nasdaq 직전 미국 세션 등락률(%%)")
    ap.add_argument("--ust10y", type=float, help="A 필수: 미국채 10Y 직전 세션 변화(bp)")
    ap.add_argument("--ust10y-yield", type=float, help="미국채 10Y 금리 수준(%%, 선택)")
    ap.add_argument("--us-date", help="미국 세션 날짜 (기본: 직전 평일, 미국 휴장일이면 지정)")
    ap.add_argument("--contract", help="국채선물 월물 YYMM (예: 2612)")
    ap.add_argument("--source", help="정답 출처 메모")
    ap.add_argument("--note", help="메모")
    ap.add_argument("--force", action="store_true", help="이상치·시각 검사 무시, 시험 파일 덮어쓰기")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    (snapshot if args.kind == "snapshot" else answer)(args)


if __name__ == "__main__":
    main()
