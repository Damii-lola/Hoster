# Hoster — Phase 1

A free AI-hosting loop: Render (public API) → TiDB (queue) → GitHub Actions (worker running Qwen2.5-7B-Instruct).

This is the **single-worker proof of concept**. Once this loop survives a crash test, it gets cloned into the staggered 5-repo fleet.

## 1. Set up TiDB

1. In your TiDB Cloud cluster, open the SQL editor (or connect with a client) and run `schema.sql` from this repo. That creates the `jobs` and `workers` tables.
2. Note your connection details: host, port (usually 4000), user, password, database name.

## 2. Set up the Render API

1. In Render, create a new Web Service pointing at this repo (it will pick up `render.yaml` automatically).
2. In the Render dashboard, set these environment variables (they're marked `sync: false` in `render.yaml` so Render will prompt you):
   - `TIDB_HOST`, `TIDB_USER`, `TIDB_PASSWORD`, `TIDB_DATABASE` (and `TIDB_PORT` if it's not 4000)
3. Deploy. Once it's live, check `GET /health` — it should return `{"ok": true, "workers_ready": 0, "pending_jobs": 0}`.

## 3. Set up the GitHub Actions worker

1. In this repo: **Settings → Secrets and variables → Actions**, add:
   - `TIDB_HOST`, `TIDB_PORT`, `TIDB_USER`, `TIDB_PASSWORD`, `TIDB_DATABASE`
2. Go to the **Actions** tab, select **Hoster Worker**, and click **Run workflow** to trigger it manually (don't wait for the daily cron while testing).
3. Watch the logs — you should see the model download, "worker ready, entering poll loop", and then it will sit polling since there are no jobs yet.

## 4. Submit a test job

While the workflow is running, call your Render API:

```bash
curl -X POST https://YOUR-RENDER-URL.onrender.com/run-ai \
  -H "Content-Type: application/json" \
  -d '{"model_name": "Qwen/Qwen2.5-7B-Instruct", "input": "Say hello in one short sentence."}'
```

This returns a `job_id`. Poll for the result:

```bash
curl https://YOUR-RENDER-URL.onrender.com/result/<job_id>
```

You should see `status` move from `pending` → `processing` → `done`, with the model's reply in `result`.

## 5. The crash-recovery test

This is the step that actually proves the architecture, not just the happy path:

1. Submit a job as above.
2. As soon as the Actions log shows `claimed job <id>`, go to the Actions tab and **cancel the running workflow** mid-job.
3. Re-run the workflow (or wait for a new one).
4. Check `GET /result/<job_id>` again — because of `reclaim_stale_jobs()`, the job should go back to `pending` after its lease (10 minutes by default) expires, and the next worker run should pick it up and complete it, rather than it being lost forever.

If that works, the core loop is proven and we move on to:
- cloning this into 4 more repos with staggered schedules,
- quantizing/benchmarking the model properly,
- and the monitoring/security layers we discussed.

## Notes / known limits of this phase-1 version

- One worker, one model, no dashboard yet — this is intentionally minimal.
- The worker downloads `Qwen2.5-7B-Instruct-Q4_K_M.gguf` (~4.7 GB, 4-bit quantized) from `bartowski/Qwen2.5-7B-Instruct-GGUF` on Hugging Face every run, since GitHub-hosted runners are ephemeral — caching across runs is a later optimization, not a phase-1 requirement. On the shared ~4 vCPU runner, expect this download plus model load to take several minutes before the worker is marked `ready`, and CPU inference itself will be noticeably slower than any GPU-hosted Qwen you've used before — that's expected at this stage, not a bug.
- `MAX_INPUT_CHARS` in the API is a basic abuse guard, not real auth — don't expose this URL publicly beyond testing without adding an API key check.
