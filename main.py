"""Job Search Agent — CLI entrypoint."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv


def setup_logging(log_level: str = "INFO") -> None:
    """Configure logging to both console and log file."""
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    run_date = datetime.now().strftime("%Y-%m-%d")
    log_file = log_dir / f"run_{run_date}.log"

    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )


def main() -> None:
    """Main CLI entrypoint for the Job Search Agent."""
    parser = argparse.ArgumentParser(
        description="Agentic AI Job Search Agent — Local, open-source job finder",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --mode daily               # Daily job search (last 24h)
  python main.py --mode weekly              # Weekly job search (last 7 days)
  python main.py --mode daily --dry-run     # Dry run without LLM scoring or email
  python main.py --mode daily --no-email    # Run without sending email
        """,
    )
    parser.add_argument(
        "--mode",
        choices=["daily", "weekly"],
        default="daily",
        help="Search mode: 'daily' (last 24h) or 'weekly' (last 7 days). Default: daily",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run pipeline without LLM scoring or email sending",
    )
    parser.add_argument(
        "--no-email",
        action="store_true",
        help="Run pipeline but skip email sending",
    )
    parser.add_argument(
        "--criteria",
        default="criteria.md",
        help="Path to criteria file. Default: criteria.md",
    )
    parser.add_argument(
        "--sources",
        default="sources.yaml",
        help="Path to sources config file. Default: sources.yaml",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        help="Log level override (DEBUG, INFO, WARNING, ERROR)",
    )

    args = parser.parse_args()

    # Load environment variables
    load_dotenv()

    # Setup logging
    log_level = args.log_level or os.getenv("LOG_LEVEL", "INFO")
    setup_logging(log_level)

    logger = logging.getLogger("job_search_agent")
    logger.info("=" * 60)
    logger.info("Job Search Agent — Starting (%s mode)", args.mode)
    logger.info("=" * 60)

    if args.dry_run:
        logger.info("DRY RUN — LLM scoring and email will be skipped")

    # Build and run the pipeline
    from src.graph import build_pipeline

    pipeline = build_pipeline()

    run_date = datetime.now().strftime("%Y-%m-%d")
    initial_state = {
        "mode": args.mode,
        "dry_run": args.dry_run,
        "no_email": args.no_email,
        "criteria_path": args.criteria,
        "sources_path": args.sources,
        "run_date": run_date,
        "errors": [],
    }

    start_time = time.time()

    try:
        result = pipeline.invoke(initial_state)
        duration = time.time() - start_time

        # Log run to database
        from src.storage.database import JobRepository

        db_path = os.getenv("DB_PATH", "jobs.db")
        repo = JobRepository(db_path)
        repo.log_run(
            run_date=run_date,
            mode=args.mode,
            total_fetched=result.get("total_fetched", 0),
            total_filtered=result.get("total_filtered", 0),
            total_matched=result.get("total_matched", 0),
            total_emailed=result.get("total_new", 0) if result.get("email_sent") else 0,
            errors=result.get("errors"),
            duration_secs=duration,
        )
        repo.close()

        logger.info("=" * 60)
        logger.info("Pipeline complete in %.1f seconds", duration)
        logger.info(
            "Results: fetched=%d, filtered=%d, matched=%d, new=%d, email=%s",
            result.get("total_fetched", 0),
            result.get("total_filtered", 0),
            result.get("total_matched", 0),
            result.get("total_new", 0),
            "sent" if result.get("email_sent") else "skipped",
        )
        if result.get("errors"):
            logger.warning("Errors: %s", result["errors"])
        logger.info("=" * 60)

    except Exception as e:
        duration = time.time() - start_time
        logger.error("Pipeline failed after %.1f seconds: %s", duration, e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
