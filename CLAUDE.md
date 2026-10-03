# Instructions for the coding agent (Claude Code / Codex)

You are helping a Lead AI Solution Engineer stand up a **sandbox PoC** of a secure, permission-aware enterprise
assistant on **Microsoft Foundry + Foundry Models + Azure AI Search + Microsoft Agent Framework**.
Read `README.md` for the full design. Work phase by phase, stop at each checkpoint, and summarise in plain language.

## Guardrails (non-negotiable)
1. **Sandbox only.** Before creating anything, run `az account show` and ask the user to confirm the subscription.
2. **Ask before any action that creates resources, costs money or changes access** (resource creation, model
   deployments, role assignments). Show the exact command first. Read-only commands need no confirmation.
3. **Never print, echo, log or commit secrets.** Do not `cat .env`. Prefer Entra ID (`AUTH_MODE=entra`); do not fetch
   API keys unless the user explicitly asks for `AUTH_MODE=key`.
4. **Verify Azure CLI syntax before using it** (`az <group> --help`). The CLI and Foundry change often; if a command
   doesn't exist, say so and give the user the portal steps instead of guessing.
5. **Do not weaken security controls to make something pass** — security filter (`poc/identity.py`), tool schemas
   (`poc/tools.py`), approval guards (`poc/approval_api.py`), output guard (`poc/safety.py`) and the evaluation
   expectations (`eval/cases.yaml`). If a test fails, report why and propose a fix to the cause.
6. Use the user-confirmed sandbox resource group; keep its actual name in local configuration only. Keep everything in one region.

## Phases and checkpoints

### Phase 1 — Local prep
- Check `python --version` (≥3.10) and `az --version`. Create `.venv`, `pip install -r requirements.txt`, `pytest -q`.
- **Checkpoint:** 27 tests pass.

### Phase 2 — Azure sign-in and region
- `az login` (if needed), confirm subscription with the user.
- Check model availability in **uaenorth** first: `az cognitiveservices model list -l uaenorth -o table`
  (verify with `--help`). Look for (a) a GPT chat model that supports tool calling and (b) `text-embedding-3-small`.
- If either is missing in uaenorth, tell the user, propose a sandbox region that has both, and note it as a
  **Gate 1 finding** in `SETUP_LOG.md`.
- **Checkpoint:** region and two model names agreed with the user.

### Phase 3 — Foundry resource, project, model deployments
- Preferred: create via CLI if supported in the installed version (verify `az cognitiveservices account create --help`
  and project/deployment subcommands). Otherwise give the user exact portal steps at ai.azure.com and wait.
- Deploy the chat model and the embedding model; record deployment names.
- Collect: project endpoint (`https://<resource>.services.ai.azure.com/api/projects/<project>`), the resource's
  Azure OpenAI endpoint, and the resource ID.
- **Checkpoint:** user confirms the project exists and both deployments are listed.

### Phase 4 — Search, App Insights, roles
- Run `infra/setup.sh` with `LOCATION`, `RG=<sandbox-resource-group>` and `FOUNDRY_RESOURCE_ID` set (after confirmation).
- **Checkpoint:** script completes; remind that role assignments take 5–10 minutes.

### Phase 5 — Configure and verify
- Create `.env` from `.env.example` (fill endpoints and deployment names; no keys with `AUTH_MODE=entra`).
  App Insights connection string: write it into `.env` without printing it.
- Start `uvicorn poc.approval_api:app --port 8000` in the background; run `python -m poc.check_setup`.
- **Checkpoint:** all checks green, including "Agent Framework on Foundry". On 403, wait for RBAC propagation and retry.

### Phase 6 — Ingest and demo
- `python -m poc.ingest --recreate`; then tell the user to run `streamlit run poc/ui.py` and try the five demo
  prompts in README §5.
- **Checkpoint:** grounded EN and AR answers with citations.

### Phase 7 — Acceptance tests
- `python -m eval.run_eval`; summarise `eval/results.md`. Critical tests (★) must pass.
- If a non-critical test fails, explain whether it's model behaviour, data or code, and suggest the smallest fix.

## Keep a log
Append a short entry to `SETUP_LOG.md` at each checkpoint: date, phase, what was created (names only), findings
(e.g. model availability in UAE North), and any open issues. Never include secrets.

## Useful commands
```bash
pytest -q                                   # offline tests (no Azure)
uvicorn poc.approval_api:app --port 8000    # approval API + mock ServiceNow
python -m poc.check_setup                   # connectivity check
python -m poc.ingest --recreate             # build the index
streamlit run poc/ui.py                     # demo UI
python -m eval.run_eval                     # acceptance tests → eval/results.md
```
