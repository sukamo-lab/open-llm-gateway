# Security policy

## Supported versions

Until the first stable release, security fixes are provided for the latest published release only. Maintainers may ask reporters to verify a fix against the default branch.

## Reporting a vulnerability

Do not open a public issue, discussion, or pull request for a suspected vulnerability. Use GitHub's private vulnerability reporting for this repository:

https://github.com/sukamo-lab/open-llm-gateway/security/advisories/new

Include affected versions, impact, prerequisites, reproduction steps, and any suggested mitigation. Remove credentials, bearer tokens, customer data, prompts, and provider responses from the report. If private reporting is unavailable, open a public issue containing no vulnerability details and ask a maintainer to establish a private channel.

Maintainers will acknowledge reports when capacity permits, validate severity, coordinate remediation, and publish an advisory when appropriate. The project does not promise a response or remediation SLA. Please allow a reasonable remediation period before public disclosure.

## Scope

Reports about authentication or authorization bypass, cross-tenant access, credential exposure, injection into privileged control paths, audit or encryption failures, unsafe default deployment behavior, and dependency vulnerabilities with a demonstrated impact are in scope.

Provider outages, provider model behavior, unsupported deployment configurations, social engineering, denial of service requiring unrealistic resources, and vulnerabilities already fixed in the latest release are generally out of scope unless they demonstrate a gateway-specific security defect.
