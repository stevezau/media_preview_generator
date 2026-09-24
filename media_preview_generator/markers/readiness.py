"""Setup Health rows for Intro & Credits (spec §7 item 6; plan phase 4, Task 9).

Every row is built from the facts the server Edit tab already computed — the capability report behind
``GET /api/markers/servers/<id>/status`` — so the two surfaces can never disagree, and a server with Intro &
Credits switched off is never contacted for them.

Two things ``web/static/js/servers.js`` requires of these rows:

* ``severity`` is never ``"info"``: ``_partitionChecks`` drops an info row on purpose, so an info row would
  simply never render. A row with nothing to fix is ``"recommended"`` with ``ok: True`` and lands in "All good"
  (plan P-R6).
* The Jellyfin and Emby plugin rows live in a section whose ``id`` is ``"plugin"``, whose first check's
  ``current`` is ``"not installed"`` or a version string — the install controls read exactly that.

A fact the capability check never reached is unknown, and an unknown fact emits no row: ``capability()`` stops at
the first problem it finds, so a Plex whose database is on another machine says nothing about Plex Pass.

The one exception is each Plex library's own "Intro markers" / "Credits markers" setting: the Edit tab doesn't show
it, so :func:`marker_facts` reads it itself whenever Plex answered the capability check. While one is off, Plex hides
every skip marker of that type in the library, ours included, so an off setting is a must-fix row.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field, replace
from typing import Any

from loguru import logger

from ..servers.base import ServerConfig, ServerType
from .publishers.base import Capability

SECTION_ID = "markers"
SECTION_TITLE = "Intro & Credits"
DOCS_ANCHOR = "intro-credits"
PLUGIN_SECTION_ID = "plugin"
PLUGIN_DOCS_ANCHOR = "plugin"
CRITICAL = "critical"
RECOMMENDED = "recommended"

# --- The approved copy (evidence/design/phase4/ui-copy.md §7) ------------------------------------------------
OFF_LABEL = "Intro & Credits is off for this server"
OFF_REASON = "Nothing here is checked until you switch it on."
OFF_TOOLTIP = "Turn it on in the Intro & Credits tab to send intro and credits markers to this server."

PASS_LABEL = "Skip buttons need Plex Pass"
PASS_LABEL_OK = "Plex Pass is active"
PASS_REASON = "Nothing is written until the server has Plex Pass."
PASS_TOOLTIP = "Plex only shows Skip Intro and Skip Credits to viewers on a server with Plex Pass."

TAG_ROW_LABEL = "Plex hasn't made its marker list yet"
TAG_ROW_LABEL_OK = "Plex's marker list is ready"
TAG_ROW_REASON = "Turn on Plex's own intro detection for one library and play a file, then check again."
TAG_ROW_TOOLTIP = "Plex builds this list the first time it finds a marker itself. This app never creates it."

LOCAL_DB_LABEL = "Plex's library database isn't on this machine"
LOCAL_DB_LABEL_OK = "Plex's library database is on this machine"
LOCAL_DB_REASON = "Run the Plex marker agent next to Plex, or run this app on the same machine as Plex."
LOCAL_DB_TOOLTIP = (
    "Markers go straight into Plex's database, so this app must run on the Plex machine, or reach it through the agent."
)
# With an agent the check ran on ITS machine, so the row can't say "this machine" — the app is already where it
# belongs, and the advice above would tell the user to move the one thing the agent exists to avoid moving.
LOCAL_DB_LABEL_AGENT = "The Plex marker agent isn't on the machine with Plex's database"
LOCAL_DB_LABEL_AGENT_OK = "The Plex marker agent is on the machine with Plex's database"
LOCAL_DB_REASON_AGENT = (
    "Run the Plex marker agent on the Plex machine, with Plex's config folder mounted from a local disk."
)
LOCAL_DB_CURRENT_AGENT = "the agent's machine"
LOCAL_DB_TOOLTIP_AGENT = (
    "Markers go straight into Plex's database, so the Plex marker agent must run on the Plex machine with that "
    "machine's own copy of Plex's config folder."
)
LOCAL_DB_TODO = (
    "<p><strong>What to do:</strong> run this app on the Plex machine, or run the Plex marker agent next to "
    "Plex. Everything else about this server keeps working either way.</p>"
)
LOCAL_DB_TODO_AGENT = (
    "<p><strong>What to do:</strong> the Plex marker agent already does this write for you, so this app can "
    "stay where it is. Run the agent's container on the Plex machine and mount Plex's config folder into it "
    "from one of that machine's own disks. Everything else about this server keeps working either way.</p>"
)

AGENT_LABEL_OK = "The Plex marker agent is connected"
AGENT_RECOMMENDED = "connected"
AGENT_TOOLTIP = (
    "A Plex on another machine is written by the Plex marker agent's container running beside it. Without the "
    "agent answering, no intro or credits marker reaches this server."
)
# Connection state (``publishers.plex_remote.AGENT_*``) → this row's label, ``current`` and reason. An unknown state
# reads as "can't be reached", the same fallback the Edit tab's badge uses (``markers_server_tab.js AGENT_BADGES``).
AGENT_STATES: dict[str, tuple[str, str, str]] = {
    "unreachable": (
        "The Plex marker agent isn't answering",
        "can't be reached",
        "Markers wait here until it answers again. Nothing is lost.",
    ),
    "rejected": (
        "The Plex marker agent refused this app's key",
        "key refused",
        "Set the same shared key on the agent and in the Intro & Credits tab.",
    ),
    "incompatible": (
        "The Plex marker agent and this app are different versions",
        "version mismatch",
        "The Intro & Credits tab says which of the two to update.",
    ),
}
AGENT_FALLBACK_STATE = "unreachable"
# Connected, and refused anyway: the agent is beside a different Plex than this server (a mistyped address).
AGENT_WRONG_PLEX = (
    "The Plex marker agent is beside a different Plex",
    "wrong Plex server",
    "Check its address in the Intro & Credits tab: markers would have gone into the wrong database.",
)
AGENT_EXPLANATION = (
    "<p><strong>What it checks:</strong> whether the Plex marker agent running beside Plex answers this app.</p>"
    "<p><strong>Why it matters:</strong> Plex has no API for intro and credits markers, so they are written "
    "into its database — which only works from the machine that database is on. When Plex is on another "
    "machine, the agent is the only way in, and while it isn't answering no marker reaches this server at "
    "all.</p>"
    "<p><strong>Nothing is lost:</strong> markers stay in this app. The next Intro &amp; Credits run sends "
    "them as soon as the agent answers again.</p>"
)

# Plex's names for its settings: each library's own (Edit library → Advanced) and the server-wide ones (Settings →
# Library).
LIBRARY_SETTING_NAMES = {"intro": "Intro markers", "credits": "Credits markers"}
SERVER_SETTING_NAMES = {"intro": "Generate intro video markers", "credits": "Generate credits video markers"}
_DETECTING = {"intro": "intros", "credits": "credits"}

# A library whose own "Intro markers" / "Credits markers" setting is off: Plex serves no marker of that type there.
HIDDEN_LABEL = "{library} — skip buttons are hidden"
HIDDEN_LABEL_OK = "Plex shows skip buttons in your Intro & Credits libraries"
HIDDEN_REASON_BOTH = (
    'Plex\'s "Intro markers" and "Credits markers" are off for this library, and Plex then hides every skip marker in '
    "it, ours included."
)
HIDDEN_REASON_ONE = (
    'Plex\'s "{setting}" is off for this library, and Plex then hides every {kind} marker in it, ours included.'
)
HIDDEN_TOOLTIP = (
    "While a library's own Intro markers or Credits markers setting is off, Plex hides every skip marker of that type "
    "in it, ours included."
)
HIDDEN_TURN_ON = "Turn on"
HIDDEN_EXPLANATION = (
    "<p><strong>What it checks:</strong> each Intro &amp; Credits library's own <em>Intro markers</em> and "
    "<em>Credits markers</em> settings in Plex (Edit library → Advanced). Only TV libraries have the intro one.</p>"
    "<p><strong>Why it matters:</strong> while one is off, Plex leaves every marker of that type out of what it serves "
    "for the library — its own and ours — so no viewer sees Skip Intro or Skip Credits there, even though the markers "
    "are in Plex's database.</p>"
    "<p><strong>Turn on:</strong> switches that library's setting back on, only for the types listed.</p>"
    "<p><strong>If you turned it off to stop Plex detecting:</strong> set Plex's server-wide <em>Generate intro video "
    "markers</em> / <em>Generate credits video markers</em> (Plex settings → Library) to Never instead. That stops "
    "Plex's own detection in every library and keeps skip buttons working.</p>"
)
# The note under the last hidden library: the server-wide switch that does what turning a library's setting off was for.
NEVER_NOTE_BOTH = (
    "Don't want Plex detecting intros itself? Set Plex's server-wide \"Generate intro video markers\" and "
    '"Generate credits video markers" to Never instead. That stops Plex\'s detection and keeps skip buttons working.'
)
NEVER_NOTE_ONE = (
    "Don't want Plex detecting {detecting} itself? Set Plex's server-wide \"{setting}\" to Never instead. That stops "
    "Plex's detection and keeps skip buttons working."
)
NEVER_BUTTON = "Set server-wide to Never"
NEVER_TOOLTIP = (
    "Plex settings → Library → Generate intro / credits video markers → Never. Stops Plex's own detection in every "
    "library; every library keeps its skip buttons."
)

DETECTION_LABEL = "Plex's own detection can replace your markers"
DETECTION_LABEL_OK = "Plex's own detection is off"
DETECTION_LABEL_OK_LIBRARIES = "Plex's own detection doesn't reach your Intro & Credits libraries"
DETECTION_LABEL_KEEP = "Keeping Plex's own markers: its detection can stay on"
DETECTION_REASON = "Plex settings → Library → Generate intro and credits video markers → Never."
DETECTION_TOOLTIP = (
    "Plex finds markers itself in a library only when its server setting and that library's own setting are both "
    "on. When it does, it overwrites ours; the next Intro & Credits run puts them back."
)
DETECTION_EXPLANATION = (
    "<p><strong>What it checks:</strong> whether Plex's own intro and credits detection reaches the libraries Intro "
    "&amp; Credits goes to: Plex's server-wide <em>Generate intro video markers</em> / <em>Generate credits video "
    "markers</em> (Plex settings → Library). Only TV libraries have intro detection.</p>"
    "<p><strong>Why it matters:</strong> when Plex analyses a file again it replaces every marker on it with its "
    "own, including ours. Nothing is lost for long — the next Intro &amp; Credits run notices and puts ours back — "
    "but the file shows Plex's times until it does.</p>"
    "<p><strong>Set server-wide to Never:</strong> stops Plex's own detection in every library, and every library "
    "keeps its skip buttons. Don't turn a library's own <em>Intro markers</em> / <em>Credits markers</em> setting "
    "off instead: Plex then hides every skip marker in that library, ours included.</p>"
    "<p><strong>If you'd rather keep Plex's:</strong> choose \"Keep Plex's\" under \"When Plex has its own "
    "markers\" in the Intro &amp; Credits tab. Plex's detection is then welcome, and this row has nothing to warn "
    "about.</p>"
)

PLUGIN_MISSING_REASON = "Without it, nothing can send markers to this server."
JELLYFIN_PLUGIN_TOOLTIP = (
    "Jellyfin takes intro and credits markers only through this plugin. Installing it restarts Jellyfin."
)
EMBY_PLUGIN_LABEL = "Media Preview Bridge for Emby plugin"
EMBY_PLUGIN_TOOLTIP = "Emby takes intro and credits markers only through this plugin."
EMBY_MANUAL_REASON = (
    "This Emby's plugin catalogue doesn't list it. Install it by hand — the guide is in the Intro & Credits tab."
)
# The Edit tab makes no claim about the catalogue it couldn't read (``markers_server_tab.js``), and neither
# does this row: it offers no button either way, but it doesn't say Emby hasn't got the plugin listed.
EMBY_CATALOG_UNREAD_REASON = (
    "Couldn't read this Emby's plugin catalogue. Install it by hand — the guide is in the Intro & Credits tab."
)

OUTDATED_LABEL = "The plugin is too old for intro and credits markers"
OUTDATED_REASON = "Previews still work. Markers wait until it's updated."
OUTDATED_TOOLTIP = (
    "This version of the plugin can't take intro and credits markers. Updating installs the newest version."
)
# No version number is known to recommend: "outdated" means the installed build doesn't answer the markers
# feature, not that it is older than a number this app carries.
NEWEST = "newest"

_UNKNOWN_STATE = "unknown"


@dataclass(frozen=True)
class MarkerFacts:
    """What the Edit tab's capability check knows about one server's Intro & Credits setup.

    Attributes:
        enabled: Whether Intro & Credits is switched on for this server; ``None`` when that couldn't be read
            (then no Intro & Credits row is emitted at all).
        state: The :class:`~media_preview_generator.markers.publishers.base.Capability` value the check
            returned, or ``"unknown"``.
        details: That report's ``details`` — the facts the rows below are read from.
        keep_plex: Whether this Plex server is set to "Keep Plex's" (``on_plex_redetect``).
        libraries: ``(id, name)`` of each library in this server's Intro & Credits selection, in order.
        library_detection: Each library's own "Intro markers" / "Credits markers" settings, as
            :meth:`~media_preview_generator.servers.plex.PlexServer.get_library_marker_detection` returns them;
            None when they weren't read or couldn't be.
    """

    enabled: bool | None
    state: str = _UNKNOWN_STATE
    details: dict[str, Any] = field(default_factory=dict)
    keep_plex: bool = False
    libraries: tuple[tuple[str, str], ...] = ()
    library_detection: dict[str, dict[str, Any]] | None = None

    @property
    def on(self) -> bool:
        """Whether Intro & Credits is switched on for this server."""
        return self.enabled is True

    @property
    def plex_pass(self) -> bool | None:
        """Whether this Plex server has Plex Pass; None when it couldn't be read."""
        value = self.details.get("plex_pass")
        return value if isinstance(value, bool) else None

    @property
    def agent(self) -> dict[str, Any] | None:
        """The Plex marker agent's block from the capability details; None when no agent did the check.

        Its presence is what switches this server's Plex rows to the agent's wording, exactly as it does on the
        Edit tab (``markers_server_tab.js agentDetails``).
        """
        value = self.details.get("agent")
        return value if isinstance(value, dict) else None

    @property
    def agent_state(self) -> str | None:
        """The agent's connection state, or None when no agent did the check.

        ``""`` for an agent block that doesn't say — read as :data:`AGENT_FALLBACK_STATE` where it is used.
        """
        agent = self.agent
        return None if agent is None else str(agent.get("state") or "")

    @property
    def agent_refused(self) -> bool:
        """Whether the capability check stopped at the agent, so nothing past it was read at all."""
        return self.state == Capability.AGENT_UNAVAILABLE.value

    @property
    def db_on_this_machine(self) -> bool | None:
        """Whether Plex's database is the file this app can write; None when the check never got that far.

        ``lock_holder`` True is Plex holding this very file open, which is the only proof of "same machine"
        there is; ``needs_local_db`` is the refusal, whether for a network share or for a second copy of the
        file under a different path.
        """
        if self.state == Capability.NEEDS_LOCAL_DB.value:
            return False
        return True if self.details.get("lock_holder") is True else None

    @property
    def marker_list_present(self) -> bool | None:
        """Whether Plex's ``tag_type=12`` marker row exists; None when the check never got that far.

        The tag row is the last thing ``capability()`` reads, so only a READY server proves it is there.
        """
        if self.state == Capability.NEEDS_PLEX_DETECTION_ONCE.value:
            return False
        return True if self.state == Capability.READY.value else None

    @property
    def plex_detection_on(self) -> bool | None:
        """Whether Plex's own intro/credits detection is on; None when the prefs couldn't be read.

        Same rule as the Edit tab's ``plexDetectionOn`` (``markers_server_tab.js``): any pref that answered
        something other than ``never`` counts as on.
        """
        detection = self.details.get("detection")
        if not isinstance(detection, dict):
            return None
        values = [detection.get("intro"), detection.get("credits")]
        if all(value is None for value in values):
            return None
        return any(value is not None and value != "never" for value in values)

    @property
    def plex_detects(self) -> tuple[str, ...]:
        """The types Plex's server-wide prefs let it detect: answered, and not ``never``."""
        detection = self.details.get("detection")
        if not isinstance(detection, dict):
            return ()
        return tuple(kind for kind in ("intro", "credits") if detection.get(kind) not in (None, "never"))

    def detected_types(self) -> tuple[str, ...] | None:
        """The types Plex's own detection reaches in the Intro & Credits libraries.

        A type counts where the server-wide pref lets Plex detect it and a selected library has it (intro only in TV
        libraries), whatever that library's own setting says: an off one is a must-fix row
        (:func:`library_marker_checks`), and Plex detects there again once it is turned back on.

        Returns:
            Those types, in :attr:`plex_detects` order; None when the libraries' kinds couldn't be read.
        """
        types = self.plex_detects
        if not types or not self.libraries:
            return ()
        if self.library_detection is None:
            return None
        kinds = {
            entry.get("type")
            for library_id, _name in self.libraries
            if (entry := self.library_detection.get(library_id)) is not None
        }
        reachable = {"intro": "show" in kinds, "credits": bool(kinds)}
        return tuple(kind for kind in types if reachable[kind])

    @property
    def plugin_installed(self) -> bool | None:
        """Whether the Bridge plugin is installed; None when the check couldn't tell."""
        if self.state == Capability.NEEDS_PLUGIN.value:
            return False
        if self.state in (Capability.PLUGIN_OUTDATED.value, Capability.READY.value):
            return True
        return None

    @property
    def plugin_takes_markers(self) -> bool | None:
        """Whether the installed plugin answers the markers feature; None when the check couldn't tell."""
        if self.state == Capability.PLUGIN_OUTDATED.value:
            return False
        return True if self.state == Capability.READY.value else None

    @property
    def plugin_version(self) -> str:
        """The installed plugin's version, or ``""`` when it isn't known."""
        return str(self.details.get("plugin_version") or "")

    @property
    def catalog_listed(self) -> bool | None:
        """Whether Emby's own plugin catalogue offers the plugin; None when the catalogue couldn't be read."""
        value = self.details.get("catalog_listed")
        return value if isinstance(value, bool) else None


def marker_facts(server: Any, config: ServerConfig | None) -> MarkerFacts:
    """This server's Intro & Credits facts, from the payload the Edit tab already asks for.

    Nothing is contacted when the feature is off for this server: the switch is read from the stored settings
    first, so a user who never turned Intro & Credits on pays no probe for these rows. The one exception is
    Plex's per-library "Intro markers" / "Credits markers" settings, read fresh below (never a second probe
    otherwise) so the rows reflect a setting flipped after the Edit tab last asked.

    Args:
        server: Live client for ``config``.
        config: The server's config; ``None`` for a client built from the legacy single-server Config.

    Returns:
        The facts. ``enabled`` is ``None`` when the settings or the status payload couldn't be read — then no
        Intro & Credits row is emitted rather than a guessed one.
    """
    if config is None:
        return MarkerFacts(enabled=None)
    # ``load_server`` is the one reader of the stored block everywhere else too, so a block it refuses (a Plex
    # switched on without the database-write confirmation) reads as off here exactly as it does to the job
    # that would publish. The Edit tab is where the refusal itself is explained.
    from .settings import load_server

    try:
        settings = load_server(config.markers, config.type.value)
    except Exception as exc:
        logger.debug("Intro & Credits settings for {} unreadable: {}", config.name, type(exc).__name__)
        return MarkerFacts(enabled=None)
    if not settings.enabled:
        return MarkerFacts(enabled=False)

    from .inspect import server_status_payload

    try:
        payload = server_status_payload(server, config)
    except Exception as exc:
        logger.debug("Intro & Credits status for {} failed: {}", config.name, type(exc).__name__)
        return MarkerFacts(enabled=None)
    capability = payload.get("capability") or {}
    details = capability.get("details")
    facts = MarkerFacts(
        enabled=bool(payload.get("enabled")),
        state=str(capability.get("state") or _UNKNOWN_STATE),
        details=dict(details) if isinstance(details, dict) else {},
    )
    if config.type is not ServerType.PLEX:
        return facts
    from .ownership import marker_libraries

    facts = replace(
        facts,
        keep_plex=settings.on_plex_redetect == "keep_plex",
        libraries=tuple((str(lib.id), str(lib.name or lib.id)) for lib in marker_libraries(config)),
    )
    # The one fact the capability check doesn't carry, read fresh so the rows change as soon as a library's setting
    # is turned back on. Whatever Plex's detection does: an off setting hides "Keep Plex's" own markers just the same.
    # Only once Plex answered the check (Plex Pass or its detection prefs): an unreachable Plex isn't asked again.
    plex_answered = facts.plex_pass is not None or facts.plex_detection_on is not None
    if not facts.libraries or not plex_answered:
        return facts
    try:
        library_detection = server.get_library_marker_detection([library_id for library_id, _ in facts.libraries])
    except Exception as exc:
        logger.debug("Plex library detection prefs for {} failed: {}", config.name, type(exc).__name__)
        library_detection = None
    return replace(facts, library_detection=library_detection if isinstance(library_detection, dict) else None)


def _row(
    check_id: str,
    label: str,
    *,
    severity: str,
    ok: bool,
    tooltip: str,
    explanation: str,
    current: Any = None,
    recommended: Any = None,
    reason: str | None = None,
    actions: dict[str, Any] | None = None,
    fix_action: str | None = None,
    fix_label: str | None = None,
    fix_where: str | None = None,
    bulk: bool = True,
    docs_anchor: str = DOCS_ANCHOR,
) -> dict[str, Any]:
    """One check in the envelope ``MediaServer.previews_readiness`` documents.

    ``fix_label`` names the fix button when "Apply recommended" wouldn't say what it does. ``bulk=False`` keeps the
    fix out of "Fix critical" / "Fix all": it is applied only from its own button, after its own confirmation.
    """
    check: dict[str, Any] = {
        "id": check_id,
        "label": label,
        "docs_anchor": docs_anchor,
        "tooltip": tooltip,
        "explanation": explanation,
        "ok": ok,
        "severity": severity,
        "current": current,
        "recommended": recommended,
        "actions": actions or {},
        "reason": reason,
        "meta": {},
    }
    if fix_action is not None:
        check["fix_action"] = fix_action
    if fix_label is not None:
        check["fix_label"] = fix_label
    if fix_where is not None:
        check["fix_where"] = fix_where
    if not bulk:
        check["bulk"] = False
    return check


def _section(checks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Wrap Intro & Credits rows in their section; None when there is nothing to say."""
    if not checks:
        return None
    failing_critical = any(c["ok"] is False and c["severity"] == CRITICAL for c in checks)
    return {
        "id": SECTION_ID,
        "title": SECTION_TITLE,
        "docs_anchor": DOCS_ANCHOR,
        "ok": all(c["ok"] for c in checks),
        "severity": CRITICAL if failing_critical else RECOMMENDED,
        "checks": checks,
    }


def off_section() -> dict[str, Any]:
    """The one row a server with Intro & Credits switched off gets (plan P-R6).

    Emitted as ``recommended`` + ``ok: True`` so it lands in "All good": an ``info`` row is dropped by
    ``servers.js _partitionChecks`` and would never render. No ``current``/``recommended`` pair — the label
    already says the state.
    """
    row = _row(
        "markers_off",
        OFF_LABEL,
        severity=RECOMMENDED,
        ok=True,
        tooltip=OFF_TOOLTIP,
        explanation=(
            "<p><strong>What this means:</strong> this server isn't being sent intro and credits markers, so "
            "none of the Intro &amp; Credits checks apply to it.</p>"
            "<p><strong>Turn it on:</strong> the Intro &amp; Credits tab of this dialog. The checks for this "
            "server — Plex Pass, the plugin, where Plex's database lives — appear here as soon as you do.</p>"
        ),
        reason=OFF_REASON,
    )
    return {
        "id": SECTION_ID,
        "title": SECTION_TITLE,
        "docs_anchor": DOCS_ANCHOR,
        "ok": True,
        "severity": RECOMMENDED,
        "checks": [row],
    }


def agent_check(facts: MarkerFacts) -> dict[str, Any] | None:
    """The Plex marker agent's row, or None when no agent is configured for this server.

    The agent is the whole write path for a Plex on another machine, and a refusal by it (``AGENT_UNAVAILABLE``)
    stops the capability check before Plex Pass, the marker list and the database are read. Without this row those
    unknown facts emit nothing, so a Plex writing no markers at all would show either no Intro & Credits section
    or — when Plex itself still answers over HTTP — a lone green "Plex Pass is active".

    Args:
        facts: This server's facts, from :func:`marker_facts`.

    Returns:
        The row, or None when no agent answered this check — this server has none, or the check stopped before
        it (Intro & Credits off, or the database write not confirmed yet).
    """
    if facts.agent is None and not facts.agent_refused:
        return None
    agent = facts.agent or {}
    state = facts.agent_state or AGENT_FALLBACK_STATE
    # Connected AND not refused: an agent that answered is still refused when it turns out to be beside a
    # different Plex than this server, and that must not read as a passing row.
    ok = state == AGENT_RECOMMENDED and not facts.agent_refused
    if ok:
        label, current, reason = AGENT_LABEL_OK, AGENT_RECOMMENDED, None
    elif agent.get("wrong_plex"):
        # Only ``_another_plexs_agent`` sets this. Inferring it from "connected yet refused" would also catch an
        # agent whose answer simply couldn't be decoded, and send the user to change a correct address.
        label, current, reason = AGENT_WRONG_PLEX
    else:
        label, current, reason = AGENT_STATES.get(state, AGENT_STATES[AGENT_FALLBACK_STATE])
    return _row(
        "markers_plex_agent",
        label,
        severity=CRITICAL,
        ok=ok,
        tooltip=AGENT_TOOLTIP,
        explanation=AGENT_EXPLANATION,
        current=current,
        recommended=AGENT_RECOMMENDED,
        reason=reason,
        fix_where="agent",
    )


def plex_section(facts: MarkerFacts) -> dict[str, Any] | None:
    """The Intro & Credits rows for a Plex server, or None when nothing about markers is known.

    Args:
        facts: This server's facts, from :func:`marker_facts`.

    Returns:
        The section to append to Plex's readiness envelope; None when Intro & Credits is off (use
        :func:`off_section`), or when the capability check couldn't establish a single fact.
    """
    if not facts.on:
        return None
    checks: list[dict[str, Any]] = []

    agent_row = agent_check(facts)
    if agent_row is not None:
        checks.append(agent_row)

    has_pass = facts.plex_pass
    if has_pass is not None:
        checks.append(
            _row(
                "markers_plex_pass",
                PASS_LABEL_OK if has_pass else PASS_LABEL,
                severity=CRITICAL,
                ok=has_pass,
                tooltip=PASS_TOOLTIP,
                explanation=(
                    "<p><strong>What it checks:</strong> whether this Plex server has an active Plex Pass.</p>"
                    "<p><strong>Why it matters:</strong> a server without Plex Pass serves no intro or credits "
                    "markers at all — not ours, not its own — so no viewer ever sees a Skip Intro or Skip "
                    "Credits button. This app writes nothing to it until the server has a Pass.</p>"
                    "<p><strong>Viewers need one too:</strong> a Plex Pass on the server isn't enough on its "
                    "own — the account watching needs Plex Pass or to be in your Plex Home.</p>"
                ),
                current="active" if has_pass else "not active",
                recommended="active",
                reason=None if has_pass else PASS_REASON,
            )
        )

    tag_row = facts.marker_list_present
    if tag_row is not None:
        checks.append(
            _row(
                "markers_plex_tag_row",
                TAG_ROW_LABEL_OK if tag_row else TAG_ROW_LABEL,
                severity=CRITICAL,
                ok=tag_row,
                tooltip=TAG_ROW_TOOLTIP,
                explanation=(
                    "<p><strong>What it checks:</strong> whether Plex's database already has the one row every "
                    "intro and credits marker hangs off.</p>"
                    "<p><strong>Why this app won't create it:</strong> Plex only serves markers attached to the "
                    "row it made itself; a row created by anything else is ignored until Plex restarts, and "
                    "Plex then makes its own anyway.</p>"
                    "<p><strong>How to get one:</strong> let Plex find one marker by itself — turn its own "
                    "intro detection on for a library, play a file, and check again. Afterwards you can set "
                    "Plex's server-wide detection back to Never; leave the library's own setting on, or Plex "
                    "hides every skip marker in it.</p>"
                ),
                current="present" if tag_row else "missing",
                recommended="present",
                reason=None if tag_row else TAG_ROW_REASON,
            )
        )

    local_db = facts.db_on_this_machine
    if local_db is not None:
        if facts.agent is not None:
            label = LOCAL_DB_LABEL_AGENT_OK if local_db else LOCAL_DB_LABEL_AGENT
            here, reason, what_to_do = LOCAL_DB_CURRENT_AGENT, LOCAL_DB_REASON_AGENT, LOCAL_DB_TODO_AGENT
            tooltip = LOCAL_DB_TOOLTIP_AGENT
        else:
            label = LOCAL_DB_LABEL_OK if local_db else LOCAL_DB_LABEL
            here, reason, what_to_do = "this machine", LOCAL_DB_REASON, LOCAL_DB_TODO
            tooltip = LOCAL_DB_TOOLTIP
        checks.append(
            _row(
                "markers_plex_db_local",
                label,
                severity=CRITICAL,
                ok=local_db,
                tooltip=tooltip,
                explanation=(
                    "<p><strong>What it checks:</strong> that the Plex library database being written is the "
                    "same file the running Plex has open.</p>"
                    "<p><strong>Why it matters:</strong> Plex has no API for writing intro and credits markers, "
                    "so they go into the database itself. SQLite only allows that from the machine the file is "
                    "on — over a network share the write would corrupt it, so it is refused and Plex is left "
                    "read-only.</p>"
                    f"{what_to_do}"
                ),
                current=here if local_db else "another machine",
                recommended=here,
                reason=None if local_db else reason,
                fix_where="agent" if facts.agent is not None else None,
            )
        )

    checks.extend(library_marker_checks(facts))

    detection_row = detection_check(facts)
    if detection_row is not None:
        checks.append(detection_row)

    return _section(checks)


def _setting_names_html(names: dict[str, str], types: tuple[str, ...]) -> str:
    return " and ".join(f"<em>{names[kind]}</em>" for kind in types)


def _turn_on_action(library_name: str, library_id: str, types: tuple[str, ...]) -> dict[str, Any]:
    """The per-library Turn on action: that library's own "Intro markers" / "Credits markers" for ``types``."""
    from ..servers.plex import LIBRARY_MARKER_DETECTION_PREFS

    settings = _setting_names_html(LIBRARY_SETTING_NAMES, types)
    return {
        "action": "turn_on_plex_library_markers",
        "args": {"library_id": library_id, "prefs": [LIBRARY_MARKER_DETECTION_PREFS[kind] for kind in types]},
        "confirm": {
            "kind": "button",
            "phrase": "",
            # The confirm modal renders this as HTML, so the library's name is escaped.
            "body": (
                f"Turns Plex's {settings} back on in {html.escape(library_name)} only (Edit library → Advanced), so "
                "Plex serves the skip markers there again, ours included."
            ),
        },
    }


def _never_action(types: tuple[str, ...]) -> dict[str, Any]:
    """Set Plex's server-wide detection of ``types`` to Never: its detection stops, and nothing is hidden."""
    what = " and ".join(_DETECTING[kind] for kind in types)
    settings = _setting_names_html(SERVER_SETTING_NAMES, types)
    return {
        "action": "set_plex_detection_never",
        "args": {"types": list(types)},
        "confirm": {
            "kind": "button",
            "phrase": "",
            "body": (
                f"Sets Plex's server-wide {settings} to Never (Plex settings → Library). Plex stops detecting {what} "
                "itself in every library, and every library keeps its skip buttons, ours included."
            ),
        },
    }


def _hidden_reason(types: tuple[str, ...]) -> str:
    if len(types) > 1:
        return HIDDEN_REASON_BOTH
    return HIDDEN_REASON_ONE.format(setting=LIBRARY_SETTING_NAMES[types[0]], kind=types[0])


def _never_note(types: tuple[str, ...]) -> dict[str, Any]:
    if len(types) > 1:
        text = NEVER_NOTE_BOTH
    else:
        text = NEVER_NOTE_ONE.format(detecting=_DETECTING[types[0]], setting=SERVER_SETTING_NAMES[types[0]])
    return {"text": text, "tooltip": NEVER_TOOLTIP, "button": NEVER_BUTTON, "action": _never_action(types)}


def library_marker_checks(facts: MarkerFacts) -> list[dict[str, Any]]:
    """One must-fix row per Intro & Credits library with its own "Intro markers" or "Credits markers" setting off.

    Plex serves no marker of that type in such a library, its own or ours, so no skip button shows there: the fix is
    to turn the setting back on, never off. The last row carries a note offering Plex's server-wide Never for the
    types in scope — what a user turning a library's setting off wanted, without hiding anything — unless Plex
    already never detects them or the server keeps Plex's own markers.

    Args:
        facts: This server's facts, from :func:`marker_facts`.

    Returns:
        The rows; one passing row when every selected library's settings are known and on; none when a setting
        couldn't be read (an unknown fact emits no row).
    """
    if facts.library_detection is None:
        return []
    hidden: list[tuple[str, str, tuple[str, ...]]] = []
    seen = False
    all_known = True
    for library_id, name in facts.libraries:
        entry = facts.library_detection.get(library_id)
        if entry is None:
            continue  # not a movie or TV library: nothing to show
        seen = True
        kinds = ("intro", "credits") if entry.get("type") == "show" else ("credits",)
        off = tuple(kind for kind in kinds if entry.get(kind) is False)
        if off:
            hidden.append((library_id, name, off))
        elif any(entry.get(kind) is None for kind in kinds):
            all_known = False
    if not hidden:
        if not seen or not all_known:
            return []
        return [
            _row(
                "markers_plex_library_markers",
                HIDDEN_LABEL_OK,
                severity=CRITICAL,
                ok=True,
                tooltip=HIDDEN_TOOLTIP,
                explanation=HIDDEN_EXPLANATION,
                current="on",
                recommended="on",
            )
        ]
    rows = [
        _row(
            f"markers_plex_library_markers_{library_id}",
            HIDDEN_LABEL.format(library=name),
            severity=CRITICAL,
            ok=False,
            tooltip=HIDDEN_TOOLTIP,
            explanation=HIDDEN_EXPLANATION,
            current="off",
            recommended="on",
            reason=_hidden_reason(off),
            actions={"enable": _turn_on_action(name, library_id, off)},
            fix_action="enable",
            fix_label=HIDDEN_TURN_ON,
        )
        for library_id, name, off in hidden
    ]
    off_anywhere = {kind for _id, _name, off in hidden for kind in off}
    never_types = () if facts.keep_plex else tuple(kind for kind in facts.plex_detects if kind in off_anywhere)
    if never_types:
        rows[-1]["note"] = _never_note(never_types)
    return rows


def detection_check(facts: MarkerFacts) -> dict[str, Any] | None:
    """The "Plex's own detection" row, or None when Plex's server-wide detection prefs couldn't be read.

    Under "Use ours" Plex's detection overwrites our markers wherever it reaches, and the fix is its server-wide
    Never: turning a library's own setting off instead would hide every skip marker in that library. Under "Keep
    Plex's" Plex's detection is what the user asked for, so there is nothing to warn about.

    Args:
        facts: This server's facts, from :func:`marker_facts`.

    Returns:
        The row, or None when nothing about Plex's detection is known.
    """
    detection_on = facts.plex_detection_on
    if detection_on is None:
        return None

    def row(label: str, *, ok: bool, current: Any = None, recommended: Any = None, **fix: Any) -> dict[str, Any]:
        return _row(
            "markers_plex_detection",
            label,
            severity=RECOMMENDED,
            ok=ok,
            tooltip=DETECTION_TOOLTIP,
            explanation=DETECTION_EXPLANATION,
            current=current,
            recommended=recommended,
            **fix,
        )

    if facts.keep_plex:
        # No value pair: the label says the state, and it holds whether Plex's detection is on or off.
        return row(DETECTION_LABEL_KEEP, ok=True)
    if not detection_on:
        return row(DETECTION_LABEL_OK, ok=True, current="Off", recommended="Off")
    types = facts.detected_types()
    if types == ():
        return row(DETECTION_LABEL_OK_LIBRARIES, ok=True)
    # Libraries whose kind couldn't be read: every type Plex detects server-wide may reach them.
    return row(
        DETECTION_LABEL,
        ok=False,
        current="On",
        recommended="Never",
        reason=DETECTION_REASON,
        actions={"disable": _never_action(types or facts.plex_detects)},
        fix_action="disable",
        fix_label=NEVER_BUTTON,
        # Never changes Plex's detection in every library, not only the ones Intro & Credits goes to.
        bulk=False,
    )


def _install_action(vendor: str, *, update: bool) -> dict[str, Any]:
    """The install (or update) action for a Bridge plugin row.

    An update runs the same endpoint but is named apart: the card waits for an install to make the plugin
    appear, and on an update the plugin is there from the first poll, so it would report success while the
    server is still restarting. ``update_plugin`` tells the card to wait for the row itself to clear instead.
    """
    server = "Jellyfin" if vendor == "jellyfin" else "Emby"
    what = "Updates" if update else "Installs"
    catalogue = "our plugin repository" if vendor == "jellyfin" else f"{server}'s plugin catalogue"
    return {
        "action": "update_plugin" if update else "install_plugin",
        "args": {},
        "confirm": {
            "kind": "button",
            "phrase": "",
            "body": (
                f"{what} the Media Preview Bridge plugin from {catalogue} and restarts {server} "
                f"(~30 seconds). It is the only way intro and credits markers can reach {server}. "
                "Previews are unaffected either way."
            ),
        },
    }


def outdated_plugin_check(facts: MarkerFacts, *, vendor: str, offer_update: bool) -> dict[str, Any] | None:
    """The "plugin too old" row, or None when the installed plugin does take markers (or isn't known to).

    Args:
        facts: This server's facts, from :func:`marker_facts`.
        vendor: ``"jellyfin"`` or ``"emby"`` — names the server in the confirmation copy.
        offer_update: Whether an update can be started from here (Emby: only when its catalogue lists the
            plugin). False leaves the row with no button, so it carries the shipped
            ``Change in <vendor> UI`` badge.

    Returns:
        The check, or None when there is nothing to say.
    """
    if not facts.on or facts.plugin_takes_markers is not False:
        return None
    actions = {"enable": _install_action(vendor, update=True)} if offer_update else {}
    return _row(
        "markers_plugin_outdated",
        OUTDATED_LABEL,
        severity=RECOMMENDED,
        ok=False,
        tooltip=OUTDATED_TOOLTIP,
        explanation=(
            "<p><strong>What it checks:</strong> whether the installed Media Preview Bridge build answers the "
            "markers feature.</p>"
            "<p><strong>Why it matters:</strong> intro and credits markers are newer than this build. Preview "
            "thumbnails keep working exactly as they do now; markers this app decides simply wait until the "
            "plugin can take them.</p>"
            "<p><strong>What to do:</strong> update the plugin to the newest build. Nothing already published "
            "is lost.</p>"
        ),
        current=facts.plugin_version or _UNKNOWN_STATE,
        recommended=NEWEST,
        reason=OUTDATED_REASON,
        actions=actions,
        # Pinned rather than inferred: ``servers.js`` would read the string "newest" as truthy and pick
        # ``enable`` by luck, not by contract.
        fix_action="enable" if offer_update else None,
        docs_anchor=PLUGIN_DOCS_ANCHOR,
    )


def _missing_plugin_reason(catalog_listed: bool | None) -> str:
    """Why Emby hasn't got the plugin, told apart from a catalogue this app couldn't read."""
    if catalog_listed is True:
        return PLUGIN_MISSING_REASON
    return EMBY_MANUAL_REASON if catalog_listed is False else EMBY_CATALOG_UNREAD_REASON


def emby_plugin_section(facts: MarkerFacts, *, catalog_listed: bool | None = None) -> dict[str, Any] | None:
    """Emby's plugin rows: installed (critical) and, when it is too old for markers, outdated (recommended).

    The section id is ``plugin`` and the first check's ``current`` is ``"not installed"`` or a version — the
    Setup Health install controls read exactly that (``servers.js`` ``_deriveBadgeState``,
    ``_pluginInstalledFromEnvelope``).

    Args:
        facts: This server's facts, from :func:`marker_facts`.
        catalog_listed: Whether Emby's catalogue lists the plugin, when the caller knows it and the capability
            report doesn't (it only carries the fact while the plugin is missing).

    Returns:
        The section, or None when Intro & Credits is off or the plugin's state isn't known.
    """
    if not facts.on:
        return None
    installed = facts.plugin_installed
    if installed is None:
        return None
    listed = facts.catalog_listed if facts.catalog_listed is not None else catalog_listed
    # Same rule as the Edit tab: an Install button only when Emby's catalogue is known to list the plugin,
    # otherwise the row says to install it by hand (``markers_server_tab.js``).
    can_install = listed is True
    checks = [
        _row(
            "markers_plugin_installed",
            EMBY_PLUGIN_LABEL,
            severity=CRITICAL,
            ok=installed,
            tooltip=EMBY_PLUGIN_TOOLTIP,
            explanation=(
                "<p><strong>What it is:</strong> Media Preview Bridge for Emby, the small plugin this app "
                "publishes. Emby has no API for writing intro and credits markers, so the plugin is the only "
                "way they can reach it.</p>"
                "<p><strong>Without it:</strong> preview thumbnails still work — they don't go through the "
                "plugin. Only intro and credits markers wait.</p>"
                "<p><strong>Installing:</strong> from Emby's own plugin catalogue when it lists the plugin; "
                "otherwise the Intro &amp; Credits tab links the guide for copying the file in by hand. Either "
                "way Emby restarts once.</p>"
            ),
            current=(facts.plugin_version or "installed") if installed else "not installed",
            recommended="installed",
            reason=None if installed else _missing_plugin_reason(listed),
            actions={"enable": _install_action("emby", update=False)} if (can_install and not installed) else {},
            fix_action="enable" if (can_install and not installed) else None,
            docs_anchor=PLUGIN_DOCS_ANCHOR,
        )
    ]
    outdated = outdated_plugin_check(facts, vendor="emby", offer_update=can_install)
    if outdated is not None:
        checks.append(outdated)
    failing_critical = any(c["ok"] is False and c["severity"] == CRITICAL for c in checks)
    return {
        "id": PLUGIN_SECTION_ID,
        # The subheading the approved pack draws over these rows. Not the plugin's name: that is the first
        # row's label, and the card would print it twice.
        "title": "Plugin",
        "docs_anchor": PLUGIN_DOCS_ANCHOR,
        "ok": all(c["ok"] for c in checks),
        "severity": CRITICAL if failing_critical else RECOMMENDED,
        "checks": checks,
    }
