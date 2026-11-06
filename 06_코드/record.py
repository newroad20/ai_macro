#!/usr/bin/env python3
"""스냅샷·정답 기록기. 숫자 몇 개만 넘기면 나머지는 자동으로 채운다.

  # 거래일 T(10:00)에 — 입력 6개
  python record.py snapshot 2026-11-06 --f3 103.62 --f10 108.71 --k200 395.12 --fx 1339.8 --spx 0.41 --ndx 0.62

  # 장 마감 후 (예측이 저장된 뒤) — 입력 4개
  python record.py answer 2026-11-06 --f3 103.90 --f10 108.95 --k200 396.40 --fx 1337.5

자동으로 채우는 값: scheduled_at(설정의 T), captured_at(지금), 리포트 생성·입수 시각(PDF),
전일 종가(직전 정답 파일), 미국 세션 날짜(직전 평일).
--prev-f3/--prev-f10/--prev-k200/--prev-fx 로 전일 종가를 직접 줄 수 있다 (첫날 등).
--dry-run 은 파일을 쓰지 않고 결과만 출력한다.
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pypdf
from jsonschema import Draft202012Validator

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(os.environ.get("AI_MACRO_ROOT") or Path(__file__).resolve().parents[1])
CODE = Path(__file__).resolve().parent
TRACK = "B"
KST = timezone(timedelta(hours=9))
INSTR = {"f3": "ktb3y_fut", "f10": "ktb10y_fut", "k200": "kospi200", "fx": "usdkrw"}
# 전일 종가 대비 허용 변화율(%). 초과하면 오입력으로 보고 거부 (--force 로 무시)
MAX_MOVE_PCT = {"ktb3y_fut": 1.0, "ktb10y_fut": 1.5, "kospi200": 6.0, "usdkrw": 2.5}
MAX_US_PCT = 8.0
MARKET_CLOSE = (15, 45)


def fail(msg):
    print(f"[중단] {msg}", file=sys.stderr)
    sys.exit(2)


def kst_str(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S+09:00")


def load_json(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def validate(schema_name, obj):
    schema = load_json(CODE / "schemas" / schema_name)
    errs = [f"{'/'.join(map(str, e.path)) or '(root)'}: {e.message}" for e in Draft202012Validator(schema).iter_errors(obj)]
    if errs:
        fail("스키마 검증 실패:\n  " + "\n  ".join(errs))


def previous_close(date):
    """직전 정답 파일의 종가 (날짜가 date보다 이른 것 중 가장 최근)."""
    d = ROOT / "02_시장데이터" / "정답" / TRACK
    files = sorted(p for p in d.glob("*.json") if p.stem < date) if d.exists() else []
    if not files:
        return None, None
    return load_json(files[-1])["close"], files[-1].stem


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


def check_args(args, today):
    now = datetime.now(KST).replace(microsecond=0)
    if not args.dry_run and args.date != today:
        fail(f"정식 기록은 거래일 당일에만 가능 (오늘 {today}). 연습은 --dry-run.")
    return now


def snapshot(args):
    cfg = load_json(CODE / "experiment_config.json")
    now = check_args(args, datetime.now(KST).strftime("%Y-%m-%d"))
    out = ROOT / "02_시장데이터" / "스냅샷" / TRACK / f"{args.date}.json"
    if out.exists() and not args.dry_run:
        fail(f"이미 스냅샷이 있음: {out} (덮어쓰기 금지)")
    vals = {k: getattr(args, k) for k in INSTR}
    prev_in = {k: getattr(args, "prev_" + k) for k in INSTR}
    missing = [f"--{k}" for k, v in vals.items() if v is None]
    if missing or args.spx is None or args.ndx is None:
        fail("필수 입력 누락: " + " ".join(missing + [f"--{n}" for n, v in (("spx", args.spx), ("ndx", args.ndx)) if v is None]))
    prev, prev_date = previous_close(args.date)
    kr = {}
    for k, name in INSTR.items():
        p = prev_in[k] if prev_in[k] is not None else (prev or {}).get(name)
        if p is None:
            fail(f"전일 종가를 알 수 없음({name}). 직전 정답 파일이 없으니 --prev-{k} 로 직접 입력하세요.")
        move = abs(vals[k] / p - 1) * 100
        if move > MAX_MOVE_PCT[name] and not args.force:
            fail(f"{name}: 전일 종가 {p} 대비 {move:.2f}% 변화 (허용 {MAX_MOVE_PCT[name]}%). 오입력이면 수정, 맞으면 --force.")
        kr[name] = {"prev_close": p, "value_at_t": vals[k]}
    for n, v in (("S&P500", args.spx), ("Nasdaq", args.ndx)):
        if abs(v) > MAX_US_PCT and not args.force:
            fail(f"{n} 등락률 {v}% 는 비정상 범위. 오입력이면 수정, 맞으면 --force.")
    hh, mm = map(int, cfg["tracks"][TRACK]["cutoff_time_kst"].split(":"))
    sched = datetime.strptime(args.date, "%Y-%m-%d").replace(hour=hh, minute=mm, tzinfo=KST)
    snap = {"schema_version": "0.1", "track": TRACK, "trade_date": args.date,
            "scheduled_at": kst_str(sched), "captured_at": kst_str(now),
            "reports": find_reports(args.date),
            "us_close": {"as_of_date": args.us_date or previous_weekday(args.date),
                         "sp500": {"change_pct": args.spx}, "nasdaq": {"change_pct": args.ndx}},
            "kr": kr}
    validate("snapshot.schema.json", snap)
    late_min = (now - sched).total_seconds() / 60
    print(f"  전일 종가 출처: {'직전 정답 ' + prev_date if prev else '직접 입력'} / 미국 세션 {snap['us_close']['as_of_date']}")
    if late_min > cfg["late_tolerance_minutes"]:
        print(f"[late 예상] 기록 시각이 T보다 {late_min:.0f}분 늦음 (허용 {cfg['late_tolerance_minutes']}분). 예측 실행 시 --allow-late 필요.")
    show(snap, out, args.dry_run)


def answer(args):
    now = check_args(args, datetime.now(KST).strftime("%Y-%m-%d"))
    out = ROOT / "02_시장데이터" / "정답" / TRACK / f"{args.date}.json"
    if out.exists() and not args.dry_run:
        fail(f"이미 정답이 있음: {out} (덮어쓰기 금지)")
    pred = ROOT / "04_예측결과" / TRACK
    has_pred = any((d / f"{args.date}.json").exists() for d in (pred, pred / "late"))
    if not has_pred and not args.force and not args.dry_run:
        fail("이 날짜의 예측 파일이 없음. 예측 전에 정답을 기록하면 안 됨 (--force 로 무시 가능).")
    if (now.hour, now.minute) < MARKET_CLOSE and not args.force and not args.dry_run:
        fail(f"아직 장 마감 전 ({now:%H:%M}). {MARKET_CLOSE[0]}:{MARKET_CLOSE[1]} 이후에 기록 (--force 로 무시 가능).")
    missing = [f"--{k}" for k in INSTR if getattr(args, k) is None]
    if missing:
        fail("필수 입력 누락: " + " ".join(missing))
    ans = {"schema_version": "0.1", "track": TRACK, "trade_date": args.date, "recorded_at": kst_str(now),
           "close": {name: getattr(args, k) for k, name in INSTR.items()}}
    if args.source:
        ans["source"] = args.source
    validate("answer.schema.json", ans)
    show(ans, out, args.dry_run)


def show(obj, out, dry):
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    if dry:
        print("[dry-run] 저장하지 않음:\n" + text)
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"[저장] {out.relative_to(ROOT)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kind", choices=["snapshot", "answer"])
    ap.add_argument("date", help="거래일 YYYY-MM-DD")
    for k, label in (("f3", "3년 국채선물"), ("f10", "10년 국채선물"), ("k200", "KOSPI200"), ("fx", "USD/KRW")):
        ap.add_argument(f"--{k}", type=float, help=f"{label} (snapshot: T 시점 값, answer: 종가)")
        ap.add_argument(f"--prev-{k}", type=float, help=f"{label} 전일 종가 직접 입력")
    ap.add_argument("--spx", type=float, help="S&P500 전일 등락률(%%)")
    ap.add_argument("--ndx", type=float, help="Nasdaq 전일 등락률(%%)")
    ap.add_argument("--us-date", help="미국 세션 날짜 (기본: 직전 평일, 미국 휴장일이면 지정)")
    ap.add_argument("--source", help="정답 출처 메모")
    ap.add_argument("--force", action="store_true", help="이상치·시각 검사 무시")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    (snapshot if args.kind == "snapshot" else answer)(args)


if __name__ == "__main__":
    main()
