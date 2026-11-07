# ADR 0001: Build D interfaces against an explicit gateway
Status: proposed to team for integration review.

The remote main branch initially contained README and LICENSE only. Implementing
missing A/B/C modules under D's commits would obscure responsibility and create
conflicting designs. D therefore publishes an ApplicationGateway plus atomic
ReviewRepository contract, a clearly labelled volatile preview, and independently
testable provider/CLI/Web/review code.

Consequences: preview works immediately; production task execution fails clearly
until the team supplies a factory. Shared ports may be adapted after C03; the
gateway is not a substitute for A's domain ports. B supplies transactions and
restart recovery; C supplies approvals, tools, sandbox and harness. The team must
review and test the actual integration on Ubuntu before main is accepted.
