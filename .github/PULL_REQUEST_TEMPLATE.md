<!--
Thanks for contributing to call-report. Please read the contributing guide first:
https://call-report.readthedocs.io/en/latest/get_involved/contributing.html

Title the pull request "[TAG] Imperative summary (#N)", where TAG is one of
ENH, BUG, DOC, TST, MNT, or PLAN, and N is the issue number.

Delete any section that does not apply, but please do not submit an empty template.
-->

#### Reference issues and plan

<!--
Example: Closes #123. Use "Part of #123" when this covers only part of an issue.
If the change has a plan, link it too, for example plans/plan_issue_123.md.
-->

#### What does this change, and why?

<!--
Describe the change and the reason for it. A reviewer should be able to
understand both without reading the diff first.
-->

#### Departures from the plan

<!--
For a pull request that implements a plan, list each place the change differs
from the plan and say why. Write "None." if it follows the plan exactly.
Delete this section for a plan pull request or a change without a plan.
-->

#### New dependencies

<!--
Name any new runtime, optional, or development dependency and say why it is
needed. A new dependency needs a maintainer's approval before it is added.
Write "None." if there are none.
-->

#### AI usage

<!-- Keep the one line that applies and delete the others. -->

- [ ] I did not use AI tools for this pull request.
- [ ] I used AI tools for this pull request. I have reviewed and understood every change, and I describe the tool and how I used it below.
- [ ] An AI agent made this pull request by following the plan workflow in `CLAUDE.md`.

#### Checklist

- [ ] The title follows `[TAG] Imperative summary (#N)`.
- [ ] Tests cover the change, and coverage stays at 100%, branches included.
- [ ] `pre-commit`, `ruff`, `mypy`, and `pytest` pass locally.
- [ ] Docstrings follow the documentation style, and any new public API is listed in `docs/source/api_reference.rst`.
- [ ] The `run-exhaustive` label is added if the change affects what the FCA archive contains or how it is parsed.
