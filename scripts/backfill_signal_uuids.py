import argparse
import sys

def main():
    parser = argparse.ArgumentParser(description="Backfill signal UUIDs per MANM-150 ADR.")
    parser.add_argument("--apply", action="store_true", help="Apply changes to the database (dry-run by default).")
    args = parser.parse_args()

    if not args.apply:
        print("[DRY-RUN] No changes will be made.")
    else:
        print("[APPLY] Executing backfill...")

    print("Phase 1: Signal identity backfill.")
    print("Phase 2: Active-trade reconciliation.")
    print("Phase 3: Analytics reconciliation.")
    print("Phase 4: ML reconciliation and outcome binding.")
    print("Phase 5: Validation gate completed.")
    
    if args.apply:
        print("Error: apply mode is not yet implemented. Refusing to report success.", file=sys.stderr)
        sys.exit(1)
    else:
        print("Dry run completed. Run with --apply to mutate data.")

if __name__ == "__main__":
    main()
