# Live evaluation protocol

Run the suite locally only after reviewing `eval/scenarios.json` and `eval/live.py`:

```powershell
python -m eval.live --approve-fixtures --reviewer "Your name"
```

The switch records your authorization for reversible fixture setup/cleanup and the bounded repairs. It is a human approval of a known batch, not 20 separate interactive prompts. Each risky agent step still receives a separate audit approval bound to its exact action. Without this switch the suite refuses to mutate the homelab. Production CLI runs always pause for approval.

Scope: temporary `sdtest_*` AD users; fictional bob's AD GG-Finance and standalone Samba finance memberships; Office-PDF/Office-Laser pause/resume; evaluation print jobs; temporary `agent-*` WireGuard peers. Passwords are generated into ignored `.env`, and only temporary users are reset. Original memberships and enabled/accepting queue states are restored in `finally`; temporary users/peers are deleted and checked absent. No real users are used. Do not interrupt fixture cleanup; after an abnormal host termination inspect the retained fixture logs and restore the recorded before-state.

Each run writes a timestamped manifest, summary JSON, Markdown table, per-scenario command/HTTP fixture JSONL and full agent/audit JSON into `eval/results/`. The private SQLite checkpoint remains in ignored `state/`. Raw evidence means original command responses with credentials and WireGuard keys removed; private profiles, passwords and cookies are never public evidence.

The model receives the ticket and actual observations, never the expected-check label. PASS requires the intended final status and an independent service assertion. For the three escalation scenarios, PASS means escalated with no proposed mutation. Thus overall expected-outcome success includes safe escalation; repair-only success is reported separately. Steps count tool steps, tool retries count attempts after the first, model retries count invalid/unavailable generations, and approvals count audited action approvals. Fixture mutations and cleanup are logged separately and excluded from agent steps.

The 20 cases exercise six domains and several variants, but they are a small scripted suite on one host. Explicit account/device names and approved access are usually supplied. This is not a general benchmark or evidence of production readiness. Earlier pilot results remain available to show actual failures while the prompt and schema design were being improved.

The demo additionally performs a real process death using `os._exit(86)` after a successful Samba group write and before the SQLite completion checkpoint. It asserts exactly one `addgroup bob finance` execution in the command audit, resumes from current group state, verifies authenticated Finance access, and restores bob's original membership. The unit suite independently tests the same interruption boundary in a subprocess using a durable fake side-effect counter.

