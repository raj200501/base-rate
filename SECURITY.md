# Security

## Reporting a vulnerability

Please report security problems privately to the repository owner through
GitHub's private vulnerability reporting for this repository. Do not open a
public issue for a security problem.

## Scope

base-rate measures monitors of AI agents. It uses mock tools, disposable
sandboxes, and abstract policy violations only (for example a mock
`customer.export_all` call). It contains no real credentials, no real
systems, and no attack payloads, and it does not research ways for agents to
evade monitors. If you find content in this repository that looks like a real
credential or a working exploit, treat it as a security problem and report it.

## Secrets

Keys and tokens live only in environment variables. `make secrets-scan`
checks tracked files and git history and runs in CI and in `make ship-check`.
