# RHOAI STRAT Security NFR Checklist

Recurring security requirements extracted from 422 STRAT security reviews across 4 pipeline runs. These are non-functional requirements that repeat across most reviews and should be addressed by STRAT authors as standard practice, rather than rediscovered per-STRAT by the security review skill.

Source data: James Tanner's test harness (experiments/rfe-strat-tester), Runs 1-4, covering all approved RFEs in RHAIRFE. Cross-referenced against the 39-pattern risk catalog and RHOAI organizational constraints.

## How to use this checklist

STRAT authors: review before submitting. If your STRAT touches a category, address the items in that category. "Not applicable" is a valid answer. The goal is to show you considered it, not to force-fit requirements.

Security reviewers: use this as a baseline. If a STRAT addresses all applicable items from this checklist, focus review time on STRAT-specific architectural risks rather than rediscovering generic NFRs.

---

## 1. Authentication & Authorization

Applies when: the STRAT introduces new endpoints, services, APIs, or modifies access control.

- [ ] Auth mechanism specified for every new endpoint or service
- [ ] Uses an approved RHOAI auth pattern, not custom auth:
  - kube-auth-proxy via ext_authz at the Gateway API layer (platform ingress)
  - kube-rbac-proxy sidecar for per-service Kubernetes RBAC (SubjectAccessReview)
  - Kuadrant (Authorino + Limitador) AuthPolicy/TokenRateLimitPolicy (API-level)
- [ ] RBAC scoped to namespace (not cluster-wide). New ServiceAccounts use Role/RoleBinding, not ClusterRole/ClusterRoleBinding. Note: 9 of 10 current RHOAI operators run with ClusterRole; any new component following this pattern is auto-High severity
- [ ] Token handling specified: type (OAuth, OIDC, ServiceAccount), lifetime, rotation policy
- [ ] Agent workloads have workload identity (SPIFFE/SPIRE, OAuth2 token exchange, SA token)
- [ ] Agent identity propagation: agents acting on behalf of users propagate user identity for audit and authorization

## 2. Data Protection

Applies when: the STRAT involves sensitive data (PII, credentials, tokens, model weights) or persistent storage.

- [ ] Data classification addressed (what data is stored/transmitted, sensitivity level). At minimum specify: public, internal, confidential, or restricted
- [ ] Encryption at rest specified for persistent data
- [ ] Encryption in transit (TLS) for all external and cross-component communications
- [ ] Secrets stored in OpenShift Secrets or external secret stores, not in ConfigMaps or environment variables
- [ ] Secrets not exposed in delivery channels: credentials do not appear in logs, pipeline parameters, error messages, or API responses
- [ ] Data retention and deletion requirements specified

## 3. Cryptographic Compliance

Applies when: the STRAT involves any cryptographic operations, TLS endpoints, or certificate management.

- [ ] FIPS 140-3: all crypto uses FIPS-validated modules on RHEL 9
  - Go: CGO_ENABLED=1 + GOEXPERIMENT=strictfipsruntime with RHEL Go compiler
  - Python: no banned packages (pycrypto, pycryptodome, blake3, rsa)
  - Java: automatic with RH JDK
- [ ] Post-quantum readiness: FIPS modules do not yet support PQ algorithms. STRATs should not mandate PQ-only, but should document readiness posture for when FIPS-validated PQ modules become available
- [ ] TLS profile compliance: honors cluster-wide TLS settings, no hardcoded TLS versions/cipher suites/curve preferences. OCP 4.22 requires ML-KEM negotiation; OCP 5.0 makes this a release blocker
- [ ] Certificate management uses service-CA or a specified CA mechanism. If using custom CA, specify trust chain and rotation

## 4. Network & API Security

Applies when: the STRAT introduces new network-accessible endpoints or modifies network topology.

- [ ] Rate limiting or DoS protection for public-facing or externally-accessible endpoints
- [ ] NetworkPolicy specified for new services
- [ ] Uses OpenShift Route/Gateway API, not upstream Kubernetes Gateway API
- [ ] Service mesh (Istio) not introduced as a new dependency. If the STRAT requires Istio, justify why NetworkPolicy + mTLS via service-CA is insufficient

## 5. Supply Chain & Dependencies

Applies when: the STRAT introduces new external dependencies, container images, or build pipelines.

- [ ] Container images from trusted registries (registry.redhat.io, quay.io) or Konflux build pipeline
- [ ] External dependencies version-pinned with integrity verification
- [ ] ML model artifacts: provenance verified, not loaded from untrusted sources
- [ ] No unsafe deserialization formats (Pickle, H5) without safety controls

## 6. Infrastructure & Deployment

Applies when: the STRAT introduces new pods, containers, CRDs, or modifies deployment topology.

- [ ] Pod security standards specified (restricted or baseline profile)
- [ ] Cross-namespace access justified if present
- [ ] CRDs scoped to namespace unless cluster-scope is justified. Namespace-scoped ServiceAccounts must not create or manage cluster-scoped CRDs
- [ ] Resource quotas specified for shared compute/storage resources to prevent noisy-neighbor effects

## 7. Tenant Isolation

Applies when: the STRAT involves shared resources (storage, compute, registry, queue) or multi-tenant deployments.

- [ ] Tenant isolation model specified: how tenants are separated (namespace, network, storage, compute)
- [ ] Cross-tenant data access prevented: one tenant's workload cannot read or modify another tenant's data, models, or artifacts
- [ ] Shared resource access controls: registries, queues, object stores have per-tenant authorization

## 8. Operational Security

Applies when: always. These are baseline operational requirements.

- [ ] Audit logging specified for security-relevant events (auth decisions, data access, privilege changes)
- [ ] Monitoring and alerting for security-relevant conditions

## 9. Agent & MCP Security

Applies when: the STRAT involves agent runtimes, MCP servers, tool registrations, or agent-to-agent communication.

- [ ] Agent runtime sandboxed (Kata Containers, gVisor, restricted ServiceAccount)
- [ ] Tool permissions scoped per-agent or per-invocation, not blanket grants
- [ ] MCP server credentials are short-lived or per-session, not static long-lived tokens
- [ ] MCP tool descriptions are integrity-verified at registration time to prevent tool-description injection
- [ ] MCP servers run in isolated namespaces with dedicated ServiceAccounts, not shared with application workloads
- [ ] Agent-to-agent communication has integrity verification (mutual auth, message signing)
- [ ] Agent actions (tool calls, model invocations, data access) have audit logging
- [ ] No lethal trifecta: MCP server does not combine (1) private data access, (2) untrusted content exposure, AND (3) external communication capability
- [ ] Skill/tool distribution does not enable agents to compose the lethal trifecta via combination

## 10. Upstream Component Risk

Applies when: the STRAT uses Ray, MLflow, vLLM, or Kubeflow. These components have known recurring vulnerabilities that require explicit mitigation.

- [ ] Ray: auth mitigation for dashboard (CVE-2023-48022 / ShadowRay)
- [ ] MLflow: path traversal mitigation for recurring CVEs
- [ ] vLLM: Pickle deserialization risk addressed when loading untrusted models
- [ ] Kubeflow: Profile Controller cluster-admin scope acknowledged

## 11. Governance

Applies when: the STRAT introduces new repositories, build pipelines, or changes code ownership.

- [ ] Changes land in opendatahub-io repos (upstream-first), not red-hat-data-services directly. This is an organizational policy, not a supply chain requirement

---

## Severity guidance

Items on this checklist are NFR Gaps (missing specifications), not Security Risks (active flaws). NFR Gaps alone do not drive a CONCERNS verdict in the security review.

However: if 5+ NFR gaps are identified in a Standard or Deep tier review, this indicates the STRAT author did not consider security systematically, and the reviewer may upgrade the verdict to CONCERNS.

A Security Risk is when the STRAT actively proposes something insecure (e.g., storing credentials in a ConfigMap). That's assessed by the security review skill, not this checklist.

---

## Source and maintenance

- Extracted from: 422 STRAT security reviews (Runs 1-4), April 2026
- Cross-referenced against: 39-pattern risk catalog (security-reviewer skill), RHOAI organizational constraints
- Checklist items: 47 across 11 categories
- Catalog coverage: 38/39 patterns mapped (97%). AUTH(6/6), DATA(4/4), CRYPTO(4/4), NET(3/3), SUPPLY(4/4), INFRA(3/3), TENANT(3/3), AGENT(5/5), MCP(4/4), UPSTREAM(4/4). Missing: none after v2 revision
- Top recurring findings from Run 1 (before checklist existed): rate limiting (61), TLS (57), encryption-at-rest (46), audit logging (40), image provenance (25), FIPS (16)
- Owner: Security Review Tiger Team (RHOAIENG-55489)
