# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in ChaChaAgent, please **do not** open a public issue.

Instead, send an email to **tennshi520@gmail.com** with:

- A description of the vulnerability
- Steps to reproduce
- Affected versions (if known)
- Any potential mitigations you've identified

You should receive a response within 48 hours. We will keep you informed of our progress and coordinate a disclosure timeline.

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 3.x     | ✅ Active support  |
| 2.x     | ❌ End of life     |
| 1.x     | ❌ End of life     |

## Security Best Practices

When using ChaChaAgent in production:

- **Never** hardcode API keys or credentials in your project's `chachaConfig.toml`
- Use environment variables for all sensitive values
- Review the [Policy Engine](docs/policy_engine.md) documentation to configure command allowlists and cost limits
- Keep ChaChaAgent and its dependencies updated regularly
