"""CLI and orchestration entrypoint for Shovel."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass

from shovel.agent import run_agent
from shovel.utils import clone_repo, load_instances

logger = logging.getLogger(__name__)


@dataclass
class RunConfig:
    input: str
    output: str
    repo_dir: str
    model: str
    max_workers: int
    max_turns: int
    split: str | None = None
    instance_ids: list[str] | None = None
    start: int | None = None
    end: int | None = None
    log_dir: str | None = "./logs"
    resume: bool = False
    project_dir: str = "."


async def process_instance(
    instance: dict,
    repo_root_dir: str,
    model: str,
    max_turns: int,
    semaphore: asyncio.Semaphore,
    log_dir: str | None = None,
    project_dir: str = ".",
) -> tuple[str, dict | None]:
    """Process one instance: clone repo and run agent."""
    instance_id = instance["instance_id"]
    async with semaphore:
        loop = asyncio.get_running_loop()
        repo_dir = await loop.run_in_executor(None, clone_repo, instance, repo_root_dir)
        if repo_dir is None:
            logger.error("[%s] Failed to clone repo, returning empty result", instance_id)
            return instance_id, {"instance_id": instance_id}

        result = await run_agent(
            instance,
            repo_dir,
            model=model,
            max_turns=max_turns,
            log_dir=log_dir,
            project_dir=project_dir,
        )
        if result is None:
            logger.warning("[%s] Agent failed or output parse failed, returning empty result", instance_id)
            result = {}

        result["instance_id"] = instance_id
        return instance_id, result


def _filter_instances(instances: dict[str, dict], cfg: RunConfig) -> dict[str, dict]:
    """Apply instance id filtering and positional slicing."""
    selected = instances
    if cfg.instance_ids:
        ids = set(cfg.instance_ids)
        selected = {k: v for k, v in selected.items() if k in ids}
        logger.info("Filtered to %s instances", len(selected))

    if cfg.start is not None or cfg.end is not None:
        all_keys = list(selected.keys())
        start_idx = (cfg.start - 1) if cfg.start is not None else 0
        end_idx = cfg.end if cfg.end is not None else len(all_keys)
        selected_keys = all_keys[start_idx:end_idx]
        selected = {k: selected[k] for k in selected_keys}
        logger.info(
            "Sliced to instances [%s-%s], %s instances",
            cfg.start or 1,
            cfg.end or len(all_keys),
            len(selected),
        )
    return selected


def _load_existing_results(cfg: RunConfig) -> dict[str, dict]:
    """Load previous output when resume mode is enabled."""
    if not cfg.resume or not os.path.exists(cfg.output):
        return {}
    with open(cfg.output) as f:
        existing = json.load(f)
    logger.info("Resuming: loaded %s existing results", len(existing))
    return existing


async def run_pipeline(cfg: RunConfig) -> None:
    """Run the full generation pipeline."""
    logger.info("Loading instances from %s", cfg.input)
    instances = load_instances(cfg.input, split=cfg.split)
    logger.info("Loaded %s instances", len(instances))

    instances = _filter_instances(instances, cfg)
    if not instances:
        logger.error("No instances to process")
        return

    os.makedirs(cfg.repo_dir, exist_ok=True)
    all_results = _load_existing_results(cfg)
    if all_results:
        instances = {k: v for k, v in instances.items() if k not in all_results}
        logger.info("Remaining: %s instances to process", len(instances))
    if not instances:
        logger.info("All instances already processed")
        return

    semaphore = asyncio.Semaphore(cfg.max_workers)
    tasks = [
        asyncio.create_task(
            process_instance(
                instance,
                cfg.repo_dir,
                cfg.model,
                cfg.max_turns,
                semaphore,
                log_dir=cfg.log_dir,
                project_dir=cfg.project_dir,
            )
        )
        for instance in instances.values()
    ]

    completed = 0
    for coro in asyncio.as_completed(tasks):
        instance_id, result = await coro
        if result is not None:
            all_results[instance_id] = result
            completed += 1
            with open(cfg.output, "w") as f:
                json.dump(all_results, f, indent=2)
            logger.info(
                "Progress: %s/%s completed, saved %s to %s",
                completed,
                len(tasks),
                instance_id,
                cfg.output,
            )

    logger.info("Done! %s results saved to %s", len(all_results), cfg.output)
    omnigril_count = sum(
        1 for val in all_results.values() if "OMNIGRIL_EXIT_CODE" in val.get("eval_script", "")
    )
    setup_count = sum(
        1 for val in all_results.values() if "setup_repo.sh" in val.get("setup_scripts", {})
    )
    logger.info(
        "Validation: %s/%s have OMNIGRIL_EXIT_CODE, %s/%s have setup_repo.sh",
        omnigril_count,
        len(all_results),
        setup_count,
        len(all_results),
    )


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser."""
    parser = argparse.ArgumentParser(
        prog="shovel",
        description="Shovel: generate Docker environment configs for SWE-bench",
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Input dataset path (JSON/JSONL) or HuggingFace dataset name",
    )
    parser.add_argument("--output", default="docker_res.json", help="Output JSON file path")
    parser.add_argument("--repo-dir", default="./repo", help="Directory for cloning repos")
    parser.add_argument("--model", default="claude-sonnet-4-5-20250929", help="Claude model to use")
    parser.add_argument("--max-workers", type=int, default=4, help="Maximum concurrent agents")
    parser.add_argument("--max-turns", type=int, default=100, help="Maximum agent turns per instance")
    parser.add_argument("--split", default=None, help="Dataset split (for HuggingFace datasets)")
    parser.add_argument("--instance-ids", nargs="+", default=None, help="Process only specific instance IDs")
    parser.add_argument("--start", type=int, default=None, help="Start index (1-based) of instances to process")
    parser.add_argument("--end", type=int, default=None, help="End index (1-based, inclusive) of instances to process")
    parser.add_argument("--log-dir", default="./logs", help="Directory to save agent trajectory logs")
    parser.add_argument("--resume", action="store_true", help="Resume from existing output file")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI main function."""
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    project_dir = os.path.dirname(os.path.abspath(__file__))
    project_dir = os.path.dirname(project_dir)
    cfg = RunConfig(
        input=args.input,
        output=args.output,
        repo_dir=args.repo_dir,
        model=args.model,
        max_workers=args.max_workers,
        max_turns=args.max_turns,
        split=args.split,
        instance_ids=args.instance_ids,
        start=args.start,
        end=args.end,
        log_dir=args.log_dir,
        resume=args.resume,
        project_dir=project_dir,
    )

    asyncio.run(run_pipeline(cfg))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
