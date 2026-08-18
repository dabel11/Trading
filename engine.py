"""
분석 CLI: 스코어 스캔 · 백테스트. (자동매매 실행은 autotrader.py 데몬 전용 —
구 `run` 모드는 이중 실행 경로 제거를 위해 폐기됐다.)

실행 모드:
  python engine.py scan       # 유니버스 스코어 출력 (주문 없음)
  python engine.py backtest   # 백테스트 실행
"""

import argparse

from scorer import score_universe


def cmd_scan(args):
    """유니버스 전체 스코어 출력 — 주문 없음."""
    print("\n🔍  시그널 스캔 중…\n")
    scores = score_universe()
    print(f"{'티커':^6}  {'종합':^6}  {'기관':^6}  {'뉴스':^6}  {'섹터':^6}  {'펀더':^6}")
    print("─" * 50)
    for s in scores:
        flag = "🟢" if s.total >= 65 else ("🟡" if s.total >= 50 else "🔴")
        print(f"{flag} {s.ticker:5s}  {s.total:5.1f}  {s.institutional:5.1f}  "
              f"{s.sentiment:5.1f}  {s.sector:5.1f}  {s.fundamental:5.1f}")


def cmd_backtest(args):
    """백테스트 실행 후 결과 출력."""
    import backtester

    print(f"\n📈  백테스트  {args.start} → {args.end}  "
          f"초기자본 ${float(args.capital):,.0f}\n")

    result = backtester.run(
        start=args.start,
        end=args.end,
        capital=float(args.capital),
    )
    print(result.report())

    # 거래 내역 상세
    if args.trades:
        print("\n  거래 내역:")
        print(f"  {'티커':^6}  {'진입':^12}  {'청산':^12}  {'수익률':^8}  {'이유'}")
        print("  " + "─" * 58)
        for t in sorted(result.trades, key=lambda x: x.entry_date):
            print(f"  {t.ticker:6s}  {str(t.entry_date):12s}  {str(t.exit_date):12s}  "
                  f"{t.pnl_pct:+7.1%}  {t.reason}")

    # 수익 곡선 저장
    if args.plot:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            fig, ax = plt.subplots(figsize=(12, 5))
            result.equity_curve.plot(ax=ax, color="steelblue", linewidth=1.5)
            ax.set_title("자본 곡선 (Equity Curve)")
            ax.set_ylabel("USD")
            ax.grid(True, alpha=0.3)
            ax.axhline(float(args.capital), color="gray", linestyle="--", alpha=0.5,
                       label="시작 자본")
            ax.legend()
            fig.tight_layout()
            out = "equity_curve.png"
            fig.savefig(out, dpi=150)
            print(f"\n  📊  수익 곡선 저장 → {out}")
        except ImportError:
            print("  (matplotlib 없음 — pip install matplotlib)")


# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="자동 매매 트레이딩 엔진")
    sub = parser.add_subparsers(dest="cmd")

    # scan
    sub.add_parser("scan", help="유니버스 스코어 출력만 (주문 없음)")

    # backtest
    bt_p = sub.add_parser("backtest", help="백테스트 실행")
    bt_p.add_argument("--start",   default="2022-01-01", help="시작일 YYYY-MM-DD")
    bt_p.add_argument("--end",     default="2024-12-31", help="종료일 YYYY-MM-DD")
    bt_p.add_argument("--capital", default="10000",      help="초기 자본 (USD)")
    bt_p.add_argument("--trades",  action="store_true",  help="거래 내역 출력")
    bt_p.add_argument("--plot",    action="store_true",  help="수익 곡선 이미지 저장")

    args = parser.parse_args()

    if args.cmd == "scan":
        cmd_scan(args)
    elif args.cmd == "backtest":
        cmd_backtest(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
