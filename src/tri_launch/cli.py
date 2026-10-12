"""tri-launch CLI. Phase 0 adds a dev `phase0` command; the product commands
(doctor/qa/plan/create/resume/verify/cleanup/new) arrive in later phases."""

from __future__ import annotations

from typing import Optional

import typer

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__)


def _load_cfg_or_exit(launch_dir: str):
    from .config import LaunchConfigError, load_launch, validate_launch

    try:
        cfg = load_launch(launch_dir)
    except LaunchConfigError as exc:
        typer.secho(f"Config error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    issues = validate_launch(cfg)
    if issues:
        typer.secho("Launch folder is not valid:", fg=typer.colors.RED, err=True)
        for issue in issues:
            typer.echo(f"  - {issue}", err=True)
        raise typer.Exit(2)
    return cfg


def _client_or_exit(cfg):
    from .clients import resolve_client
    from .envload import ConfigError, get_api_key, load_env
    from .instantly import InstantlyClient

    try:
        load_env(optional=True)
        if cfg.api_key_env:
            _, key = get_api_key(cfg.api_key_env)
        else:
            client = resolve_client(None)
            if client.key:
                key = client.key
            else:
                _, key = get_api_key()
    except ConfigError as exc:
        typer.secho(f"Configuration error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    return InstantlyClient(key)


@app.command()
def version() -> None:
    """Print the package version."""
    from . import __version__

    typer.echo(__version__)


@app.command()
def qa(
    launch_dir: str = typer.Argument(..., help="launch folder (contains launch.yaml)"),
    seed: int = typer.Option(1337, "--seed", help="Seed for reproducible rendering"),
    proofread_flag: bool = typer.Option(
        True, "--proofread/--no-proofread", help="Run the DeepSeek AI proofread when configured"
    ),
) -> None:
    """Stages 1-3: validate, build leads, QA the copy; writes qa_report.html.
    Makes no API writes."""
    from .config import LaunchConfigError, load_launch, validate_launch
    from .qa import BLOCK, analyze_launch
    from .report import write_qa_report

    try:
        cfg = load_launch(launch_dir)
    except LaunchConfigError as exc:
        typer.secho(f"Config error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    issues = validate_launch(cfg)
    if issues:
        typer.secho("Launch folder is not valid:", fg=typer.colors.RED, err=True)
        for issue in issues:
            typer.echo(f"  - {issue}", err=True)
        raise typer.Exit(2)
    result = analyze_launch(cfg, seed=seed)
    if proofread_flag:
        from .proofread import collect_samples, proofread
        from .qa import WARN, QAIssue, worst

        result.proofread = proofread(collect_samples(result))
        if result.proofread.status == "ok":
            for issue in result.proofread.issues:
                for seg in result.segments:
                    for v in seg.variants:
                        if v.label == issue.variant and (issue.step is None or v.step == issue.step):
                            v.issues.append(
                                QAIssue(
                                    "warning",
                                    "proofread",
                                    f"[{issue.sample}] {issue.problem} - \"{issue.quote}\" -> {issue.suggestion}",
                                )
                            )
                            if v.status == "PASS":
                                v.status = WARN
            for seg in result.segments:
                seg.status = worst([v.status for v in seg.variants] or [seg.status])
            result.status = worst([s.status for s in result.segments])
        if result.proofread.status == "error":
            typer.secho(f"proofread: {result.proofread.error}", fg=typer.colors.YELLOW, err=True)
        else:
            typer.echo(f"proofread: {result.proofread.status}")
    path = write_qa_report(result)
    for seg in result.segments:
        typer.echo(
            f"{seg.status:5} {seg.name}: {seg.leads_count} leads, "
            f"{len(seg.variants)} variants, {sum(1 for v in seg.variants if v.status == BLOCK)} blocked"
        )
    typer.echo(f"Overall: {result.status}")
    typer.echo(f"Report: {path}")


@app.command()
def doctor(launch_dir: str = typer.Argument(...)) -> None:
    """Check key, settings campaign, sending accounts and lead files (no writes)."""
    cfg = _load_cfg_or_exit(launch_dir)
    client = _client_or_exit(cfg)
    try:
        source = client.resolve_campaign(cfg.settings_campaign)
        accounts = client.list_accounts(limit=100)
        blocklist_count = client.count_blocklist()
    except Exception as exc:  # noqa: BLE001
        typer.secho(f"FAILED: {type(exc).__name__}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    finally:
        client.close()
    typer.echo(f"key: OK ({cfg.api_key_env or 'auto-resolved'})")
    typer.echo(f"settings campaign: {source['name']} ({source['id']}) status={source['status']}")
    typer.echo(f"sending accounts visible: {len(accounts)}")
    typer.echo(f"blocklist entries: {blocklist_count}")
    typer.secho("doctor: OK", fg=typer.colors.GREEN)


@app.command()
def plan(
    launch_dir: str = typer.Argument(...),
    seed: int = typer.Option(1337, "--seed"),
) -> None:
    """Stages 1-4: validation + QA + the creation plan (no API writes)."""
    cfg = _load_cfg_or_exit(launch_dir)
    from .create import build_plan

    run = build_plan(cfg)
    typer.echo(f"Plan for {cfg.client} ({len(run.plans)} segments):")
    if cfg.segmentation.mode == "list_name" and cfg.segmentation_report:
        rep = cfg.segmentation_report
        typer.echo(
            f"  segmentation: column {rep.get('column')!r} -> {len(rep.get('created') or [])} "
            f"campaigns from {rep.get('values', 0)} values"
        )
        if rep.get("excluded"):
            typer.echo(f"  excluded values: {', '.join(rep['excluded'])}")
        if rep.get("too_small"):
            typer.echo(f"  below min_leads: {', '.join(rep['too_small'])}")
    for p in run.plans:
        mark = "BLOCKED" if p.blocked else "ready"
        typer.echo(
            f"  [{mark:7}] {p.campaign_name}  ({p.leads_count} leads, QA {p.qa_status})"
            + (f" - {p.blocked}" if p.blocked else "")
        )


@app.command()
def campaigns(
    search: str = typer.Option("", "--search", help="Filter by campaign name"),
    limit: int = typer.Option(100, "--limit"),
    client: str = typer.Option(
        "", "--client", help="Client workspace name or key env var (default: The Revenue Inbox)"
    ),
) -> None:
    """List campaigns in the workspace (read-only) - use this to pick a settings source."""
    from .clients import client_key
    from .envload import ConfigError, load_env
    from .instantly import InstantlyClient

    try:
        load_env(optional=True)
        client_obj, key = client_key(client or None)
    except ConfigError as exc:
        typer.secho(f"Configuration error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    status_names = {0: "Draft", 1: "Active", 2: "Paused", 3: "Completed", 4: "Running Subseq"}
    client = InstantlyClient(key)
    try:
        items = client.list_campaigns(search=search or None, limit=limit)
    finally:
        client.close()
    for c in items:
        st = status_names.get(c.get("status"), str(c.get("status")))
        typer.echo(f"{st:14} | {c['id']} | {c['name']}")
    typer.echo(f"{len(items)} campaign(s) in {client_obj.name}")


@app.command()
def web(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8090, "--port"),
) -> None:
    """Run the launcher web UI (browser app). Behind a reverse proxy it can host
    serves the launcher web UI; set TRI_LAUNCH_PASSWORD to require basic auth."""
    try:
        import uvicorn
    except ImportError:
        typer.secho(
            "Web extras are not installed. Run: pip install -e '.[web]'",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)
    typer.echo(f"launcher UI on http://{host}:{port}")
    uvicorn.run("tri_launch.web:app", host=host, port=port, log_level="info")


def _run_create(
    launch_dir: str,
    yes: bool,
    only: str,
    dry_run: bool,
    seed: int,
    interrupt_after_batches: int = 0,
) -> None:
    cfg = _load_cfg_or_exit(launch_dir)
    only_list = [s.strip() for s in only.split(",") if s.strip()] or None

    if dry_run:
        from .create import create_launch

        run = create_launch(cfg, None, only=only_list, dry_run=True, seed=seed)
        for p in run.plans:
            typer.echo(f"  {'BLOCKED' if p.blocked else 'ready '}: {p.campaign_name}")
        typer.echo(f"dry-run: {run.status}")
        return

    client = _client_or_exit(cfg)
    try:
        from .create import Interrupted, build_plan, create_launch
        from .report import write_launch_report

        preview = build_plan(cfg)
        typer.echo(f"About to create {len([p for p in preview.plans if not p.blocked])} draft campaigns:")
        for p in preview.plans:
            typer.echo(
                f"  {'BLOCKED' if p.blocked else 'create '}: {p.campaign_name} "
                f"({p.leads_count} leads)"
            )
        if not yes:
            answer = typer.prompt("Type 'create' to proceed")
            if answer.strip().lower() != "create":
                typer.secho("Aborted. Nothing was created.", fg=typer.colors.YELLOW)
                raise typer.Exit(1)
        try:
            run = create_launch(
                cfg,
                client,
                only=only_list,
                seed=seed,
                interrupt_after_batches=interrupt_after_batches or None,
            )
        except Interrupted as exc:
            typer.secho(f"INTERRUPTED (simulated): {exc}", fg=typer.colors.YELLOW, err=True)
            typer.echo("Run `tri-launch resume <launch_dir>` to continue.")
            raise typer.Exit(2)
        path = write_launch_report(run)
        for s in run.segments:
            typer.echo(f"  {s.status:6} {s.campaign_name}: {s.leads_uploaded} uploaded, {s.leads_duplicated} dupes, {s.leads_skipped} skipped, {s.leads_rejected} rejected")
            for issue in s.issues:
                typer.echo(f"          - {issue}")
        typer.echo(f"Overall: {run.status}")
        typer.echo(f"Report: {path}")
        if run.status != "PASS":
            raise typer.Exit(1)
    finally:
        client.close()


@app.command()
def create(
    launch_dir: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", help="Skip the typed confirmation"),
    only: str = typer.Option("", "--only", help="Comma-separated segment slugs"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Plan only, no API writes"),
    seed: int = typer.Option(1337, "--seed"),
    interrupt_after_batches: int = typer.Option(
        0,
        "--interrupt-after-batches",
        help="Testing only: abort after N lead batches (then resume)",
    ),
) -> None:
    """Stage 5: duplicate settings, patch copy, upload leads, read back (always DRAFT)."""
    _run_create(launch_dir, yes, only, dry_run, seed, interrupt_after_batches)


@app.command()
def resume(
    launch_dir: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes"),
    only: str = typer.Option("", "--only"),
    seed: int = typer.Option(1337, "--seed"),
) -> None:
    """Continue an interrupted run: reuses tracked campaigns, re-patches, re-uploads."""
    _run_create(launch_dir, yes, only, False, seed)


@app.command()
def verify(launch_dir: str = typer.Argument(...)) -> None:
    """Re-read every campaign this tool created for the launch folder."""
    cfg = _load_cfg_or_exit(launch_dir)
    client = _client_or_exit(cfg)
    from .create import verify_launch

    try:
        results = verify_launch(cfg, client)
    finally:
        client.close()
    if not results:
        typer.echo("No tracked campaigns for this launch folder.")
        return
    for r in results:
        typer.echo(f"{r['status']:8} {r['name']} ({r['id']}): {r.get('leads', '-')} leads")
        for issue in r["issues"]:
            typer.echo(f"         - {issue}")


@app.command()
def cleanup(
    launch_dir: str = typer.Argument(...),
    confirm_name: str = typer.Option(..., "--confirm-name", help="Exact campaign name"),
) -> None:
    """Delete ONE tool-created campaign (and its leads) after exact-name confirmation."""
    cfg = _load_cfg_or_exit(launch_dir)
    client = _client_or_exit(cfg)
    from . import state
    from .create import same_launch

    try:
        records = [
            (cid, rec)
            for cid, rec in state.load()["created"].items()
            if rec.get("name") == confirm_name and same_launch(rec.get("launch_path"), cfg)
        ]
        if len(records) != 1:
            typer.secho(
                f"Refusing: {len(records)} tracked campaigns named {confirm_name!r} for this launch.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(1)
        cid, rec = records[0]
        lead_ids = rec.get("lead_ids") or []
        if lead_ids:
            client.delete_leads(cid, lead_ids, require_phase0_pattern=False)
        client.delete_campaign(cid, confirm_name=confirm_name)
        typer.secho(f"Deleted {confirm_name} ({cid}) and {len(lead_ids)} leads.", fg=typer.colors.GREEN)
    finally:
        client.close()


@app.command()
def new(client: str = typer.Argument(...), batch: str = typer.Argument(...)) -> None:
    """Scaffold launches/<client>/<date>-<batch>/ from the templates."""
    from datetime import date
    from pathlib import Path

    out = Path("launches") / client / f"{date.today().isoformat()}-{batch}"
    if out.exists():
        typer.secho(f"{out} already exists.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    (out / "leads").mkdir(parents=True)
    (out / "copy").mkdir(parents=True)
    (out / "launch.yaml").write_text(
        f"""client: {client}
api_key_env: INSTANTLY_API_KEY
settings_campaign: "NAME OR ID OF SETTINGS CAMPAIGN"
naming: "{{client}} | {{segment}} | {{angle}} | {{date}}"
date_format: "%Y-%m-%d"
lead_mode: per_file
dedupe_across_segments: true
segments:
  - slug: my-segment
    name: My Segment
    angle: Guarantee
    industry: ""
    keyword: ""
    employee_size: ""
    leads: leads/my-segment.csv
    copy: copy/my-segment.md
    priority: 1
lint:
  ban_em_dash: true
  ban_exclamation: false
  max_body_words: 120
  max_links: 1
unsubscribe:
  mode: append
""",
        encoding="utf-8",
    )
    (out / "copy" / "my-segment.md").write_text(
        "# Step 1\ndelay: 0\n\n## Variant A\nSubject: quick question\nBody:\n"
        "Hi {{firstName | there}},\n\nFirst paragraph...\n\n{{sendingAccountFirstName}}\n",
        encoding="utf-8",
    )
    (out / "leads" / "my-segment.csv").write_text(
        "email,first_name,last_name,company_name,website,phone\n", encoding="utf-8"
    )
    typer.echo(f"Created {out}")


@app.command()
def phase0(
    date_str: Optional[str] = typer.Option(None, "--date", help="YYYY-MM-DD (default today)"),
    env_file: str = typer.Option(".env", "--env-file", help="Path to .env"),
    blocklist_email: Optional[str] = typer.Option(
        None, "--blocklist-email", help="Optional address known to be on the workspace blocklist"
    ),
    keep: bool = typer.Option(False, "--keep", help="Keep the scratch campaign (debugging only)"),
) -> None:
    """Phase 0 discovery: duplicate the test settings campaign, run the checks,
    delete the scratch campaign. Never activates anything."""
    from .envload import ConfigError
    from .phase0 import run_phase0

    try:
        code = run_phase0(
            date_str=date_str, env_file=env_file, blocklist_email=blocklist_email, keep=keep
        )
    except ConfigError as exc:
        typer.secho(f"Configuration error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    raise typer.Exit(code)


if __name__ == "__main__":
    app()
