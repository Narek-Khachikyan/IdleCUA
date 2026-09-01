from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import typer
from rich.console import Console
from rich.prompt import Prompt, Confirm

from .models import Profile
from .store import save_profile


@dataclass
class Question:
    key: str  # dotted path like "user_characteristics.occupation"
    prompt: str
    help_text: str = ""
    default: str = ""
    is_list: bool = False  # if True, comma-separated list
    is_int: bool = False


# Scripted questionnaire covering all required topics per issue #5.
# Works with no LLM configured — purely typer/rich prompts.
QUESTIONS: list[Question] = [
    # User characteristics
    Question("user_characteristics.occupation", "What is your occupation?", "e.g. software engineer, researcher, student", ""),
    Question("user_characteristics.projects", "What projects are you working on? (comma-separated)", "e.g. IdleCUA, personal blog, thesis", "", is_list=True),
    Question("user_characteristics.goals", "What are your goals? (comma-separated)", "What you want IdleCUA to help research", "", is_list=True),
    Question("user_characteristics.interests", "What are your interests? (comma-separated)", "Topics you care about", "", is_list=True),
    Question("user_characteristics.technologies", "Which technologies do you use/follow? (comma-separated)", "e.g. Python, Cua, LLMs, macOS", "", is_list=True),
    Question("user_characteristics.material_types", "Preferred material types? (comma-separated)", "e.g. articles, papers, videos, GitHub repos, tools", "", is_list=True),
    Question("user_characteristics.material_depth", "Preferred material depth?", "overview / deep dive / mixed", "mixed"),
    Question("user_characteristics.content_languages", "Content languages? (comma-separated)", "e.g. English, Armenian", "English", is_list=True),
    Question("user_characteristics.unwanted_topics", "Unwanted topics to avoid? (comma-separated)", "Leave empty if none", "", is_list=True),
    # Computer usage
    Question("computer_usage.schedule", "What is your typical work schedule?", "e.g. Mon-Fri 9:00-18:00", ""),
    Question("computer_usage.idle_periods", "When is your Mac typically idle?", "e.g. meetings 10-12, nights", ""),
    Question("computer_usage.overnight_habits", "Does your Mac stay on overnight? (yes/no + details)", "e.g. yes, sleep at night", ""),
    Question("computer_usage.screen_lock_habits", "Screen-lock habits?", "e.g. auto-lock after 5 min, manual lock", ""),
    Question("computer_usage.monitors", "How many monitors / display setup?", "e.g. 1, 2 external", "1"),
    Question("computer_usage.common_apps", "Commonly open apps? (comma-separated)", "e.g. Chrome, VS Code, Slack", "", is_list=True),
    Question("computer_usage.common_sites", "Commonly open sites? (comma-separated)", "e.g. github.com, reddit.com", "", is_list=True),
    Question("computer_usage.return_signals", "How do you signal you're back? (comma-separated)", "e.g. mouse move, key press, unlock", "mouse move, key press", is_list=True),
    Question("computer_usage.idle_threshold_minutes", "Idle threshold before agent starts (minutes)?", "default 10, 1..120", "10", is_int=True),
    # Autonomy boundaries
    Question("autonomy_boundaries.allowed_sites", "Allowed sites for the agent (comma-separated domains)?", "e.g. x.com, reddit.com, github.com, arxiv.org, google.com", "x.com, reddit.com, youtube.com, github.com, news.ycombinator.com, arxiv.org, facebook.com, instagram.com, linkedin.com, tiktok.com, bsky.app, threads.net, mastodon.social, google.com", is_list=True),
    Question("autonomy_boundaries.allowed_apps", "Allowed apps for the agent (comma-separated)?", "e.g. Chrome, Preview", "Chrome", is_list=True),
    Question("autonomy_boundaries.auto_allowed_actions", "Auto-allowed actions? (comma-separated)", "e.g. open allowed site, search, read, scroll, extract", "open allowed site, search, read, scroll, open link, extract, save note, close own tab", is_list=True),
    Question("autonomy_boundaries.confirmation_required_actions", "Confirmation-required actions? (comma-separated)", "e.g. like, follow, comment, post, download", "like, follow, comment, post, message, form submit, download", is_list=True),
    Question("autonomy_boundaries.forbidden_actions", "Forbidden actions? (comma-separated)", "e.g. payments, CAPTCHA bypass, install software", "payments, CAPTCHA bypass, install software, system settings, password entry", is_list=True),
    Question("autonomy_boundaries.results_location", "Where should results/reports be saved? (path)", "e.g. ~/IdleCUA/results", "~/IdleCUA/results"),
    Question("autonomy_boundaries.session_duration_minutes", "Max session duration (minutes, 1..45)?", "default 45", "45", is_int=True),
    Question("autonomy_boundaries.daily_action_limit", "Daily action limit (1..1000)?", "default 200", "200", is_int=True),
    Question("autonomy_boundaries.daily_llm_call_limit", "Daily LLM call limit (1..1000)?", "default 150", "150", is_int=True),
    Question("autonomy_boundaries.allowed_hours", "Allowed hours (HH:MM-HH:MM)?", "e.g. 00:00-23:59 or 09:00-18:00", "00:00-23:59"),
]

DEFAULTS_FOR_ASSUMPTIONS: dict[str, str] = {q.key: q.default for q in QUESTIONS}


def _get_nested(obj: dict, dotted: str):
    parts = dotted.split(".")
    cur = obj
    for p in parts:
        cur = cur[p]
    return cur


def _set_nested(obj: dict, dotted: str, value):
    parts = dotted.split(".")
    cur = obj
    for p in parts[:-1]:
        cur = cur[p]
    cur[parts[-1]] = value


def _parse_value(q: Question, raw: str):
    raw = raw.strip()
    if not raw:
        # Empty input -> use default
        if q.is_list:
            if not q.default.strip():
                return []
            return [s.strip() for s in q.default.split(",") if s.strip()]
        if q.is_int:
            try:
                return int(q.default) if q.default.strip() else 0
            except ValueError:
                return 0
        return q.default
    if q.is_list:
        return [s.strip() for s in raw.split(",") if s.strip()]
    if q.is_int:
        try:
            return int(raw)
        except ValueError:
            try:
                return int(q.default)
            except ValueError:
                return 0
    return raw


def run_interview(
    console: Console | None = None,
    input_func: Callable[[Question], str] | None = None,
    confirm_func: Callable[[str], bool] | None = None,
) -> tuple[Profile, dict, dict]:
    """Run scripted questionnaire. Returns (profile, confirmed_facts, assumptions).

    - input_func: if provided, used to get raw answer for each question (for tests / non-interactive).
    - confirm_func: if provided, used to get confirmation bool.
    - console: rich console for output.
    """
    if console is None:
        console = Console()

    profile_dict = Profile().model_dump()
    confirmed_facts: dict[str, object] = {}
    assumptions: dict[str, object] = {}

    console.print("[bold]IdleCUA Onboarding Interview[/bold]")
    console.print("Answer each question. Leave empty to use default (counted as assumption).")
    console.print("No LLM required. Press Ctrl+C to abort.\n")

    for q in QUESTIONS:
        help_suffix = f" [dim]({q.help_text})[/dim]" if q.help_text else ""
        default_hint = f" [dim][default: {q.default}][/dim]" if q.default else " [dim][default: empty][/dim]"
        prompt_text = f"{q.prompt}{help_suffix}{default_hint}"

        if input_func is not None:
            raw = input_func(q)
        else:
            # Do not use Prompt default so we can distinguish hit-enter (assumption) vs typed value (fact)
            raw = Prompt.ask(prompt_text, default="", console=console, show_default=False)

        # Determine if this is a confirmed fact vs assumption
        # Empty input that falls back to default is assumption; non-empty is fact.
        # For int/list, similar logic.
        was_empty = (raw.strip() == "")
        parsed = _parse_value(q, raw)

        # Edge: if user typed exactly the default string, we still count as fact if they typed it,
        # but our raw vs default distinction would treat it as fact (not empty). That's desired.
        if was_empty:
            assumptions[q.key] = parsed
        else:
            # But if parsed is empty list and default is empty, still fact? User explicitly gave empty? Treat as fact if typed empty but meaning "none".
            # We already flagged was_empty as assumption; however for list defaults empty, empty == default assumption is correct.
            # If user typed something that parses to empty due to commas only, treat as fact? Rare.
            confirmed_facts[q.key] = parsed

        _set_nested(profile_dict, q.key, parsed)

    profile = Profile.model_validate(profile_dict)

    # Build summary separating confirmed vs assumptions
    console.print("\n[bold]Interview Summary[/bold]")
    console.print("[bold green]Confirmed facts (you provided):[/bold green]")
    if confirmed_facts:
        for k, v in confirmed_facts.items():
            console.print(f"  - {k}: {v!r}")
    else:
        console.print("  (none)")

    console.print("\n[bold yellow]Assumptions (defaults used where you left blank):[/bold yellow]")
    if assumptions:
        for k, v in assumptions.items():
            console.print(f"  - {k}: {v!r} [dim](default)[/dim]")
    else:
        console.print("  (none)")

    console.print("\n[dim]Profile will be written only after explicit confirmation.[/dim]")
    if confirm_func is not None:
        confirmed = confirm_func("Confirm and save profile?")
    else:
        confirmed = Confirm.ask("Confirm and save profile?", console=console, default=False)

    if confirmed:
        profile.confirmed = True
    else:
        profile.confirmed = False

    return profile, confirmed_facts, assumptions


def save_confirmed_profile(profile: Profile, path: Path, console: Console | None = None) -> bool:
    """Save only if confirmed; returns True if saved."""
    if console is None:
        console = Console()
    if not profile.confirmed:
        console.print("[yellow]Profile NOT saved — you did not confirm. Re-run interview to confirm.[/yellow]")
        return False
    save_profile(profile, path)
    console.print(f"[green]Profile saved to {path}[/green]")
    return True
