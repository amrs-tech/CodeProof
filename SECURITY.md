# Security boundaries

CodeProof is a single-user local application that processes untrusted source and proposed edits.

- ZIP extraction rejects traversal, absolute paths, Windows devices, links, special files, path collisions, conflicting file/directory entries, encryption, unsupported compression, and oversized/bomb-like inputs.
- Public GitHub intake allows only repository identifiers and approved HTTPS hosts. It executes no Git hooks/submodules and uses no ambient credentials.
- Static checks invoke trusted Ruff in isolated stdin mode and parse Python without importing it. Submitted configurations and suppression comments cannot disable the baseline.
- Model requests treat source as data and expose no tools. Strict response schemas and independent validation enforce paths, hashes, sizes, and edit policies.
- Credential paths and obvious literals are withheld from model input/indexing. Detection is heuristic; review sensitive source and understand your provider's data policy before upload.
- Substantive code runs only in the optional restricted Docker sandbox. Submitted dependency installation, arbitrary commands, writable source mounts, network, elevated privileges, and host credentials are unavailable.
- Source copies and reports remain in the configured local data directory until the operator removes them. Use a protected directory and avoid confidential source on shared machines.
- BYOK credentials are run-scoped in memory, excluded from database/report metadata and logs, and discarded on claim completion/failure or shutdown. Queued BYOK reviews require resubmission after restart. Browser model overrides cannot change provider endpoints. Use a trusted local instance when entering keys.
- HTML reports escape source/model data, contain no scripts or remote assets, and include a restrictive content security policy. Only non-secret provider/model metadata is exported.
- Optional bearer tokens, origin/host checks, bounded request bodies, and security headers protect the local interface. Bind to loopback; this release has no per-user authorization or tenant isolation.

Docker shares the host kernel. For hostile multi-user or internet-facing use, add dedicated ephemeral VM workers, stronger authentication/authorization, retention policies, secret scanning, and operational monitoring. Existing tests can be incomplete or adversarial; successful reports describe only the evidence collected.

Failed candidates are rolled back and their rationale retained. CodeProof does not claim complete vulnerability detection or formal correctness. Report sensitive issues privately when the repository supports private reporting; never post credentials or confidential source in public issues.
