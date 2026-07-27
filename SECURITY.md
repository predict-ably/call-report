# Security Policy

## Supported versions

`call-report` is pre-1.0 and under active development. Security fixes are applied
to the latest released version only; there is no backporting to older releases at
this time.

## Reporting a vulnerability

Please report security vulnerabilities privately — do **not** open a public
GitHub issue for them.

- Preferred: open a
  [private security advisory](https://github.com/predict-ably/call-report/security/advisories/new)
  on GitHub.
- Alternatively, email the maintainer at **rnkuhns@gmail.com**.

Please include enough detail to reproduce the issue. We aim to acknowledge
reports within a few business days, and will keep you informed as we work on a
fix and coordinate a release.

## Scope

As a data-processing library, `call-report` follows the usual analytics trust
model: data *sources* are assumed to be trusted, but the data itself may be
malformed. We are most interested in issues where parsing untrusted or malformed
call report files could lead to arbitrary code execution or unintended data
exfiltration.
