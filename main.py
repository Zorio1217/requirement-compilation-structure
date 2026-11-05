"""Compile a requirement directory into an executable application."""

import argparse
import json
from pathlib import Path
import sys

from infrastructure.compiler import Compiler, PIPELINE
from infrastructure.utils.cli import resolve_directories
from infrastructure.utils.presentation.logging import configure_console
from infrastructure.utils.presentation.stages import stage_label


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Read public options and validate paths and the backend port before compilation."""
    config_path = Path(__file__).with_name('compiler_config.json')
    try:
        config = json.loads(config_path.read_text(encoding='utf-8')) if config_path.is_file() else {}
        workflow = config.get('workflow', 'optimized')
        if workflow not in {'baseline', 'optimized'}:
            raise ValueError('workflow must be baseline or optimized')
    except (OSError, ValueError, AttributeError) as error:
        raise ValueError(f'Invalid compiler_config.json: {error}') from error
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Stages:\n" + "\n".join(
            "  " + stage_label(name) + f"  (--stage {name})" for name, _, _ in PIPELINE))
    parser.add_argument("requirement_path", type=Path,
        help="Requirement directory containing requirements.yaml and reference assets")
    parser.add_argument("-o", "--output-dir", type=Path, required=True,
        help="Directory for the generated application and .arc artifacts")
    parser.add_argument("--model-log", "--debug-model-io", dest="model_log", action="store_true",
        help="Show detailed model I/O and save per-call prompt/response logs; usage and timing are always shown")
    parser.add_argument("--port", type=int, default=3000,
        help="Default generated backend port and frontend API proxy target (default: 3000)")
    parser.add_argument("--stage", choices=[name for name, _, _ in PIPELINE], metavar="STAGE",
        help="Run one stage by its full name (e.g. 06_implement_backend_tdd); omit for the full pipeline")
    parser.add_argument("--pause", action="store_true", help="Wait for Enter after each display stage")
    parser.add_argument('--optimize', action=argparse.BooleanOptionalAction, default=workflow == 'optimized',
        help='Use grouped design, bounded agents and staged regression (default: enabled); explicit config/CLI may override')
    parser.add_argument('--agent-max-calls', type=int, default=24, help='Maximum model calls per optimized task')
    parser.add_argument('--agent-max-reads', type=int, default=6, help='Maximum read/discovery requests per optimized task')
    args = parser.parse_args(argv)
    try:
        if min(args.agent_max_calls, args.agent_max_reads) < 1:
            raise ValueError('Agent budgets must be positive')
        if not 1 <= args.port <= 65535:
            raise ValueError("--port must be between 1 and 65535")
        args.requirement_path, args.output_dir = resolve_directories(
            argparse.Namespace(input=args.requirement_path, output=args.output_dir))
        if not args.requirement_path.is_dir():
            raise ValueError(f"Input directory does not exist: {args.requirement_path}")
        if not (args.requirement_path / "requirements.yaml").is_file():
            raise ValueError(f"Missing requirement document: {args.requirement_path / 'requirements.yaml'}")
        if args.output_dir.exists() and not args.output_dir.is_dir():
            raise ValueError(f"Output must be a directory: {args.output_dir}")
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return args


def main(argv: list[str] | None = None) -> None:
    configure_console()
    args = parse_args(argv)
    compiler = Compiler(args.requirement_path, args.output_dir,
        model_log=args.model_log, port=args.port, pause=args.pause)
    print('Compiler workflow: ' + ('optimized' if args.optimize else 'baseline'), flush=True)
    try:
        from infrastructure.shared.agent.optimization import optimized_run
        with optimized_run(args.optimize, max_calls=args.agent_max_calls, max_reads=args.agent_max_reads):
            compiler.compile(stage=args.stage)
    except KeyboardInterrupt:
        print(f"Compilation interrupted at {compiler.current_stage or 'startup'}.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as error:
        print(f"Compilation failed at {compiler.current_stage or 'startup'}: {error}", file=sys.stderr)
        raise SystemExit(0)
    if compiler.report["status"] == "COMPLETED_WITH_FAILURES":
        print(f"All passes completed. Report: {compiler.arc_dir / 'compilation.json'}", file=sys.stderr)
        raise SystemExit(0)


if __name__ == "__main__":
    main()
