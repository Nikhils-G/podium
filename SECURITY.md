# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 1.0.x | Yes |

## Reporting a vulnerability

Please report privately through GitHub: open the repository's **Security** tab and choose
**Report a vulnerability**. Include the version or commit, the steps to reproduce, and what an
attacker gains. Please don't open a public issue for a vulnerability.

You will get an acknowledgement, a fix or a mitigation plan, and credit in the changelog if you
want it.

## Scope

[`THREAT-MODEL.md`](THREAT-MODEL.md) lists the assets, the threats Podium defends against with
pointers to the code, and the residual risks it accepts by design — read it first; a report that
breaks one of those mitigations is exactly what we want to hear about.

Out of scope: running with the shipped default `PODIUM_SECRET_KEY` or with demo accounts enabled
(the boot log, the README and a banner on every page all say to change both for a real event).
