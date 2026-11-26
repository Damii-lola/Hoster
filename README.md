# Hoster — Phase 1

A free AI-hosting loop: Render (public API) → TiDB (queue) → GitHub Actions (worker running Qwen2.5-7B-Instruct).

This is the **single-worker proof of concept**. Once this loop survives a crash test, it gets cloned into the staggered 5-repo fleet.

## 1. Set up TiDB

1. In your TiDB Cloud cluster, open the SQL editor (or connect with a client) and run `schema.sql` from this repo. That creates the `jobs` and `workers` tables.
2. Note your connection details: host, port (usually 4000), user, password, database name.

## 2. Set up the Render API

1. In Render, create a new Web Service pointing at this repo (it will pick up `render.yaml` automatically).
2. In the Render dashboard, set the one environment variable `render.yaml` marks `sync: false` so Render will prompt you for it:
   - `TIDB_CONNECTION_STRING` — paste the full connection string from TiDB Cloud's **Connect** panel (e.g. `mysql://user.root:password@gateway01...tidbcloud.com:4000/hoster?ssl-mode=VERIFY_IDENTITY`).
3. Deploy. Once it's live, check `GET /health` — it should return `{"ok": true, "workers_ready": 0, "pending_jobs": 0}`.

## 3. Set up the GitHub Actions worker

1. In this repo: **Settings → Secrets and variables → Actions**, add one secret:
   - `TIDB_CONNECTION_STRING` — the same full string you gave Render.
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

This is the step that actually proves the architecture, not just the happy path.

A job's lease is only renewed while a worker is genuinely still processing it (a background thread renews it every `LEASE_RENEWAL_INTERVAL_SECONDS`, default 30s). If the worker dies, renewals stop, and the lease (`LEASE_SECONDS`, default 90s) expires on its own — a real crash is detected in well under two minutes, while a job that's just taking a while to run is never wrongly reclaimed, no matter how long it takes.

Important: recovery still only happens when **some worker is actively polling** — a stale lease doesn't fix itself in the background with nothing running. The next worker run's `reclaim_stale_jobs()` check is what actually flips it back to `pending`.

To test it:

1. Submit a job as above.
2. As soon as the Actions log shows `claimed job <id>`, go to the Actions tab and **cancel the running workflow** mid-job.
3. Wait about 90 seconds to 2 minutes (not 10 minutes — see above).
4. **Trigger the workflow again** (a new run has to actually start and run its poll loop for the reclaim check to fire).
5. Check `GET /result/<job_id>` — it should have gone back to `pending` and then been picked up and completed by the new run, rather than being lost forever.

If you want to stress-test the lease timing itself, temporarily lower `LEASE_SECONDS` and `LEASE_RENEWAL_INTERVAL_SECONDS` even further via the workflow's `env:` block (e.g. `LEASE_SECONDS: "30"`, `LEASE_RENEWAL_INTERVAL_SECONDS: "10"`), then set them back once you're done.

If that works, the core loop is proven and we move on to:
- cloning this into 4 more repos with staggered schedules,
- quantizing/benchmarking the model properly,
- and the monitoring/security layers we discussed.

## Notes / known limits of this phase-1 version

- One worker, one model, no dashboard yet — this is intentionally minimal.
- The worker downloads `Qwen2.5-7B-Instruct-Q4_K_M.gguf` (~4.7 GB, 4-bit quantized) from `bartowski/Qwen2.5-7B-Instruct-GGUF` on Hugging Face. The workflow now caches `~/.cache/huggingface` with `actions/cache`, so after the first successful run the download should be skipped (or much faster) on later runs — GitHub keeps a cache entry for up to 7 days of inactivity or until the repo's 10 GB cache quota is hit. `hf_transfer` is also enabled, which meaningfully speeds up the initial pull. Even so, expect the very first run to take a while, and CPU inference itself will be noticeably slower than any GPU-hosted Qwen you've used before — that's expected at this stage, not a bug.
- Every database call now opens and closes its own short connection instead of holding one open for the worker's whole lifetime — this is what fixed the `MySQL server has gone away` crash that happened when a connection sat idle through a long model download.
- `MAX_INPUT_CHARS` in the API is a basic abuse guard, not real auth — don't expose this URL publicly beyond testing without adding an API key check.
