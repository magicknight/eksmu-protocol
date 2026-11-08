# Contributing to Eksmu

Thank you for helping. Issues, bug reports, reproductions and pull requests are all welcome.

## Before you open a pull request

1. Run the local checks; they use temporary data and need no cloud account:
   ```bash
   python3 -m pip install -r requirements-research.txt -r requirements-analysis.txt
   python3 scripts/check_local.py      # needs Node.js for the three UI suites
   ```
2. Keep the governance invariants in [CHARTER.md](CHARTER.md): publishing never depends on an AI score, a formal-proof result, a referee, a degree, a wallet or a payment.
3. Never commit secrets, real user data, private e-mail addresses or production host details. Test data must be synthetic (use `example.org` and the `198.51.100.0/24` documentation range).
4. Report security problems privately as described in [SECURITY.md](SECURITY.md), not in a public issue.

## Licensing of contributions (inbound = outbound)

Your contribution is licensed under the license of the files it touches (see [NOTICE](NOTICE) and the License section of [README.md](README.md)): AGPL-3.0-or-later for the platform and server, Apache-2.0 for files with an `SPDX-License-Identifier: Apache-2.0` header. New files in `tools/`, `witness/` or the verifier tests must carry that header. There is no separate contributor license agreement, and the maintainer cannot relicense your contribution without your consent.

## Developer Certificate of Origin

Every commit must be signed off, certifying the [Developer Certificate of Origin 1.1](https://developercertificate.org/): that you wrote the change or otherwise have the right to submit it under the license above.

```bash
git commit -s -m "Describe the change"
```

This adds `Signed-off-by: Your Name <you@example.org>` to the commit message. You may use a pseudonym and a GitHub `noreply` address, as long as it is consistent and reachable through your GitHub account.
