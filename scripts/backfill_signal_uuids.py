import argparse


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill signal UUIDs per MANM-150 ADR.")
    parser.add_argument("--apply", action="store_true", help="Apply changes to the database (dry-run by default).")
    args = parser.parse_args()

    if args.apply:
        parser.error(
            "apply mode is not implemented; no audit ran and no data was changed"
        )

    parser.error(
        "dry-run audit is not implemented; no validation queries ran and "
        "no data was changed"
    )
    return 2  # pragma: no cover - argparse.error exits

if __name__ == "__main__":
    raise SystemExit(main())
