"""
fpa — command line for onboarding, testing and operating the template.

  python -m fpa_agent validate [--live]              lint the semantic layer (and source schemas)
  python -m fpa_agent reconcile [--tags t] [--auto-months 3] [--out out/recon]
                                                      agent numbers vs client reference SQL
  python -m fpa_agent eval [--ids ...]               end-to-end questions through the real agent
  python -m fpa_agent ask "question"                 one question, with the audit trail
  python -m fpa_agent prompt [--agent data_explorer] print the assembled instruction
  python -m fpa_agent catalog ddl|push|pull|excel    move the semantic layer to/from BigQuery
  python -m fpa_agent demo build                     (re)build the demo source databases

Exit code is non-zero when validation errors or reconciliation failures exist,
so every command can gate CI/CD.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date


def _catalog(args):
    from fpa_agent.semantic.repository import YamlRepository, get_catalog, set_repository
    if getattr(args, "catalog_dir", None):
        set_repository(YamlRepository(args.catalog_dir))
    return get_catalog(force_reload=True)


def cmd_validate(args) -> int:
    from fpa_agent.semantic.validator import validate_against_sources, validate_catalog
    cat = _catalog(args)
    issues = validate_catalog(cat)
    if args.live:
        issues += asyncio.run(validate_against_sources(cat))
    for i in issues:
        print(i)
    errors = sum(1 for i in issues if i.level == "error")
    print(f"\n{len(cat.metrics)} metrics, {len(cat.analyses)} analyses, {len(cat.glossary)} glossary terms, "
          f"{len(cat.recon_tests)} tests — {errors} error(s), {len(issues) - errors} warning(s). "
          f"catalog version {cat.version}")
    return 1 if errors else 0


def cmd_reconcile(args) -> int:
    from fpa_agent.testing.reconcile import ReconciliationRunner, console_report, write_reports
    cat = _catalog(args)
    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    runner = ReconciliationRunner(cat, as_of=as_of)
    tests = [] if args.no_catalog_tests else runner.select(args.ids, args.tags)
    if args.auto_months:
        tests += runner.auto_tests(args.auto_months)
    if not tests:
        print("no tests selected")
        return 1
    results = asyncio.run(runner.run(tests))
    print(console_report(results))
    if args.out:
        paths = write_reports(results, args.out, cat.profile.name)
        print("\nreports:", json.dumps(paths, indent=1))
    return 0 if all(r.status in ("pass", "skipped") for r in results) else 1


def cmd_eval(args) -> int:
    from fpa_agent.testing.agent_eval import run_question_test
    cat = _catalog(args)
    tests = [t for t in cat.recon_tests.values() if t.kind == "question"
             and (not args.ids or t.test_id in args.ids)]
    ok = True
    for t in tests:
        r = asyncio.run(run_question_test(cat, t))
        ok &= r.status == "pass"
        print(f"[{r.status.upper()}] {t.test_id}: {r.message} ({r.duration_ms} ms)")
        print(f"    tools: {r.tools_called}  grounding: {r.grounding_outcome}  expected: {r.expected_figures}")
        print("    answer:", r.answer.replace("\n", "\n            ")[:1500])
    return 0 if ok else 1


def cmd_ask(args) -> int:
    from fpa_agent.testing.agent_eval import ask
    _catalog(args)
    out = asyncio.run(ask(args.question))
    print(out["answer"])
    print("\n--- tools ---")
    for c in out["tool_calls"]:
        print(f"{c['name']}({json.dumps(c['args'])})")
    print("\n--- grounding audit ---")
    print(json.dumps(out["audit"], indent=1, default=str))
    return 0


def cmd_prompt(args) -> int:
    from fpa_agent.agents import prompts
    cat = _catalog(args)
    print(prompts.explorer_instruction(cat) if args.agent == "data_explorer" else prompts.root_instruction(cat))
    return 0


def cmd_catalog(args) -> int:
    from fpa_agent.semantic import loader, schema
    if args.action == "ddl":
        print(schema.ddl(args.project or "{project}", args.dataset or "{dataset}", args.prefix))
        return 0
    if args.action == "push":
        print(json.dumps(loader.push(args.dir, args.project, args.dataset, args.prefix, args.dry_run), indent=1))
        return 0
    if args.action == "pull":
        print(json.dumps(loader.pull(args.project, args.dataset, args.dir, args.prefix), indent=1))
        return 0
    if args.action == "excel":
        print(json.dumps(loader.from_excel(args.xlsx, args.dir), indent=1))
        return 0
    return 1


def cmd_demo(args) -> int:
    import runpy
    from fpa_agent.config import PROJECT_ROOT
    sys.argv = [str(PROJECT_ROOT / "demo" / "build_demo_db.py")]
    runpy.run_path(sys.argv[0], run_name="__main__")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fpa", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--catalog-dir", help="YAML semantic layer dir (overrides SEMANTIC_BACKEND)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("validate"); p.add_argument("--live", action="store_true"); p.set_defaults(fn=cmd_validate)

    p = sub.add_parser("reconcile")
    p.add_argument("--ids", nargs="*"); p.add_argument("--tags", nargs="*")
    p.add_argument("--auto-months", type=int, default=0)
    p.add_argument("--no-catalog-tests", action="store_true")
    p.add_argument("--as-of"); p.add_argument("--out", default="out/reconciliation")
    p.set_defaults(fn=cmd_reconcile)

    p = sub.add_parser("eval"); p.add_argument("--ids", nargs="*"); p.set_defaults(fn=cmd_eval)
    p = sub.add_parser("ask"); p.add_argument("question"); p.set_defaults(fn=cmd_ask)
    p = sub.add_parser("prompt"); p.add_argument("--agent", default="fpa_analyst"); p.set_defaults(fn=cmd_prompt)

    p = sub.add_parser("catalog")
    p.add_argument("action", choices=["ddl", "push", "pull", "excel"])
    p.add_argument("--dir", default="semantic_layer/client")
    p.add_argument("--xlsx"); p.add_argument("--project"); p.add_argument("--dataset")
    p.add_argument("--prefix", default="sl_"); p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_catalog)

    p = sub.add_parser("demo"); p.add_argument("action", choices=["build"]); p.set_defaults(fn=cmd_demo)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
