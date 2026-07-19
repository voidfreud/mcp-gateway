# Project policy

This document is the repository's normative policy for planning, delivery, and
release work. It defines the workflow; implementation details and commands
belong in the contributor and testing documentation. Where another document
conflicts with this policy, this policy governs until that document is
corrected.

## Scope and principles

The gateway is a release-grade, local MCP product. Work must leave the product,
its documentation, its release evidence, and its deferred work easier to
understand than before.

- Use the smallest safe change that solves the accepted problem.
- Keep mechanical enforcement in automation, durable rules here, and
  step-by-step procedures in focused documentation.
- Do not represent an unrun check as passing, or a local/live check as CI.
- Treat support, security, compatibility, and documentation as product work,
  not post-release cleanup.

## Issue-first workflow

Every bug, feature, decision, debt item, supportable investigation, deferred
idea, parked proposal, or follow-up starts as a GitHub Issue before work is
merged. The Issue is the durable record of the problem, context, decision, and
outcome; do not use agent instructions, pull-request comments, or personal
notes as a substitute.

An Issue must state the intended outcome, scope, acceptance criteria, relevant
risks, and any required validation. Link related Issues, pull requests,
decisions, and releases. If a question is discovered while working, either
resolve it in the current Issue's scope or open and link a new Issue before
deferring it.

GitHub's open or closed state is separate from the following lifecycle
vocabulary:

- **Proposed** — an unaccepted decision or requested work item; applies to
  decisions and work, and is the default for a new proposal.
- **Accepted** — a decision has been selected; applies to decisions. Any work
  that implements it remains proposed, blocked, deferred, parked, or completed
  in its own Issue.
- **Superseded** — an accepted decision has been replaced by a linked accepted
  decision; applies to decisions only.
- **Blocked** — a decision or work item cannot progress until a named external
  decision, dependency, or condition is resolved; record the blocker and next
  action.
- **Deferred** — a valid decision or work item is intentionally scheduled
  later; record why and the condition that should cause reconsideration.
- **Parked** — a decision or work item has no current commitment but remains
  worth retaining; record why it is not planned and the reconsideration trigger.
- **Release-blocker** — a decision or work item must be resolved before a named
  release; record the release, blocking condition, owner, and exit criterion.
  It is an additional release status, not a replacement for the item's primary
  lifecycle.

Close an Issue as completed only when its acceptance criteria and required
evidence are met; close it as declined with the reason and any superseding
decision, or as duplicate with a link to the canonical Issue.

Maintainers or triage apply the documented labels: one work type (`bug`,
`feature`, `decision`, `documentation`, `maintenance`, or `security`), one
lifecycle label when applicable (`blocked`, `deferred`, or `parked`), and
optional area, priority, and release labels. Issue forms intentionally do not
preset labels that have not been verified in the repository. Until a label
exists, record its classification in the Issue text; that text remains
authoritative.

## Branches, pull requests, and merge

Make each change on a short-lived branch from current `main`. Name branches
with an Issue reference and a concise subject, for example
`codex/200-governance` or `fix/317-ready-timeout`. Do not develop directly on
`main`.

Open a pull request that links its Issue and plainly states:

- the user or operator outcome;
- notable compatibility, security, or operational effects;
- documentation changed or deliberately not needed;
- validation performed, including any required local/live receipt;
- remaining risks, follow-ups, or explicit non-goals.

Pull requests use a single conventional title. That title is the proposed
release classification:

| Title form | Meaning |
| --- | --- |
| `fix: ...` | Backward-compatible defect correction; patch candidate. |
| `feat: ...` | Backward-compatible capability; minor candidate. |
| `feat!: ...` or a `BREAKING CHANGE:` footer | Intentional incompatible change; major candidate. |
| `docs:`, `test:`, `refactor:`, `build:`, `ci:`, or `chore:` | No release by default, unless the release decision explicitly says otherwise. |

Use the body to explain a breaking migration, not an ambiguous title. One PR
should normally close one primary Issue; split unrelated work rather than hide
it behind a broad title.

Merge through a pull request using squash merge. Immediately before merging,
the branch must be current with `main`, required checks must pass, conflicts
must be resolved, review conversations must be resolved, and the branch must
satisfy the repository's protected-branch rules. The squash commit keeps the
approved conventional title and links the Issue. Do not force-push protected
branches or rewrite published history.

An automation or bot exception is permitted only for a named, narrowly scoped
operation with least-privilege access. Its scope, acting identity, mechanism,
and evidence must be recorded in the linked Issue or pull request. It does not
create a general bypass for review, required checks, or branch protection.

## Validation and evidence

Validation has three distinct tiers. A passing tier does not replace another.

| Tier | Purpose | Required evidence |
| --- | --- | --- |
| Required CI | Reproducible, hermetic checks. The current mandatory surface is the `check` and `mcp-contract` jobs in `.github/workflows/check.yml`; treat both as required even while GitHub branch protection is not yet verified. | Passing `check` and `mcp-contract` CI jobs, plus retained contract artifacts where configured. |
| Advisory CI | Useful signals whose baseline, determinism, or false-positive rate is not yet sufficient to block merges. | Review findings; address, justify, or track material findings in an Issue. |
| Required local/live validation | Behavior involving a real installation, daemon/service manager, configured backends, credentials, client registration, a fresh client session, or other machine-specific integration. | A redacted local receipt linked from the PR or release Issue. |

CI may use disposable fixtures and test credentials. It must not claim to test
the contributor's installed daemon, personal configuration, local secrets,
real external backends, or client-specific state unless an explicitly managed,
reproducible environment proves those conditions.

A local/live receipt records the date, code revision, platform, applicable
validation scenario, commands or procedure, result, and any known limitation.
It must redact tokens, URLs containing secrets, private backend names when
needed, personal paths, and customer or tool output data. A receipt may state
that a scenario is not applicable, but must give the reason. The PR describes
the receipt; no secret-bearing receipt is committed to the repository.

Changes affecting installation, update, uninstall, service lifecycle,
configuration migration, authentication, client registration, backend mounting,
or externally configured integrations require local/live validation before
merge or release. If validation cannot be performed, do not imply equivalence:
record the risk and obtain an explicit release decision in the linked Issue.

## Decisions and architecture records

Use a GitHub Issue for every proposal, trade-off, disagreement, and decision in
progress. Its comments and linked evidence are the complete discussion record.

For a new or updated decision that has a durable architectural, security,
compatibility, or operational constraint, add or update a compact accepted
summary in `docs/decisions/`. Each summary must state its status, deciding
Issue, context, decision, and consequences. It must link a superseding record
when replaced. Do not copy a backlog, a temporary experiment log, or live
machine observations into an accepted decision record. Legacy-record
normalization is separate Issue-driven work; it must not turn decision records
into a backlog or a live diary.

Rejected, deferred, and parked proposals remain Issues unless a short accepted
record needs to mention them as an alternative. Superseded records are retained
with an unambiguous status and forward link; do not silently edit history to
make old decisions appear current.

## Security and disclosure

Do not file suspected vulnerabilities, exposed credentials, or exploit details
in a public Issue, pull request, commit, CI log, or receipt. Report them through
the repository's documented private security-reporting channel. Maintain a
minimal private record containing impact, affected versions, mitigations,
validation, disclosure coordination, and any public follow-up.

Security fixes follow the normal evidence standard, with a narrow exception for
details that would increase risk before a fix is available. Public release notes
describe the impact and upgrade action once disclosure is appropriate. Never
commit secrets, local configuration, private endpoints, access tokens, or
unredacted production output.

## Documentation freshness

Documentation is part of the definition of a user-visible or operator-visible
change. Update the README, relevant `docs/` pages, configuration references,
release notes, and decision summaries when their claims change. Remove or
supersede contradictory instructions promptly.

Repository guidance must describe durable workflow and supported behavior, not
a personal to-do list, a session diary, a current machine inventory, or claims
that cannot be reproduced. Keep the canonical instructions concise and link to
focused documents for detail. Client-specific behavior must be labeled as such;
generic MCP guidance must not imply support by a particular client.

## Generated and local artifacts

Generated outputs, caches, local indexes, environments, logs, temporary
receipts, machine configuration, and secrets are local by default. Track them
only when they are necessary, reproducible, reviewable product inputs or
published release artifacts. Add durable ignore rules for local-only state and
verify that packages and releases contain only intended files.

Generated files that must be tracked identify their generator, regeneration
command or procedure, and review expectations. Never hand-edit a generated
file when the generator is the source of truth. Local tooling may be used by
contributors, but it is not a repository dependency, CI requirement, default
runtime behavior, or release artifact unless explicitly adopted through an
Issue and reviewed change.

## Dependencies, compatibility, and deprecation

Add or upgrade dependencies only for a documented need, with compatible
licensing, maintenance, security, size, and operational impact considered.
Lock or pin inputs where reproducibility requires it; update lockfiles with the
manifest. Dependency upgrades that touch compatibility seams require the
relevant contract checks and any necessary local/live validation.

Public configuration, endpoints, command behavior, output shapes, and supported
client workflows are compatibility surfaces. Preserve them by default. A
deprecation must document the replacement, affected users, migration path,
minimum supported window or removal condition, and tests. Remove a deprecated
surface only in a declared breaking release after its notice period, unless a
security or correctness emergency requires a faster change.

## Emergency bypass

An emergency bypass is exceptional and limited to preventing active harm,
restoring service, or addressing a time-sensitive security incident. The person
using it records, in a GitHub Issue as soon as disclosure permits: the exact
commit or pull request; actor and time; bypass mechanism; approval, if any;
impact; checks that did run; and the corrective Issue or pull request. A
corrective pull request then restores policy compliance, validation,
documentation, and any missing review. Emergency access must be minimal and
must not normalize direct pushes or skipped checks for ordinary work.

## Definition of done

Work is done only when the linked Issue's acceptance criteria are met and:

- the implementation is scoped, reviewed through the pull-request process, and
  merged with the correct conventional classification;
- required CI passes and advisory findings are handled or tracked;
- required local/live validation has a linked, redacted receipt;
- compatibility, security, and migration effects are documented and tested;
- user and contributor documentation is current;
- generated and local-only artifacts are correctly handled;
- decisions, follow-ups, deferrals, and residual risks are recorded as Issues;
- release notes and versioning are prepared when the change is releasable.

## Release policy

Releases use semantic versioning. The target state is reviewed release
automation that derives candidates from conventional pull-request titles:
fixes produce patch releases, features produce minor releases, and declared
breaking changes produce major releases. That automation is separate work and
is not currently implemented.

Until it is implemented, the current tag workflow must not be triggered without
an approved release-acceptance Issue and verified release metadata. The release
Issue records the intended version and confirms that the tag, package version,
lockfile workspace metadata where applicable, changelog entry, and release
artifacts agree. Only then may a maintainer create the `v*` tag that starts the
current workflow. Routine version calculation and metadata updates remain a
deliberate reviewed release task until automation replaces them; do not present
manual release work as automated.

Before publishing, attach the intended distributables and integrity metadata.
A release that changes local installation, daemon behavior, client
registration, or real backend integration also requires the applicable
local/live receipt. Hotfixes follow the same policy, using the emergency
exception only when its criteria are met.

## Reusable project assets

After this workflow has proved stable here, extract only generic, non-product
content into reusable artifacts: a project-policy template, issue and pull
request templates, shared pinned workflow components, and a repository
bootstrap checklist. Keep commands, architecture, compatibility promises,
validation scenarios, and operational risks specific to this gateway in this
repository. Reuse must reduce duplication without hiding repository-specific
responsibilities.
